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
  ``intent`` ∈ {draft, final}. Timeline and head identity match Backend:
  ``created_at`` then ``id`` (NULLS FIRST). Paired user-pill + document writes
  stamp the document 1µs later so a UUID ``id`` tiebreak cannot invert chat
  order. A document's address is its row ``id`` (PK); the nullable
  ``message_id`` column is not the address. The current version is the one
  flagged ``is_head``. Visible html/pdf rows also carry a computed
  ``display_version`` (1-based, same count as the frontend stepper) so agents
  can say "V29" without parsing S3 keys. The DB ``version_number`` /
  ``position`` columns are dead (the Backend stopped writing them when row
  identity replaced numeric versions); this module leaves them unmapped and
  never reads, writes, or publishes them.
  HTML bodies live in tenant S3 under ``cg_operation_msg_html/...``; the
  ``content`` column holds either inline HTML or that S3 key.
  Baked proofs live under
  ``cg_operation_prc_template/...`` on ``prc_template_s3_key``.
  ``solstice_operation_html`` signs both; callers GET the URLs for the
  bodies.
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
import re
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import unquote_plus
from uuid import UUID, uuid4

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, Uuid, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, undefer

from solstice_mcp.brands import (
    Brand,
    BrandTeamMember,
    UserRole,
    require_brand_role,
    role_satisfies,
)
from solstice_mcp.requests import complete_pending_requests_for_operation
from solstice_mcp.storage import S3Error, S3ObjectMissing, S3ObjectTooLarge, S3Reader
from solstice_mcp.tenants import Base, SessionFactory, TenantRegistry, tenant_session

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500


def _clamp_list_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIST_LIMIT))


def _clamp_list_offset(offset: int) -> int:
    return max(0, int(offset))

# Document rows carry a version; text/blueprint rows never do. Same set as the
# Backend's ``_DOCUMENT_ROW_TYPES`` (message_table_repository).
_DOCUMENT_ROW_TYPES = ("html", "pdf")

_HTML_S3_KEY_PREFIX = "cg_operation_msg_html"
_PRC_TEMPLATE_S3_KEY_PREFIX = "cg_operation_prc_template"
_PRC_CONTENT_TYPES = {"banner", "email", "social"}
_PRC_RESERVED_KEY_PREFIXES = ("brand_", "environment_default_", "platform_default_")
_PRC_TEMPLATE_STATUSES = {"draft", "published"}
_PRC_PUBLISH_TARGETS = {"library", "operation", "both"}

# Mirrors FONT_SHEET_HOSTS in
# Solstice-Frontend/entities/prc-template/model/lock-proof-fonts.ts — both serve
# immutable, CORS-open files, which is what the export Chromium needs. Keep the
# two lists in step: a host the frontend pins but this rejects blocks a publish.
_FONT_SHEET_HOSTS = ("fonts.googleapis.com", "use.typekit.net")

# CSS keywords and the stand-ins the frontend lock writes, none of which are a
# family anyone has to face. Mirrors GENERIC + OS_STAND_IN + STAND_IN there.
_FONT_KEYWORDS = frozenset(
    {
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-sans-serif",
        "ui-serif",
        "ui-monospace",
        "ui-rounded",
        "emoji",
        "math",
        "fangsong",
        "inherit",
        "initial",
        "unset",
        "revert",
        "revert-layer",
        "blinkmacsystemfont",
        "arial",
        "helvetica",
        "helvetica neue",
    }
)

_FONT_DECL_RE = re.compile(r"(?:(?<![-\w])font-family|(?<![-\w])font)\s*:\s*([^;{}]+)", re.IGNORECASE)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{([^}]*)\}", re.IGNORECASE)
_FONT_FACE_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;}]+)", re.IGNORECASE)
_GOOGLE_FAMILY_RE = re.compile(r"[?&]family=([^&\"'\s>]+)", re.IGNORECASE)
_FONT_SIZE_RE = re.compile(
    r"(?:\d+(?:\.\d+)?(?:px|em|rem|pt|%)|xx?-small|x-small|small|medium|large|"
    r"x-large|xx-large|smaller|larger)(?:\s*/\s*[^\s,]+)?\s+(.+)",
    re.IGNORECASE,
)


def _font_family_names(value: str) -> list[str]:
    """Family names from one declaration value, keywords and stand-ins dropped."""
    trimmed = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.IGNORECASE)
    after_size = _FONT_SIZE_RE.search(trimmed)
    stack = after_size.group(1) if after_size else trimmed
    names = []
    for raw in stack.split(","):
        name = raw.strip().strip("\"'").strip()
        if not name or not any(char.isalpha() for char in name):
            continue
        folded = " ".join(name.lower().split())
        if folded in _FONT_KEYWORDS or folded.startswith("sol-prc-"):
            continue
        names.append(folded)
    return names


def _google_sheet_families(text: str) -> set[str]:
    """Families a Google Fonts sheet URL names. Split `|` then drop `:weights`."""
    faced: set[str] = set()
    for raw in _GOOGLE_FAMILY_RE.findall(text):
        decoded = unquote_plus(raw)
        for part in decoded.split("|"):
            family = part.split(":")[0].replace("+", " ").strip()
            if family:
                faced.update(_font_family_names(family))
    return faced


def _prc_bake_unresolved_fonts(html: str) -> list[str]:
    """Families the bake names but never faces, so the proof would substitute.

    Structural only — no network and no rendering. A family counts as faced by a
    url-only ``@font-face`` (``local()`` means an installed file nobody else has)
    or by being named in a Google sheet URL.

    ponytail: a Typekit kit URL is an opaque id that never names its families, so
    any kit present waives the check for families it might carry. Fetch the kit
    CSS here if that waiver ever hides a real substitution.
    """
    # srcdoc creatives arrive escaped inside an attribute; unescape so their CSS
    # reads like the rest of the document.
    text = unescape(html)
    faced: set[str] = set()
    for body in _FONT_FACE_RE.findall(text):
        if "url(" not in body.lower():
            continue
        for match in _FONT_FACE_FAMILY_RE.findall(body):
            faced.update(_font_family_names(match))
    faced.update(_google_sheet_families(text))
    if "use.typekit.net" in text:
        return []

    named: list[str] = []
    seen: set[str] = set()
    for value in _FONT_DECL_RE.findall(_FONT_FACE_RE.sub("", text)):
        for name in _font_family_names(value):
            if name in faced or name in seen:
                continue
            seen.add(name)
            named.append(name)
    return named


def _validate_prc_template_bake_s3_key(key: str, operation_id: str) -> str:
    """Return the row UUID embedded in a prepared PRC bake key."""
    prefix = f"{_PRC_TEMPLATE_S3_KEY_PREFIX}/{operation_id}/"
    if not key.startswith(prefix) or not key.endswith(".html"):
        raise ToolError(
            "invalid_request: operation_bake_s3_key must be "
            f"{_PRC_TEMPLATE_S3_KEY_PREFIX}/{{operation_id}}/{{row_id}}.html from "
            "solstice_prepare_prc_template_bake"
        )
    row_id = key[len(prefix) : -len(".html")]
    if _normalized_uuid(row_id) is None:
        raise ToolError(
            "invalid_request: operation_bake_s3_key row_id must be a UUID"
        )
    return row_id


def _too_large_error(kind: str, size_bytes: int, max_inline_bytes: int) -> ToolError:
    return ToolError(
        f"too_large: {kind} is {size_bytes} bytes; inline limit is {max_inline_bytes}"
    )


def _ensure_inline_size(kind: str, html: str, max_inline_bytes: int) -> None:
    size_bytes = len(html.encode("utf-8"))
    if size_bytes > max_inline_bytes:
        raise _too_large_error(kind, size_bytes, max_inline_bytes)


