"""Access to Solstice content-generation projects, operations, PRC templates, and chat.

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
- ``prc_template_versions`` — versioned proof-shell HTML. Reads resolve the
  effective template through operation, brand, environment, then platform
  precedence and never expose an unscoped tenant-wide template listing. Writes
  append a new version only and require SOLSTICE_STAFF on a selected brand.

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
from uuid import UUID, uuid4

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, Uuid, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.brands import (
    Brand,
    BrandTeamMember,
    UserRole,
    require_brand_role,
    role_satisfies,
)
from solstice_mcp.storage import S3Error, S3ObjectMissing, S3ObjectTooLarge, S3Reader
from solstice_mcp.tenants import Base, SessionFactory, TenantRegistry, tenant_session

logger = logging.getLogger(__name__)

_HTML_S3_KEY_PREFIX = "cg_operation_msg_html"
_PRC_CONTENT_TYPES = {"banner", "email", "social"}
_PRC_TEMPLATE_STATUSES = {"draft", "published"}


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
    # prompt and filtered_clinical_claims_picker are write-path columns and can
    # be very large; deferred so list/read queries never load them. Deferral
    # does not affect INSERTs, and any explicit attribute access still lazy-
    # loads within the session.
    prompt: Mapped[str | None] = mapped_column(String, nullable=True, deferred=True)
    filtered_clinical_claims_picker: Mapped[Any | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    page: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(String, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # SOLSTICE_GENERATED | EDIT_HTML | EDIT_PDF | EDIT_MP4. Backend dashboards
    # and the FE category router filter on this column; a NULL here makes the
    # operation invisible to those views, so the write path always sets it.
    operation_category: Mapped[str | None] = mapped_column(String, nullable=True)
    # The backend's intake/recents filter requires is_chat_history_deleted ==
    # False (intake_dashboard_filters.py). The column default is Python-side
    # only (backend ORM), so an MCP insert that omits it stores NULL and the
    # row is filtered out (NULL == FALSE is NULL in SQL). Always set it.
    is_chat_history_deleted: Mapped[bool | None] = mapped_column(nullable=True)
    # Set by the category-aware commit finishing writes: the backend flags
    # uploaded/edited documents with is_html_saved=True so file-browser
    # queries (get_html_cg_operation_files_of_brand) include them.
    is_html_saved: Mapped[bool | None] = mapped_column(nullable=True)
    # Deferred: operation_metadata blobs run to hundreds of KB on generated
    # operations. Loading them eagerly made list_operations_for_brand pull
    # hundreds of MB on large brands and OOM-kill the worker (502 at the
    # gateway). Write paths that mutate it (update_operation, commit finishing
    # writes) still lazy-load it on attribute access inside their session.
    operation_metadata: Mapped[Any | None] = mapped_column(JSON, nullable=True, deferred=True)
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


class PrcTemplateVersion(Base):
    """Mapping of the tenant ``prc_template_versions`` table."""

    __tablename__ = "prc_template_versions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    template_key: Mapped[str] = mapped_column(String)
    version_number: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_template: Mapped[str] = mapped_column(Text, deferred=True)
    config_schema: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    default_field_values: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String)
    created_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "template_key",
            "content_type",
            "version_number",
            name="uix_prc_template_key_content_version",
        ),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_asset_url(tenant_slug: str, operation_id: str) -> str:
    """User-facing Solstice link for one operation.

    Every successful write response includes this so agents can hand the user
    a clickable URL instead of a bare operation UUID. Subdomains use hyphens
    while tenant slugs use underscores (sanofi_sandbox -> sanofi-sandbox);
    the link grants nothing — Solstice still enforces the signed-in user's
    tenant and brand access.
    """
    host = tenant_slug.replace("_", "-")
    return f"https://www.{host}.solsticehealth.co/home/assets/{operation_id}"


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


def _normalized_uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _pinned_template_id(metadata: Any, content_type: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    prc_templates = metadata.get("prc_templates")
    if isinstance(prc_templates, dict):
        config = prc_templates.get(content_type)
        if isinstance(config, dict):
            pinned = _normalized_uuid(config.get("template_version_id"))
            if pinned:
                return pinned
    if content_type == "email":
        email_settings = metadata.get("email_settings")
        if isinstance(email_settings, dict):
            legacy = email_settings.get("interactive_prc_template")
            if isinstance(legacy, dict):
                return _normalized_uuid(legacy.get("template_version_id"))
    return None


def _prc_explicitly_disabled(metadata: Any, content_type: str) -> bool:
    if not isinstance(metadata, dict):
        return False
    prc_templates = metadata.get("prc_templates")
    if isinstance(prc_templates, dict):
        config = prc_templates.get(content_type)
        if isinstance(config, dict):
            pinned = _normalized_uuid(config.get("template_version_id"))
            if config.get("enabled") is False and pinned is None:
                return True
    if content_type == "email":
        email_settings = metadata.get("email_settings")
        if isinstance(email_settings, dict):
            legacy = email_settings.get("interactive_prc_template")
            if isinstance(legacy, dict):
                pinned = _normalized_uuid(legacy.get("template_version_id"))
                return legacy.get("enabled") is False and pinned is None
    return False


def _template_by_id(session, template_id: str | None) -> PrcTemplateVersion | None:
    if template_id is None:
        return None
    return session.scalar(
        select(PrcTemplateVersion).where(
            PrcTemplateVersion.id == template_id,
            PrcTemplateVersion.deleted_at.is_(None),
        )
    )


def _latest_published_template(
    session,
    template_key: str,
    content_type: str,
) -> PrcTemplateVersion | None:
    return session.scalar(
        select(PrcTemplateVersion)
        .where(
            PrcTemplateVersion.template_key == template_key,
            PrcTemplateVersion.content_type == content_type,
            PrcTemplateVersion.status == "published",
            PrcTemplateVersion.deleted_at.is_(None),
        )
        .order_by(PrcTemplateVersion.version_number.desc())
    )


def _brand_template(
    session,
    brand_name: str,
    content_type: str,
) -> PrcTemplateVersion | None:
    suffix = f"_{content_type}"
    rows = session.scalars(
        select(PrcTemplateVersion)
        .where(
            PrcTemplateVersion.template_key.like(f"brand_%{suffix}"),
            PrcTemplateVersion.content_type == content_type,
            PrcTemplateVersion.status == "published",
            PrcTemplateVersion.deleted_at.is_(None),
        )
        .order_by(PrcTemplateVersion.version_number.desc())
    ).all()
    matches: list[tuple[int, int, PrcTemplateVersion]] = []
    lowered_name = brand_name.lower()
    for row in rows:
        slug = row.template_key[len("brand_") : -len(suffix)]
        if slug and slug in lowered_name:
            matches.append((len(slug), row.version_number, row))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _prc_template_payload(
    template: PrcTemplateVersion,
    *,
    tier: str,
    fetch: bool,
    max_inline_bytes: int,
) -> dict[str, Any]:
    payload = {
        "id": template.id,
        "template_key": template.template_key,
        "version_number": template.version_number,
        "content_type": template.content_type,
        "name": template.name,
        "description": template.description,
        "config_schema": template.config_schema,
        "default_field_values": template.default_field_values,
        "template_status": template.status,
        "resolved_tier": tier,
        "created_at": _iso(template.created_at),
        "updated_at": _iso(template.updated_at),
    }
    if not fetch:
        return {**payload, "html_template": None}
    html = template.html_template
    size_bytes = len(html.encode("utf-8"))
    if size_bytes > max_inline_bytes:
        raise ToolError(
            f"too_large: PRC template is {size_bytes} bytes; inline limit is {max_inline_bytes}"
        )
    return {**payload, "html_template": html, "html_size_bytes": size_bytes}


def resolve_prc_template_for_brand(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    content_type: str,
    *,
    operation_id: str | None = None,
    fetch: bool = False,
    max_inline_bytes: int,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any] | None:
    """Resolve one brand-scoped PRC template using Backend-Server precedence."""
    normalized_content_type = content_type.strip().lower()
    if not normalized_content_type:
        raise ToolError("invalid_request: content_type is required")
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        brand = session.scalar(
            select(Brand).where(Brand.id == brand_id, Brand.deleted_at.is_(None))
        )
        if brand is None:
            return None
        metadata = brand.brand_metadata if isinstance(brand.brand_metadata, dict) else {}
        if _prc_explicitly_disabled(metadata, normalized_content_type):
            return None

        template = None
        tier = ""
        parsed_operation_id = _normalized_uuid(operation_id)
        if parsed_operation_id:
            operation = session.scalar(
                select(CgOperation).where(
                    CgOperation.id == parsed_operation_id,
                    CgOperation.brand_id == brand_id,
                    func.lower(CgOperation.content_type) == normalized_content_type,
                    CgOperation.deleted_at.is_(None),
                )
            )
            operation_metadata = operation.operation_metadata if operation is not None else None
            operation_pin = (
                _normalized_uuid(operation_metadata.get("prc_template_version_id"))
                if isinstance(operation_metadata, dict)
                else None
            )
            template = _template_by_id(session, operation_pin)
            if template is not None and template.content_type == normalized_content_type:
                tier = "operation"
            else:
                template = None

        if template is None:
            template = _template_by_id(
                session, _pinned_template_id(metadata, normalized_content_type)
            )
            if template is not None and template.content_type == normalized_content_type:
                tier = "brand"
            else:
                template = None

        if template is None:
            template = _brand_template(session, brand.name, normalized_content_type)
            if template is not None:
                tier = "brand"

        if template is None:
            template = _latest_published_template(
                session, f"environment_default_{normalized_content_type}", normalized_content_type
            )
            if template is not None:
                tier = "environment"

        if template is None:
            template = _latest_published_template(
                session, f"platform_default_{normalized_content_type}", normalized_content_type
            )
            if template is not None:
                tier = "default"

        if template is None:
            return None
        return _prc_template_payload(
            template,
            tier=tier,
            fetch=fetch,
            max_inline_bytes=max_inline_bytes,
        )


def create_prc_template_version(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    template_key: str,
    content_type: str,
    name: str,
    html_template: str,
    status: str,
    confirmed: bool,
    description: str | None = None,
    config_schema: dict[str, Any] | None = None,
    default_field_values: dict[str, Any] | None = None,
    *,
    max_inline_bytes: int,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Append one PRC template version without selecting or mutating another row."""
    identity = require_brand_role(
        subject,
        tenant_slug,
        brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry,
        session_factory=session_factory,
    )
    if not confirmed:
        raise ToolError(
            "confirmation_required: show the user the template key, content type, "
            "name, status, and HTML preview before retrying with confirmed=true"
        )

    normalized_key = template_key.strip()
    normalized_content_type = content_type.strip().lower()
    normalized_name = name.strip()
    normalized_status = status.strip().lower()
    if not normalized_key:
        raise ToolError("invalid_request: template_key is required")
    if len(normalized_key) > 255:
        raise ToolError("invalid_request: template_key must be at most 255 characters")
    if normalized_content_type not in _PRC_CONTENT_TYPES:
        allowed = ", ".join(sorted(_PRC_CONTENT_TYPES))
        raise ToolError(f"invalid_request: content_type must be one of {allowed}")
    if not normalized_name:
        raise ToolError("invalid_request: name is required")
    if len(normalized_name) > 255:
        raise ToolError("invalid_request: name must be at most 255 characters")
    if normalized_status not in _PRC_TEMPLATE_STATUSES:
        allowed = ", ".join(sorted(_PRC_TEMPLATE_STATUSES))
        raise ToolError(f"invalid_request: status must be one of {allowed}")
    if not html_template.strip():
        raise ToolError("invalid_request: html_template is required")
    html_size_bytes = len(html_template.encode("utf-8"))
    if html_size_bytes > max_inline_bytes:
        raise ToolError(
            f"too_large: PRC template is {html_size_bytes} bytes; inline limit is {max_inline_bytes}"
        )

    now = datetime.now(UTC)
    with tenant_session(tenant_slug, session_factory) as session:
        brand = session.scalar(
            select(Brand)
            .where(Brand.id == brand_id, Brand.deleted_at.is_(None))
            .with_for_update()
        )
        if brand is None:
            raise ToolError("not_authorized: unknown brand")
        latest_version = session.scalar(
            select(func.max(PrcTemplateVersion.version_number)).where(
                PrcTemplateVersion.template_key == normalized_key,
                PrcTemplateVersion.content_type == normalized_content_type,
            )
        )
        template = PrcTemplateVersion(
            id=str(uuid4()),
            template_key=normalized_key,
            version_number=(latest_version or 0) + 1,
            content_type=normalized_content_type,
            name=normalized_name,
            description=description.strip() if description and description.strip() else None,
            html_template=html_template,
            config_schema=config_schema,
            default_field_values=default_field_values,
            status=normalized_status,
            created_by=identity.user_id,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        session.add(template)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ToolError(
                "conflict: another PRC template version was created concurrently; "
                "retry to append the next version"
            ) from exc

    return {
        "id": template.id,
        "template_key": template.template_key,
        "version_number": template.version_number,
        "content_type": template.content_type,
        "name": template.name,
        "description": template.description,
        "config_schema": template.config_schema,
        "default_field_values": template.default_field_values,
        "template_status": template.status,
        "created_at": _iso(template.created_at),
        "html_size_bytes": html_size_bytes,
        "brand_selection_updated": False,
    }


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
    operation_category: str = "SOLSTICE_GENERATED",
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Create a new operation and append it to a project's dir_map folder.

    Transactional: inserts one ``n_cg_operations`` row (status ``EDITING``,
    ``version_number`` 1) and appends a leaf into ``projects.dir_map`` at
    ``folder_path`` (root by default). Mirrors the Backend-Server
    ``ProjectService.add_operation_to_project`` leaf shape so the operation
    appears in the UI file tree.

    ``operation_category`` is keyword-only and never caller-supplied at the
    tool boundary: the create tool always passes SOLSTICE_GENERATED and the
    edit tool maps its ``kind`` to EDIT_HTML / EDIT_PDF.

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
            # NULL here would hide the operation from category-filtered
            # dashboards and the FE router (parse-operation
            # getOperationCategory returns null for unknown values).
            operation_category=operation_category,
            # Visibility filters also require this to be FALSE, not NULL —
            # the backend default is ORM-side only and does not apply here.
            is_chat_history_deleted=False,
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
        "operation_category": operation_category,
        "version_number": 1,
        "asset_url": build_asset_url(tenant_slug, operation_id),
    }


