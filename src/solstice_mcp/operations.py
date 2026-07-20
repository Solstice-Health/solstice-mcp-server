"""Read-only access to Solstice content-generation projects, operations, and chat.

Mirrors the Backend-Server data model (see
``Backend-Server/src/content_generation_new/db/content_generation_models.py``)
without importing it:

- ``projects`` — a brand's folder tree in ``dir_map`` (JSON). Leaves reference
  an ``operation_id``; folders nest ``items``.
- ``n_cg_operations`` — a content-generation workspace. FK to ``brand_id`` and
  optional ``project_id``.
- ``n_cg_operation_messages`` — chat + document versions on an operation.
  ``type`` ∈ {text, html, pdf, blueprint}; document rows (html/pdf) carry an
  ``intent`` ∈ {draft, final}. HTML bodies live in tenant S3 under
  ``cg_operation_msg_html/...``; the ``content`` column holds either inline
  HTML or that S3 key. This module returns the S3 key but NOT the body — the
  body fetch is deferred to a future tool (see ``solstice_operation_html``).

Authorization: every function routes through ``require_brand_role`` (MEMBER),
  so the subject must hold a live ``brand_team_members`` row on the brand that
  owns the resource. Role is derived server-side from the JWT subject; tool
  arguments only select resources, they never grant access.

Intent visibility (the RBAC rule this module enforces server-side, which the
Backend-Server does NOT enforce on its own GET /messages route):
- SOLSTICE_STAFF on the brand → sees all document rows (draft + final).
- MEMBER / ADMIN → draft document rows are excluded. Text/blueprint rows
  (intent NULL) remain visible to everyone.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, String, Uuid, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.brands import (
    UserRole,
    require_brand_role,
    reset_brand_role,
    role_satisfies,
)
from solstice_mcp.tenants import Base, SessionFactory, TenantRegistry, tenant_session

logger = logging.getLogger(__name__)

_HTML_S3_KEY_PREFIX = "cg_operation_msg_html"


def _looks_like_s3_key(content: str | None) -> bool:
    if not content:
        return False
    return content.startswith(_HTML_S3_KEY_PREFIX) and "<" not in content and ">" not in content


class Project(Base):
    """Read-only mapping of the tenant ``projects`` table."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String)
    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    dir_map: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CgOperation(Base):
    """Read-only mapping of the tenant ``n_cg_operations`` table."""

    __tablename__ = "n_cg_operations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    project_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(String, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    version_number: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CgOperationMessage(Base):
    """Read-only mapping of the tenant ``n_cg_operation_messages`` table.

    The DB column ``metadata`` is mapped to the Python attribute
    ``message_metadata`` (``metadata`` is reserved by SQLAlchemy's Base); we do
    not select it here because it is a large blob and not needed for read
    summaries.
    """

    __tablename__ = "n_cg_operation_messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    operation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    type: Mapped[str] = mapped_column(String)
    content: Mapped[str | None] = mapped_column(String, nullable=True)
    version_number: Mapped[int | None] = mapped_column(nullable=True)
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column()
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _project_summary(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "brand_id": project.brand_id,
        "created_at": _iso(project.created_at),
    }


def _operation_summary(op: CgOperation) -> dict[str, Any]:
    return {
        "id": op.id,
        "brand_id": op.brand_id,
        "project_id": op.project_id,
        "operation_status": op.status,
        "chat_title": op.chat_title,
        "file_name": op.file_name,
        "version_number": op.version_number,
        "created_at": _iso(op.created_at),
        "updated_at": _iso(op.updated_at),
    }


def _message_summary(msg: CgOperationMessage) -> dict[str, Any]:
    """Project one message row to a read summary.

    For ``text``: return the content inline (chat is the agent-readable payload).
    For ``html`` / ``pdf``: return the S3 key when ``content`` is one, else an
    ``inline`` flag; never return the body (deferred to a future tool).
    For ``blueprint``: return existence only (the JSON payload is large).
    """
    base = {
        "id": msg.id,
        "message_id": msg.message_id,
        "type": msg.type,
        "intent": msg.intent,
        "version_number": msg.version_number,
        "author_id": msg.author_id,
        "position": msg.position,
        "created_at": _iso(msg.created_at),
    }
    if msg.type == "text":
        return {**base, "content": msg.content}
    if msg.type in ("html", "pdf"):
        if _looks_like_s3_key(msg.content):
            return {**base, "s3_key": msg.content, "body": None}
        return {**base, "s3_key": None, "inline": True, "body": None}
    if msg.type == "blueprint":
        return {**base, "has_blueprint": True, "body": None}
    return base


