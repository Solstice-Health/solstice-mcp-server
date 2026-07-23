"""Edit-request operations: solstice_create_edit_operation + category-aware
commit finishing writes + type="source" attachment.

Route-level integration tests via the shared TestClient harness: every case
boots the app, calls the tool over MCP JSON-RPC, and asserts both the tool
payload and the resulting DB rows.
"""

from __future__ import annotations

from typing import Any

from conftest import (
    BRAND_A1,
    DELETED_SUB,
    PROJECT_P2,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
)
from sqlalchemy import func, select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperation, CgOperationMessage

TENANT = "tenant_a"
BUCKET = "test-bucket-a"


def _result(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool_error_text(response) -> str:
    result = _result(response)
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness, "tools/call", token=token,
        params={"name": name, "arguments": args},
    )


def _operation(harness: AppHarness, op_id: str) -> CgOperation | None:
    with harness.session_factory(TENANT) as session:
        op = session.scalar(select(CgOperation).where(CgOperation.id == op_id))
        if op is not None:
            # Touch deferred columns while the session is open so assertions
            # can read them after detach.
            _ = op.operation_metadata
        return op


def _message_count(harness: AppHarness, op_id: str) -> int:
    with harness.session_factory(TENANT) as session:
        return session.scalar(
            select(func.count(CgOperationMessage.id)).where(
                CgOperationMessage.operation_id == op_id
            )
        )


def _doc_message(harness: AppHarness, op_id: str, message_id: str) -> CgOperationMessage:
    with harness.session_factory(TENANT) as session:
        msg = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.message_id == message_id,
            )
        )
        assert msg is not None
        # Touch the deferred column while the session is open.
        _ = msg.message_metadata
        return msg


