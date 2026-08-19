from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    BRAND_A1,
    BRAND_A3,
    OP_A1,
    OP_A2,
    OP_A3,
    OTHER_SUB,
    PROJECT_P1,
    PROJECT_P2,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
)
from test_server import rpc, tool_payload

from solstice_mcp.operations import (
    CgOperationMessage,
    get_operation_html,
    get_operation_info,
    get_project_info,
    list_operation_messages,
    list_operations_for_brand,
    list_projects_for_brand,
)


def _result(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool_error_text(response) -> str:
    result = _result(response)
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# solstice_list_projects
# ---------------------------------------------------------------------------


def test_list_projects_for_brand_member(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_projects",
                "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A1}},
    )
    payload = tool_payload(response)
    names = {p["name"] for p in payload["projects"]}
    assert names == {"Project P1", "Project P2"}
    assert payload["count"] == 2
    assert payload["has_more"] is False
    assert payload["limit"] == 100
    assert payload["offset"] == 0


def test_list_projects_denied_for_non_member_brand(app_harness: AppHarness, mint_token):
    # SHARED is not on BRAND_A3 (only OTHER is).
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_projects",
                "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A3}},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_project_info
# ---------------------------------------------------------------------------


def test_project_info_returns_dir_map(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_project_info",
                "arguments": {"tenant_slug": "tenant_a", "project_id": PROJECT_P1}},
    )
    payload = tool_payload(response)
    assert payload["status"] == "ok"
    assert payload["brand_id"] == BRAND_A1
    items = payload["dir_map"]["items"]
    assert items[0] == {"name": "op_a1.html", "operation_id": OP_A1}
    assert items[1]["name"] == "Folder"
    assert items[1]["items"][0]["operation_id"] == OP_A2


def test_project_info_unknown_id_is_uniform_not_authorized(app_harness: AppHarness, mint_token):
    # Unknown project ids return the same not_authorized deny as projects on
    # brands the caller is not a member of, so a tenant member cannot use this
    # tool as an existence oracle to enumerate other brands' project ids.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_project_info",
                "arguments": {"tenant_slug": "tenant_a", "project_id": "00000000-0000-0000-0000-000000009999"}},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_project_info_empty_project_returns_empty_dir_map(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_project_info",
                "arguments": {"tenant_slug": "tenant_a", "project_id": PROJECT_P2}},
    )
    payload = tool_payload(response)
    assert payload["dir_map"] == {"items": []}


# ---------------------------------------------------------------------------
# solstice_list_operations
# ---------------------------------------------------------------------------


def test_list_operations_for_brand_member(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_operations",
                "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A1}},
    )
    payload = tool_payload(response)
    ids = {op["id"] for op in payload["operations"]}
    assert ids == {OP_A1, OP_A2}
    assert payload["count"] == 2


def test_list_operations_denied_for_non_member_brand(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_operations",
                "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A3}},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_operation_info
# ---------------------------------------------------------------------------


def test_operation_info_ok_for_member(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_info",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A1}},
    )
    payload = tool_payload(response)
    assert payload["status"] == "ok"
    assert payload["id"] == OP_A1
    assert payload["brand_id"] == BRAND_A1
    assert payload["chat_title"] == "Op A1"
    assert payload["operation_status"] == "editing"


def test_operation_info_denied_for_brand_user_is_not_on(app_harness: AppHarness, mint_token):
    # OP_A3 belongs to BRAND_A3. SHARED is not a member of BRAND_A3. Passing the
    # operation_id must NOT grant access — the server resolves brand_id from the
    # row and re-checks SHARED's own membership.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_info",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A3}},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_operation_info_unknown_id_is_uniform_not_authorized(app_harness: AppHarness, mint_token):
    # Uniform with the cross-brand deny above: unknown ids and inaccessible
    # ids are indistinguishable (no existence oracle).
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_info",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": "00000000-0000-0000-0000-000000009999"}},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_operation_messages — intent filtering (the RBAC rule)
# ---------------------------------------------------------------------------


def test_messages_non_staff_sees_final_only(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on BRAND_A1 — not staff — so draft document rows are hidden.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A1}},
    )
    payload = tool_payload(response)
    intents = [(m["message_id"], m["type"], m["intent"]) for m in payload["messages"]]
    # text (m1), html final (m2), blueprint (m4). draft html (m3) EXCLUDED.
    assert intents == [
        ("m1", "text", None),
        ("m2", "html", "final"),
        ("m4", "blueprint", None),
    ]
    assert payload["count"] == 3