def _brand_id_for_project(session, project_id: str) -> str | None:
    row = session.scalar(
        select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    )
    return row.brand_id if row is not None else None


def _brand_id_for_operation(session, operation_id: str) -> str | None:
    row = session.scalar(
        select(CgOperation).where(
            CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
        )
    )
    return row.brand_id if row is not None else None


def list_projects_for_brand(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[dict[str, Any]]:
    """List non-deleted projects for a brand. Gated at MEMBER."""
    try:
        require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.MEMBER,
            registry=registry, session_factory=session_factory,
        )
        with tenant_session(tenant_slug, session_factory) as session:
            rows = session.scalars(
                select(Project).where(
                    Project.brand_id == brand_id, Project.deleted_at.is_(None)
                ).order_by(Project.name)
            ).all()
        return [_project_summary(p) for p in rows]
    finally:
        reset_brand_role()


def get_project_info(
    subject: str,
    tenant_slug: str,
    project_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any] | None:
    """Return one project's dir_map tree. Gated at MEMBER on the project's brand."""
    try:
        with tenant_session(tenant_slug, session_factory) as session:
            project = session.scalar(
                select(Project).where(
                    Project.id == project_id, Project.deleted_at.is_(None)
                )
            )
            if project is None:
                return None
            brand_id = project.brand_id
        # Re-validate membership on the brand that owns the project. brand_id is
        # derived from the row, never from a caller argument.
        require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.MEMBER,
            registry=registry, session_factory=session_factory,
        )
        return {
            "id": project.id,
            "name": project.name,
            "brand_id": brand_id,
            "dir_map": project.dir_map,
        }
    finally:
        reset_brand_role()


def list_operations_for_brand(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[dict[str, Any]]:
    """List non-deleted operations for a brand. Gated at MEMBER."""
    try:
        require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.MEMBER,
            registry=registry, session_factory=session_factory,
        )
        with tenant_session(tenant_slug, session_factory) as session:
            rows = session.scalars(
                select(CgOperation).where(
                    CgOperation.brand_id == brand_id, CgOperation.deleted_at.is_(None)
                ).order_by(CgOperation.created_at)
            ).all()
        return [_operation_summary(op) for op in rows]
    finally:
        reset_brand_role()


def get_operation_info(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any] | None:
    """Return one operation's metadata (no messages). Gated at MEMBER on the op's brand."""
    try:
        with tenant_session(tenant_slug, session_factory) as session:
            op = session.scalar(
                select(CgOperation).where(
                    CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
                )
            )
            if op is None:
                return None
            brand_id = op.brand_id
        require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.MEMBER,
            registry=registry, session_factory=session_factory,
        )
        return _operation_summary(op)
    finally:
        reset_brand_role()


def list_operation_messages(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[dict[str, Any]]:
    """Return an operation's chat + document-version summaries.

    Intent visibility is enforced server-side from the subject's brand role:
    SOLSTICE_STAFF sees draft + final; MEMBER / ADMIN see final only (drafts
    excluded). The role is derived from the JWT subject — there is no
    ``intent`` or ``role`` argument the caller can use to bypass this.
    """
    try:
        with tenant_session(tenant_slug, session_factory) as session:
            op = session.scalar(
                select(CgOperation).where(
                    CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
                )
            )
            if op is None:
                raise ToolError("not_authorized: unknown operation")
            brand_id = op.brand_id
        identity = require_brand_role(
            subject, tenant_slug, brand_id,
            min_role=UserRole.MEMBER,
            registry=registry, session_factory=session_factory,
        )
        staff = role_satisfies(identity.role, UserRole.SOLSTICE_STAFF)
        with tenant_session(tenant_slug, session_factory) as session:
            stmt = select(CgOperationMessage).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.deleted_at.is_(None),
            )
            if not staff:
                # Exclude draft document rows. NULL intent (text/blueprint/legacy)
                # stays visible to everyone.
                stmt = stmt.where(
                    or_(
                        CgOperationMessage.intent.is_(None),
                        CgOperationMessage.intent != "draft",
                    )
                )
            stmt = stmt.order_by(
                CgOperationMessage.created_at.is_(None),
                CgOperationMessage.created_at,
                CgOperationMessage.position,
            )
            rows = session.scalars(stmt).all()
        return [_message_summary(m) for m in rows]
    finally:
        reset_brand_role()


__all__ = [
    "CgOperation",
    "CgOperationMessage",
    "Project",
    "get_operation_info",
    "get_project_info",
    "list_operation_messages",
    "list_operations_for_brand",
    "list_projects_for_brand",
]
