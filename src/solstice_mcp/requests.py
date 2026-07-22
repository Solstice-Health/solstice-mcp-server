"""Read + dismiss access to the tenant ``admin_requests`` table.

Mirrors the Backend-Server data model (see
``Backend-Server/src/shared/databases/sql_database/operation_models/admin_request_models.py``)
without importing it:

- ``admin_requests`` — one row per user-initiated request (the source of truth
  for the admin "Requests" dashboard). ``request_type`` ∈ {initial_save,
  change_request_complex, change_request_review, approval_request};
  ``status`` ∈ {pending, completed, dismissed}.
- ``cg_operation_id`` is intentionally NOT a foreign key: requests are
  permanent audit records and survive operation deletion/GC. The GC stamps
  ``request_metadata.operation_deleted`` so readers can tell without probing
  the operation. Requests are therefore never deleted here — only dismissed.

Authorization:
- LIST is tenant-wide but staff-gated: the subject must hold a live
  SOLSTICE_STAFF role on at least one live brand in the tenant. Solstice staff
  triage the whole tenant's queue ("what's on my plate today"), so the read is
  not limited to their own brands. MEMBER/ADMIN-only users get nothing.
- DISMISS mirrors the Backend-Server route exactly
  (``POST /admin-requests/{id}/dismiss``): SOLSTICE_STAFF on the row's OWN
  brand (resolved from the row, never a caller argument), pending rows only,
  mandatory reason category, optional note capped at 500 chars. The write is
  one status flip plus a ``request_metadata.dismissal`` audit stamp — the
  linked operation is never touched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, String, Uuid, select
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.brands import (
    Brand,
    BrandTeamMember,
    UserRole,
    require_brand_role,
    reset_brand_role,
)
from solstice_mcp.tenants import (
    Base,
    SessionFactory,
    TenantRegistry,
    User,
    resolve_tenant_identity,
    tenant_session,
)

logger = logging.getLogger(__name__)

REQUEST_STATUSES = ("pending", "completed", "dismissed")
# Mirrors AdminRequestDismissCategory in the Backend-Server schemas.
DISMISS_CATEGORIES = ("duplicate", "invalid", "out_of_scope", "other")
# Mirrors _DISMISSAL_REASON_TEXT_MAX in the Backend-Server route.
DISMISS_REASON_TEXT_MAX = 500
MAX_LIST_LIMIT = 500


class AdminRequest(Base):
    """Mapping of the tenant ``admin_requests`` table."""

    __tablename__ = "admin_requests"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    # Not an FK in prod — may dangle after operation GC (see module docstring).
    cg_operation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    requester_user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    request_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bulk_group_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String, nullable=True)
    request_metadata: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_version_number: Mapped[int | None] = mapped_column(nullable=True)
    assigned_to: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def require_staff_in_tenant(
    subject: str,
    tenant_slug: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> str:
    """Gate: subject holds live SOLSTICE_STAFF on at least one live brand.

    Returns the subject's tenant user_id. This is deliberately broader than
    ``require_brand_role`` — staff triage the whole tenant queue — but the
    authority still comes only from the subject's own ``brand_team_members``
    rows, never from an argument.
    """
    identity = resolve_tenant_identity(
        subject, tenant_slug, registry=registry, session_factory=session_factory
    )
    if identity is None:
        raise ToolError("not_authorized: unknown tenant or subject is not a member")
    with tenant_session(tenant_slug, session_factory) as session:
        staff_row = session.scalar(
            select(BrandTeamMember)
            .join(Brand, Brand.id == BrandTeamMember.brand_id)
            .where(
                BrandTeamMember.user_id == identity.user_id,
                BrandTeamMember.user_role == UserRole.SOLSTICE_STAFF.value,
                BrandTeamMember.deleted_at.is_(None),
                Brand.deleted_at.is_(None),
            )
            .limit(1)
        )
    if staff_row is None:
        raise ToolError(
            "not_authorized: requests access requires SOLSTICE_STAFF on at least one brand in this tenant"
        )
    return identity.user_id


def _request_summary(
    row: AdminRequest,
    brand_name: str | None,
    requester_name: str | None,
    requester_email: str | None,
) -> dict[str, Any]:
    """Project one admin_request row to a triage-friendly summary.

    ``request_metadata`` is trimmed to the keys an agent needs: the
    approval-request ``message``, the change-request ``additional_comment`` and
    comment count (full comment payloads are large), the GC-stamped
    ``operation_deleted`` flag, and the ``dismissal`` audit record.
    """
    meta = row.request_metadata if isinstance(row.request_metadata, dict) else {}
    return {
        "id": row.id,
        "operation_id": row.cg_operation_id,
        "brand_id": row.brand_id,
        "brand_name": brand_name,
        "requester": {
            "user_id": row.requester_user_id,
            "name": requester_name,
            "email": requester_email,
        },
        "request_type": row.request_type,
        "status": row.status,
        "priority": row.priority,
        "display_name": row.display_name,
        "project_id": row.project_id,
        "project_name": row.project_name,
        "assigned_to": row.assigned_to,
        "batch_id": row.batch_id,
        "bulk_group_id": row.bulk_group_id,
        "message": meta.get("message"),
        "additional_comment": meta.get("additional_comment"),
        "comment_count": len(meta.get("comments") or []),
        "operation_deleted": bool(meta.get("operation_deleted", False)),
        "dismissal": meta.get("dismissal"),
        "resolved_by_user_id": row.resolved_by_user_id,
        "resolved_at": _iso(row.resolved_at),
        "resolved_version_number": row.resolved_version_number,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def list_requests(
    subject: str,
    tenant_slug: str,
    status: str = "pending",
    brand_id: str | None = None,
    limit: int = 100,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[dict[str, Any]]:
    """List a tenant's admin requests, newest first.

    Gated by ``require_staff_in_tenant``. ``status`` is one of
    pending/completed/dismissed/all (default pending — "what's on the tab").
    ``brand_id`` optionally narrows to one brand. Soft-deleted rows excluded.
    """
    if status not in (*REQUEST_STATUSES, "all"):
        raise ToolError(
            f"invalid_arguments: status must be one of {', '.join((*REQUEST_STATUSES, 'all'))}"
        )
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    require_staff_in_tenant(
        subject, tenant_slug, registry=registry, session_factory=session_factory
    )
    with tenant_session(tenant_slug, session_factory) as session:
        stmt = (
            select(AdminRequest, Brand.name, User.name, User.email)
            .outerjoin(Brand, Brand.id == AdminRequest.brand_id)
            .outerjoin(User, User.id == AdminRequest.requester_user_id)
            .where(AdminRequest.deleted_at.is_(None))
        )
        if status != "all":
            stmt = stmt.where(AdminRequest.status == status)
        if brand_id:
            stmt = stmt.where(AdminRequest.brand_id == brand_id)
        stmt = stmt.order_by(
            AdminRequest.created_at.is_(None), AdminRequest.created_at.desc()
        ).limit(limit)
        rows = session.execute(stmt).all()
    return [
        _request_summary(row, brand_name, requester_name, requester_email)
        for row, brand_name, requester_name, requester_email in rows
    ]


def dismiss_request(
    subject: str,
    tenant_slug: str,
    request_id: str,
    reason_category: str,
    reason_text: str | None = None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Dismiss one pending admin request with a structured reason.

    Mirrors the Backend-Server dismiss route: SOLSTICE_STAFF on the row's own
    brand, pending rows only, ``reason_category`` mandatory
    (duplicate/invalid/out_of_scope/other), ``reason_text`` optional and capped
    at 500 chars. Writes status=dismissed + resolution audit columns + the
    ``request_metadata.dismissal`` record. Never deletes the row and never
    touches the linked operation.
    """
    if reason_category not in DISMISS_CATEGORIES:
        raise ToolError(
            f"invalid_arguments: reason_category must be one of {', '.join(DISMISS_CATEGORIES)}"
        )
    if reason_text is not None and len(reason_text) > DISMISS_REASON_TEXT_MAX:
        raise ToolError(
            f"invalid_arguments: reason_text exceeds {DISMISS_REASON_TEXT_MAX} characters"
        )
    try:
        with tenant_session(tenant_slug, session_factory) as session:
            row = session.scalar(
                select(AdminRequest).where(
                    AdminRequest.id == request_id, AdminRequest.deleted_at.is_(None)
                )
            )
            if row is None:
                raise ToolError("not_authorized: unknown request")
            brand_id = row.brand_id
        # Brand-scoped gate, matching the BE route: staff-in-tenant is NOT
        # enough — the caller must be SOLSTICE_STAFF on this row's own brand.
        identity = require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.SOLSTICE_STAFF,
            registry=registry, session_factory=session_factory,
        )
        with tenant_session(tenant_slug, session_factory) as session:
            locked = session.scalar(
                select(AdminRequest).where(
                    AdminRequest.id == request_id, AdminRequest.deleted_at.is_(None)
                ).with_for_update()
            )
            if locked is None:
                raise ToolError("not_authorized: unknown request")
            if locked.status != "pending":
                raise ToolError(
                    f"invalid_request: only pending requests can be dismissed (current status: {locked.status!r})"
                )
            dismissed_at = datetime.now(UTC)
            # Merge, don't replace: preserve existing metadata keys (comments,
            # resubmit context, operation_deleted). Reassign the dict so the
            # JSON/JSONB change is tracked.
            metadata = dict(locked.request_metadata) if isinstance(
                locked.request_metadata, dict
            ) else {}
            metadata["dismissal"] = {
                "category": reason_category,
                "text": reason_text,
                "dismissed_at": dismissed_at.isoformat(),
                "dismissed_by_user_id": identity.user_id,
            }
            locked.request_metadata = metadata
            locked.status = "dismissed"
            locked.resolved_at = dismissed_at
            locked.resolved_by_user_id = identity.user_id
            locked.updated_at = dismissed_at
            session.commit()
        return {
            "request_id": request_id,
            "brand_id": brand_id,
            "status": "dismissed",
            "reason_category": reason_category,
            "dismissed_at": dismissed_at.isoformat(),
        }
    finally:
        reset_brand_role()


__all__ = [
    "DISMISS_CATEGORIES",
    "AdminRequest",
    "dismiss_request",
    "list_requests",
    "require_staff_in_tenant",
]
