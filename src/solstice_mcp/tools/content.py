"""Register Solstice content, PRC template, and document tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from solstice_mcp import feature_flags
from solstice_mcp.audit import audited_tool
from solstice_mcp.brands import list_brand_users
from solstice_mcp.operations import (
    approve_operation_version,
    build_asset_url,
    commit_operation_version,
    create_edit_operation,
    create_operation,
    create_prc_template_version,
    get_operation_html,
    get_operation_info,
    get_project_info,
    list_operation_messages,
    list_operations_for_brand,
    list_projects_for_brand,
    prepare_operation_version,
    prepare_prc_template_bake,
    resolve_prc_template_for_brand,
    update_operation,
)
from solstice_mcp.prc_client import PrcBackendClient, PrcBackendError
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

PRC_TEMPLATE_PROFILES = ("email", "banner", "social", "website")
def _backend_call(fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Run a Backend call and re-raise its failure as the tool-facing error.

    The client has already mapped the Backend's code to the string the tool
    descriptions name, so this only changes the exception type.
    """
    try:
        return fn(**kwargs)
    except PrcBackendError as exc:
        raise ToolError(str(exc)) from exc


def _row_id_from_key(s3_key: str) -> str:
    """The row id the Backend embedded in an artifact key.

    Both artifact prefixes end ``/{operation_id}/{row_id}.html``. The tool has
    always published this as ``message_id``, and callers pass it back, so it is
    read from the key rather than invented here.
    """
    return s3_key.rsplit("/", 1)[-1].removesuffix(".html")


