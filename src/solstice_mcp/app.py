"""Stateless Streamable HTTP Solstice MCP application."""

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
from solstice_mcp.slack_stub import slack_react, slack_read, slack_search, slack_send
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


def build_mcp_app(
    *,
    runtime_settings: Settings = settings,
    registry: TenantRegistry | None = None,
    session_factory: Callable[[str], Session] | None = None,
    cache: TenantMembershipCache | None = None,
    jwks_cache: JWKSCache | None = None,
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
    access_gate = SolsticeAccessGate(allowed_domain=runtime_settings.ALLOWED_EMAIL_DOMAIN)
    sibling_registry = SiblingMCPRegistry()
    sibling_registry.load(runtime_settings.SIBLING_MCP_CONFIG_PATH)

    mcp = FastMCP(
        name=MCP_SERVER_NAME,
        instructions=(
            "Call solstice_list_tenants after OAuth, then pass tenant_slug to tenant-bound tools. "
            "Slack tools are non-operational stubs."
        ),
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
        """Return public server and tool metadata."""
        return {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
            "resource_url": resource,
            "required_scope": MCP_REQUIRED_SCOPE,
            "slack_status": "not_connected",
            "tools": [
                "solstice_server_info",
                "solstice_list_tenants",
                "solstice_whoami",
                "solstice_check_access",
                "solstice_list_sibling_mcps",
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
