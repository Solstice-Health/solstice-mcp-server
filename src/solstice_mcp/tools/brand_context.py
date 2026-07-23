"""Register read-only brand context tools (rules, design assets, claims)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.brand_context import (
    get_brand_claims,
    get_brand_design_assets,
    get_brand_rules,
)
from solstice_mcp.storage import S3Reader
from solstice_mcp.tenants import SessionFactory, TenantRegistry

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def register_brand_context_tools(
    mcp: FastMCP,
    *,
    require_subject: Callable[[], str],
    require_access_token: Callable[[], Any],
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int,
    record_activity: Callable[..., Any] | None = None,
) -> None:
    read_only_tool = audited_tool(
        mcp,
        require_access_token,
        annotations=READ_ONLY,
        record_activity=record_activity,
    )

    @read_only_tool
    def solstice_brand_rules(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """Return brand guidelines plus design_bible / ISI / drug_info.

        Read-only; gated at MEMBER. Use before converting designs so the HTML
        matches brand rules and safety chrome.
        """
        return get_brand_rules(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
        )

    @read_only_tool
    def solstice_brand_design_assets(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """Return design-library assets (logos, heroes, etc.) with time-limited URLs.

        Read-only; gated at MEMBER. S3-backed assets return a presigned GET URL;
        public forever URLs are returned as-is when no S3 key is set.
        """
        return get_brand_design_assets(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            presign_expiry=presign_expiry,
        )

    @read_only_tool
    def solstice_brand_claims(
        tenant_slug: str,
        brand_id: str,
        limit: int = 100,
        extracted_only: bool = True,
    ) -> dict[str, Any]:
        """Return clinical claims for a brand (verbatim claim text for conversion).

        Read-only; gated at MEMBER. Defaults to extracted claims only, capped at
        ``limit`` (max 500). Pass extracted_only=false to include non-extracted
        rows. Use claim_text verbatim — never invent medical claims.
        """
        return get_brand_claims(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
            limit=limit,
            extracted_only=extracted_only,
        )


__all__ = ["register_brand_context_tools"]
