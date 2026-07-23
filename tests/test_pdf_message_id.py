"""PDF version commits must carry a real message_id.

PDF s3 keys don't embed a message_id (unlike html keys), so commit used to
fall back to "" — which broke two things downstream:

  * ``solstice_approve_operation_version`` targets rows by message_id, so an
    MCP-created PDF draft could never be approved via MCP.
  * The FE version stepper identifies versions by the message metadata ``id``;
    empty ids collide across versions, marking several rows "Current" and
    breaking version navigation on the asset page.

The fix mints a fresh UUID at commit for pdf rows (DB column + metadata id).
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


def _create_edit_pdf(harness: AppHarness, token: str) -> str:
    payload = tool_payload(_call(
        harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "ids.pdf",
         "kind": "pdf", "content_type": "EMAIL"},
    ))
    return payload["operation_id"]


def _land_pdf(harness: AppHarness, token: str, op_id: str, file_name: str) -> dict[str, Any]:
    prep = tool_payload(_call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "pdf", "file_name": file_name},
    ))
    harness.s3.put(BUCKET, prep["s3_key"], b"%PDF-1.4")
    return tool_payload(_call(
        harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "pdf",
         "s3_key": prep["s3_key"], "file_name": file_name},
    ))


def _doc_row(harness: AppHarness, op_id: str, version: int) -> CgOperationMessage:
    with harness.session_factory(TENANT) as session:
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.version_number == version,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        assert row is not None
        _ = row.message_metadata  # force-load deferred column before detach
        return row


def test_pdf_commit_returns_real_message_id(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)
    payload = _land_pdf(app_harness, token, op_id, "ids.pdf")
    assert payload["message_id"], "pdf commit must mint a non-empty message_id"
    row = _doc_row(app_harness, op_id, 1)
    assert row.message_id == payload["message_id"]
    # FE version stepper identity: metadata.id must match the row message_id.
    assert (row.message_metadata or {}).get("id") == payload["message_id"]


def test_pdf_message_ids_unique_across_versions(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    op_id = _create_edit_pdf(app_harness, token)
    first = _land_pdf(app_harness, token, op_id, "v1.pdf")
    second = _land_pdf(app_harness, token, op_id, "v2.pdf")
    assert first["message_id"] != second["message_id"]


def test_pdf_draft_approvable_via_returned_message_id(app_harness: AppHarness, mint_token):
    member_token = mint_token(sub=SHARED_SUB)
    staff_token = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF -> draft commits
    op_id = _create_edit_pdf(app_harness, member_token)
    committed = _land_pdf(app_harness, staff_token, op_id, "draft.pdf")
    assert committed["intent"] == "draft"
    response = _call(
        app_harness, staff_token, "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "message_id": committed["message_id"]},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "final"
    assert payload["already_final"] is False
    assert _doc_row(app_harness, op_id, 1).intent == "final"