def test_messages_staff_sees_drafts(app_harness: AppHarness, mint_token):
    # STAFF_SUB is SOLSTICE_STAFF on BRAND_A1 — sees everything including drafts.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=STAFF_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A1}},
    )
    payload = tool_payload(response)
    intents = [(m["message_id"], m["type"], m["intent"]) for m in payload["messages"]]
    assert intents == [
        ("m1", "text", None),
        ("m2", "html", "final"),
        ("m3", "html", "draft"),
        ("m4", "blueprint", None),
    ]
    assert payload["count"] == 4


def test_messages_member_role_also_hides_drafts(app_harness: AppHarness, mint_token):
    # OTHER is MEMBER on BRAND_A1 (non-staff) — drafts hidden.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=OTHER_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A1}},
    )
    payload = tool_payload(response)
    assert {m["message_id"] for m in payload["messages"]} == {"m1", "m2", "m4"}


def test_messages_text_content_returned_inline(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A2}},
    )
    payload = tool_payload(response)
    msg = payload["messages"][0]
    assert msg["type"] == "text"
    assert msg["content"] == "hi from op a2"


def test_messages_html_returns_s3_key_not_body(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A1}},
    )
    payload = tool_payload(response)
    html_msg = next(m for m in payload["messages"] if m["type"] == "html")
    assert html_msg["s3_key"].startswith("cg_operation_msg_html/")
    assert html_msg["body"] is None


def test_messages_denied_for_brand_user_is_not_on(app_harness: AppHarness, mint_token):
    # SHARED calling messages on OP_A3 (BRAND_A3) — not a member. The brand_id is
    # resolved from the operation row, not from an argument.
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": OP_A3}},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_messages_unknown_operation_denied(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness, "tools/call", token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_operation_messages",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": "00000000-0000-0000-0000-000000009999"}},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# Unit tests for the resolver layer (intent filter is in the query, not arg)
# ---------------------------------------------------------------------------