def _load_uploaded_operation_bake(
    *,
    operation_bake_s3_key: str,
    operation_id: str,
    bucket: str,
    s3: S3Reader,
    max_inline_bytes: int,
) -> tuple[str, str]:
    """Load a presigned-upload proof edit; composition validates the result."""
    row_id = _validate_prc_template_bake_s3_key(operation_bake_s3_key, operation_id)
    size = s3.head(bucket, operation_bake_s3_key)
    if size is None:
        raise ToolError(
            "not_found: object not uploaded - PUT the bake HTML to upload_url first"
        )
    if size > max_inline_bytes:
        raise _too_large_error("operation bake html", size, max_inline_bytes)
    try:
        # ponytail: loads bake into MCP memory; stream-parse if process OOM
        html = s3.download(bucket, operation_bake_s3_key, max_inline_bytes).decode("utf-8")
    except S3ObjectMissing:
        raise ToolError(
            "not_found: object not uploaded - PUT the bake HTML to upload_url first"
        ) from None
    except S3ObjectTooLarge:
        raise _too_large_error("operation bake html", size, max_inline_bytes) from None
    except S3Error as exc:
        raise ToolError(f"not_available: s3 read failed: {exc}") from exc
    return row_id, html


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
    # writes, approve_operation_version) still lazy-load it on attribute
    # access inside their session.
    operation_metadata: Mapped[Any | None] = mapped_column(JSON, nullable=True, deferred=True)
    version_number: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CgOperationMessage(Base):
    """Read-only mapping of the tenant ``n_cg_operation_messages`` table.

    The DB column ``metadata`` is mapped to the Python attribute
    ``message_metadata`` (``metadata`` is reserved by SQLAlchemy's Base); we do
    load it deferred by default because it can be large. Message timeline reads
    undefer it only to resolve legacy ``metadata.versionIntent``.

    The DB still has ``version_number`` and ``position`` columns, deliberately
    left unmapped: the Backend dropped both from its own model and its INSERT
    when row identity replaced numeric versions, so they are NULL on ~85-90% of
    live rows. Mapping them would only let a reader sort on a stale number.
    Order by ``created_at`` then ``id``; see ``_summarize_message_timeline`` for
    the derived display label.
    """

    __tablename__ = "n_cg_operation_messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    operation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    type: Mapped[str] = mapped_column(String)
    content: Mapped[str | None] = mapped_column(String, nullable=True)
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    # The DB column ``metadata`` (jsonb in prod) is mapped to ``message_metadata``
    # and loaded deferred by default. Timeline reads explicitly undefer it for
    # the legacy versionIntent fallback; write paths also access it in-session.
    message_metadata: Mapped[Any | None] = mapped_column(
        "metadata", JSON, nullable=True, deferred=True
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Server-owned S3 key for the baked proof HTML (not the creative body).
    prc_template_s3_key: Mapped[str | None] = mapped_column(String, nullable=True)


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


def _resolved_message_intent(msg: CgOperationMessage) -> str | None:
    """Resolve the authoritative intent with the Backend's legacy fallback."""
    if msg.intent in {"draft", "final"}:
        return msg.intent
    if msg.intent is not None:
        return None
    metadata = msg.message_metadata
    if not isinstance(metadata, dict):
        return None
    fallback = metadata.get("versionIntent")
    return fallback if fallback in {"draft", "final"} else None


def _final_document_visibility_clause():
    """Backend-equivalent SQL predicate for a final html/pdf document."""
    return or_(
        CgOperationMessage.intent == "final",
        and_(
            CgOperationMessage.intent.is_(None),
            CgOperationMessage.message_metadata["versionIntent"].as_string() == "final",
        ),
    )


def _message_summary(
    msg: CgOperationMessage,
    *,
    is_head: bool = False,
    display_version: int | None = None,
) -> dict[str, Any]:
    """Project one message row to a read summary.

    For ``text``: return the content inline (chat is the agent-readable payload).
    For ``html`` / ``pdf``: return the S3 key when ``content`` is one, else an
    ``inline`` flag; never return the body (deferred to a future tool). Document
    rows also carry ``is_head`` (current version) and ``display_version`` (1-based
    index among visible html/pdf rows — the same V the frontend stepper shows).
    For ``blueprint``: return existence only (the JSON payload is large).

    ``display_version`` is computed over the already-filtered list; it is not
    the dead DB ``version_number`` column and must not be parsed from S3 keys.
    """
    base = {
        "id": msg.id,
        "message_id": msg.message_id,
        "type": msg.type,
        "intent": _resolved_message_intent(msg),
        "author_id": msg.author_id,
        "created_at": _iso(msg.created_at),
    }
    if msg.type == "text":
        return {**base, "content": msg.content}
    if msg.type in _DOCUMENT_ROW_TYPES:
        extra = {}
        if msg.prc_template_s3_key:
            extra["prc_template_s3_key"] = msg.prc_template_s3_key
        document: dict[str, Any] = {"is_head": is_head}
        if display_version is not None:
            document["display_version"] = display_version
        if _looks_like_s3_key(msg.content):
            return {**base, **document, "s3_key": msg.content, "body": None, **extra}
        return {**base, **document, "s3_key": None, "inline": True, "body": None, **extra}
    if msg.type == "blueprint":
        return {**base, "has_blueprint": True, "body": None}
    return base


def _summarize_message_timeline(
    rows: Sequence[CgOperationMessage],
) -> list[dict[str, Any]]:
    """Project an ordered timeline and mark the head document row.

    ``rows`` must already be in Backend timeline order
    (``_MESSAGE_ORDER_OLDEST_FIRST``) and already filtered to what this caller may
    see, so the head is the newest document the same user sees in the UI. Only
    ``html`` / ``pdf`` rows can be the head; ``text`` / ``blueprint`` never are.
    """
    document_indexes = [i for i, msg in enumerate(rows) if msg.type in _DOCUMENT_ROW_TYPES]
    head_index = document_indexes[-1] if document_indexes else None
    display_by_index = {idx: n for n, idx in enumerate(document_indexes, start=1)}
    return [
        _message_summary(
            msg,
            is_head=i == head_index,
            display_version=display_by_index.get(i),
        )
        for i, msg in enumerate(rows)
    ]


def _normalized_uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _prc_template_configs(metadata: Any, content_type: str) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    configs: list[dict[str, Any]] = []
    prc_templates = metadata.get("prc_templates")
    if isinstance(prc_templates, dict):
        config = prc_templates.get(content_type)
        if isinstance(config, dict):
            configs.append(config)
    if content_type == "email":
        email_settings = metadata.get("email_settings")
        if isinstance(email_settings, dict):
            legacy = email_settings.get("interactive_prc_template")
            if isinstance(legacy, dict):
                configs.append(legacy)
    return configs


def _pinned_template_id(metadata: Any, content_type: str) -> str | None:
    for config in _prc_template_configs(metadata, content_type):
        pinned = _normalized_uuid(config.get("template_version_id"))
        if pinned:
            return pinned
    return None


def _prc_explicitly_disabled(metadata: Any, content_type: str) -> bool:
    """Read behavior keeps an explicitly pinned selection inspectable."""
    for config in _prc_template_configs(metadata, content_type):
        pinned = _normalized_uuid(config.get("template_version_id"))
        if config.get("enabled") is False and pinned is None:
            return True
    return False


def _prc_writer_explicitly_disabled(metadata: Any, content_type: str) -> bool:
    """Writer behavior treats every explicit enabled=false as keyless."""
    return any(
        config.get("enabled") is False
        for config in _prc_template_configs(metadata, content_type)
    )


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
    prefix = "brand_"
    suffix = f"_{content_type}"
    templates = session.scalars(
        select(PrcTemplateVersion)
        .where(
            PrcTemplateVersion.template_key.startswith(prefix),
            PrcTemplateVersion.template_key.endswith(suffix),
            PrcTemplateVersion.content_type == content_type,
            PrcTemplateVersion.status == "published",
            PrcTemplateVersion.deleted_at.is_(None),
        )
        .order_by(PrcTemplateVersion.version_number.desc())
    )
    latest_by_slug: dict[str, PrcTemplateVersion] = {}
    for template in templates:
        slug = template.template_key[len(prefix) : -len(suffix)]
        if slug:
            latest_by_slug.setdefault(slug, template)

    lowered = brand_name.lower()
    for slug in sorted(latest_by_slug, key=len, reverse=True):
        if slug in lowered:
            return latest_by_slug[slug]
    return None


def _resolved_prc_template_for_operation(
    session,
    operation: CgOperation,
) -> tuple[bool, str, str | None]:
    content_type = (operation.content_type or "").strip().lower()
    if content_type not in _PRC_CONTENT_TYPES:
        return False, content_type, None
    brand = session.scalar(select(Brand).where(Brand.id == operation.brand_id, Brand.deleted_at.is_(None)))
    if brand is None:
        raise ToolError("not_authorized: unknown brand")
    metadata = brand.brand_metadata if isinstance(brand.brand_metadata, dict) else {}
    if _prc_writer_explicitly_disabled(metadata, content_type):
        return False, content_type, None

    operation_metadata = operation.operation_metadata if isinstance(operation.operation_metadata, dict) else {}
    template = _template_by_id(
        session,
        _normalized_uuid(operation_metadata.get("prc_template_version_id")),
    )
    if template is None or template.content_type != content_type:
        template = _template_by_id(session, _pinned_template_id(metadata, content_type))
    if template is None or template.content_type != content_type:
        template = _brand_template(session, brand.name, content_type)
    if template is None:
        template = _latest_published_template(session, f"environment_default_{content_type}", content_type)
    if template is None:
        template = _latest_published_template(session, f"platform_default_{content_type}", content_type)
    return True, content_type, template.html_template if template is not None else None


def _download_html(
    *,
    bucket: str,
    key: str,
    s3: S3Reader,
    missing_error: str,
    max_inline_bytes: int,
) -> str:
    size = s3.head(bucket, key)
    if size is None:
        raise ToolError(missing_error)
    if size > max_inline_bytes:
        raise _too_large_error("html object", size, max_inline_bytes)
    try:
        return s3.download(bucket, key, max_inline_bytes).decode("utf-8")
    except (S3ObjectMissing, UnicodeDecodeError):
        raise ToolError(missing_error) from None
    except S3ObjectTooLarge:
        raise _too_large_error("html object", size, max_inline_bytes) from None
    except S3Error as exc:
        raise ToolError(f"not_available: s3 read failed: {exc}") from exc


def _compose_supplied_prc_proof(
    supplied_proof: str,
    creative: str,
    content_type: str,
) -> str:
    from solstice_mcp.prc_proof_composer import InvalidPrcProofError, compose_prc_proof

    try:
        return compose_prc_proof(supplied_proof, creative, content_type)
    except InvalidPrcProofError as exc:
        raise ToolError(f"invalid_request: {exc}") from exc


def _raise_if_unresolved_prc_fonts(proof: str) -> None:
    unresolved = _prc_bake_unresolved_fonts(proof)
    if unresolved:
        raise ToolError(
            "invalid_request: operation_bake_html names fonts it never faces, so the "
            f"proof would render a substitute: {', '.join(sorted(unresolved))}. Declare an "
            "@font-face with a woff2 URL for each, or switch to a family served by "
            f"{' or '.join(_FONT_SHEET_HOSTS)}"
        )


def _finalize_html_prc_transition(
    *,
    session,
    operation: CgOperation,
    row_id: str,
    creative_key: str,
    bucket: str,
    s3: S3Reader,
    staff: bool,
    supplied_proof: str | None = None,
    precomposed_proof: str | None = None,
    max_inline_bytes: int,
) -> tuple[str | None, int]:
    enabled, content_type, resolved_template = _resolved_prc_template_for_operation(session, operation)
    if not enabled:
        return None, 0

    from solstice_mcp.prc_proof_composer import InvalidPrcProofError, compose_prc_proof

    creative = _download_html(
        bucket=bucket,
        key=creative_key,
        s3=s3,
        missing_error="not_found: current html object missing in s3",
        max_inline_bytes=max_inline_bytes,
    )
    proof: str | None = precomposed_proof
    if proof is not None:
        _ensure_inline_size("operation bake html", proof, max_inline_bytes)
    if proof is None and supplied_proof is not None:
        _ensure_inline_size("operation bake html", supplied_proof, max_inline_bytes)
        proof = _compose_supplied_prc_proof(supplied_proof, creative, content_type)
    if proof is None:
        prior_filters = [
            CgOperationMessage.operation_id == operation.id,
            CgOperationMessage.type == "html",
            CgOperationMessage.deleted_at.is_(None),
            CgOperationMessage.prc_template_s3_key.is_not(None),
        ]
        if not staff:
            prior_filters.append(_final_document_visibility_clause())
        prior_rows = session.scalars(
            select(CgOperationMessage).where(*prior_filters).order_by(*_MESSAGE_ORDER_NEWEST_FIRST)
        ).all()
        prefix = f"{_PRC_TEMPLATE_S3_KEY_PREFIX}/{operation.id}/"
        for prior in prior_rows:
            key = prior.prc_template_s3_key
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            try:
                size = s3.head(bucket, key)
            except S3Error as exc:
                raise ToolError(f"not_available: s3 read failed: {exc}") from exc
            if size is None:
                continue
            if size > max_inline_bytes:
                raise _too_large_error("prior PRC proof", size, max_inline_bytes)
            try:
                base = s3.download(bucket, key, max_inline_bytes).decode("utf-8")
                proof = compose_prc_proof(base, creative, content_type)
                break
            except S3ObjectMissing:
                continue
            except S3ObjectTooLarge:
                raise _too_large_error("prior PRC proof", size, max_inline_bytes) from None
            except S3Error as exc:
                raise ToolError(f"not_available: s3 read failed: {exc}") from exc
            except (UnicodeDecodeError, InvalidPrcProofError):
                continue
        if proof is None and isinstance(resolved_template, str) and resolved_template.strip():
            _ensure_inline_size("resolved PRC template", resolved_template, max_inline_bytes)
            try:
                proof = compose_prc_proof(resolved_template, creative, content_type)
            except InvalidPrcProofError as exc:
                raise ToolError(f"invalid_state: PRC template composition failed: {exc}") from exc
    if proof is None:
        raise ToolError(f"invalid_state: no PRC template resolved for {content_type}")
    _raise_if_unresolved_prc_fonts(proof)
    bake_key = f"{_PRC_TEMPLATE_S3_KEY_PREFIX}/{operation.id}/{row_id}.html"
    try:
        s3.put(bucket, bake_key, proof.encode("utf-8"), "text/html")
    except S3Error as exc:
        raise ToolError(f"not_available: s3 write failed: {exc}") from exc
    return bake_key, len(proof.encode("utf-8"))


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
        matched_operation = None
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
            matched_operation = operation
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
            if matched_operation is not None and parsed_operation_id is not None:
                bake = _latest_operation_bake(session, parsed_operation_id)
                if bake:
                    return {
                        "id": None,
                        "template_key": None,
                        "version_number": None,
                        "content_type": normalized_content_type,
                        "name": None,
                        "resolved_tier": "operation_bake",
                        "operation_bake": bake,
                        "publish_targets": ["operation", "library", "both"],
                        "html_template": None,
                    }
            return None
        payload = _prc_template_payload(
            template,
            tier=tier,
            fetch=fetch,
            max_inline_bytes=max_inline_bytes,
        )
        if matched_operation is not None and parsed_operation_id is not None:
            payload["operation_bake"] = _latest_operation_bake(session, parsed_operation_id)
            payload["publish_targets"] = ["operation", "library", "both"]
        return payload


# Backend message_table_repository: created_at ASC NULLS FIRST, id ASC.
# `IS NULL DESC` is the portable NULLS FIRST; head is the reverse of this.
_MESSAGE_ORDER_OLDEST_FIRST = (
    CgOperationMessage.created_at.is_(None).desc(),
    CgOperationMessage.created_at.asc(),
    CgOperationMessage.id.asc(),
)
_MESSAGE_ORDER_NEWEST_FIRST = (
    CgOperationMessage.created_at.is_(None),
    CgOperationMessage.created_at.desc(),
    CgOperationMessage.id.desc(),
)


def _latest_message(session, *where: Any) -> CgOperationMessage | None:
    """Head live row: last in Backend ``(created_at NULLS FIRST, id)`` order."""
    return session.scalar(
        select(CgOperationMessage).where(*where).order_by(*_MESSAGE_ORDER_NEWEST_FIRST).limit(1)
    )


def _latest_operation_bake(session, operation_id: str) -> dict[str, Any] | None:
    """Latest html row that already carries a baked proof key."""
    latest = _latest_message(
        session,
        CgOperationMessage.operation_id == operation_id,
        CgOperationMessage.type == "html",
        CgOperationMessage.deleted_at.is_(None),
        CgOperationMessage.prc_template_s3_key.is_not(None),
    )
    if latest is None:
        return None
    return {
        "message_id": latest.message_id,
        "row_id": latest.id,
        "prc_template_s3_key": latest.prc_template_s3_key,
        "created_at": _iso(latest.created_at),
    }


def _latest_html_creative(session, operation_id: str) -> CgOperationMessage | None:
    """Head html document: newest ``created_at``, then ``id``."""
    return _latest_message(
        session,
        CgOperationMessage.operation_id == operation_id,
        CgOperationMessage.type == "html",
        CgOperationMessage.deleted_at.is_(None),
    )


def _head_document(session, operation_id: str) -> CgOperationMessage | None:
    """The operation's current document version, ignoring who may see it."""
    return _latest_message(
        session,
        CgOperationMessage.operation_id == operation_id,
        CgOperationMessage.type.in_(_DOCUMENT_ROW_TYPES),
        CgOperationMessage.deleted_at.is_(None),
    )


def _visible_head_document(
    session, operation_id: str, *, staff: bool
) -> CgOperationMessage | None:
    """The current document version as THIS caller sees it.

    Mirrors the read filter in ``list_operation_messages``: staff see every
    document row, MEMBER / ADMIN see only resolved-final rows. When the two
    heads differ, the newest content is invisible to the caller.
    """
    if staff:
        return _head_document(session, operation_id)
    return _latest_message(
        session,
        CgOperationMessage.operation_id == operation_id,
        CgOperationMessage.type.in_(_DOCUMENT_ROW_TYPES),
        CgOperationMessage.deleted_at.is_(None),
        _final_document_visibility_clause(),
    )


def _identifies(row: CgOperationMessage, candidate: str) -> bool:
    """True when ``candidate`` names this row.

    Accepts the row ``id`` (what reads publish as ``head_message_id``) or the
    nullable ``message_id`` column so leftover callers still resolve. Pure
    Python comparison — no SQL — so an arbitrary candidate string is safe here.
    """
    if candidate in {row.message_id, row.id}:
        return True
    normalized = _normalized_uuid(candidate)
    return normalized is not None and normalized in {
        _normalized_uuid(row.id),
        _normalized_uuid(row.message_id),
    }


def _find_message(
    session, operation_id: str, candidate: str, *, lock: bool = False
) -> CgOperationMessage | None:
    """Resolve one live message on this operation by row id, else ``message_id``.

    Reads publish the row ``id`` as ``head_message_id``. The ``message_id``
    column is nullable, so lookups still accept either form.

    The row-id branch runs only when the candidate parses as a UUID. ``id`` is a
    uuid column, so comparing a non-UUID string against it is a Postgres cast
    error rather than a miss (SQLite would silently tolerate it, which is how
    such a bug reaches production green).
    """
    row_id = _normalized_uuid(candidate)
    identifiers: list[Any] = [CgOperationMessage.message_id == candidate]
    if row_id is not None:
        identifiers.extend(
            [
                CgOperationMessage.id == row_id,
                func.lower(CgOperationMessage.message_id) == row_id,
            ]
        )
    stmt = select(CgOperationMessage).where(
        CgOperationMessage.operation_id == operation_id,
        CgOperationMessage.deleted_at.is_(None),
        or_(*identifiers),
    )
    rows = session.scalars(stmt.with_for_update() if lock else stmt).all()
    if len(rows) > 1:
        raise ToolError("invalid_request: ambiguous message identifier")
    return rows[0] if rows else None


def prepare_prc_template_bake(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    operation_id: str,
    content_type: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int = 600,
) -> dict[str, Any]:
    """Issue a presigned PUT URL for a Contract v2 operation PRC bake.

    Two-step write (step 1 of 2): upload the bake HTML directly to
    ``upload_url``, then call ``solstice_create_prc_template_version`` with
    ``publish_target="operation"`` (or ``"both"``) and the returned
    ``prc_template_s3_key`` as ``operation_bake_s3_key``. Requires
    SOLSTICE_STAFF on the operation's brand.
    """
    parsed_operation_id = _normalized_uuid(operation_id)
    if parsed_operation_id is None:
        raise ToolError("invalid_request: operation_id must be a UUID")
    normalized_content_type = content_type.strip().lower()
    if normalized_content_type not in _PRC_CONTENT_TYPES:
        allowed = ", ".join(sorted(_PRC_CONTENT_TYPES))
        raise ToolError(f"invalid_request: content_type must be one of {allowed}")
    with tenant_session(tenant_slug, session_factory) as session:
        op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == parsed_operation_id, CgOperation.deleted_at.is_(None)
            )
        )
        if op is None or op.brand_id != brand_id:
            raise ToolError("not_authorized: unknown operation")
        if (op.content_type or "").lower() != normalized_content_type:
            raise ToolError(
                "invalid_request: operation content_type does not match the proof template"
            )
    require_brand_role(
        subject,
        tenant_slug,
        brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry,
        session_factory=session_factory,
    )
    tenant_config = registry.get(tenant_slug)
    bucket = tenant_config.s3_bucket if tenant_config is not None else ""
    if not bucket:
        raise ToolError("not_configured: tenant has no s3_bucket")
    row_id = str(uuid4())
    bake_key = f"{_PRC_TEMPLATE_S3_KEY_PREFIX}/{parsed_operation_id}/{row_id}.html"
    upload_url = s3.presign_put(bucket, bake_key, presign_expiry, "text/html")
    return {
        "operation_id": parsed_operation_id,
        "prc_template_s3_key": bake_key,
        "upload_url": upload_url,
        "expires_in": presign_expiry,
    }


