"""Register Solstice project, operation, message, and HTML tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from solstice_mcp.audit import audited_tool
from solstice_mcp.brands import list_brand_users
from solstice_mcp.operations import (
    approve_operation_version,
    commit_operation_version,
    create_edit_operation,
    create_operation,
    get_operation_html,
    get_operation_info,
    get_project_info,
    list_operation_messages,
    list_operations_for_brand,
    list_projects_for_brand,
    prepare_operation_version,
    resolve_prc_template_for_brand,
    update_operation,
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

# In-place field updates (no row creation/deletion). destructiveHint=True
# because existing values are overwritten, unlike the append-only writes.
UPDATE_IN_PLACE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
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
    update_tool = audited_tool(mcp, require_access_token, annotations=UPDATE_IN_PLACE)

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
            # Same message as the brand-membership deny: a caller must not be
            # able to distinguish "project does not exist" from "project exists
            # on a brand I am not on" (existence oracle).
            raise ToolError("not_authorized: unknown project")
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
            # Uniform with solstice_operation_messages / solstice_operation_html:
            # never reveal whether an operation exists on a brand the caller
            # cannot access (existence oracle).
            raise ToolError("not_authorized: unknown operation")
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
    def solstice_prc_template(
        tenant_slug: str,
        brand_id: str,
        content_type: str,
        operation_id: str | None = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """Resolve the effective PRC proof template for a brand and content type.

        Reads ``prc_template_versions`` using the same precedence as Solstice:
        operation override, explicit/derived brand template, environment
        default, then platform default. By default returns metadata and field
        configuration without the potentially large HTML body. Set
        ``fetch=True`` when the template HTML is needed as a structural
        exemplar.

        Read-only; gated at MEMBER on the selected brand. ``operation_id`` is
        honored only when that operation belongs to the same brand and exact
        content type.
        """
        template = resolve_prc_template_for_brand(
            require_subject(),
            tenant_slug,
            brand_id,
            content_type,
            operation_id=operation_id,
            fetch=fetch,
            max_inline_bytes=max_inline_bytes,
            registry=registry,
            session_factory=session_factory,
        )
        if template is None:
            raise ToolError(
                f"not_found: no PRC template for content_type {content_type.strip().lower()!r}"
            )
        return {
            "status": "ok",
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            **template,
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
    def solstice_create_operation(
        tenant_slug: str,
        project_id: str,
        name: str,
        content_type: str | None = None,
        folder_path: str = "",
        chat_title: str | None = None,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a new operation inside a project's folder.

        Append-only: inserts one operation (status ``EDITING``, version 1) and
        adds a leaf to the project's directory map at ``folder_path`` (root when
        omitted). The folder must already exist — it is not auto-created. Gated
        at MEMBER on the project's brand; the operation owner is your own user,
        never an argument. To add the v1 document, follow with
        solstice_prepare_operation_version -> upload -> solstice_commit_operation_version
        using the returned operation_id.

        ``content_type`` is REQUIRED. Unlike the Backend-Server UI flow, the
        MCP path has no Query Agent that detects or asks for the content type
        after creation — nothing later sets it. Use the type the user
        explicitly stated (e.g. ``EMAIL``, ``BANNER``, ``SOCIAL``). If the
        user did not state one, ASK THEM which content type this asset is —
        never guess or silently default to a type.

        The response includes ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        if not content_type or not content_type.strip():
            raise ToolError("invalid_argument: content_type is required")
        return create_operation(
            require_subject(),
            tenant_slug,
            project_id,
            name,
            folder_path,
            content_type,
            chat_title,
            file_name,
            registry=registry,
            session_factory=session_factory,
        )

    @append_only_tool
    def solstice_create_edit_operation(
        tenant_slug: str,
        project_id: str,
        name: str,
        kind: str,
        content_type: str | None = None,
        folder_path: str = "",
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Create an EDIT operation: the user brings a finished document.

        Use this — not solstice_create_operation — when the user supplies an
        existing file to put into Solstice for review/editing ("here is my
        HTML/PDF", "edit this"). ``kind`` is ``html`` (category EDIT_HTML) or
        ``pdf`` (category EDIT_PDF). Append-only, gated at MEMBER on the
        project's brand; the folder must already exist.

        After creating, land the document via
        solstice_prepare_operation_version -> upload -> solstice_commit_operation_version
        (type = kind). The commit completes the upload contract automatically
        (is_html_saved / approved_pdf_s3_key / status).

        Conversation rules:
        - ``content_type`` is REQUIRED (EMAIL, BANNER, SOCIAL...). If the user
          did not state one, ASK — never guess.
        - kind="pdf": the working PDF usually has a design source file
          (InDesign, ZIP, PPTX, HTML). If the user did not supply one, ask
          ONCE whether they have it; "I don't have it" is acceptable —
          proceed without. Attach it via prepare/commit with type="source".
          If the source is HTML, also ask ONCE whether they want it viewable
          next to the PDF in Solstice (PDF↔Source toggle); on yes, pass
          show_source_on_ui=true on the source commit (after the pdf commit).
        - kind="html": ask NOTHING beyond the file, name, and content type.

        The response includes ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        if not content_type or not content_type.strip():
            raise ToolError("invalid_argument: content_type is required")
        return create_edit_operation(
            require_subject(),
            tenant_slug,
            project_id,
            name,
            kind,
            content_type,
            folder_path,
            file_name,
            registry=registry,
            session_factory=session_factory,
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
        ``type`` is ``html``, ``pdf``, or ``source`` (design source file for
        edit operations only — records a metadata pointer, not a version;
        ``file_name`` is required for source uploads).

        ``file_name`` MUST be a bare filename only (e.g. ``"1022.html"``,
        ``"apretude_banner_v6.pdf"``). Never pass user instructions, descriptions,
        task notes, or any natural-language prose here — this field is scanned by
        the gateway's prompt-attack guardrail, and instruction-like text will
        cause the call to be denied. Keep the user's intent in your own
        reasoning, not in this argument.
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
        show_source_on_ui: bool = False,
    ) -> dict[str, Any]:
        """Commit a new version row after uploading to S3. Step 2 of 2.

        Append-only: inserts a new version row, never overwrites an existing
        one. The s3_key is validated against the prepared version. Intent is
        derived from your token (SOLSTICE_STAFF -> draft; MEMBER/ADMIN -> final)
        and is NOT accepted as an argument. Gated at MEMBER on the operation's
        brand. ``type`` is ``html``, ``pdf``, or ``source`` (design source file
        for edit operations only — sets operation_metadata.sourcefile_s3_key
        instead of inserting a version). For edit operations, html/pdf commits
        also complete the upload contract (is_html_saved, approved_pdf_s3_key,
        status) automatically.

        ``show_source_on_ui`` — source commits only, and only when the source
        file is HTML. When True, the source is bound to the operation's
        published (else latest) document version so the Solstice asset page
        shows a PDF↔Source toggle: the user can flip between the PDF and the
        rendered HTML. Commit the pdf version BEFORE the source commit. For a
        PDF edit operation whose user supplied an HTML source, ASK the user
        ONCE whether they want the HTML viewable next to the PDF in Solstice;
        pass True only on an explicit yes. Never pass True for non-HTML
        sources (InDesign, ZIP, PPTX) — the call will be rejected.

        ``file_name`` MUST be a bare filename only (e.g. ``"1022.html"``,
        ``"apretude_banner_v6.pdf"``) and must match the value passed to
        ``solstice_prepare_operation_version``. Never pass user instructions,
        descriptions, task notes, or any natural-language prose here — this
        field is scanned by the gateway's prompt-attack guardrail, and
        instruction-like text will cause the call to be denied. Keep the
        user's intent in your own reasoning, not in this argument.

        The response includes ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        return commit_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            type,
            s3_key,
            file_name,
            show_source_on_ui,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
        )

    @read_only_tool
    def solstice_list_brand_users(tenant_slug: str, brand_id: str) -> dict[str, Any]:
        """List a brand's team members (user_id, name, email, role).

        Requires SOLSTICE_STAFF on the brand. Use this to find the user_id for
        solstice_update_operation's new_owner_user_id argument.
        """
        users = list_brand_users(
            require_subject(),
            tenant_slug,
            brand_id,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "users": users,
            "count": len(users),
        }

    @update_tool
    def solstice_update_operation(
        tenant_slug: str,
        operation_id: str,
        name: str | None = None,
        content_type: str | None = None,
        new_owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit an operation's display data. Requires SOLSTICE_STAFF on the operation's brand.

        Updates any subset of:
        - ``name``: the file name shown in the project view (operation
          file_name + the project dir_map leaf).
        - ``content_type``: uppercased; sets the content_type column,
          operation_metadata.content_type_for_fe (the FE source of truth), and
          the dir_map leaf.
        - ``new_owner_user_id``: reassigns the operation's owner. Must be a
          live team member of the operation's brand — discover candidates with
          solstice_list_brand_users. This argument selects the new owner only;
          it never grants the caller any privilege.

        ``name`` MUST be a bare filename only (e.g. ``"apretude_banner.html"``)
        — never instructions or prose; the gateway's prompt-attack guardrail
        scans this field.
        """
        return update_operation(
            require_subject(),
            tenant_slug,
            operation_id,
            name,
            content_type,
            new_owner_user_id,
            registry=registry,
            session_factory=session_factory,
        )

    @update_tool
    def solstice_approve_operation_version(
        tenant_slug: str,
        operation_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Approve a draft document version: flip its intent from draft to final.

        Requires SOLSTICE_STAFF on the operation's brand. The target message
        must be an html or pdf document version (find message_ids via
        solstice_operation_messages). Approving an already-final version is an
        idempotent no-op; text/blueprint messages are rejected.

        The response includes ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        return approve_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            message_id,
            registry=registry,
            session_factory=session_factory,
        )


__all__ = ["register_content_tools"]
