"""Register Solstice discovery, identity, and brand tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.brands import UserRole, list_brands_for_user, require_brand_role
from solstice_mcp.gate import SolsticeAccessGate
from solstice_mcp.sibling_mcps import SiblingMCPRegistry
from solstice_mcp.tenants import (
    SessionFactory,
    TenantMembershipCache,
    TenantRegistry,
    discover_tenants_for_sub,
    resolve_tenant_identity,
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def register_discovery_tools(
    mcp: FastMCP,
    *,
    resource_url: str,
    required_scope: str,
    server_name: str,
    server_version: str,
    require_subject: Callable[[], str],
    require_access_token: Callable[[], Any],
    registry: TenantRegistry,
    session_factory: SessionFactory,
    membership_cache: TenantMembershipCache,
    access_gate: SolsticeAccessGate,
    sibling_registry: SiblingMCPRegistry,
    record_activity: Callable[..., Any] | None = None,
) -> None:
    read_only_tool = audited_tool(
        mcp,
        require_access_token,
        annotations=READ_ONLY,
        record_activity=record_activity,
    )

    def _resolve_email(token: Any) -> str | None:
        """Email for the sibling-MCP gate: token claim, else tenant DB.

        Gateway-minted OAuth tokens do not carry an ``email`` claim, so fall
        back to the same source solstice_whoami uses — the subject's user row
        in a tenant they belong to. The gate caches its decision per subject,
        so the tenant scan runs at most once per cache window.
        """
        claims = token.claims or {}
        email = claims.get("email")
        if isinstance(email, str) and email:
            return email
        memberships = discover_tenants_for_sub(
            token.subject,
            registry=registry,
            session_factory=session_factory,
            cache=membership_cache,
        )
        for membership in memberships:
            identity = resolve_tenant_identity(
                token.subject,
                membership.slug,
                registry=registry,
                session_factory=session_factory,
            )
            if identity is not None and identity.email:
                return identity.email
        return None

    @read_only_tool
    def solstice_server_info() -> dict[str, Any]:
        """Return public server and tool metadata, including the RBAC model."""
        return {
            "name": server_name,
            "version": server_version,
            "resource_url": resource_url,
            "required_scope": required_scope,
            "product": "Solstice — MLR content-review platform (tenant -> company -> brand)",
            "rbac": {
                "access_chain": "tenant membership -> brand_team_members row",
                "roles": ["MEMBER", "ADMIN", "SOLSTICE_STAFF"],
                "super_user": "SOLSTICE_STAFF (brand-scoped, not tenant-wide)",
                "rule": "Role is derived server-side from the OAuth subject. Tool arguments never grant authority.",
            },
            "deep_links": {
                "asset_url": "https://[www.]<tenant_slug>.solsticehealth.co/home/assets/<operation_id>",
                "parsing": "subdomain (strip leading www.) maps to tenant_slug; hyphens and "
                "underscores are interchangeable (sanofi-sandbox -> sanofi_sandbox); "
                "trailing UUID path segment = operation_id",
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
                "solstice_brand_rules",
                "solstice_brand_design_assets",
                "solstice_brand_claims",
                "solstice_list_projects",
                "solstice_project_info",
                "solstice_list_operations",
                "solstice_operation_info",
                "solstice_operation_messages",
                "solstice_operation_html",
                "solstice_create_operation",
                "solstice_prepare_operation_version",
                "solstice_commit_operation_version",
                "solstice_list_recent_work",
            ],
        }

    @read_only_tool
    def solstice_list_tenants() -> dict[str, Any]:
        """List tenant databases containing the authenticated user."""
        memberships = discover_tenants_for_sub(
            require_subject(),
            registry=registry,
            session_factory=session_factory,
            cache=membership_cache,
        )
        return {"tenants": [membership.as_dict() for membership in memberships], "count": len(memberships)}

    @read_only_tool
    def solstice_whoami(tenant_slug: str) -> dict[str, Any]:
        """Return identity after revalidating membership in one tenant."""
        identity = resolve_tenant_identity(
            require_subject(),
            tenant_slug,
            registry=registry,
            session_factory=session_factory,
        )
        if identity is None:
            return {
                "status": "not_member",
                "tenant_slug": tenant_slug,
                "message": "Unknown tenant, environment mismatch, or user is not a member.",
            }
        return {"status": "ok", **identity.as_dict()}

    @read_only_tool
    def solstice_check_access() -> dict[str, Any]:
        """Return whether the caller may see the sibling MCP directory."""
        token = require_access_token()
        decision = access_gate.evaluate(token.subject, _resolve_email(token))
        return {
            "allowed": decision.allowed,
            "email": decision.email,
            "reason": decision.reason,
            "allowed_domain": access_gate.allowed_domain,
        }

    @read_only_tool
    def solstice_list_sibling_mcps() -> dict[str, Any]:
        """List sibling MCPs the caller is authorized to use, if allowed."""
        token = require_access_token()
        decision = access_gate.evaluate(token.subject, _resolve_email(token))
        if not decision.allowed:
            return {"allowed": False, "reason": decision.reason, "sibling_mcps": []}
        entries = sibling_registry.list()
        return {"allowed": True, "sibling_mcps": entries, "count": len(entries)}

    @read_only_tool
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
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brands": [membership.as_dict() for membership in memberships],
            "count": len(memberships),
        }

    @read_only_tool
    def solstice_brand_info(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """Return details for one brand after revalidating the caller's per-brand membership.

        Gated at UserRole.MEMBER: any brand member may read their brand's info.
        brand_id selects the resource; it does not grant access — the subject's
        own brand_team_members row is checked by require_brand_role.
        """
        identity = require_brand_role(
            require_subject(),
            tenant_slug,
            brand_id,
            min_role=UserRole.MEMBER,
            registry=registry,
            session_factory=session_factory,
        )
        return {"status": "ok", **identity.as_dict()}


__all__ = ["register_discovery_tools"]
