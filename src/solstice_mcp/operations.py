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
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, String, Uuid, func, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.brands import (
    BrandTeamMember,
    UserRole,
    require_brand_role,
    role_satisfies,
)
from solstice_mcp.storage import S3Error, S3ObjectMissing, S3ObjectTooLarge, S3Reader
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
    """Mapping of the tenant ``n_cg_operations`` table.

    Reads use a subset of columns; the write path (``create_operation``) also
    populates the prod NOT NULL columns (``prompt``, ``user_id``,
    ``filtered_clinical_claims_picker``, ``page``) so an INSERT succeeds against
    real Postgres, not just SQLite in tests. ``content_type`` and
    ``operation_metadata`` are mapped so the dir_map leaf can mirror the
    Backend-Server shape.
    """

    __tablename__ = "n_cg_operations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    project_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    # NOT NULL in prod; populated on insert by the write path.
    user_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    filtered_clinical_claims_picker: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    page: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(String, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    operation_metadata: Mapped[Any | None] = mapped_column(JSON, nullable=True)
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
    # The DB column ``metadata`` (jsonb in prod) is mapped to ``message_metadata``
    # and loaded deferred so read queries never pull the blob. The write path
    # (version commit) populates it; reads in this module never access it.
    message_metadata: Mapped[Any | None] = mapped_column(
        "metadata", JSON, nullable=True, deferred=True
    )
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


def get_project_info(
    subject: str,
    tenant_slug: str,
    project_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any] | None:
    """Return one project's dir_map tree. Gated at MEMBER on the project's brand."""
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


def _items_at_path(dir_map: dict[str, Any], folder_path: str) -> list[dict[str, Any]]:
    """Resolve the ``items`` list at ``folder_path`` in a dir_map.

    Mirrors the Backend-Server ``ProjectService._get_items_at_path``: root
    (``""``) returns ``dir_map["items"]``; otherwise walk slash-separated folder
    names matched by ``name`` + ``items``. A missing folder raises ToolError —
    we never auto-create folders, matching the backend's 404.
    """
    items = dir_map.get("items", [])
    if not folder_path:
        return items
    for part in [p for p in folder_path.split("/") if p]:
        found = None
        for item in items:
            if item.get("name") == part and "items" in item:
                found = item
                break
        if found is None:
            raise ToolError(f"not_found: path not found: {folder_path}")
        items = found.get("items", [])
    return items


def create_operation(
    subject: str,
    tenant_slug: str,
    project_id: str,
    name: str,
    folder_path: str = "",
    content_type: str | None = None,
    chat_title: str | None = None,
    file_name: str | None = None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Create a new operation and append it to a project's dir_map folder.

    Transactional: inserts one ``n_cg_operations`` row (status ``EDITING``,
    ``version_number`` 1) and appends a leaf into ``projects.dir_map`` at
    ``folder_path`` (root by default). Mirrors the Backend-Server
    ``ProjectService.add_operation_to_project`` leaf shape so the operation
    appears in the UI file tree.

    Authorization is gated at MEMBER on the project's brand (resolved from the
    project row, never a caller argument). ``user_id`` on the new row is the
    authenticated subject's user_id — it is not accepted as an argument. Add v1
    content afterwards via ``prepare_operation_version`` +
    ``commit_operation_version``.
    """
    with tenant_session(tenant_slug, session_factory) as session:
        project = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.deleted_at.is_(None)
            )
        )
        if project is None:
            # Uniform deny (existence oracle): same message as the membership
            # gate so a caller cannot probe which project ids exist.
            raise ToolError("not_authorized: unknown project")
        brand_id = project.brand_id
    # brand_id is derived from the project row, never a caller argument.
    identity = require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    operation_id = str(uuid4())
    now = datetime.now(UTC)
    with tenant_session(tenant_slug, session_factory) as session:
        locked = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.deleted_at.is_(None)
            ).with_for_update()
        )
        if locked is None:
            # Project vanished between the auth read and the locked re-read;
            # same uniform-deny message as the first lookup.
            raise ToolError("not_authorized: unknown project")
        new_map = deepcopy(locked.dir_map) or {"items": []}
        items = _items_at_path(new_map, folder_path)
        op = CgOperation(
            id=operation_id,
            brand_id=brand_id,
            project_id=project_id,
            user_id=identity.user_id,
            prompt="",
            filtered_clinical_claims_picker=[],
            page=1,
            status="EDITING",
            chat_title=chat_title or name,
            file_name=file_name or name,
            content_type=content_type,
            operation_metadata={},
            version_number=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        session.add(op)
        items.append(
            {
                "name": name,
                "operation_id": operation_id,
                "content_type": content_type,
                "veeva_document_number": None,
            }
        )
        # Reassign so SQLAlchemy tracks the JSON/JSONB change (in-place
        # mutation of the nested list is not tracked). Mirrors the backend.
        locked.dir_map = new_map
        session.commit()
    return {
        "operation_id": operation_id,
        "project_id": project_id,
        "brand_id": brand_id,
        "folder_path": folder_path,
        "name": name,
        "status": "EDITING",
        "version_number": 1,
    }


def list_operations_for_brand(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[dict[str, Any]]:
    """List non-deleted operations for a brand. Gated at MEMBER."""
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


def get_operation_info(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any] | None:
    """Return one operation's metadata (no messages). Gated at MEMBER on the op's brand."""
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


def get_operation_html(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    message_id: str,
    *,
    fetch: bool,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int = 600,
    max_inline_bytes: int = 2_000_000,
) -> dict[str, Any]:
    """Return the HTML body for one operation message.

    By default returns a presigned GET URL only (no body transfer). When
    ``fetch=True`` the body is downloaded inline, subject to a size cap.

    Authorization + intent filter:
    - Gated at MEMBER on the operation's brand (resolved from the row).
    - The intent filter is re-applied here: a non-staff caller cannot retrieve
      a ``draft`` document message at all — no URL, no body — because a
      presigned URL is itself a read capability. Only SOLSTICE_STAFF sees
      drafts; MEMBER/ADMIN see final only.
    """
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None:
            raise ToolError("not_authorized: unknown operation")
        brand_id = op.brand_id
    # Authorize BEFORE the message lookup so an unauthorized caller cannot
    # learn whether a message exists on an operation they can't access.
    identity = require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    staff = role_satisfies(identity.role, UserRole.SOLSTICE_STAFF)
    with tenant_session(tenant_slug, session_factory) as session:
        msg = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.message_id == message_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        if msg is None:
            raise ToolError("not_found: unknown message")
    if msg.intent == "draft" and not staff:
        # Draft visibility is enforced for both the URL and the body: a
        # presigned URL is a read capability, so it must not be handed to a
        # non-staff caller any more than the inline body would be.
        raise ToolError("not_authorized: draft messages require SOLSTICE_STAFF")
    if msg.type != "html":
        raise ToolError("not_found: message is not an html document")

    result: dict[str, Any] = {
        "operation_id": operation_id,
        "message_id": message_id,
        "type": msg.type,
        "intent": msg.intent,
        "version_number": msg.version_number,
        "url": None,
        "s3_key": None,
        "html": None,
    }

    if _looks_like_s3_key(msg.content):
        s3_key = msg.content
        tenant_config = registry.get(tenant_slug)
        bucket = tenant_config.s3_bucket if tenant_config is not None else ""
        if not bucket:
            raise ToolError("not_configured: tenant has no s3_bucket")
        result["s3_key"] = s3_key
        result["url"] = s3.presign(bucket, s3_key, presign_expiry)
        if fetch:
            try:
                body = s3.download(bucket, s3_key, max_inline_bytes)
            except S3ObjectTooLarge:
                result["too_large"] = True
            except S3ObjectMissing:
                raise ToolError("not_found: html object missing in s3") from None
            except S3Error as exc:
                raise ToolError(f"not_available: s3 read failed: {exc}") from exc
            else:
                result["html"] = body.decode("utf-8", errors="replace")
    else:
        # Inline HTML stored in the row (not yet offloaded to S3).
        result["inline"] = True
        if fetch:
            result["html"] = msg.content or ""
    return result


def _find_leaf(items: list[dict[str, Any]], operation_id: str) -> dict[str, Any] | None:
    """Depth-first search of a dir_map ``items`` tree for the leaf of one operation."""
    for item in items:
        if item.get("operation_id") == operation_id:
            return item
        found = _find_leaf(item.get("items", []), operation_id)
        if found is not None:
            return found
    return None


def update_operation(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    name: str | None = None,
    content_type: str | None = None,
    new_owner_user_id: str | None = None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Staff-only edit of an operation's display data.

    Updates any subset of:
    - ``name`` — the file name shown in the project view: sets
      ``n_cg_operations.file_name`` and the project's dir_map leaf ``name``.
    - ``content_type`` — uppercased, mirroring the Backend-Server admin route:
      sets the ``content_type`` column, ``operation_metadata.content_type_for_fe``
      (the FE source of truth), and the dir_map leaf ``content_type``.
    - ``new_owner_user_id`` — reassigns ``user_id``; must be a live team member
      of the operation's brand (validated server-side, use
      ``solstice_list_brand_users`` to discover candidates).

    Gated at SOLSTICE_STAFF on the operation's brand (resolved from the row,
    never a caller argument). This selector does NOT grant authority — the
    caller's own role still comes from the JWT subject.
    """
    if name is None and content_type is None and new_owner_user_id is None:
        raise ToolError("invalid_arguments: provide at least one of name, content_type, new_owner_user_id")
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None:
            raise ToolError("not_authorized: unknown operation")
        brand_id = op.brand_id
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry, session_factory=session_factory,
    )
    normalized_type = content_type.strip().upper() if content_type else None
    if content_type is not None and not normalized_type:
        raise ToolError("invalid_arguments: content_type must be non-empty")
    if name is not None and not name.strip():
        raise ToolError("invalid_arguments: name must be non-empty")
    changed: list[str] = []
    with tenant_session(tenant_slug, session_factory) as session:
        locked = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            ).with_for_update()
        )
        if locked is None:
            raise ToolError("not_authorized: unknown operation")
        if new_owner_user_id is not None:
            member = session.scalar(
                select(BrandTeamMember).where(
                    BrandTeamMember.brand_id == brand_id,
                    BrandTeamMember.user_id == new_owner_user_id,
                    BrandTeamMember.deleted_at.is_(None),
                )
            )
            if member is None:
                raise ToolError(
                    "invalid_arguments: new_owner_user_id is not a live team member of this brand"
                )
            locked.user_id = new_owner_user_id
            changed.append("user_id")
        if name is not None:
            locked.file_name = name
            changed.append("file_name")
        if normalized_type is not None:
            locked.content_type = normalized_type
            # content_type_for_fe in operation_metadata is the FE source of
            # truth; reassign the dict so the JSON change is tracked.
            metadata = dict(locked.operation_metadata) if isinstance(
                locked.operation_metadata, dict
            ) else {}
            metadata["content_type_for_fe"] = normalized_type
            locked.operation_metadata = metadata
            changed.append("content_type")
        # Mirror name/content_type into the project's dir_map leaf so the
        # project view reflects the change.
        if locked.project_id and (name is not None or normalized_type is not None):
            project = session.scalar(
                select(Project).where(
                    Project.id == locked.project_id, Project.deleted_at.is_(None)
                ).with_for_update()
            )
            if project is not None:
                new_map = deepcopy(project.dir_map) or {"items": []}
                leaf = _find_leaf(new_map.get("items", []), operation_id)
                if leaf is not None:
                    if name is not None:
                        leaf["name"] = name
                    if normalized_type is not None:
                        leaf["content_type"] = normalized_type
                    project.dir_map = new_map
        locked.updated_at = datetime.now(UTC)
        session.commit()
    return {
        "operation_id": operation_id,
        "brand_id": brand_id,
        "changed": changed,
        "file_name": name,
        "content_type": normalized_type,
        "user_id": new_owner_user_id,
    }


