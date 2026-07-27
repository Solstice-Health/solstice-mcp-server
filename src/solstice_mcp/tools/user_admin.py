"""Register staff-only user-administration tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.tenants import SessionFactory, TenantRegistry
from solstice_mcp.user_admin import (
    Auth0UserAdmin,
    CentralSessionFactory,
    add_user,
    change_brand_role,
    reset_password,
)

# Overwrites an Auth0 credential (temp mode) or dispatches a fresh reset email
# each call; reaches Auth0 (open world).
RESET_PASSWORD = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# One in-place column flip; re-setting the same role is a no-op. DB only.
CHANGE_ROLE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

# Creates/completes rows (never destroys), but may send an email per call and
# reaches Auth0.
ADD_USER = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def register_user_admin_tools(
    mcp: FastMCP,
    *,
    require_subject: Callable[[], str],
    require_access_token: Callable[[], Any],
    registry: TenantRegistry,
    session_factory: SessionFactory,
    auth0: Auth0UserAdmin,
    central_session_factory: CentralSessionFactory,
) -> None:
    reset_tool = audited_tool(mcp, require_access_token, annotations=RESET_PASSWORD)
    role_tool = audited_tool(mcp, require_access_token, annotations=CHANGE_ROLE)
    add_tool = audited_tool(mcp, require_access_token, annotations=ADD_USER)

    @reset_tool
    def solstice_reset_password(
        tenant_slug: str,
        email: str,
        mode: str = "email",
    ) -> dict[str, Any]:
        """Reset a user's Solstice password. Requires SOLSTICE_STAFF in the tenant.

        Only call this when the user explicitly asks for a password reset.
        ``mode="email"`` (default) makes Auth0 send its "Change Password" email;
        the user clicks the link and picks a new password. ``mode="temp_password"``
        overwrites the account password with a generated one and returns it in
        the result — use only when the reset email did not arrive (e.g.
        corporate mail quarantine), deliver the password to the user verbatim,
        and tell them to change it after first login. A previously sent reset
        link still works and would overwrite the temp password if clicked. The
        target must be an existing member of ``tenant_slug``.
        """
        return reset_password(
            require_subject(),
            tenant_slug,
            email,
            mode,
            auth0=auth0,
            registry=registry,
            session_factory=session_factory,
        )

    @role_tool
    def solstice_change_brand_role(
        tenant_slug: str,
        brand_id: str,
        email: str,
        new_role: str,
    ) -> dict[str, Any]:
        """Change a user's role on one brand. Requires SOLSTICE_STAFF on that brand.

        ``new_role`` is MEMBER, ADMIN, or SOLSTICE_STAFF. Granting
        SOLSTICE_STAFF is a privilege escalation (brand-scoped super user who
        sees draft content) — confirm with the user before doing it. The
        target must already be a member of the brand; to attach someone to a
        brand use solstice_add_user instead. Discover current members and
        roles with solstice_list_brand_users.
        """
        return change_brand_role(
            require_subject(),
            tenant_slug,
            brand_id,
            email,
            new_role,
            registry=registry,
            session_factory=session_factory,
        )

    @add_tool
    def solstice_add_user(
        tenant_slug: str,
        email: str,
        name: str,
        brand_id: str | None = None,
        role: str = "MEMBER",
        send_reset_email: bool = True,
    ) -> dict[str, Any]:
        """Onboard a user into a tenant (and optionally a brand). Requires SOLSTICE_STAFF in the tenant.

        Creates the Auth0 account if missing (email pre-verified), upserts the
        canonical central-auth user row and the tenant user row, optionally
        attaches the user to ``brand_id`` with ``role`` (MEMBER/ADMIN/
        SOLSTICE_STAFF, reviving a soft-deleted membership), and by default
        sends the Auth0 password-reset email so the user sets their own
        password. Every step is an upsert: safe to re-run for users who exist
        in only some of the systems, and the way to grant an existing user
        access to an additional tenant or brand. Pass ``brand_id`` when the
        tenant has more than one company, since the company is otherwise
        ambiguous. If the result has reset_email_sent=false with a
        reset_email_error, the user WAS fully onboarded — only the email
        failed; re-trigger it with solstice_reset_password.
        """
        return add_user(
            require_subject(),
            tenant_slug,
            email,
            name,
            brand_id,
            role,
            send_reset_email,
            auth0=auth0,
            registry=registry,
            session_factory=session_factory,
            central_session_factory=central_session_factory,
        )


__all__ = ["register_user_admin_tools"]