def bake_prc_template_to_operation(
    *,
    identity,
    tenant_slug: str,
    brand_id: str,
    operation_id: str,
    content_type: str,
    operation_bake_s3_key: str,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    max_inline_bytes: int = 2_000_000,
) -> dict[str, Any]:
    """Append a draft version that copies the creative and stores an operation bake.

    Mirrors Backend-Server ``finalize_html_version_message``: creative stays on
    ``cg_operation_msg_html/...``; the proof lands at
    ``cg_operation_prc_template/{operation_id}/{row_id}.html`` and is stamped
    on ``n_cg_operation_messages.prc_template_s3_key``. Head creative is
    newest ``created_at`` then ``id``. The stamped version is the document-row
    count + 1 — a display label for the S3 key (same as
    ``commit_operation_version``), not identity. The bake must already be PUT to
    ``operation_bake_s3_key``.
    """
    parsed_operation_id = _normalized_uuid(operation_id)
    if parsed_operation_id is None:
        raise ToolError("invalid_request: operation_id must be a UUID")
    tenant_config = registry.get(tenant_slug)
    bucket = tenant_config.s3_bucket if tenant_config is not None else ""
    if not bucket:
        raise ToolError("not_configured: tenant has no s3_bucket")
    row_id, supplied_proof = _load_uploaded_operation_bake(
        operation_bake_s3_key=operation_bake_s3_key,
        operation_id=parsed_operation_id,
        bucket=bucket,
        s3=s3,
        max_inline_bytes=max_inline_bytes,
    )

    with tenant_session(tenant_slug, session_factory) as session:
        locked = session.scalar(
            select(CgOperation).where(
                CgOperation.id == parsed_operation_id, CgOperation.deleted_at.is_(None)
            ).with_for_update()
        )
        if locked is None or locked.brand_id != brand_id:
            raise ToolError("not_authorized: unknown operation")
        if (locked.content_type or "").lower() != content_type:
            raise ToolError(
                "invalid_request: operation content_type does not match the proof template"
            )
        head = _latest_html_creative(session, parsed_operation_id)
        if head is None:
            raise ToolError(
                "invalid_state: operation has no html document to attach a proof bake to"
            )
        head_content = head.content or ""
        if _looks_like_s3_key(head_content):
            creative_html = _download_html(
                bucket=bucket,
                key=head_content,
                s3=s3,
                missing_error="not_found: current html object missing in s3",
                max_inline_bytes=max_inline_bytes,
            )
        else:
            creative_html = head_content
            if not creative_html.encode("utf-8").strip():
                raise ToolError("invalid_state: current html document is empty")
        supplied_proof = _compose_supplied_prc_proof(
            supplied_proof, creative_html, content_type
        )
        _raise_if_unresolved_prc_fonts(supplied_proof)
        message_id = str(uuid4())
        creative_key = _version_s3_key("html", parsed_operation_id, message_id, locked.file_name)
        if _looks_like_s3_key(head_content):
            try:
                s3.copy_object(bucket, head_content, creative_key, "text/html")
            except S3ObjectMissing:
                raise ToolError("not_found: current html object missing in s3") from None
            except S3Error as exc:
                raise ToolError(f"not_available: s3 write failed: {exc}") from exc
        else:
            creative = creative_html.encode("utf-8")
            if not creative.strip():
                raise ToolError("invalid_state: current html document is empty")
            try:
                s3.put(bucket, creative_key, creative, "text/html")
            except S3Error as exc:
                raise ToolError(f"not_available: s3 write failed: {exc}") from exc
        finalized_bake_key, html_size_bytes = _finalize_html_prc_transition(
            session=session,
            operation=locked,
            row_id=row_id,
            creative_key=creative_key,
            bucket=bucket,
            s3=s3,
            staff=True,
            precomposed_proof=supplied_proof,
            max_inline_bytes=max_inline_bytes,
        )
        if finalized_bake_key is None:
            raise ToolError("invalid_state: PRC is explicitly disabled for this operation")
        now = datetime.now(UTC)
        # Backend sorts (created_at, id); 1µs gap so UUID tiebreak cannot invert the pair.
        doc_at = now + timedelta(microseconds=1)
        intent = "draft"
        message_metadata = _doc_message_metadata(
            kind="html",
            intent=intent,
            s3_key=creative_key,
            message_id=message_id,
            now=doc_at,
            file_name=locked.file_name,
        )
        message_metadata.update(_html_snapshot_metadata(head))
        session.add(
            CgOperationMessage(
                id=str(uuid4()),
                operation_id=parsed_operation_id,
                message_id=str(uuid4()),
                author_id=identity.user_id,
                type="text",
                content="PRC template update",
                intent=None,
                message_metadata={
                    "id": str(uuid4()),
                    "timestamp": now.isoformat(),
                    "type": "user",
                    "finalContent": "PRC template update",
                    "kind": "user_feedback",
                },
                created_at=now,
                deleted_at=None,
            )
        )
        session.add(
            CgOperationMessage(
                id=row_id,
                operation_id=parsed_operation_id,
                message_id=message_id,
                author_id=None,
                type="html",
                content=creative_key,
                intent=intent,
                prc_template_s3_key=finalized_bake_key,
                message_metadata=message_metadata,
                created_at=doc_at,
                deleted_at=None,
            )
        )
        locked.updated_at = now
        session.commit()
    return {
        "operation_id": parsed_operation_id,
        "intent": intent,
        "message_id": message_id,
        "s3_key": creative_key,
        "prc_template_s3_key": finalized_bake_key,
        "html_size_bytes": html_size_bytes,
        "asset_url": build_asset_url(tenant_slug, parsed_operation_id),
    }


