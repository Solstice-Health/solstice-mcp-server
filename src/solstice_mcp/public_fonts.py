"""List hosted font files in ``solstice-public-forever/permanent_assets/``.

The bake HTML ``@font-face`` is the store. This list is the search index when
a family is named but not yet url-hosted: match the family against the
filename after the optional ``{32hex}_`` prefix.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from solstice_mcp.storage import S3Error, S3Reader
from solstice_mcp.tenants import SessionFactory, TenantRegistry, resolve_tenant_identity

PUBLIC_FOREVER_BUCKET = "solstice-public-forever"
PUBLIC_FOREVER_PREFIX = "permanent_assets/"
PUBLIC_FOREVER_URL = "https://solstice-public-forever.s3.us-east-1.amazonaws.com"
FONT_SUFFIXES = (".woff2", ".woff", ".ttf", ".otf")
_HASH_PREFIX = re.compile(r"^[0-9a-f]{32}_", re.I)
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500
# ponytail: prefix scan; add a fonts/ index if this prefix grows past the cap.
_SCAN_CAP = 2000


def font_label(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    name = _HASH_PREFIX.sub("", name)
    lower = name.lower()
    for suffix in FONT_SUFFIXES:
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.replace("-", " ").replace("_", " ").strip()


def is_font_key(key: str) -> bool:
    lower = key.lower()
    return any(lower.endswith(suffix) for suffix in FONT_SUFFIXES)


def matches_query(key: str, query: str) -> bool:
    tokens = query.lower().split()
    if not tokens:
        return True
    hay = font_label(key).lower()
    return all(token in hay for token in tokens)


def public_font_url(key: str) -> str:
    return f"{PUBLIC_FOREVER_URL}/{key}"


def list_public_fonts(
    subject: str,
    tenant_slug: str,
    *,
    query: str = "",
    limit: int = _DEFAULT_LIMIT,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
) -> dict[str, Any]:
    """Return public font keys matching ``query``. Gated at tenant membership."""
    if (
        resolve_tenant_identity(
            subject,
            tenant_slug,
            registry=registry,
            session_factory=session_factory,
        )
        is None
    ):
        raise ToolError("not_authorized: unknown tenant or subject is not a member")

    capped = max(1, min(limit, _MAX_LIMIT))
    try:
        keys = s3.list_keys(PUBLIC_FOREVER_BUCKET, PUBLIC_FOREVER_PREFIX, max_keys=_SCAN_CAP)
    except S3Error as exc:
        raise ToolError(f"not_available: s3 list failed: {exc}") from exc

    fonts = [
        {"key": key, "label": font_label(key), "url": public_font_url(key)}
        for key in keys
        if is_font_key(key) and matches_query(key, query)
    ]
    fonts.sort(key=lambda row: row["label"].lower())
    scan_hit_cap = len(keys) >= _SCAN_CAP
    return {
        "tenant_slug": tenant_slug,
        "query": query,
        "limit": capped,
        "count": min(len(fonts), capped),
        "truncated": len(fonts) > capped or scan_hit_cap,
        "fonts": fonts[:capped],
    }


__all__ = [
    "PUBLIC_FOREVER_BUCKET",
    "PUBLIC_FOREVER_PREFIX",
    "font_label",
    "is_font_key",
    "list_public_fonts",
    "matches_query",
    "public_font_url",
]
