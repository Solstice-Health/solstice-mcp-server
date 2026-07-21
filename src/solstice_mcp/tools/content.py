"""Register Solstice project, operation, message, and HTML tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.operations import (
    commit_operation_version,
    get_operation_html,
    get_operation_info,
    get_project_info,
    list_operation_messages,
    list_operations_for_brand,
    list_projects_for_brand,
    prepare_operation_version,
)
from solstice_mcp.storage import S3Reader
from solstice_mcp.tenants import SessionFactory, TenantRegistry

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

APPEND_ONLY_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def register_content_tools(
    mcp: FastMCP,
    *,
    require_subject: Callable[[], str],
    require_access_token: Callable[[], Any],
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int,
    max_inline_bytes: int,
) -> None:
    read_only_tool = audited_tool(mcp, require_access_token, annotations=READ_ONLY)
    append_only_tool = audited_tool(mcp, require_access_token, annotations=APPEND_ONLY_WRITE)

    @read_only_tool
    def solstice_list_projects(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """List projects for a brand. Read-only; gated at MEMBER."""
        projects = list_projects_for_brand(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "projects": projects,
            "count": len(projects),
        }

    @read_only_tool
    def solstice_project_info(tenant_slug: str, project_id: str) -> dict[str, Any]:
        """Return one project's directory map (folders + operation_ids).

        Read-only; gated at MEMBER on the project's brand.
        """
        info = get_project_info(
            require_subject(),
            tenant_slug,
            project_id,
            registry=registry,
            session_factory=session_factory,
        )
        if info is None:
            raise ToolError("not_found: unknown project")
        return {"status": "ok", **info}

    @read_only_tool
    def solstice_list_operations(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """List content-generation operations for a brand. Read-only; gated at MEMBER."""
        operations = list_operations_for_brand(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "operations": operations,
            "count": len(operations),
        }

    @read_only_tool
    def solstice_operation_info(tenant_slug: str, operation_id: str) -> dict[str, Any]:
        """Return one operation's metadata (no messages). Read-only; gated at MEMBER on the operation's brand."""
        info = get_operation_info(
            require_subject(),
            tenant_slug,
            operation_id,
            registry=registry,
            session_factory=session_factory,
        )
        if info is None:
            raise ToolError("not_found: unknown operation")
        return {"status": "ok", **info}

    @read_only_tool
    def solstice_operation_messages(tenant_slug: str, operation_id: str) -> dict[str, Any]:
        """Return an operation's chat + document-version summaries. Read-only; gated at MEMBER.

        Intent visibility is enforced server-side: SOLSTICE_STAFF sees draft and
        final document messages; MEMBER and ADMIN see final only. There is no
        intent/role argument — the filter is derived from your token.
        """
        messages = list_operation_messages(
            require_subject(),
            tenant_slug,
            operation_id,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "operation_id": operation_id,
            "messages": messages,
            "count": len(messages),
        }

    @read_only_tool
    def solstice_operation_html(
        tenant_slug: str,
        operation_id: str,
        message_id: str,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """Return the HTML body for one operation message.

        By default returns a presigned GET URL (no body transfer). Set
        fetch=True to download the HTML inline — use that only when the user
        explicitly asks to read, save, or visualize the document.

        Gated at MEMBER on the operation's brand. Draft visibility is enforced
        here too: a non-staff caller cannot retrieve a draft message's URL or
        body (a presigned URL is a read capability, so it is not handed out for
        drafts). SOLSTICE_STAFF sees drafts; MEMBER/ADMIN see final only.
        """
        return get_operation_html(
            require_subject(),
            tenant_slug,
            operation_id,
            message_id,
            fetch=fetch,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            presign_expiry=presign_expiry,
            max_inline_bytes=max_inline_bytes,
        )

    @append_only_tool
    def solstice_prepare_operation_version(
        tenant_slug: str,
        operation_id: str,
        type: str,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Prepare a new HTML or PDF version upload on an operation. Step 1 of 2.

        Returns a presigned PUT URL and target s3_key for the next version (v1
        if the operation has no document versions, else max+1). Upload the file
        bytes directly to upload_url, then call solstice_commit_operation_version
        with the returned s3_key. Gated at MEMBER on the operation's brand.
        ``type`` is ``html`` or ``pdf``.
        """
        return prepare_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            type,
            file_name,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            presign_expiry=presign_expiry,
        )

    @append_only_tool
    def solstice_commit_operation_version(
        tenant_slug: str,
        operation_id: str,
        type: str,
        s3_key: str,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Commit a new version row after uploading to S3. Step 2 of 2.

        Append-only: inserts a new version row, never overwrites an existing
        one. The s3_key is validated against the prepared version. Intent is
        derived from your token (SOLSTICE_STAFF -> draft; MEMBER/ADMIN -> final)
        and is NOT accepted as an argument. Gated at MEMBER on the operation's
        brand. ``type`` is ``html`` or ``pdf``.
        """
        return commit_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            type,
            s3_key,
            file_name,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
        )


__all__ = ["register_content_tools"]