def create_prc_template_version(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    template_key: str,
    content_type: str,
    name: str,
    confirmed: bool,
    html_template: str | None = None,
    operation_bake_html: str | None = None,
    operation_bake_s3_key: str | None = None,
    description: str | None = None,
    config_schema: dict[str, Any] | None = None,
    default_field_values: dict[str, Any] | None = None,
    status: str = "published",
    publish_target: str = "library",
    operation_id: str | None = None,
    *,
    max_inline_bytes: int,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader | None = None,
) -> dict[str, Any]:
    """Append a catalog template and/or a distinct composed bake onto an operation."""
    identity = require_brand_role(
        subject,
        tenant_slug,
        brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry,
        session_factory=session_factory,
    )
    normalized_target = publish_target.strip().lower() or "library"
    if normalized_target not in _PRC_PUBLISH_TARGETS:
        allowed = ", ".join(sorted(_PRC_PUBLISH_TARGETS))
        raise ToolError(f"invalid_request: publish_target must be one of {allowed}")
    if not confirmed:
        raise ToolError(
            "confirmation_required: ask whether to bake the proof onto the "
            "associated operation, publish to the library, or both; then show "
            "the template key, content type, name, and HTML preview before "
            "retrying with confirmed=true"
        )

    normalized_key = template_key.strip()
    normalized_content_type = content_type.strip().lower()
    normalized_name = name.strip()
    normalized_status = status.strip().lower()
    wants_library = normalized_target in {"library", "both"}
    wants_operation = normalized_target in {"operation", "both"}
    if wants_library:
        if not normalized_key:
            raise ToolError("invalid_request: template_key is required")
        if len(normalized_key) > 255:
            raise ToolError("invalid_request: template_key must be at most 255 characters")
        if normalized_key.lower().startswith(_PRC_RESERVED_KEY_PREFIXES):
            raise ToolError(
                "invalid_request: template_key uses a reserved auto-resolving prefix; "
                "use a custom key and select the version in Template Settings"
            )
        if not normalized_name:
            raise ToolError("invalid_request: name is required")
        if len(normalized_name) > 255:
            raise ToolError("invalid_request: name must be at most 255 characters")
    if normalized_content_type not in _PRC_CONTENT_TYPES:
        allowed = ", ".join(sorted(_PRC_CONTENT_TYPES))
        raise ToolError(f"invalid_request: content_type must be one of {allowed}")
    if normalized_status not in _PRC_TEMPLATE_STATUSES:
        allowed = ", ".join(sorted(_PRC_TEMPLATE_STATUSES))
        raise ToolError(f"invalid_request: status must be one of {allowed}")
    catalog_html = html_template or ""
    if wants_library:
        if not catalog_html.strip():
            raise ToolError("invalid_request: html_template is required for library publishing")
        html_size_bytes = len(catalog_html.encode("utf-8"))
        if html_size_bytes > max_inline_bytes:
            raise ToolError(
                f"too_large: PRC template is {html_size_bytes} bytes; inline limit is {max_inline_bytes}"
            )
    else:
        html_size_bytes = 0
    if wants_operation and not operation_id:
        raise ToolError(
            "invalid_request: operation_id is required when publish_target is "
            "operation or both"
        )
    if wants_operation and operation_bake_html:
        raise ToolError(
            "invalid_request: operation bakes must use solstice_prepare_prc_template_bake, "
            "PUT to upload_url, then operation_bake_s3_key"
        )
    if wants_operation and not operation_bake_s3_key:
        raise ToolError(
            "invalid_request: operation_bake_s3_key is required when publish_target is "
            "operation or both"
        )

    # ponytail: bake before library. A later library conflict still leaves the
    # bake; one txn across S3 + two tables is the upgrade if retries double-publish.
    operation_bake = None
    if wants_operation:
        if s3 is None:
            raise ToolError("not_configured: s3 is required to bake a proof onto an operation")
        operation_bake = bake_prc_template_to_operation(
            identity=identity,
            tenant_slug=tenant_slug,
            brand_id=brand_id,
            operation_id=operation_id or "",
            content_type=normalized_content_type,
            operation_bake_s3_key=operation_bake_s3_key or "",
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            max_inline_bytes=max_inline_bytes,
        )

    library: dict[str, Any] | None = None
    now = datetime.now(UTC)
    if wants_library:
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
                html_template=catalog_html,
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
        library = {
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

    if library is not None:
        return {
            **library,
            "publish_target": normalized_target,
            "operation_bake": operation_bake,
        }
    return {
        "publish_target": normalized_target,
        "operation_bake": operation_bake,
        "html_size_bytes": operation_bake["html_size_bytes"] if operation_bake else 0,
        "brand_selection_updated": False,
        **(operation_bake or {}),
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
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """List non-deleted projects for a brand. Gated at MEMBER.

    Bounded page: default ``limit`` 100, hard max 500. ``has_more`` is true when
    at least one more row exists after this page.
    """
    limit = _clamp_list_limit(limit)
    offset = _clamp_list_offset(offset)
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        rows = session.scalars(
            select(Project).where(
                Project.brand_id == brand_id, Project.deleted_at.is_(None)
            ).order_by(Project.name, Project.id).offset(offset).limit(limit + 1)
        ).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    projects = [_project_summary(p) for p in page]
    return {
        "projects": projects,
        "count": len(projects),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


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
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """List non-deleted operations for a brand. Gated at MEMBER.

    Bounded page: default ``limit`` 100, hard max 500. ``has_more`` is true when
    at least one more row exists after this page.
    """
    limit = _clamp_list_limit(limit)
    offset = _clamp_list_offset(offset)
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.MEMBER,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        rows = session.scalars(
            select(CgOperation).where(
                CgOperation.brand_id == brand_id, CgOperation.deleted_at.is_(None)
            ).order_by(CgOperation.created_at, CgOperation.id).offset(offset).limit(limit + 1)
        ).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    operations = [_operation_summary(op) for op in page]
    return {
        "operations": operations,
        "count": len(operations),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


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

    Oldest first, in Backend timeline order (``created_at`` then ``id``). The
    document row flagged ``is_head`` is the current version; its
    ``display_version`` is the V label the same user sees in the UI. Callers
    must not order or renumber document rows themselves.

    Intent visibility is enforced server-side from the subject's brand role:
    SOLSTICE_STAFF sees draft + final; MEMBER / ADMIN see final only (drafts
    excluded). The role is derived from the JWT subject — there is no
    ``intent`` or ``role`` argument the caller can use to bypass this. Because
    the numbering and the head are computed over the rows this caller may see, a
    member's head can be an older row than a staff caller's.
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
        stmt = (
            select(CgOperationMessage)
            .options(undefer(CgOperationMessage.message_metadata))
            .where(
                CgOperationMessage.operation_id == operation_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        if not staff:
            # Text/blueprint stay visible. Documents must resolve to final using
            # the same column-then-metadata fallback as Backend/FE.
            stmt = stmt.where(
                or_(
                    CgOperationMessage.type.not_in(_DOCUMENT_ROW_TYPES),
                    and_(
                        CgOperationMessage.type.in_(_DOCUMENT_ROW_TYPES),
                        _final_document_visibility_clause(),
                    ),
                )
            )
        stmt = stmt.order_by(*_MESSAGE_ORDER_OLDEST_FIRST)
        rows = session.scalars(stmt).all()
    return _summarize_message_timeline(rows)


def get_operation_html(
    subject: str,
    tenant_slug: str,
    operation_id: str,
    message_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int = 600,
) -> dict[str, Any]:
    """Return presigned GET URLs for the HTML creative and baked PRC proof.

    Mirrors Backend ``content-url``: the document key and ``prc_template_s3_key``
    are signed from the same row. Callers that need the body GET ``url`` /
    ``prc_proof_url``. Bodies are never inlined.

    Authorization + intent filter:
    - Gated at MEMBER on the operation's brand (resolved from the row).
    - The intent filter is re-applied here: a non-staff caller cannot retrieve
      a ``draft`` document message at all — a presigned URL is a read
      capability. Only SOLSTICE_STAFF sees drafts; MEMBER/ADMIN see final only.
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
        msg = _find_message(session, operation_id, message_id)
        if msg is None:
            raise ToolError("not_found: unknown message")
        msg_type = msg.type
        msg_intent = _resolved_message_intent(msg)
        msg_content = msg.content
        bake_key = msg.prc_template_s3_key
    if msg_type in _DOCUMENT_ROW_TYPES and msg_intent != "final" and not staff:
        # Draft visibility is enforced for both the URL and the body: a
        # presigned URL is a read capability, so it must not be handed to a
        # non-staff caller any more than the inline body would be.
        raise ToolError("not_authorized: draft messages require SOLSTICE_STAFF")
    if msg_type != "html":
        raise ToolError("not_found: message is not an html document")

    result: dict[str, Any] = {
        "operation_id": operation_id,
        "id": msg.id,
        "message_id": msg.message_id,
        "type": msg_type,
        "intent": msg_intent,
        "url": None,
        "s3_key": None,
        "prc_proof_url": None,
        "prc_proof_s3_key": None,
    }

    needs_s3 = _looks_like_s3_key(msg_content) or bool(bake_key)
    bucket = ""
    if needs_s3:
        tenant_config = registry.get(tenant_slug)
        bucket = tenant_config.s3_bucket if tenant_config is not None else ""
        if not bucket:
            raise ToolError("not_configured: tenant has no s3_bucket")

    if _looks_like_s3_key(msg_content):
        s3_key = msg_content or ""
        result["s3_key"] = s3_key
        result["url"] = s3.presign(bucket, s3_key, presign_expiry)
    else:
        # No URL exists until the row is offloaded; this is the only inline body.
        result["inline"] = True
        result["html"] = msg_content or ""

    if bake_key:
        result["prc_proof_s3_key"] = bake_key
        result["prc_proof_url"] = s3.presign(bucket, bake_key, presign_expiry)
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


def _close_pending_change_requests(metadata: dict[str, Any]) -> int:
    """Flip pending change-request batches so non-admins can view the asset.

    Mirrors Backend UI publish metadata writes (not notify / status / QC):
    pending ``change_request_history`` entries become ``approved``, the legacy
    ``approved_resubmit_metadata`` singleton is dropped, and
    ``change_request_claim`` is released. Missing batch ``status`` is treated
    as pending (same as the FE gate). Mutates ``metadata`` in place.
    """
    flipped = 0
    history = metadata.get("change_request_history")
    if isinstance(history, list):
        approved_at = datetime.now(UTC).isoformat()
        for entry in history:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status is None or status == "pending":
                entry["status"] = "approved"
                entry["approved_at"] = approved_at
                flipped += 1
    metadata.pop("approved_resubmit_metadata", None)
    metadata["change_request_claim"] = None
    return flipped


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
    ``versionIntent`` key in the message metadata (the FE reads both).

    A draft→final flip also clears BOTH locks that keep brand members on the
    "Your asset is being prepared." screen, matching what admin Publish does:
    pending change-request batches in the operation metadata, and pending
    ``admin_requests`` rows for the operation (the content editor gates on
    each independently, so closing only one still leaves non-admins blocked).
    Approving an already-final version is an idempotent no-op — message,
    change requests, and requests all untouched. Text/blueprint messages are
    rejected.

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
    identity = require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        msg = _find_message(session, operation_id, message_id, lock=True)
        if msg is None:
            raise ToolError("not_found: unknown message")
        if msg.type not in _DOCUMENT_ROW_TYPES:
            raise ToolError(
                f"invalid_message: type {msg.type!r} is not a document (html/pdf) version"
            )
        resolved_intent = _resolved_message_intent(msg)
        if resolved_intent == "final":
            return {
                "operation_id": operation_id,
                "id": msg.id,
                "message_id": msg.message_id,
                "intent": "final",
                "already_final": True,
                "change_requests_resolved": 0,
                "requests_completed": 0,
                "asset_url": build_asset_url(tenant_slug, operation_id),
            }
        if resolved_intent != "draft":
            raise ToolError(
                f"invalid_message: intent {resolved_intent!r} is not a draft version"
            )
        msg.intent = "final"
        if isinstance(msg.message_metadata, dict):
            msg_metadata = dict(msg.message_metadata)
            msg_metadata["versionIntent"] = "final"
            msg.message_metadata = msg_metadata
        locked_op = session.scalar(
            select(CgOperation).where(
                CgOperation.id == operation_id, CgOperation.deleted_at.is_(None)
            ).with_for_update()
        )
        if locked_op is None:
            raise ToolError("not_authorized: unknown operation")
        op_metadata = dict(locked_op.operation_metadata) if isinstance(
            locked_op.operation_metadata, dict
        ) else {}
        resolved = _close_pending_change_requests(op_metadata)
        locked_op.operation_metadata = op_metadata
        # Audit column mirrors Backend Publish: the message ROW id, not the
        # FE-facing message_id.
        requests_completed = complete_pending_requests_for_operation(
            session,
            operation_id,
            resolved_by_user_id=identity.user_id,
            resolved_message_id=msg.id,
        )
        session.commit()
    return {
        "operation_id": operation_id,
        "id": msg.id,
        "message_id": msg.message_id,
        "intent": "final",
        "already_final": False,
        "change_requests_resolved": resolved,
        "requests_completed": requests_completed,
        "asset_url": build_asset_url(tenant_slug, operation_id),
    }


def _sanitize_file_name(file_name: str | None) -> str:
    """Reduce a user-supplied file name to a safe S3 path segment."""
    if not file_name:
        return ""
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base.replace(" ", "_")


_VERSION_KINDS = _DOCUMENT_ROW_TYPES
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
    kind: str, operation_id: str, message_id: str, file_name: str | None
) -> str:
    """Storage key for one document version, keyed by the row's own id.

    No version number in the path. The old ``v{n}`` segment came from a mutable
    row count, which meant two prepares on the same operation produced the SAME
    key — and for pdf, whose key carried no message_id at all, the second upload
    silently overwrote the first caller's committed document. Row identity is
    unique per prepare and never shifts.

    Mirrors the Backend's own shapes: ``cg_operation_msg_html/{op}/{row}.html``
    (operation_duplicate) and ``approved_pdfs/{op}/{id}_{name}``
    (content_gen_sqlalchemy upload).
    """
    if kind == "html":
        return f"cg_operation_msg_html/{operation_id}/{message_id}.html"
    return f"approved_pdfs/{operation_id}/{message_id}_{_sanitize_file_name(file_name) or 'document.pdf'}"


def _validate_version_key(kind: str, s3_key: str, operation_id: str) -> str:
    """Validate a client-supplied s3_key and return the message_id it names.

    Shape- and operation-scoped so a caller cannot target another operation or an
    arbitrary object. Deliberately depends on NO row count: a version landing
    between prepare and commit must not invalidate a key the caller has already
    uploaded to, and reporting that as ``invalid_key`` hid the real cause.
    """
    if kind == "html":
        prefix = f"cg_operation_msg_html/{operation_id}/"
        suffix = ".html"
        if not (s3_key.startswith(prefix) and s3_key.endswith(suffix)):
            raise ToolError(
                "invalid_key: not a prepared html version key for this operation"
            )
        message_id = s3_key[len(prefix) : -len(suffix)]
    elif kind == "pdf":
        prefix = f"approved_pdfs/{operation_id}/"
        if not s3_key.startswith(prefix):
            raise ToolError(
                "invalid_key: not a prepared pdf version key for this operation"
            )
        # `{message_id}_{file name}` — the id is a UUID, so it has no underscore.
        message_id = s3_key[len(prefix) :].split("_", 1)[0]
    else:
        raise ToolError(f"invalid_key: unsupported type {kind!r}")
    if "/" in message_id or _normalized_uuid(message_id) is None:
        raise ToolError("invalid_key: malformed message_id segment")
    return message_id


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
    intent: str,
    s3_key: str,
    message_id: str,
    now: datetime,
    file_name: str | None,
) -> dict[str, Any]:
    """Mirror the Backend-Server bot document-message metadata shape so the
    frontend renders an MCP-created version identically to a UI-created one.

    ``type: "bot"`` and ``isFinalDocument`` are required: the FE version stepper
    (isDocumentVersionMessage, shared/content-document/pdf-document-message.ts)
    reads both from this metadata blob, not the DB ``type`` column, and drops any
    document row missing them. Without them an MCP-created version is invisible in
    the UI.

    No numeric version keys are written. Backend
    ``build_final_document_bot_metadata`` stamps only ``versionIntent`` — row
    identity and canonical order replaced ``documentVersion`` /
    ``htmlDocumentVersion`` / ``htmlDocumentLastVersion``, so writing them here
    would revive a number no reader trusts.

    PDF versions additionally carry ``approved_pdf_s3_key`` — the FE version
    history and Apryse viewer resolve each PDF version from the message
    metadata (use-editorial-version-history.ts), not the DB content column."""
    metadata = {
        "id": message_id,
        "timestamp": now.isoformat(),
        "type": "bot",
        "isFinalDocument": True,
        "versionIntent": intent,
        "finalContentS3Key": s3_key,
        "finalContent": "",
        "fileName": file_name,
    }
    if kind == "pdf":
        metadata["approved_pdf_s3_key"] = s3_key
    return metadata


def _html_snapshot_metadata(base: CgOperationMessage | None) -> dict[str, Any]:
    """Copy client-owned HTML snapshots from the last HTML row."""
    if base is None or not isinstance(base.message_metadata, dict):
        return {}
    snapshots: dict[str, Any] = {
        key: dict(value)
        for key in ("prc_template_fields", "email_settings")
        if isinstance((value := base.message_metadata.get(key)), dict)
    }
    template_version_id = base.message_metadata.get("prc_template_version_id")
    if isinstance(template_version_id, str) and template_version_id.strip():
        snapshots["prc_template_version_id"] = template_version_id
    return snapshots


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
    from the row). No version row is created here; the key is keyed by the
    ``message_id`` minted now, so a version landing before the commit cannot
    invalidate it.

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
            "message_id": None,
            "s3_key": key,
            "upload_url": upload_url,
            "expires_in": presign_expiry,
        }
    message_id = str(uuid4())
    key = _version_s3_key(kind, operation_id, message_id, file_name)
    content_type = "text/html" if kind == "html" else "application/pdf"
    upload_url = s3.presign_put(bucket, key, presign_expiry, content_type)
    return {
        "operation_id": operation_id,
        "type": kind,
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
    base_message_id: str | None = None,
    confirmed: bool = False,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    max_inline_bytes: int = 2_000_000,
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

    ``base_message_id`` is the document the caller actually read and edited, and
    it makes this a compare-and-swap. Required whenever the operation already has
    a document version; omitted for the first one. An agent's read-edit-commit
    spans several turns of conversation, and in that window the frontend, another
    agent, or a staff publish can land a new version — committing blind would
    bury it. If the caller's base is no longer the head, the write is refused
    with ``conflict: not_latest_document`` so the caller re-reads and reapplies.
    Mirrors the Backend's own 409 contract (``operations_routes.undo``).

    ``confirmed`` covers the one conflict a re-read cannot resolve: when the
    newer head is a ``draft`` and the caller is not staff, re-reading still hides
    it, so retrying would loop. There the refusal is ``confirmation_required``
    and proceeding is an explicit user decision. That check runs after key and
    upload validation, so no draft's existence is disclosed for a request that
    was going to fail anyway.

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
    # Same predicate the read path uses, so "what this caller can see" means the
    # same thing on both sides of a read-edit-commit.
    staff = role_satisfies(identity.role, UserRole.SOLSTICE_STAFF)
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
        bound_message_id: str | None = None
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
                # wins, else the latest document of any intent. Head is
                # created_at then id.
                docs = (
                    CgOperationMessage.operation_id == operation_id,
                    CgOperationMessage.type.in_(_DOCUMENT_ROW_TYPES),
                    CgOperationMessage.deleted_at.is_(None),
                )
                target = _latest_message(
                    session, *docs, _final_document_visibility_clause()
                ) or _latest_message(session, *docs)
                if target is None:
                    raise ToolError(
                        "invalid_state: no document version to bind the source to - "
                        "commit the pdf/html version first"
                    )
                message_metadata = dict(target.message_metadata) if isinstance(
                    target.message_metadata, dict
                ) else {}
                message_metadata["source_html_s3_key"] = s3_key
                message_metadata["show_source_on_ui"] = True
                target.message_metadata = message_metadata
                bound_message_id = target.id
            session.commit()
        return {
            "operation_id": operation_id,
            "type": kind,
            "s3_key": s3_key,
            "sourcefile_s3_key": s3_key,
            "size": size,
            "show_source_on_ui": show_source_on_ui,
            "bound_message_id": bound_message_id,
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
        # Compare-and-swap first: a stale base also makes the prepared s3_key
        # stale, so checking it here reports the real cause instead of a
        # misleading invalid_key.
        visible_head = _visible_head_document(session, operation_id, staff=staff)
        true_head = _head_document(session, operation_id)
        if visible_head is None:
            # Nothing this caller can see to base an edit on: either the first
            # version, or every document is a draft they cannot read.
            if base_message_id:
                raise ToolError(
                    "invalid_request: you have no readable document version on this "
                    "operation - omit base_message_id"
                )
        elif not base_message_id:
            raise ToolError(
                "invalid_request: base_message_id is required - pass the "
                "row id of the version you read and edited (the head_message_id "
                "from solstice_operation_messages) so a version added while you "
                "were working is not overwritten"
            )
        elif not _identifies(visible_head, base_message_id):
            raise ToolError(
                "conflict: not_latest_document - the version you edited is no "
                "longer the current one; a newer version was added while you were "
                "working. Re-read solstice_operation_messages, reapply your change "
                "to the new head_message_id, and commit that"
            )
        # The prepare-derived message id remains the upload/content identity.
        # The row id is minted separately below and owns the proof key.
        message_id = _validate_version_key(kind, s3_key, operation_id)
        size = s3.head(bucket, s3_key)
        if size is None:
            raise ToolError("not_found: object not uploaded - PUT to the upload_url first")
        # The caller's base IS their visible head, but a newer row exists that
        # they cannot read — so re-reading would hand them the same stale base
        # and a retry would loop. Only an explicit user decision gets past this.
        if not confirmed and true_head is not None and (
            visible_head is None or true_head.id != visible_head.id
        ):
            # Wording is deliberately generic: it must warn that the edit is not
            # based on the latest version, without telling a non-staff caller
            # that the newer row is specifically an unapproved draft. That the
            # read path hides drafts is the RBAC rule; naming the intent class
            # here would hand back what the read withheld.
            raise ToolError(
                "confirmation_required: a newer version of this asset exists "
                "that your token cannot read, so the version you edited is not "
                "the latest. Committing adds yours on top of it. Ask the user "
                "whether to go ahead anyway, then retry with confirmed=true"
            )
        html_snapshot_base = None
        if kind == "html":
            html_snapshot_base = (
                _latest_html_creative(session, operation_id)
                if staff
                else _latest_message(
                    session,
                    CgOperationMessage.operation_id == operation_id,
                    CgOperationMessage.type == "html",
                    CgOperationMessage.deleted_at.is_(None),
                    _final_document_visibility_clause(),
                )
            )
        row_id = str(uuid4())
        prc_template_s3_key = None
        if kind == "html":
            prc_template_s3_key, _ = _finalize_html_prc_transition(
                session=session,
                operation=locked,
                row_id=row_id,
                creative_key=s3_key,
                bucket=bucket,
                s3=s3,
                staff=staff,
                max_inline_bytes=max_inline_bytes,
            )
        now = datetime.now(UTC)
        # Backend sorts (created_at, id); 1µs gap so UUID tiebreak cannot invert the pair.
        doc_at = now + timedelta(microseconds=1)
        message_metadata = _doc_message_metadata(
            kind=kind,
            intent=intent,
            s3_key=s3_key,
            message_id=message_id,
            now=doc_at,
            file_name=file_name,
        )
        if kind == "html":
            message_metadata.update(_html_snapshot_metadata(html_snapshot_base))
        pill = CgOperationMessage(
            id=str(uuid4()),
            operation_id=operation_id,
            message_id=str(uuid4()),
            author_id=identity.user_id,
            type="text",
            content="Save new version",
            intent=None,
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
            id=row_id,
            operation_id=operation_id,
            message_id=message_id,
            author_id=None,
            type=kind,
            content=s3_key,
            intent=intent,
            prc_template_s3_key=prc_template_s3_key,
            message_metadata=message_metadata,
            created_at=doc_at,
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
        "intent": intent,
        "id": doc.id,
        "head_message_id": doc.id,
        "message_id": message_id,
        "s3_key": s3_key,
        "prc_template_s3_key": doc.prc_template_s3_key,
        "size": size,
        "asset_url": build_asset_url(tenant_slug, operation_id),
    }


__all__ = [
    "CgOperation",
    "CgOperationMessage",
    "Project",
    "approve_operation_version",
    "bake_prc_template_to_operation",
    "build_asset_url",
    "commit_operation_version",
    "create_edit_operation",
    "create_operation",
    "create_prc_template_version",
    "get_operation_html",
    "get_operation_info",
    "get_project_info",
    "list_operation_messages",
    "list_operations_for_brand",
    "list_projects_for_brand",
    "prepare_operation_version",
    "prepare_prc_template_bake",
    "update_operation",
]