def approve_operation_version(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    message_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Staff-only approval: flip one draft document version to final.

    The target message must be a document row (type ``html`` or ``pdf``) with
    intent ``draft``. The flip updates the ``intent`` column and the
    ``versionIntent`` key in the message metadata (the FE reads both). Approving
    an already-final version is an idempotent no-op. Text/blueprint messages
    are rejected.

    Gated at SOLSTICE_STAFF on the operation's brand (resolved from the row).
    """
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None:
            raise ToolError("not_authorized: unknown operation")
        brand_id = op.brand_id
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        msg = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.message_id == message_id,
                CgOperationMessage.deleted_at.is_(None),
            ).with_for_update()
        )
        if msg is None:
            raise ToolError("not_found: unknown message")
        if msg.type not in ("html", "pdf"):
            raise ToolError(
                f"invalid_message: type {msg.type!r} is not a document (html/pdf) version"
            )
        if msg.intent == "final":
            return {
                "operation_id": operation_id,
                "message_id": message_id,
                "version_number": msg.version_number,
                "intent": "final",
                "already_final": True,
            }
        if msg.intent != "draft":
            raise ToolError(
                f"invalid_message: intent {msg.intent!r} is not a draft version"
            )
        msg.intent = "final"
        if isinstance(msg.message_metadata, dict):
            metadata = dict(msg.message_metadata)
            metadata["versionIntent"] = "final"
            msg.message_metadata = metadata
        session.commit()
        version_number = msg.version_number
    return {
        "operation_id": operation_id,
        "message_id": message_id,
        "version_number": version_number,
        "intent": "final",
        "already_final": False,
    }


def _sanitize_file_name(file_name: str | None) -> str:
    """Reduce a user-supplied file name to a safe S3 path segment."""
    if not file_name:
        return ""
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base.replace(" ", "_")


_VERSION_KINDS = ("html", "pdf")


def _require_version_kind(kind: str) -> None:
    if kind not in _VERSION_KINDS:
        raise ToolError(f"invalid_arguments: type must be one of {', '.join(_VERSION_KINDS)}")


def _version_s3_key(
    kind: str, operation_id: str, version: int, message_id: str, file_name: str | None
) -> str:
    if kind == "html":
        return f"cg_operation_msg_html/{operation_id}/v{version}/{message_id}/v{version}.html"
    return f"approved_pdfs/{operation_id}/v{version}_{_sanitize_file_name(file_name) or f'v{version}.pdf'}"


def _validate_version_key(
    kind: str, s3_key: str, operation_id: str, version: int
) -> str:
    """Strictly validate a client-supplied s3_key for a committed version.

    Returns the message_id embedded in the key (html) or "" (pdf). Raises
    ToolError on any deviation from the expected shape so a caller cannot
    target an arbitrary key, another operation, or a stale version segment.
    """
    if kind == "html":
        prefix = f"cg_operation_msg_html/{operation_id}/v{version}/"
        suffix = f"/v{version}.html"
        if not (s3_key.startswith(prefix) and s3_key.endswith(suffix)):
            raise ToolError("invalid_key: key does not match the prepared version")
        message_id = s3_key[len(prefix) : -len(suffix)]
        if not message_id or "/" in message_id:
            raise ToolError("invalid_key: malformed message_id segment")
        return message_id
    if kind == "pdf":
        prefix = f"approved_pdfs/{operation_id}/v{version}_"
        if not s3_key.startswith(prefix):
            raise ToolError("invalid_key: key does not match the prepared version")
        suffix = s3_key[len(prefix) :]
        if not suffix or "/" in suffix:
            raise ToolError("invalid_key: malformed pdf file name segment")
        return ""
    raise ToolError(f"invalid_key: unsupported type {kind!r}")


def _doc_message_metadata(
    *, version: int, intent: str, s3_key: str, message_id: str, now: datetime, file_name: str | None
) -> dict[str, Any]:
    """Mirror the Backend-Server bot document-message metadata shape so the
    frontend renders an MCP-created version identically to a UI-created one.

    ``type: "bot"`` is required: the FE version stepper
    (isDocumentVersionMessage) reads the message ``type`` from this metadata
    blob, not the DB ``type`` column, and drops any document row that isn't
    ``type == "bot"``. Without it an MCP-created version is invisible in the UI.
    ``htmlDocumentLastVersion`` mirrors BE (build_final_document_bot_metadata):
    the *previous* version number, i.e. version - 1."""
    return {
        "id": message_id,
        "timestamp": now.isoformat(),
        "type": "bot",
        "isFinalDocument": True,
        "documentVersion": version,
        "htmlDocumentVersion": version,
        "htmlDocumentLastVersion": version - 1,
        "versionIntent": intent,
        "finalContentS3Key": s3_key,
        "finalContent": "",
        "fileName": file_name,
    }


def prepare_operation_version(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    kind: str,
    file_name: str | None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int = 600,
) -> dict[str, Any]:
    """Issue a presigned PUT URL for the next document version on an operation.

    Two-step write (step 1 of 2): the caller uploads the file bytes directly to
    tenant S3 at the returned ``upload_url``, then calls
    ``commit_operation_version`` with the returned ``s3_key`` to insert the DB
    row. Authorization is gated at MEMBER on the operation's brand (resolved
    from the row). No version row is created here; the version number is
    recomputed at commit under an operation-row lock.
    """
    _require_version_kind(kind)
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None:
            raise ToolError("not_authorized: unknown operation")
        brand_id = op.brand_id
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    tenant_config = registry.get(tenant_slug)
    bucket = tenant_config.s3_bucket if tenant_config is not None else ""
    if not bucket:
        raise ToolError("not_configured: tenant has no s3_bucket")
    with tenant_session(tenant_slug, session_factory) as session:
        max_v = session.scalar(
            select(func.max(CgOperationMessage.version_number)).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
    next_v = (max_v or 0) + 1
    message_id = str(uuid4())
    key = _version_s3_key(kind, operation_id, next_v, message_id, file_name)
    content_type = "text/html" if kind == "html" else "application/pdf"
    upload_url = s3.presign_put(bucket, key, presign_expiry, content_type)
    return {
        "operation_id": operation_id,
        "type": kind,
        "version_number": next_v,
        "message_id": message_id,
        "s3_key": key,
        "upload_url": upload_url,
        "expires_in": presign_expiry,
    }


def commit_operation_version(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    kind: str,
    s3_key: str,
    file_name: str | None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
) -> dict[str, Any]:
    """Insert a new document version row after the client has uploaded to S3.

    Two-step write (step 2 of 2). Append-only: only INSERTs a new row, never
    updates an existing one. The version number is recomputed under an
    operation-row lock and the client-supplied ``s3_key`` is strictly
    validated against the prepared version, so a caller cannot target another
    operation, an arbitrary key, or a stale version segment.

    Intent is derived server-side from the subject's brand role:
    SOLSTICE_STAFF -> ``draft``; MEMBER / ADMIN -> ``final``. There is no
    ``intent`` argument — the filter is derived from the token, mirroring the
    read-side rule.
    """
    _require_version_kind(kind)
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
    intent = "draft" if identity.role == UserRole.SOLSTICE_STAFF else "final"
    tenant_config = registry.get(tenant_slug)
    bucket = tenant_config.s3_bucket if tenant_config is not None else ""
    if not bucket:
        raise ToolError("not_configured: tenant has no s3_bucket")
    with tenant_session(tenant_slug, session_factory) as session:
        locked = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            ).with_for_update()
        )
        if locked is None:
            raise ToolError("not_authorized: unknown operation")
        max_v = session.scalar(
            select(func.max(CgOperationMessage.version_number)).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        next_v = (max_v or 0) + 1
        message_id = _validate_version_key(kind, s3_key, operation_id, next_v)
        # Confirm the client uploaded. Done under the operation-row lock so a
        # concurrent committer cannot land between validation and insert.
        size = s3.head(bucket, s3_key)
        if size is None:
            raise ToolError("not_found: object not uploaded - PUT to the upload_url first")
        max_pos = session.scalar(
            select(func.max(CgOperationMessage.position)).where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        base_pos = (max_pos or -1) + 1
        now = datetime.now(UTC)
        pill = CgOperationMessage(
            id=str(uuid4()),
            operation_id=operation_id,
            message_id=str(uuid4()),
            author_id=identity.user_id,
            type="text",
            content="Save new version",
            version_number=None,
            intent=None,
            position=base_pos,
            message_metadata={
                "id": str(uuid4()),
                "timestamp": now.isoformat(),
                "type": "user",
                "finalContent": "Save new version",
                "kind": "user_feedback",
            },
            created_at=now,
            deleted_at=None,
        )
        doc = CgOperationMessage(
            id=str(uuid4()),
            operation_id=operation_id,
            message_id=message_id,
            author_id=None,
            type=kind,
            content=s3_key,
            version_number=next_v,
            intent=intent,
            position=base_pos + 1,
            message_metadata=_doc_message_metadata(
                version=next_v, intent=intent, s3_key=s3_key,
                message_id=message_id, now=now, file_name=file_name,
            ),
            created_at=now,
            deleted_at=None,
        )
        session.add(pill)
        session.add(doc)
        session.commit()
    return {
        "operation_id": operation_id,
        "type": kind,
        "version_number": next_v,
        "intent": intent,
        "message_id": message_id,
        "s3_key": s3_key,
        "size": size,
    }


__all__ = [
    "CgOperation",
    "CgOperationMessage",
    "Project",
    "approve_operation_version",
    "commit_operation_version",
    "create_operation",
    "get_operation_html",
    "get_operation_info",
    "get_project_info",
    "list_operation_messages",
    "list_operations_for_brand",
    "list_projects_for_brand",
    "prepare_operation_version",
    "update_operation",
]