def _committed_response(
    committed: dict[str, Any],
    *,
    operation_id: str,
    kind: str,
    s3_key: str,
    message_id: str,
) -> dict[str, Any]:
    """The tool's long-standing response shape, from the Backend's envelope.

    The Backend answers with what it durably knows; everything else here the
    caller already supplied or the prepare step already returned, so the tool
    contract holds without the Backend echoing it back.
    """
    head = str(committed.get("head_message_id") or "")
    return {
        "operation_id": operation_id,
        "type": kind,
        "intent": committed.get("intent"),
        "id": head,
        "head_message_id": head,
        "message_id": message_id,
        "s3_key": s3_key,
        "prc_template_s3_key": committed.get("prc_template_s3_key"),
        "asset_url": committed.get("asset_url"),
    }


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
    prc_backend: PrcBackendClient | None = None,
) -> None:
    def via_backend(tenant_slug: str, brand_id: str | None = None) -> bool:
        """True when this tenant's PRC writes belong to the Backend.

        Credentials gate the flag, not the other way round: a task without them
        keeps the local path however the flag is set, so enabling a tenant
        cannot route a write somewhere this process cannot reach.
        """
        if prc_backend is None:
            return False
        return feature_flags.prc_writes_via_backend(tenant_slug=tenant_slug, brand_id=brand_id)

    def backend() -> PrcBackendClient:
        if prc_backend is None:  # pragma: no cover - guarded by via_backend
            raise ToolError("not_configured: PRC backend client is unavailable")
        return prc_backend

    read_only_tool = audited_tool(mcp, require_access_token, annotations=READ_ONLY)
    append_only_tool = audited_tool(mcp, require_access_token, annotations=APPEND_ONLY_WRITE)
    update_tool = audited_tool(mcp, require_access_token, annotations=UPDATE_IN_PLACE)

    @read_only_tool
    def solstice_list_projects(
        tenant_slug: str,
        brand_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List projects for a brand. Read-only; gated at MEMBER.

        Paged: default ``limit`` 100, hard max 500. Pass ``offset`` to fetch the
        next page when ``has_more`` is true.
        """
        page = list_projects_for_brand(
            require_subject(),
            tenant_slug,
            brand_id,
            limit=limit,
            offset=offset,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            **page,
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
    def solstice_list_operations(
        tenant_slug: str,
        brand_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List content-generation operations for a brand. Read-only; gated at MEMBER.

        Paged: default ``limit`` 100, hard max 500. Pass ``offset`` to fetch the
        next page when ``has_more`` is true.
        """
        page = list_operations_for_brand(
            require_subject(),
            tenant_slug,
            brand_id,
            limit=limit,
            offset=offset,
            registry=registry,
            session_factory=session_factory,
        )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            **page,
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

        Messages come back oldest first. HTML/PDF rows carry ``display_version``
        (1, 2, … among rows you can see — the same V the Solstice stepper shows)
        and ``is_head`` on the current one, repeated as top-level
        ``head_message_id``. That value is the row's ``id`` (PK), not the
        nullable ``message_id`` column. When the user says "the latest / current
        version", report ``V{display_version}`` of the head row and use
        ``head_message_id`` for reads and commits. Do NOT parse ``v{n}`` from
        S3 keys (those are leftover path segments and will not match the UI).
        Do NOT sort or renumber the rows yourself.

        Intent visibility is enforced server-side: SOLSTICE_STAFF sees draft and
        final document messages; MEMBER and ADMIN see final only. There is no
        intent/role argument — the filter is derived from your token. Numbering
        and the head are computed over the rows you can see, so a non-staff
        caller's V labels and head can differ from a staff caller's.
        """
        messages = list_operation_messages(
            require_subject(),
            tenant_slug,
            operation_id,
            registry=registry,
            session_factory=session_factory,
        )
        head = next((m for m in messages if m.get("is_head")), None)
        head_message_id = head.get("id") if head else None
        return {
            "tenant_slug": tenant_slug,
            "operation_id": operation_id,
            "messages": messages,
            "count": len(messages),
            "head_message_id": head_message_id,
        }

    @read_only_tool
    def solstice_prc_template_rules(profile: str) -> dict[str, Any]:
        """Return the Contract v2 authoring contract for email, banner, social, or website.

        Served by the Backend that enforces it, so the contract and the checks
        that reject a save cannot drift. ``rules`` is the enforceable subset as
        structured bullets; ``document`` is the whole authoring contract as
        markdown — the layer vocabulary, the reserved namespace, the bake stage
        — and is what to read before authoring. Read-only; pass one profile.
        """
        normalized = profile.strip().lower()
        if normalized not in PRC_TEMPLATE_PROFILES:
            allowed = ", ".join(PRC_TEMPLATE_PROFILES)
            raise ToolError(f"invalid_argument: profile must be one of {allowed}")
        return {"status": "ok", **_backend_call(backend().template_rules, profile=normalized)}

    @read_only_tool
    def solstice_prc_template(
        tenant_slug: str,
        brand_id: str,
        content_type: str,
        operation_id: str | None = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """Resolve the effective PRC proof template for a brand and content type.

        Uses operation override, explicit/derived brand template, environment
        default, then platform default. When ``operation_id`` is set, also
        returns ``operation_bake`` (newest-``created_at`` html row's ``prc_template_s3_key``)
        and ``publish_targets`` so the caller can ask whether to bake onto
        that operation, publish to the library, or both.

        By default returns metadata and field configuration without the
        potentially large HTML body. Set ``fetch=True`` when the template HTML
        is needed as a structural exemplar (IDs, slots, builders). Do not copy
        layout, palette, or typography from it. An unpinned brand opt-out
        returns ``not_found`` instead of falling through to a default.

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
            raise ToolError(f"not_found: no PRC template for content_type {content_type.strip().lower()!r}")
        return {
            "status": "ok",
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            **template,
        }

    @append_only_tool
    def solstice_create_prc_template_version(
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
    ) -> dict[str, Any]:
        """Publish a PRC proof template to the library, bake it onto an operation, or both.

        ``html_template`` is the reusable Contract v2 catalog shell and is
        required for ``library`` / ``both``. Call
        ``solstice_prc_template_rules(profile)`` before authoring it.

        If ``solstice_prc_template(..., operation_id=)`` returned
        ``operation_bake`` or the user is editing a specific asset, ask one
        question first: apply this proof to the operation, publish it to the
        library, or both. Then pass
        ``publish_target`` as ``operation``, ``library``, or ``both``.

        Library / both: after the HTML preview, ask separately for display
        name and template key, then ``confirmed=true``. This inserts a new
        library version. Reserved auto-resolving key prefixes are rejected.

        Operation / both: ``solstice_prepare_prc_template_bake``, PUT the bake
        HTML to ``upload_url``, then pass ``operation_id`` and
        ``operation_bake_s3_key``. Size does not matter — never inline the bake
        as ``operation_bake_html``. Upload the approved, self-contained Contract v2
        operation bake, not a reusable catalog shell. It is rebound to the current
        creative and appended as one complete draft version. If validation fails, repair it against
        ``solstice_prc_template_rules``, preview it, and retry only after approval.
        Requires SOLSTICE_STAFF on the selected brand.
        """
        baker = None
        if via_backend(tenant_slug, brand_id):

            def baker(*, operation_id: str, content_type: str, operation_bake_s3_key: str) -> dict[str, Any]:
                """Bake through the Backend as a proof commit.

                An operation bake IS a proof edit: the proof is supplied, and
                the creative it wraps is the one the operation already holds.
                """
                committed = _backend_call(
                    backend().commit_version,
                    tenant_slug=tenant_slug,
                    actor_sub=require_subject(),
                    operation_id=operation_id,
                    body={"kind": "proof", "proof": {"s3_key": operation_bake_s3_key}},
                )
                head = str(committed.get("head_message_id") or "")
                return {
                    "operation_id": operation_id,
                    "intent": committed.get("intent"),
                    "message_id": _row_id_from_key(operation_bake_s3_key),
                    "id": head,
                    "s3_key": committed.get("content"),
                    "prc_template_s3_key": committed.get("prc_template_s3_key"),
                    # Not reported by the Backend, which stores the proof
                    # rather than measuring it. Zero rather than a guess.
                    "html_size_bytes": 0,
                    "asset_url": committed.get("asset_url"),
                }

        template = create_prc_template_version(
            require_subject(),
            tenant_slug,
            brand_id,
            template_key,
            content_type,
            name,
            confirmed=confirmed,
            html_template=html_template,
            operation_bake_html=operation_bake_html,
            operation_bake_s3_key=operation_bake_s3_key,
            description=description,
            config_schema=config_schema,
            default_field_values=default_field_values,
            status=status,
            publish_target=publish_target,
            operation_id=operation_id,
            operation_baker=baker,
            max_inline_bytes=max_inline_bytes,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
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
        """Return presigned GET URLs for one operation HTML message.

        To read the CURRENT version, pass the ``head_message_id`` from
        ``solstice_operation_messages`` (the row ``id``). If you intend to edit
        and save it back, keep that id — it is the ``base_message_id`` the
        commit requires. The user-facing label is ``V{display_version}`` on
        that row, not a number in the S3 key.

        ``url`` / ``s3_key`` are the creative; ``prc_proof_url`` /
        ``prc_proof_s3_key`` are the proof when present.
        Download those URLs when you need the body — the payload never inlines
        HTML. ``fetch`` is ignored (kept so older callers do not error). Catalog
        ``solstice_prc_template`` HTML is not a substitute for the bake.

        Gated at MEMBER on the operation's brand. Draft visibility is enforced
        here too: a non-staff caller cannot retrieve a draft message's URL
        (a presigned URL is a read capability). SOLSTICE_STAFF sees drafts;
        MEMBER/ADMIN see final only.
        """
        _ = fetch
        return get_operation_html(
            require_subject(),
            tenant_slug,
            operation_id,
            message_id,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            presign_expiry=presign_expiry,
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

        ``content_type`` is REQUIRED. Use the type the user explicitly stated
        (e.g. ``EMAIL``, ``BANNER``, ``SOCIAL``). If the user did not state one,
        ASK THEM which content type this asset is — never guess or silently default.

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

        Returns a presigned PUT URL and target s3_key for the next version. Upload
        the file bytes directly to upload_url, then call
        solstice_commit_operation_version with the returned s3_key. Gated at
        MEMBER on the operation's brand.
        The returned ``message_id`` is embedded in ``s3_key`` and remains the
        upload identity. After commit, use ``head_message_id`` / ``id`` as the
        new row address.
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
        if type == "html" and via_backend(tenant_slug):
            prepared = _backend_call(
                backend().prepare_upload,
                tenant_slug=tenant_slug,
                actor_sub=require_subject(),
                operation_id=operation_id,
                artifact="creative",
            )
            return {
                "operation_id": operation_id,
                "type": type,
                # The row id the Backend minted is embedded in the key it
                # returned; the tool has always published it as message_id.
                "message_id": _row_id_from_key(prepared["s3_key"]),
                "s3_key": prepared["s3_key"],
                "upload_url": prepared["upload_url"],
                "expires_in": prepared["expires_in"],
            }
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
    def solstice_prepare_prc_template_bake(
        tenant_slug: str,
        brand_id: str,
        operation_id: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Prepare a Contract v2 PRC operation bake upload. Step 1 of 2.

        Returns a presigned PUT URL and ``prc_template_s3_key`` for
        ``cg_operation_prc_template/{operation_id}/{row_id}.html``. Upload the
        bake HTML bytes directly to ``upload_url``, then call
        ``solstice_create_prc_template_version`` with ``publish_target``
        ``operation`` or ``both``, ``confirmed=true``, and
        ``operation_bake_s3_key`` set to the returned key. Requires
        SOLSTICE_STAFF on the operation's brand.
        """
        if via_backend(tenant_slug, brand_id):
            prepared = _backend_call(
                backend().prepare_upload,
                tenant_slug=tenant_slug,
                actor_sub=require_subject(),
                operation_id=operation_id,
                artifact="proof",
            )
            return {
                "operation_id": operation_id,
                "prc_template_s3_key": prepared["s3_key"],
                "upload_url": prepared["upload_url"],
                "expires_in": prepared["expires_in"],
            }
        return prepare_prc_template_bake(
            require_subject(),
            tenant_slug,
            brand_id,
            operation_id,
            content_type,
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
        base_message_id: str | None = None,
        confirmed: bool = False,
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
        HTML commits for PRC-enabled email, banner, and social assets also
        create that row's proof. Explicit ``enabled=false`` is the keyless
        exception. Do not run a separate PRC bake after commit.

        ``base_message_id`` — the row ``id`` of the version you actually read
        and edited (``head_message_id`` from ``solstice_operation_messages``).
        REQUIRED whenever that call gave you a ``head_message_id``; omit it only
        when that was null (no readable version yet). Your read, your edit, and
        this commit can span many turns, and in that window the Solstice UI,
        another agent, or a staff approval may add a new version. Committing
        without declaring your base would silently bury it. If the head moved
        you get ``conflict: not_latest_document`` — re-read
        solstice_operation_messages, reapply your change on top of the new
        ``head_message_id``, and commit that. Never re-send a base you did not
        read this session. The leftover ``message_id`` column is still accepted
        if passed, but reads always publish the row ``id``.

        ``confirmed`` — leave False on the first attempt. If the call comes back
        ``confirmation_required``, a newer version exists that your token cannot
        read, so re-reading will not help and retrying would loop. Tell the user
        their edit is not based on the latest version and that saving it will
        supersede that newer version, then pass True only on an explicit yes.
        Never set it pre-emptively.

        ``show_source_on_ui`` — source commits only, and only when the source
        file is HTML. When True, the source is bound to the operation's
        published (else latest) document version — returned as
        ``bound_message_id`` (the row ``id``) — so the Solstice asset page shows a PDF↔Source
        toggle: the user can flip between the PDF and the rendered HTML.
        Commit the pdf version BEFORE the source commit. For a
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

        The response includes ``id`` / ``head_message_id`` for the newly inserted
        row. Use that value as the next ``base_message_id``. ``message_id``
        remains the prepare-derived upload identity. The response also includes
        ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        if type == "html" and via_backend(tenant_slug):
            body: dict[str, Any] = {
                "kind": "content",
                "creative": {"s3_key": s3_key},
                "confirmed": confirmed,
            }
            if base_message_id:
                body["base_message_id"] = base_message_id
            committed = _backend_call(
                backend().commit_version,
                tenant_slug=tenant_slug,
                actor_sub=require_subject(),
                operation_id=operation_id,
                body=body,
            )
            return _committed_response(
                committed,
                operation_id=operation_id,
                kind=type,
                s3_key=s3_key,
                message_id=_row_id_from_key(s3_key),
            )
        return commit_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            type,
            s3_key,
            file_name,
            show_source_on_ui,
            base_message_id,
            confirmed,
            registry=registry,
            session_factory=session_factory,
            s3=s3,
            max_inline_bytes=max_inline_bytes,
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
        must be an html or pdf document version. Pass the row ``id`` from
        ``solstice_operation_messages`` (for the current draft this equals
        ``head_message_id``); the nullable ``message_id`` column is accepted
        only for compatibility. A real draft→final flip also closes the
        two things that keep brand members on "Your asset is being prepared."
        — pending change-request batches and pending admin requests on the
        operation. Approving an already-final version is an idempotent no-op.
        Text/blueprint messages are rejected.

        The response includes ``asset_url`` — the operation's Solstice page.
        End your user-facing reply with ``[Open asset in Solstice](<asset_url>)``
        instead of handing the user the operation UUID.
        """
        if via_backend(tenant_slug):
            published = _backend_call(
                backend().publish_version,
                tenant_slug=tenant_slug,
                actor_sub=require_subject(),
                operation_id=operation_id,
                message_id=message_id,
            )
            return {
                "operation_id": operation_id,
                "id": message_id,
                "message_id": message_id,
                "intent": "final",
                "already_final": False,
                "change_requests_resolved": published.get("change_requests_resolved", 0),
                "requests_completed": published.get("requests_completed", 0),
                "asset_url": build_asset_url(tenant_slug, operation_id),
            }
        return approve_operation_version(
            require_subject(),
            tenant_slug,
            operation_id,
            message_id,
            registry=registry,
            session_factory=session_factory,
        )


__all__ = ["register_content_tools"]
