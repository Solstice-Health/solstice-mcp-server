"""Staff-only tools: solstice_list_brand_users, solstice_update_operation,
solstice_approve_operation_version."""

from __future__ import annotations

from typing import Any

from conftest import (
    BRAND_A1,
    OP_A1,
    OP_A2,
    PROJECT_P1,
    SHARED_SUB,
    STAFF_SUB,
    USER_A_OTHER,
    USER_B_SHARED,
    AppHarness,
)
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperation, CgOperationMessage, Project

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


def _operation(harness: AppHarness, op_id: str) -> CgOperation:
    with harness.session_factory(TENANT) as session:
        op = session.scalar(select(CgOperation).where(CgOperation.id == op_id))
        assert op is not None
        return op


def _project_dir_map(harness: AppHarness, project_id: str) -> dict[str, Any]:
    with harness.session_factory(TENANT) as session:
        project = session.scalar(select(Project).where(Project.id == project_id))
        assert project is not None
        return project.dir_map


def _message(harness: AppHarness, op_id: str, message_id: str) -> CgOperationMessage:
    with harness.session_factory(TENANT) as session:
        msg = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.message_id == message_id,
            )
        )
        assert msg is not None
        # Force-load the deferred metadata before the session closes.
        _ = msg.message_metadata
        return msg


# ---------------------------------------------------------------------------
# solstice_list_brand_users
# ---------------------------------------------------------------------------


def test_list_brand_users_as_staff(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_list_brand_users",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1},
    )
    payload = tool_payload(response)
    assert payload["count"] == 3
    by_email = {u["email"]: u for u in payload["users"]}
    assert by_email["alice@a.test"]["role"] == "ADMIN"
    assert by_email["other@a.test"]["role"] == "MEMBER"
    assert by_email["staff@a.test"]["role"] == "SOLSTICE_STAFF"
    assert by_email["other@a.test"]["user_id"] == USER_A_OTHER


def test_list_brand_users_denied_for_admin(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on BRAND_A1 — below the SOLSTICE_STAFF gate.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_list_brand_users",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_update_operation
# ---------------------------------------------------------------------------


def test_update_name_updates_row_and_dir_map_leaf(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "name": "renamed.html"},
    )
    payload = tool_payload(response)
    assert payload["changed"] == ["file_name"]
    assert _operation(app_harness, OP_A1).file_name == "renamed.html"
    leaf = _project_dir_map(app_harness, PROJECT_P1)["items"][0]
    assert leaf["operation_id"] == OP_A1
    assert leaf["name"] == "renamed.html"


def test_update_content_type_sets_column_metadata_and_leaf(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "content_type": "banner"},
    )
    payload = tool_payload(response)
    assert payload["changed"] == ["content_type"]
    assert payload["content_type"] == "BANNER"
    op = _operation(app_harness, OP_A1)
    assert op.content_type == "BANNER"
    assert op.operation_metadata["content_type_for_fe"] == "BANNER"
    leaf = _project_dir_map(app_harness, PROJECT_P1)["items"][0]
    assert leaf["content_type"] == "BANNER"


def test_update_owner_to_brand_member(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "new_owner_user_id": USER_A_OTHER},
    )
    payload = tool_payload(response)
    assert payload["changed"] == ["user_id"]
    assert _operation(app_harness, OP_A1).user_id == USER_A_OTHER


def test_update_owner_rejects_non_member(app_harness: AppHarness, mint_token):
    # USER_B_SHARED exists in tenant_b, not on BRAND_A1's team.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "new_owner_user_id": USER_B_SHARED},
    )
    assert "invalid_arguments" in _tool_error_text(response)
    assert _operation(app_harness, OP_A1).user_id != USER_B_SHARED


def test_update_without_fields_is_rejected(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1},
    )
    assert "invalid_arguments" in _tool_error_text(response)


def test_update_denied_for_admin(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "name": "nope.html"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert _operation(app_harness, OP_A1).file_name == "op_a1.html"


def test_update_operation_without_project_skips_dir_map(app_harness: AppHarness, mint_token):
    # OP_A2 has project_id=None: the row updates, no dir_map to touch.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_update_operation",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "name": "standalone.html"},
    )
    assert tool_payload(response)["changed"] == ["file_name"]
    assert _operation(app_harness, OP_A2).file_name == "standalone.html"


# ---------------------------------------------------------------------------
# solstice_approve_operation_version
# ---------------------------------------------------------------------------


def test_approve_flips_draft_to_final(app_harness: AppHarness, mint_token):
    # m3 is the seeded html draft v2 on OP_A1.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m3"},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "final"
    assert payload["version_number"] == 2
    assert payload["already_final"] is False
    assert _message(app_harness, OP_A1, "m3").intent == "final"


def test_approve_updates_version_intent_metadata(app_harness: AppHarness, mint_token):
    # Commit a fresh staff draft (which carries metadata), then approve it.
    token = mint_token(sub=STAFF_SUB)
    prep = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "file_name": "v3.html"},
    ))
    app_harness.s3.put(BUCKET, prep["s3_key"], b"<html>v3</html>")
    commit = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep["s3_key"]},
    ))
    assert commit["intent"] == "draft"
    payload = tool_payload(_call(
        app_harness, token, "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": commit["message_id"]},
    ))
    assert payload["intent"] == "final"
    msg = _message(app_harness, OP_A1, commit["message_id"])
    assert msg.intent == "final"
    assert msg.message_metadata["versionIntent"] == "final"


def test_approve_already_final_is_idempotent(app_harness: AppHarness, mint_token):
    # m2 is the seeded html final v1 on OP_A1.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m2"},
    )
    payload = tool_payload(response)
    assert payload["already_final"] is True
    assert payload["intent"] == "final"


def test_approve_rejects_text_message(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m1"},
    )
    assert "invalid_message" in _tool_error_text(response)


def test_approve_denied_for_admin(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m3"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert _message(app_harness, OP_A1, "m3").intent == "draft"
