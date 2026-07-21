"""Stateless Streamable HTTP Solstice MCP application composition."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from solstice_mcp.auth import JWKSCache, MCPAccessTokenVerifier
from solstice_mcp.gate import SolsticeAccessGate
from solstice_mcp.settings import Settings, settings
from solstice_mcp.sibling_mcps import SiblingMCPRegistry
from solstice_mcp.storage import S3Reader, TenantS3
from solstice_mcp.tenants import TenantDatabaseFactory, TenantMembershipCache, TenantRegistry
from solstice_mcp.tools.content import register_content_tools
from solstice_mcp.tools.discovery import register_discovery_tools

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
    "(strip any leading www.) maps to the tenant_slug; the trailing UUID path "
    "segment is the operation_id. Subdomains use hyphens while tenant slugs use "
    "underscores (e.g. sanofi-sandbox -> sanofi_sandbox) - the server accepts "
    "either form, so pass the subdomain as-is. The operation tools "
    "(solstice_operation_info, solstice_operation_messages, solstice_operation_html, "
    "solstice_prepare_operation_version, solstice_commit_operation_version) take "
    "tenant_slug and operation_id directly - no brand argument is needed, the "
    "server resolves the brand and enforces RBAC. When given such a link, parse "
    "it and call those tools directly; do not ask the user for a brand. The same "
    "pattern applies to other Solstice routes that embed an id in the path (e.g. "
    "/home/generating/<operation_id>, /home/review-request/<operation_id>, "
    "/home/projects/<project_id>). The tenant registry mirrors "
    "Backend-Server/config/tenants.json.\n"
    "Version writes: to add a new HTML or PDF version to an operation, call "
    "solstice_prepare_operation_version(tenant_slug, operation_id, type, file_name) "
    "to get a presigned PUT URL and target s3_key for the next version (v1 if none "
    "exist, else max+1). Upload the file bytes directly to that URL, then call "
    "solstice_commit_operation_version(tenant_slug, operation_id, type, s3_key, "
    "file_name) to insert the version row. Versions are append-only - existing "
    "versions are never overwritten. Intent is derived from your token, never an "
    "argument: SOLSTICE_STAFF -> draft; MEMBER/ADMIN -> final."
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

    def require_access_token() -> Any:
        token = get_access_token()
        if token is None or not token.subject:
            raise RuntimeError("Authenticated MCP subject is missing")
        return token

    def require_subject() -> str:
        return require_access_token().subject

    register_discovery_tools(
        mcp,
        resource_url=resource,
        required_scope=MCP_REQUIRED_SCOPE,
        server_name=MCP_SERVER_NAME,
        server_version=MCP_SERVER_VERSION,
        require_subject=require_subject,
        require_access_token=require_access_token,
        registry=tenant_registry,
        session_factory=open_session,
        membership_cache=membership_cache,
        access_gate=access_gate,
        sibling_registry=sibling_registry,
    )
    register_content_tools(
        mcp,
        require_subject=require_subject,
        registry=tenant_registry,
        session_factory=open_session,
        s3=s3_reader,
        presign_expiry=runtime_settings.S3_PRESIGN_EXPIRY_SECONDS,
        max_inline_bytes=runtime_settings.S3_MAX_INLINE_BYTES,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "service": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            headers={"Cache-Control": "no-store"},
        )

    return mcp


def build_asgi_app():
    return build_mcp_app().streamable_http_app()