def _create_edit(harness: AppHarness, token: str, kind: str, **overrides):
    args = {
        "tenant_slug": TENANT,
        "project_id": PROJECT_P2,
        "name": f"edit-{kind}.bin",
        "kind": kind,
        "content_type": "EMAIL",
        **overrides,
    }
    return _call(harness, token, "solstice_create_edit_operation", args)


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


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_edit_html_row_and_category(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    payload = tool_payload(_create_edit(app_harness, token, "html"))
    assert payload["operation_category"] == "EDIT_HTML"
    op = _operation(app_harness, payload["operation_id"])
    assert op is not None
    assert op.brand_id == BRAND_A1
    assert op.operation_category == "EDIT_HTML"
    assert op.is_chat_history_deleted is False
    assert op.content_type == "EMAIL"
    assert op.status == "EDITING"


def test_create_edit_pdf_row_and_category(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(_create_edit(app_harness, token, "pdf"))
    op = _operation(app_harness, payload["operation_id"])
    assert op is not None
    assert op.operation_category == "EDIT_PDF"


def test_create_edit_invalid_kind_rejected(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _create_edit(app_harness, token, "docx")
    assert "invalid_argument" in _tool_error_text(response)


def test_create_edit_requires_content_type(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "x.html", "kind": "html"},
    )
    assert "content_type" in _tool_error_text(response)


def test_create_edit_denied_for_non_member(app_harness: AppHarness, mint_token):
    token = mint_token(sub=DELETED_SUB)
    response = _create_edit(app_harness, token, "html")
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# category-aware commit finishing writes
# ---------------------------------------------------------------------------


def test_edit_html_commit_sets_is_html_saved(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN -> final intent
    op_id = tool_payload(_create_edit(app_harness, token, "html"))["operation_id"]
    committed = _land_version(app_harness, token, op_id, "html", "doc.html")
    assert committed["version_number"] == 1
    op = _operation(app_harness, op_id)
    assert op is not None
    assert op.is_html_saved is True
    # HTML commits never write the PDF pointer or flip status.
    assert "approved_pdf_s3_key" not in (op.operation_metadata or {})
    assert op.status == "EDITING"


def test_edit_pdf_commit_final_finishing_writes(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN -> final intent
    op_id = tool_payload(_create_edit(app_harness, token, "pdf"))["operation_id"]
    committed = _land_version(app_harness, token, op_id, "pdf", "doc.pdf")
    assert committed["intent"] == "final"
    op = _operation(app_harness, op_id)
    assert op is not None
    assert op.operation_metadata["approved_pdf_s3_key"] == committed["s3_key"]
    assert op.status == "COMPLETED"
    assert op.is_html_saved is True
    # The FE version history resolves PDF versions from the message metadata.
    doc = _doc_message(app_harness, op_id, committed["message_id"])
    assert doc.message_metadata["approved_pdf_s3_key"] == committed["s3_key"]


def test_edit_pdf_draft_commit_skips_status_flip(app_harness: AppHarness, mint_token):
    creator = mint_token(sub=SHARED_SUB)
    op_id = tool_payload(_create_edit(app_harness, creator, "pdf"))["operation_id"]
    staff = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF on BRAND_A1 -> draft intent
    committed = _land_version(app_harness, staff, op_id, "pdf", "doc.pdf")
    assert committed["intent"] == "draft"
    op = _operation(app_harness, op_id)
    assert op is not None
    # Pointer is always written; the status/is_html_saved flip is final-only,
    # mirroring the backend's as_draft behavior.
    assert op.operation_metadata["approved_pdf_s3_key"] == committed["s3_key"]
    assert op.status == "EDITING"
    assert op.is_html_saved is not True


def test_generated_op_commit_has_no_finishing_writes(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    created = tool_payload(_call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "gen.html",
         "content_type": "EMAIL"},
    ))
    _land_version(app_harness, token, created["operation_id"], "html", "gen.html")
    op = _operation(app_harness, created["operation_id"])
    assert op is not None
    assert op.is_html_saved is not True
    assert op.status == "EDITING"


# ---------------------------------------------------------------------------
# type="source" attachment
# ---------------------------------------------------------------------------


def test_source_attach_sets_metadata_pointer(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = tool_payload(_create_edit(app_harness, token, "pdf"))["operation_id"]
    before = _message_count(app_harness, op_id)

    prep = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source",
         "file_name": "design source.zip"},
    ))
    assert prep["s3_key"] == f"sourcefiles/{op_id}/design_source.zip"
    assert prep["version_number"] is None
    app_harness.s3.put(BUCKET, prep["s3_key"], b"zip-bytes")

    committed = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source",
         "s3_key": prep["s3_key"], "file_name": "design source.zip"},
    ))
    assert committed["sourcefile_s3_key"] == prep["s3_key"]

    op = _operation(app_harness, op_id)
    assert op is not None
    assert op.operation_metadata["sourcefile_s3_key"] == prep["s3_key"]
    # Source attach records a pointer only — no version/message rows.
    assert _message_count(app_harness, op_id) == before


def test_source_rejected_on_generated_operation(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    created = tool_payload(_call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "gen.html",
         "content_type": "EMAIL"},
    ))
    response = _call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": created["operation_id"],
         "type": "source", "file_name": "src.zip"},
    )
    assert "invalid_state" in _tool_error_text(response)


def test_source_requires_file_name(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = tool_payload(_create_edit(app_harness, token, "pdf"))["operation_id"]
    response = _call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source"},
    )
    assert "file_name is required" in _tool_error_text(response)


def test_source_commit_rejects_foreign_key(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = tool_payload(_create_edit(app_harness, token, "pdf"))["operation_id"]
    other_id = tool_payload(_create_edit(app_harness, token, "pdf", name="other.pdf"))[
        "operation_id"
    ]
    app_harness.s3.put(BUCKET, f"sourcefiles/{other_id}/src.zip", b"zip")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source",
         "s3_key": f"sourcefiles/{other_id}/src.zip", "file_name": "src.zip"},
    )
    assert "invalid_key" in _tool_error_text(response)