_EDIT_KIND_TO_CATEGORY = {"html": "EDIT_HTML", "pdf": "EDIT_PDF"}


def create_edit_operation(
    subject: str,
    tenant_slug: str,
    project_id: str,
    name: str,
    kind: str,
    content_type: str,
    folder_path: str = "",
    file_name: str | None = None,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Create an *edit request* operation (user brings a finished document).

    Identical write shape to ``create_operation`` except the category is
    EDIT_HTML / EDIT_PDF (mapped from ``kind``, never caller-supplied
    directly). The document itself lands afterwards via
    ``prepare_operation_version`` + ``commit_operation_version``, whose
    category-aware finishing writes complete the backend upload contract
    (``is_html_saved``, ``approved_pdf_s3_key``, ``status``).
    """
    category = _EDIT_KIND_TO_CATEGORY.get(kind)
    if category is None:
        raise ToolError("invalid_argument: kind must be 'html' or 'pdf'")
    return create_operation(
        subject,
        tenant_slug,
        project_id,
        name,
        folder_path,
        content_type,
        None,
        file_name,
        operation_category=category,
        registry=registry,
        session_factory=session_factory,
    )


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
                "asset_url": build_asset_url(tenant_slug, operation_id),
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
        "asset_url": build_asset_url(tenant_slug, operation_id),
    }


def _sanitize_file_name(file_name: str | None) -> str:
    """Reduce a user-supplied file name to a safe S3 path segment."""
    if not file_name:
        return ""
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base.replace(" ", "_")


_VERSION_KINDS = ("html", "pdf")
# "source" is not a version: it uploads the design source file (InDesign,
# ZIP, PPTX, HTML...) alongside an edit operation's working document. It
# shares the prepare/commit tool pair but writes a metadata pointer
# (operation_metadata.sourcefile_s3_key) instead of message rows.
_UPLOAD_KINDS = (*_VERSION_KINDS, "source")
# Categories whose operations may carry a design source file.
_EDIT_CATEGORIES = ("EDIT_HTML", "EDIT_PDF")


def _require_upload_kind(kind: str) -> None:
    if kind not in _UPLOAD_KINDS:
        raise ToolError(f"invalid_arguments: type must be one of {', '.join(_UPLOAD_KINDS)}")


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


def _validate_source_key(s3_key: str, operation_id: str) -> None:
    """Validate a client-supplied s3_key for a source-file upload.

    The key must sit directly under ``sourcefiles/{operation_id}/`` — no
    nesting, no other operation, no arbitrary prefix."""
    prefix = f"sourcefiles/{operation_id}/"
    if not s3_key.startswith(prefix):
        raise ToolError("invalid_key: key does not match the prepared source upload")
    remainder = s3_key[len(prefix) :]
    if not remainder or "/" in remainder:
        raise ToolError("invalid_key: malformed source file name segment")


def _is_html_source_name(name: str) -> bool:
    """True when ``name`` looks like an HTML document (``.html`` / ``.htm``).

    Mirrors Backend-Server ``is_html_source_filename`` (source_html_version.py):
    only HTML sources are renderable in the FE's PDF↔Source toggle, so the
    ``show_source_on_ui`` opt-in is gated on the extension.
    """
    return name.lower().rstrip().endswith((".html", ".htm"))


def _doc_message_metadata(
    *,
    kind: str,
    version: int,
    intent: str,
    s3_key: str,
    message_id: str,
    now: datetime,
    file_name: str | None,
) -> dict[str, Any]:
    """Mirror the Backend-Server bot document-message metadata shape so the
    frontend renders an MCP-created version identically to a UI-created one.

    ``type: "bot"`` is required: the FE version stepper
    (isDocumentVersionMessage) reads the message ``type`` from this metadata
    blob, not the DB ``type`` column, and drops any document row that isn't
    ``type == "bot"``. Without it an MCP-created version is invisible in the UI.
    ``htmlDocumentLastVersion`` mirrors BE (build_final_document_bot_metadata):
    the *previous* version number, i.e. version - 1.

    PDF versions additionally carry ``approved_pdf_s3_key`` — the FE version
    history and Apryse viewer resolve each PDF version from the message
    metadata (use-editorial-version-history.ts), not the DB content column."""
    metadata = {
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
    if kind == "pdf":
        metadata["approved_pdf_s3_key"] = s3_key
    return metadata


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

    ``kind="source"`` prepares a design source-file upload instead of a
    document version: the key targets ``sourcefiles/{operation_id}/`` and the
    commit step records ``operation_metadata.sourcefile_s3_key`` rather than
    inserting version rows. Only edit operations (EDIT_HTML / EDIT_PDF) may
    carry a source file.
    """
    _require_upload_kind(kind)
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None:
            raise ToolError("not_authorized: unknown operation")
        brand_id = op.brand_id
        operation_category = op.operation_category
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    tenant_config = registry.get(tenant_slug)
    bucket = tenant_config.s3_bucket if tenant_config is not None else ""
    if not bucket:
        raise ToolError("not_configured: tenant has no s3_bucket")
    if kind == "source":
        # Fail fast at prepare so the caller does not upload bytes it can
        # never commit. Same rule is re-checked at commit under the lock.
        if operation_category not in _EDIT_CATEGORIES:
            raise ToolError(
                "invalid_state: source files attach to edit operations (EDIT_HTML/EDIT_PDF) only"
            )
        safe_name = _sanitize_file_name(file_name)
        if not safe_name:
            raise ToolError("invalid_arguments: file_name is required for source uploads")
        key = f"sourcefiles/{operation_id}/{safe_name}"
        upload_url = s3.presign_put(bucket, key, presign_expiry, "application/octet-stream")
        return {
            "operation_id": operation_id,
            "type": kind,
            "version_number": None,
            "message_id": None,
            "s3_key": key,
            "upload_url": upload_url,
            "expires_in": presign_expiry,
        }
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
    show_source_on_ui: bool = False,
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

    ``kind="source"`` commits a design source-file upload instead: it sets
    ``operation_metadata.sourcefile_s3_key`` on the operation row and inserts
    no message rows. Restricted to edit operations (EDIT_HTML / EDIT_PDF).

    ``show_source_on_ui`` (source commits only, SOL-1255 parity): when True
    and the source file is HTML, the bound document version's message
    metadata is stamped with ``source_html_s3_key`` + ``show_source_on_ui``
    so the editorial asset view offers the PDF↔Source toggle. Binding mirrors
    Backend-Server ``select_source_html_target_message``: the latest
    final-intent version wins, else the latest version. False never clears a
    prior opt-in (turn it off via the platform UI's source re-upload).

    Category-aware finishing writes (mirroring the Backend-Server upload
    contract) run for edit operations after the version rows are inserted:
    - EDIT_HTML + html: ``is_html_saved=True``.
    - EDIT_PDF + pdf: ``operation_metadata.approved_pdf_s3_key`` always;
      ``status="COMPLETED"`` + ``is_html_saved=True`` only when the derived
      intent is ``final`` (the backend's as_draft path skips the status flip).
    """
    _require_upload_kind(kind)
    if show_source_on_ui and kind != "source":
        raise ToolError(
            "invalid_argument: show_source_on_ui applies to type='source' commits only"
        )
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
    if kind == "source":
        _validate_source_key(s3_key, operation_id)
        if show_source_on_ui and not _is_html_source_name(s3_key):
            # Only HTML sources render in the FE's PDF↔Source toggle; mirror
            # the Backend's extension gate rather than stamping a flag the
            # viewer can never honor.
            raise ToolError(
                "invalid_argument: show_source_on_ui requires an HTML source "
                "file (.html/.htm)"
            )
        size = s3.head(bucket, s3_key)
        if size is None:
            raise ToolError("not_found: object not uploaded - PUT to the upload_url first")
        bound_version_number: int | None = None
        with tenant_session(tenant_slug, session_factory) as session:
            locked = session.scalar(
                select(CgOperation).where(
                    CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
                ).with_for_update()
            )
            if locked is None:
                raise ToolError("not_authorized: unknown operation")
            if locked.operation_category not in _EDIT_CATEGORIES:
                raise ToolError(
                    "invalid_state: source files attach to edit operations (EDIT_HTML/EDIT_PDF) only"
                )
            metadata = dict(locked.operation_metadata) if isinstance(
                locked.operation_metadata, dict
            ) else {}
            metadata["sourcefile_s3_key"] = s3_key
            # Reassign so SQLAlchemy tracks the JSON change (in-place mutation
            # is not tracked). Mirrors update_operation / create_operation.
            locked.operation_metadata = metadata
            locked.updated_at = datetime.now(UTC)
            if show_source_on_ui:
                # Bind the HTML source to a document version so the FE offers
                # the PDF↔Source toggle. Mirrors Backend-Server
                # select_source_html_target_message: published (final) head
                # wins, else the latest version of any intent.
                docs = session.scalars(
                    select(CgOperationMessage).where(
                        CgOperationMessage.operation_id == operation_id,
                        CgOperationMessage.version_number.is_not(None),
                        CgOperationMessage.deleted_at.is_(None),
                    )
                ).all()
                pool = [d for d in docs if d.intent == "final"] or list(docs)
                if not pool:
                    raise ToolError(
                        "invalid_state: no document version to bind the source to - "
                        "commit the pdf/html version first"
                    )
                target = max(pool, key=lambda d: d.version_number or 0)
                message_metadata = dict(target.message_metadata) if isinstance(
                    target.message_metadata, dict
                ) else {}
                message_metadata["source_html_s3_key"] = s3_key
                message_metadata["show_source_on_ui"] = True
                target.message_metadata = message_metadata
                bound_version_number = target.version_number
            session.commit()
        return {
            "operation_id": operation_id,
            "type": kind,
            "s3_key": s3_key,
            "sourcefile_s3_key": s3_key,
            "size": size,
            "show_source_on_ui": show_source_on_ui,
            "bound_version_number": bound_version_number,
            "asset_url": build_asset_url(tenant_slug, operation_id),
        }
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
        if not message_id:
            # PDF keys don't embed a message_id (html keys do), so mint one at
            # commit. Without it the row lands with message_id "" — the FE
            # version stepper keys versions by metadata.id, so empty ids
            # collide across versions (several rows marked "Current",
            # navigation broken), and solstice_approve_operation_version
            # cannot address the row.
            message_id = str(uuid4())
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
                kind=kind, version=next_v, intent=intent, s3_key=s3_key,
                message_id=message_id, now=now, file_name=file_name,
            ),
            created_at=now,
            deleted_at=None,
        )
        session.add(pill)
        session.add(doc)
        # Category-aware finishing writes: complete the Backend-Server upload
        # contract for edit operations so the FE renders them exactly like a
        # UI upload (content_gen_sqlalchemy.py upload flows).
        if locked.operation_category == "EDIT_HTML" and kind == "html":
            # Backend bootstrap sets this at create; MCP sets it when the
            # document actually lands. Flags the op for HTML file-browser
            # queries (is_html_saved filter).
            locked.is_html_saved = True
            locked.updated_at = now
        elif locked.operation_category == "EDIT_PDF" and kind == "pdf":
            # Pointer is always written (backend sets it before the as_draft
            # branch); the status flip is final-intent only, mirroring
            # admin_approve_cg_operation_with_pdf_only's as_draft behavior.
            metadata = dict(locked.operation_metadata) if isinstance(
                locked.operation_metadata, dict
            ) else {}
            metadata["approved_pdf_s3_key"] = s3_key
            locked.operation_metadata = metadata
            if intent == "final":
                locked.status = "COMPLETED"
                locked.is_html_saved = True
            locked.updated_at = now
        session.commit()
    return {
        "operation_id": operation_id,
        "type": kind,
        "version_number": next_v,
        "intent": intent,
        "message_id": message_id,
        "s3_key": s3_key,
        "size": size,
        "asset_url": build_asset_url(tenant_slug, operation_id),
    }


__all__ = [
    "CgOperation",
    "CgOperationMessage",
    "Project",
    "approve_operation_version",
    "build_asset_url",
    "commit_operation_version",
    "create_edit_operation",
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
