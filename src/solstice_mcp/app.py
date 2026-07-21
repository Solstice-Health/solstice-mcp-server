"""Stateless Streamable HTTP Solstice MCP application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from solstice_mcp.auth import JWKSCache, MCPAccessTokenVerifier
from solstice_mcp.brands import (
    UserRole,
    list_brands_for_user,
    require_brand_role,
    reset_brand_role,
)
from solstice_mcp.gate import SolsticeAccessGate
from solstice_mcp.operations import (
    commit_operation_version,
    get_operation_html,
    get_operation_info,
    get_project_info,
    list_operation_messages,
    list_operations_for_brand,
    list_projects_for_brand,
    prepare_operation_version,
)
from solstice_mcp.settings import Settings, settings
from solstice_mcp.sibling_mcps import SiblingMCPRegistry
from solstice_mcp.slack_stub import slack_react, slack_read, slack_search, slack_send
from solstice_mcp.storage import S3Reader, TenantS3
from solstice_mcp.tenants import (
    TenantDatabaseFactory,
    TenantMembershipCache,
    TenantRegistry,
    discover_tenants_for_sub,
    resolve_tenant_identity,
)

MCP_REQUIRED_SCOPE = "mcp:connect"
MCP_SERVER_NAME = "solstice-mcp"
MCP_SERVER_VERSION = "1.0.0"

MCP_INSTRUCTIONS = (
    "Solstice is an MLR (Medical / Legal / Regulatory) content-review platform. "
    "Structure: tenant -> company -> brand -> project -> operation. Each tenant has "
    "its own database.\n"
    "Access model: a user is a member of a tenant, and per-brand access is granted "
    "via the brand_team_members table with roles ADMIN, MEMBER (normal users) and "
    "SOLSTICE_STAFF (brand-scoped super user). A user may hold different roles on "
    "different brands within the same tenant. SOLSTICE_STAFF is per-brand, not "
    "tenant-wide.\n"
    "Content model: a brand has projects; each project has a dir_map (a folder tree "
    "whose leaves reference operation_ids). An operation is a content-generation "
    "workspace with a chat (n_cg_operation_messages). Message type is text/html/pdf/"
    "blueprint; document rows (html/pdf) carry an intent of draft or final. HTML "
    "bodies live in tenant S3 under cg_operation_msg_html/ and are returned by their "
    "key, not inline. Use solstice_operation_html to get a presigned URL for an html "
    "message; pass fetch=true only when the user explicitly asks to read, save, or "
    "visualize the document body.\n"
    "Intent visibility: SOLSTICE_STAFF sees draft and final document messages; "
    "MEMBER and ADMIN see final only. This filter is enforced server-side from your "
    "token - you cannot request draft messages by passing an argument.\n"
    "Authorization is derived server-side from your OAuth token. NEVER pass a role, "
    "user_id, or assumed privilege as a tool argument - it is ignored. Always call "
    "solstice_list_tenants, then solstice_list_brands(tenant_slug) to discover what "
    "you may access; the server returns only the brands the authenticated user can "
    "see.\n"
    "Deep links: a user may paste a Solstice asset URL like "
    "https://www.incyte.solsticehealth.co/home/assets/<operation_id>. The subdomain "
    "(strip any leading www.) is the tenant_slug; the trailing UUID path segment is "
    "the operation_id. The operation tools (solstice_operation_info, "
    "solstice_operation_messages, solstice_operation_html) take tenant_slug and "
    "operation_id directly - no brand argument is needed, the server resolves the "
    "brand and enforces RBAC. When given such a link, parse it and call those tools "
    "directly; do not ask the user for a brand. The same pattern applies to other "
    "Solstice routes that embed an id in the path (e.g. /home/generating/<operation_id>, "
    "/home/review-request/<operation_id>, /home/projects/<project_id>).\n"
    "Version writes: to add a new HTML or PDF version to an operation, call "
    "solstice_prepare_operation_version(tenant_slug, operation_id, type, file_name) "
    "to get a presigned PUT URL and target s3_key for the next version (v1 if none "
    "exist, else max+1). Upload the file bytes directly to that URL, then call "
    "solstice_commit_operation_version(tenant_slug, operation_id, type, s3_key, "
    "file_name) to insert the version row. Versions are append-only - existing "
    "versions are never overwritten. Intent is derived from your token, never an "
    "argument: SOLSTICE_STAFF -> draft; MEMBER/ADMIN -> final.\n"
    "Slack tools are non-operational stubs."
)


def build_mcp_app(
    *,
    runtime_settings: Settings = settings,
    registry: TenantRegistry | None = None,
    session_factory: Callable[[str], Session] | None = None,
    cache: TenantMembershipCache | None = None,
    jwks_cache: JWKSCache | None = None,
    s3: S3Reader | None = None,
) -> FastMCP:
    resource = runtime_settings.MCP_RESOURCE_URL
    issuer = runtime_settings.issuer_url
    resource_parts = urlsplit(resource)
    if resource_parts.scheme not in {"http", "https"} or not resource_parts.hostname:
        raise ValueError("MCP_RESOURCE_URL must be an absolute HTTP(S) URL")
    if not runtime_settings.AUTH0_DOMAIN:
        raise ValueError("AUTH0_DOMAIN is required")

    tenant_registry = registry or TenantRegistry()
    if not tenant_registry.slugs:
        tenant_registry.load(runtime_settings.TENANT_CONFIG_PATH)
    open_session = session_factory
    if open_session is None:
        templates = runtime_settings.database_url_templates
        if not templates:
            raise ValueError("At least one database URL template is required (DATABASE_URL_TEMPLATE_DEV/PROD)")
        open_session = TenantDatabaseFactory(tenant_registry, templates)
    membership_cache = cache or TenantMembershipCache()
    s3_reader = s3 or TenantS3(region_name=runtime_settings.AWS_REGION)
    access_gate = SolsticeAccessGate(allowed_domain=runtime_settings.ALLOWED_EMAIL_DOMAIN)
    sibling_registry = SiblingMCPRegistry()
    sibling_registry.load(runtime_settings.SIBLING_MCP_CONFIG_PATH)

    mcp = FastMCP(
        name=MCP_SERVER_NAME,
        instructions=MCP_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        token_verifier=MCPAccessTokenVerifier(audience=resource, issuer=issuer, jwks_cache=jwks_cache),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=[MCP_REQUIRED_SCOPE],
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[resource_parts.netloc],
            allowed_origins=[f"{resource_parts.scheme}://{resource_parts.netloc}"],
        ),
    )

    def require_subject() -> str:
        token = get_access_token()
        if token is None or not token.subject:
            raise RuntimeError("Authenticated MCP subject is missing")
        return token.subject

    def require_access_token() -> Any:
        token = get_access_token()
        if token is None or not token.subject:
            raise RuntimeError("Authenticated MCP subject is missing")
        return token

    @mcp.tool()
    def solstice_server_info() -> dict[str, Any]:
        """Return public server and tool metadata, including the RBAC model."""
        return {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
            "resource_url": resource,
            "required_scope": MCP_REQUIRED_SCOPE,
            "product": "Solstice — MLR content-review platform (tenant -> company -> brand)",
            "rbac": {
                "access_chain": "tenant membership -> brand_team_members row",
                "roles": ["MEMBER", "ADMIN", "SOLSTICE_STAFF"],
                "super_user": "SOLSTICE_STAFF (brand-scoped, not tenant-wide)",
                "rule": "Role is derived server-side from the OAuth subject. Tool arguments never grant authority.",
            },
            "slack_status": "not_connected",
            "deep_links": {
                "asset_url": "https://[www.]<tenant_slug>.solsticehealth.co/home/assets/<operation_id>",
                "parsing": "subdomain (strip leading www.) = tenant_slug; trailing UUID path segment = operation_id",
                "usage": "Call solstice_operation_info / solstice_operation_messages / "
                         "solstice_operation_html with tenant_slug + operation_id. No brand "
                         "argument needed; the server resolves the brand and enforces RBAC.",
            },
            "tools": [
                "solstice_server_info",
                "solstice_list_tenants",
                "solstice_whoami",
                "solstice_check_access",
                "solstice_list_sibling_mcps",
                "solstice_list_brands",
                "solstice_brand_info",
                "solstice_list_projects",
                "solstice_project_info",
                "solstice_list_operations",
                "solstice_operation_info",
                "solstice_operation_messages",
                "solstice_operation_html",
                "solstice_prepare_operation_version",
                "solstice_commit_operation_version",
                "solstice_slack_search",
                "solstice_slack_read",
                "solstice_slack_send",
                "solstice_slack_react",
            ],
        }

    @mcp.tool()
    def solstice_list_tenants() -> dict[str, Any]:
        """List tenant databases containing the authenticated user."""
        memberships = discover_tenants_for_sub(
            require_subject(),
            registry=tenant_registry,
            session_factory=open_session,
            cache=membership_cache,
        )
        return {"tenants": [membership.as_dict() for membership in memberships], "count": len(memberships)}

    @mcp.tool()
    def solstice_whoami(tenant_slug: str) -> dict[str, Any]:
        """Return identity after revalidating membership in one tenant."""
        identity = resolve_tenant_identity(
            require_subject(),
            tenant_slug,
            registry=tenant_registry,
            session_factory=open_session,
        )
        if identity is None:
            return {
                "status": "not_member",
                "tenant_slug": tenant_slug,
                "message": "Unknown tenant, environment mismatch, or user is not a member.",
            }
        return {"status": "ok", **identity.as_dict()}

    @mcp.tool()
    def solstice_check_access() -> dict[str, Any]:
        """Return whether the caller may see the sibling MCP directory."""
        token = require_access_token()
        claims = token.claims or {}
        email = claims.get("email")
        if isinstance(email, str):
            email_value: str | None = email
        else:
            email_value = None
        decision = access_gate.evaluate(token.subject, email_value)
        return {
            "allowed": decision.allowed,
            "email": decision.email,
            "reason": decision.reason,
            "allowed_domain": access_gate.allowed_domain,
        }

    @mcp.tool()
    def solstice_list_sibling_mcps() -> dict[str, Any]:
        """List sibling MCPs the caller is authorized to use, if allowed."""
        token = require_access_token()
        claims = token.claims or {}
        email = claims.get("email")
        email_value = email if isinstance(email, str) else None
        decision = access_gate.evaluate(token.subject, email_value)
        if not decision.allowed:
            return {"allowed": False, "reason": decision.reason, "sibling_mcps": []}
        return {
            "allowed": True,
            "sibling_mcps": sibling_registry.list(),
            "count": len(sibling_registry.list()),
        }

    @mcp.tool()
    def solstice_list_brands(tenant_slug: str) -> dict[str, Any]:
        """List brands the authenticated user can access in a tenant, with per-brand role.

        Returns exactly the brands where the subject has a live
        brand_team_members row in that tenant's database. A user who is MEMBER
        on one brand and SOLSTICE_STAFF on another (same tenant) gets both,
        each with its own role. The tenant_slug argument selects the tenant;
        it never grants access — membership is re-derived from the token.
        """
        memberships = list_brands_for_user(
            require_subject(),
            tenant_slug,
            registry=tenant_registry,
            session_factory=open_session,
        )
        return {
            "tenant_slug": tenant_slug,
            "brands": [m.as_dict() for m in memberships],
            "count": len(memberships),
        }

    @mcp.tool()
    def solstice_brand_info(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """Return details for one brand after revalidating the caller's per-brand membership.

        Gated at UserRole.MEMBER: any brand member may read their brand's info.
        brand_id selects the resource; it does not grant access — the subject's
        own brand_team_members row is checked by require_brand_role.
        """
        try:
            identity = require_brand_role(
                require_subject(),
                tenant_slug,
                brand_id,
                min_role=UserRole.MEMBER,
                registry=tenant_registry,
                session_factory=open_session,
            )
            return {"status": "ok", **identity.as_dict()}
        finally:
            reset_brand_role()

    @mcp.tool()
    def solstice_list_projects(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """List projects for a brand. Read-only; gated at MEMBER."""
        projects = list_projects_for_brand(
            require_subject(), tenant_slug, brand_id,
            registry=tenant_registry, session_factory=open_session,
        )
        return {"tenant_slug": tenant_slug, "brand_id": brand_id, "projects": projects, "count": len(projects)}

    @mcp.tool()
    def solstice_project_info(tenant_slug: str, project_id: str) -> dict[str, Any]:
        """Return one project's directory map (folders + operation_ids).

        Read-only; gated at MEMBER on the project's brand.
        """
        info = get_project_info(
            require_subject(), tenant_slug, project_id,
            registry=tenant_registry, session_factory=open_session,
        )
        if info is None:
            raise ToolError("not_found: unknown project")
        return {"status": "ok", **info}

    @mcp.tool()
    def solstice_list_operations(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """List content-generation operations for a brand. Read-only; gated at MEMBER."""
        ops = list_operations_for_brand(
            require_subject(), tenant_slug, brand_id,
            registry=tenant_registry, session_factory=open_session,
        )
        return {"tenant_slug": tenant_slug, "brand_id": brand_id, "operations": ops, "count": len(ops)}

    @mcp.tool()
    def solstice_operation_info(tenant_slug: str, operation_id: str) -> dict[str, Any]:
        """Return one operation's metadata (no messages). Read-only; gated at MEMBER on the operation's brand."""
        info = get_operation_info(
            require_subject(), tenant_slug, operation_id,
            registry=tenant_registry, session_factory=open_session,
        )
        if info is None:
            raise ToolError("not_found: unknown operation")
        return {"status": "ok", **info}

    @mcp.tool()
    def solstice_operation_messages(tenant_slug: str, operation_id: str) -> dict[str, Any]:
        """Return an operation's chat + document-version summaries. Read-only; gated at MEMBER.

        Intent visibility is enforced server-side: SOLSTICE_STAFF sees draft and
        final document messages; MEMBER and ADMIN see final only. There is no
        intent/role argument — the filter is derived from your token.
        """
        messages = list_operation_messages(
            require_subject(), tenant_slug, operation_id,
            registry=tenant_registry, session_factory=open_session,
        )
        return {"tenant_slug": tenant_slug, "operation_id": operation_id, "messages": messages, "count": len(messages)}

    @mcp.tool()
    def solstice_operation_html(
        tenant_slug: str, operation_id: str, message_id: str, fetch: bool = False
    ) -> dict[str, Any]:
        """Return the HTML body for one operation message.

        By default returns a presigned GET URL (no body transfer). Set
        fetch=True to download the HTML inline — use that only when the user
        explicitly asks to read, save, or visualize the document.

        Gated at MEMBER on the operation's brand. Draft visibility is enforced
        here too: a non-staff caller cannot retrieve a draft message's URL or
        body (a presigned URL is a read capability, so it is not handed out for
        drafts). SOLSTICE_STAFF sees drafts; MEMBER/ADMIN see final only.
        """
        return get_operation_html(
            require_subject(), tenant_slug, operation_id, message_id,
            fetch=fetch,
            registry=tenant_registry, session_factory=open_session, s3=s3_reader,
            presign_expiry=runtime_settings.S3_PRESIGN_EXPIRY_SECONDS,
            max_inline_bytes=runtime_settings.S3_MAX_INLINE_BYTES,
        )

    @mcp.tool()
    def solstice_prepare_operation_version(
        tenant_slug: str, operation_id: str, type: str, file_name: str | None = None
    ) -> dict[str, Any]:
        """Prepare a new HTML or PDF version upload on an operation. Step 1 of 2.

        Returns a presigned PUT URL and target s3_key for the next version (v1
        if the operation has no document versions, else max+1). Upload the file
        bytes directly to upload_url, then call solstice_commit_operation_version
        with the returned s3_key. Gated at MEMBER on the operation's brand.
        ``type`` is ``html`` or ``pdf``.
        """
        return prepare_operation_version(
            require_subject(), tenant_slug, operation_id, type, file_name,
            registry=tenant_registry, session_factory=open_session, s3=s3_reader,
            presign_expiry=runtime_settings.S3_PRESIGN_EXPIRY_SECONDS,
        )

    @mcp.tool()
    def solstice_commit_operation_version(
        tenant_slug: str, operation_id: str, type: str, s3_key: str, file_name: str | None = None
    ) -> dict[str, Any]:
        """Commit a new version row after uploading to S3. Step 2 of 2.

        Append-only: inserts a new version row, never overwrites an existing
        one. The s3_key is validated against the prepared version. Intent is
        derived from your token (SOLSTICE_STAFF -> draft; MEMBER/ADMIN -> final)
        and is NOT accepted as an argument. Gated at MEMBER on the operation's
        brand. ``type`` is ``html`` or ``pdf``.
        """
        return commit_operation_version(
            require_subject(), tenant_slug, operation_id, type, s3_key, file_name,
            registry=tenant_registry, session_factory=open_session, s3=s3_reader,
        )

    @mcp.tool()
    def solstice_slack_search(query: str, channel: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return a truthful non-operational Slack search result."""
        return slack_search(query, channel=channel, limit=limit)

    @mcp.tool()
    def solstice_slack_read(channel: str, latest: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return a truthful non-operational Slack read result."""
        return slack_read(channel, latest=latest, limit=limit)

    @mcp.tool()
    def solstice_slack_send(channel: str, message: str, thread_ts: str | None = None) -> dict[str, Any]:
        """Return a truthful result without sending a Slack message."""
        return slack_send(channel, message, thread_ts=thread_ts)

    @mcp.tool()
    def solstice_slack_react(channel: str, timestamp: str, emoji: str) -> dict[str, Any]:
        """Return a truthful result without adding a Slack reaction."""
        return slack_react(channel, timestamp, emoji)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "service": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            headers={"Cache-Control": "no-store"},
        )

    return mcp


def build_asgi_app():
    return build_mcp_app().streamable_http_app()