def test_list_operation_messages_unit_hides_drafts_for_non_staff(app_harness: AppHarness):
    msgs = list_operation_messages(
        SHARED_SUB, "tenant_a", OP_A1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert [m["message_id"] for m in msgs] == ["m1", "m2", "m4"]


def test_list_operation_messages_unit_shows_drafts_for_staff(app_harness: AppHarness):
    msgs = list_operation_messages(
        STAFF_SUB, "tenant_a", OP_A1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert [m["message_id"] for m in msgs] == ["m1", "m2", "m3", "m4"]


def test_list_operation_messages_unit_denies_non_member(app_harness: AppHarness):
    with pytest.raises(Exception, match="not_authorized"):
        list_operation_messages(
            SHARED_SUB, "tenant_a", OP_A3,
            registry=app_harness.registry, session_factory=app_harness.session_factory,
        )


def test_list_projects_for_brand_unit(app_harness: AppHarness):
    page = list_projects_for_brand(
        SHARED_SUB, "tenant_a", BRAND_A1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert {p["name"] for p in page["projects"]} == {"Project P1", "Project P2"}
    assert page["has_more"] is False
    assert page["limit"] == 100


def test_get_project_info_unit_returns_dir_map(app_harness: AppHarness):
    info = get_project_info(
        SHARED_SUB, "tenant_a", PROJECT_P1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert info is not None
    assert info["brand_id"] == BRAND_A1
    assert info["dir_map"]["items"][0]["operation_id"] == OP_A1


def test_get_operation_info_unit(app_harness: AppHarness):
    info = get_operation_info(
        SHARED_SUB, "tenant_a", OP_A1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert info is not None
    assert info["brand_id"] == BRAND_A1


def test_list_operations_for_brand_unit(app_harness: AppHarness):
    page = list_operations_for_brand(
        SHARED_SUB, "tenant_a", BRAND_A1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert {op["id"] for op in page["operations"]} == {OP_A1, OP_A2}
    assert page["has_more"] is False


def test_list_operations_respects_limit(app_harness: AppHarness):
    page = list_operations_for_brand(
        SHARED_SUB, "tenant_a", BRAND_A1,
        limit=1, offset=0,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert page["count"] == 1
    assert page["has_more"] is True
    page2 = list_operations_for_brand(
        SHARED_SUB, "tenant_a", BRAND_A1,
        limit=1, offset=1,
        registry=app_harness.registry, session_factory=app_harness.session_factory,
    )
    assert page2["count"] == 1
    assert page2["has_more"] is False
    assert {page["operations"][0]["id"], page2["operations"][0]["id"]} == {OP_A1, OP_A2}
    # Stable pages: walking offset must not reshuffle the same two ids.
    assert page["operations"][0]["id"] != page2["operations"][0]["id"]


# ---------------------------------------------------------------------------
# solstice_operation_html — presigned URL + s3_key; bodies are not inlined
# ---------------------------------------------------------------------------


def _call_html(harness, mint_token, *, sub=SHARED_SUB, op=OP_A1, msg="m2", fetch=False):
    return rpc(
        harness, "tools/call", token=mint_token(sub=sub),
        params={"name": "solstice_operation_html",
                "arguments": {"tenant_slug": "tenant_a", "operation_id": op,
                              "message_id": msg, "fetch": fetch}},
    )


def test_html_returns_presigned_url_without_body(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="m2")
    payload = tool_payload(response)
    assert payload["url"].startswith("https://fake-s3/test-bucket-a/")
    assert "html" not in payload
    assert "prc_proof_html" not in payload
    assert payload["s3_key"].startswith("cg_operation_msg_html/")
    assert payload["intent"] == "final"
    assert payload["prc_proof_url"] is None
    assert app_harness.s3.download_calls == []


def test_html_fetch_is_ignored_and_does_not_inline(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="m2", fetch=True)
    payload = tool_payload(response)
    assert "html" not in payload
    assert payload["url"].startswith("https://fake-s3/")
    assert app_harness.s3.download_calls == []


def test_html_staff_can_presign_draft(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=STAFF_SUB, msg="m3")
    payload = tool_payload(response)
    assert payload["intent"] == "draft"
    assert payload["url"].startswith("https://fake-s3/")
    assert "html" not in payload


def test_html_non_staff_denied_draft_url(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="m3")
    assert "not_authorized" in _tool_error_text(response)
    assert app_harness.s3.presign_calls == []


def test_html_denied_for_brand_user_is_not_on(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, op=OP_A3, msg="m2")
    assert "not_authorized" in _tool_error_text(response)


def test_html_unknown_message(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="no-such-msg")
    assert "not_found" in _tool_error_text(response)


def test_html_non_html_message_rejected(app_harness: AppHarness, mint_token):
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="m1")
    assert "not_found" in _tool_error_text(response)


def test_html_unit_staff_presign_draft(app_harness: AppHarness):
    result = get_operation_html(
        STAFF_SUB, "tenant_a", OP_A1, "m3",
        registry=app_harness.registry, session_factory=app_harness.session_factory,
        s3=app_harness.s3,
    )
    assert result["intent"] == "draft"
    assert result["url"].startswith("https://fake-s3/")
    assert "html" not in result


def test_html_unit_non_staff_denied_draft(app_harness: AppHarness):
    with pytest.raises(Exception, match="not_authorized"):
        get_operation_html(
            SHARED_SUB, "tenant_a", OP_A1, "m3",
            registry=app_harness.registry, session_factory=app_harness.session_factory,
            s3=app_harness.s3,
        )


_PROOF_ROW_ID = "00000000-0000-0000-0000-000000000502"
_PROOF_KEY = f"cg_operation_prc_template/{OP_A1}/proof.html"


def _attach_bake(harness: AppHarness, html: bytes = b"<html>baked proof</html>") -> str:
    with harness.session_factory("tenant_a") as session:
        msg = session.get(CgOperationMessage, _PROOF_ROW_ID)
        assert msg is not None
        msg.prc_template_s3_key = _PROOF_KEY
        session.commit()
    harness.s3.put("test-bucket-a", _PROOF_KEY, html)
    return _PROOF_KEY


def test_html_presigns_prc_proof_without_download(app_harness: AppHarness, mint_token):
    key = _attach_bake(app_harness)
    response = _call_html(app_harness, mint_token, sub=SHARED_SUB, msg="m2")
    payload = tool_payload(response)
    assert payload["prc_proof_s3_key"] == key
    assert payload["prc_proof_url"].endswith(f"{key}?expires=600")
    assert "prc_proof_html" not in payload
    assert "html" not in payload
    assert app_harness.s3.download_calls == []
