"""Register Solstice admin-request triage tools (staff-only)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.requests import dismiss_request, list_requests
from solstice_mcp.tenants import SessionFactory, TenantRegistry

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Dismiss flips one status field in place (terminal state, never a delete).
UPDATE_IN_PLACE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def register_request_tools(
    mcp: FastMCP,
    *,
    require_subject: Callable[[], str],
    require_access_token: Callable[[], Any],
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> None:
    read_only_tool = audited_tool(mcp, require_access_token, annotations=READ_ONLY)
    update_tool = audited_tool(mcp, require_access_token, annotations=UPDATE_IN_PLACE)

    @read_only_tool
    def solstice_list_requests(
        tenant_slug: str,
        status: str = "pending",
        brand_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List a tenant's admin requests (the staff "Requests" dashboard), newest first.

        Requires SOLSTICE_STAFF on at least one brand in the tenant; the read
        then covers the whole tenant queue. For "what's on my plate today",
        call this per tenant (discover tenants with solstice_list_tenants) with
        the default status="pending", and use each row's assigned_to to spot
        the ones assigned to the caller. ``status`` is one of
        pending/completed/dismissed/all; ``brand_id`` optionally narrows to one
        brand. Rows whose operation was deleted have operation_deleted=true —
        they can still be dismissed, never deleted.
        """
        requests = list_requests(
            require_subject(),
            tenant_slug,
            status,
            brand_id,
            limit,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "status": status,
            "requests": requests,
            "count": len(requests),
        }

    @update_tool
    def solstice_dismiss_request(
        tenant_slug: str,
        request_id: str,
        reason_category: str,
        reason_text: str | None = None,
    ) -> dict[str, Any]:
        """Dismiss one pending admin request with a reason. Requires SOLSTICE_STAFF on the request's brand.

        Mirrors the platform's dismiss action: only pending requests, and the
        reason is mandatory — always ask the user WHY before calling this.
        ``reason_category`` is one of duplicate/invalid/out_of_scope/other;
        ``reason_text`` is an optional note (max 500 chars) stored in the
        request's dismissal audit record. Dismissal is terminal and this tool
        never deletes the row or touches the linked operation. completed/
        dismissed rows are rejected.
        """
        return dismiss_request(
            require_subject(),
            tenant_slug,
            request_id,
            reason_category,
            reason_text,
            registry=registry,
            session_factory=session_factory,
        )


__all__ = ["register_request_tools"]
