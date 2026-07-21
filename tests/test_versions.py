from __future__ import annotations

from typing import Any

from conftest import (
    OP_A1,
    OP_A2,
    OP_A3,
    OTHER_SUB,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
)
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperationMessage

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


def _live_rows(harness: AppHarness, op_id: str):
    with harness.session_factory(TENANT) as session:
        rows = session.scalars(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.deleted_at.is_(None),
            ).order_by(CgOperationMessage.position)
        ).all()
        return [
            (r.type, r.version_number, r.intent, r.content, r.position, r.message_id)
            for r in rows
        ]


def _metadata_for(harness: AppHarness, op_id: str, message_id: str):
    with harness.session_factory(TENANT) as session:
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.message_id == message_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        assert row is not None
        return dict(row.message_metadata or {})


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_returns_next_version_when_versions_exist(app_harness: AppHarness, mint_token):
    # OP_A1 has v1 (final) and v2 (draft) -> next is v3.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "file_name": "op_a1.html"},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 3
    assert payload["type"] == "html"
    assert payload["s3_key"] == f"cg_operation_msg_html/{OP_A1}/v3/{payload['message_id']}/v3.html"
    assert payload["upload_url"].startswith("https://fake-s3/")
    assert payload["expires_in"] > 0
    # A presigned PUT was issued against the tenant bucket.
    assert any(c[1] == payload["s3_key"] for c in app_harness.s3.presign_put_calls)


def test_prepare_returns_v1_when_no_document_versions(app_harness: AppHarness, mint_token):
    # OP_A2 has only a text message (no document versions) -> v1.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html"},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 1
    assert payload["s3_key"] == f"cg_operation_msg_html/{OP_A2}/v1/{payload['message_id']}/v1.html"


def test_prepare_pdf_key_uses_approved_pdfs_prefix(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "pdf", "file_name": "doc.pdf"},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 3
    assert payload["s3_key"] == f"approved_pdfs/{OP_A1}/v3_doc.pdf"


def test_prepare_denied_for_non_member(app_harness: AppHarness, mint_token):
    # SHARED is not on BRAND_A3 (which owns OP_A3).
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A3, "type": "html"},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def _prepare_and_upload(harness: AppHarness, token: str, op_id: str, kind: str, file_name: str | None):
    response = _call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": kind, "file_name": file_name},
    )
    prep = tool_payload(response)
    harness.s3.put(BUCKET, prep["s3_key"], b"<html>new</html>" if kind == "html" else b"%PDF-1.4")
    return prep


def test_commit_member_creates_final_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    before = _live_rows(app_harness, OP_A1)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "op_a1.html"},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 3
    assert payload["intent"] == "final"
    assert payload["message_id"] == prep["message_id"]
    assert payload["s3_key"] == prep["s3_key"]
    # Existing rows untouched (append-only); two new rows appended (pill + doc).
    after = _live_rows(app_harness, OP_A1)
    assert len(after) - len(before) == 2
    assert before == after[: len(before)]
    doc = after[-1]
    assert doc[0] == "html"
    assert doc[1] == 3
    assert doc[2] == "final"
    assert doc[3] == prep["s3_key"]
    pill = after[-2]
    assert pill[0] == "text"
    assert pill[1] is None
    assert pill[3] == "Save new version"


def test_commit_staff_creates_draft_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF on BRAND_A1
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep["s3_key"]},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "draft"
    after = _live_rows(app_harness, OP_A1)
    assert after[-1][2] == "draft"


def test_commit_v1_when_no_versions(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html", "s3_key": prep["s3_key"]},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 1
    assert payload["intent"] == "final"


def test_commit_second_version_increments(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    _prepare_and_upload(app_harness, token, OP_A1, "html", "a.html")
    first = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": _prepare_and_upload(app_harness, token, OP_A1, "html", "a.html")["s3_key"]},
    )
    # second prepare must observe the committed v3 -> next is v4
    prep2 = _prepare_and_upload(app_harness, token, OP_A1, "html", "b.html")
    assert prep2["version_number"] == 4
    second = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep2["s3_key"]},
    )
    assert tool_payload(first)["version_number"] == 3
    assert tool_payload(second)["version_number"] == 4


def test_commit_pdf_row(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "pdf", "doc.pdf")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "pdf", "s3_key": prep["s3_key"]},
    )
    payload = tool_payload(response)
    assert payload["version_number"] == 3
    assert payload["s3_key"] == f"approved_pdfs/{OP_A1}/v3_doc.pdf"
    after = _live_rows(app_harness, OP_A1)
    assert after[-1][0] == "pdf"
    assert after[-1][3] == payload["s3_key"]


def test_commit_rejects_key_with_wrong_operation(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    # Key targets OP_A2 but committed against OP_A1.
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep["s3_key"]},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_rejects_key_with_wrong_version_segment(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    # A v99 key for OP_A1 when next is v3.
    bogus = (
        "cg_operation_msg_html/00000000-0000-0000-0000-000000000401/v99/"
        "00000000-0000-0000-0000-000000000099/v99.html"
    )
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": bogus},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_rejects_wrong_prefix(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    other = f"approved_pdfs/{OP_A1}/v3_doc.pdf"  # pdf prefix for an html commit
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": other},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_before_upload_is_rejected(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html"},
    )
    prep = tool_payload(response)
    # Do NOT upload; head will return None.
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep["s3_key"]},
    )
    assert "not_found" in _tool_error_text(response)


def test_commit_denied_for_non_member(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, mint_token(sub=OTHER_SUB), OP_A1, "html", None)
    # SHARED is ADMIN on A1 but try OP_A3 (BRAND_A3, no membership).
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A3, "type": "html", "s3_key": prep["s3_key"]},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_commit_metadata_mirrors_be_shape(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "op_a1.html"},
    )
    meta = _metadata_for(app_harness, OP_A1, prep["message_id"])
    assert meta["documentVersion"] == 3
    assert meta["htmlDocumentVersion"] == 3
    assert meta["htmlDocumentLastVersion"] == 3
    assert meta["versionIntent"] == "final"
    assert meta["isFinalDocument"] is True
    assert meta["finalContentS3Key"] == prep["s3_key"]
    assert meta["fileName"] == "op_a1.html"
