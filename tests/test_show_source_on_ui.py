"""SOL-1255 parity: opt a PDF edit operation's HTML source into the FE's
PDF↔Source toggle.

``solstice_commit_operation_version(type="source", show_source_on_ui=True)``
stamps ``source_html_s3_key`` + ``show_source_on_ui`` onto the bound document
version's message metadata — mirroring Backend-Server's
``upload_sourcefile`` (content_gen_sqlalchemy.py) so the editorial asset view
(ContentEditorShell / AssetPdfSourceToggle) offers the toggle. Only HTML
sources are renderable, so the flag is gated on the file extension.
"""

from __future__ import annotations

from typing import Any

from conftest import PROJECT_P2, SHARED_SUB, STAFF_SUB, AppHarness
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperationMessage

TENANT = "tenant_a"
BUCKET = "test-bucket-a"


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness, "tools/call", token=token,
        params={"name": name, "arguments": args},
    )


def _tool_error_text(response) -> str:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _create_edit_pdf(harness: AppHarness, token: str) -> str:
    payload = tool_payload(_call(
        harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "toggle.pdf",
         "kind": "pdf", "content_type": "EMAIL"},
    ))
    return payload["operation_id"]


def _land_version(
    harness: AppHarness, token: str, op_id: str, kind: str, file_name: str
) -> dict[str, Any]:
    prep = tool_payload(_call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": kind, "file_name": file_name},
    ))
    harness.s3.put(BUCKET, prep["s3_key"], b"file-bytes")
    return tool_payload(_call(
        harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": kind,
         "s3_key": prep["s3_key"], "file_name": file_name},
    ))


def _commit_source(
    harness: AppHarness, token: str, op_id: str, file_name: str, **extra
):
    prep = tool_payload(_call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source", "file_name": file_name},
    ))
    harness.s3.put(BUCKET, prep["s3_key"], b"source-bytes")
    return prep["s3_key"], _call(
        harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source",
         "s3_key": prep["s3_key"], "file_name": file_name, **extra},
    )


def _doc_metadata(harness: AppHarness, op_id: str, version: int) -> dict[str, Any]:
    with harness.session_factory(TENANT) as session:
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.version_number == version,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        assert row is not None
        return dict(row.message_metadata or {})


def test_source_commit_with_flag_stamps_version_message(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN -> final intent
    op_id = _create_edit_pdf(app_harness, token)
    _land_version(app_harness, token, op_id, "pdf", "toggle.pdf")
    source_key, response = _commit_source(
        app_harness, token, op_id, "source.html", show_source_on_ui=True
    )
    payload = tool_payload(response)
    assert payload["show_source_on_ui"] is True
    assert payload["bound_version_number"] == 1
    metadata = _doc_metadata(app_harness, op_id, 1)
    assert metadata["source_html_s3_key"] == source_key
    assert metadata["show_source_on_ui"] is True


def test_source_commit_default_leaves_message_untouched(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)
    _land_version(app_harness, token, op_id, "pdf", "toggle.pdf")
    _key, response = _commit_source(app_harness, token, op_id, "source.html")
    payload = tool_payload(response)
    assert payload["show_source_on_ui"] is False
    assert payload["bound_version_number"] is None
    metadata = _doc_metadata(app_harness, op_id, 1)
    assert "source_html_s3_key" not in metadata
    assert "show_source_on_ui" not in metadata


def test_flag_requires_html_source(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)
    _land_version(app_harness, token, op_id, "pdf", "toggle.pdf")
    _key, response = _commit_source(
        app_harness, token, op_id, "design.zip", show_source_on_ui=True
    )
    assert "invalid_argument" in _tool_error_text(response)


def test_flag_rejected_for_version_commits(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)
    prep = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "pdf", "file_name": "toggle.pdf"},
    ))
    app_harness.s3.put(BUCKET, prep["s3_key"], b"%PDF-1.4")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "pdf",
         "s3_key": prep["s3_key"], "file_name": "toggle.pdf", "show_source_on_ui": True},
    )
    assert "invalid_argument" in _tool_error_text(response)


def test_flag_requires_a_document_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)  # no versions landed yet
    _key, response = _commit_source(
        app_harness, token, op_id, "source.html", show_source_on_ui=True
    )
    assert "invalid_state" in _tool_error_text(response)


def test_flag_binds_published_version_over_draft(app_harness: AppHarness, mint_token):
    member_token = mint_token(sub=SHARED_SUB)  # final intent
    staff_token = mint_token(sub=STAFF_SUB)  # draft intent
    op_id = _create_edit_pdf(app_harness, member_token)
    _land_version(app_harness, member_token, op_id, "pdf", "v1.pdf")  # v1 final
    _land_version(app_harness, staff_token, op_id, "pdf", "v2.pdf")  # v2 draft
    source_key, response = _commit_source(
        app_harness, member_token, op_id, "source.html", show_source_on_ui=True
    )
    payload = tool_payload(response)
    assert payload["bound_version_number"] == 1
    v1_metadata = _doc_metadata(app_harness, op_id, 1)
    assert v1_metadata["source_html_s3_key"] == source_key
    assert v1_metadata["show_source_on_ui"] is True
    v2_metadata = _doc_metadata(app_harness, op_id, 2)
    assert "show_source_on_ui" not in v2_metadata
