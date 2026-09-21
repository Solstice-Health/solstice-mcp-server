"""The cutover path, through the tool face, with the flag on.

Every other suite runs flag-off, so the Backend branch has only ever been
exercised against hand-written doubles inside service unit tests. These drive
real JSON-RPC tool calls, which is where the agent contract actually lives: a
field that changes name or disappears breaks callers without breaking a
signature, and no unit test of the service would see it.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import COMMITTED_HEAD_ID, OP_A1, PREPARED_ROW_ID, SHARED_SUB, AppHarness
from test_server import rpc, tool_payload

TENANT_A = "tenant_a"


@pytest.fixture(autouse=True)
def _via_backend(monkeypatch):
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", "true")


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(harness, "tools/call", token=token, params={"name": name, "arguments": args})


def _named(harness: AppHarness, name: str) -> dict[str, Any]:
    return next(kwargs for called, kwargs in harness.prc_backend.calls if called == name)


def test_prepare_publishes_the_key_and_the_row_id_inside_it(app_harness, mint_token):
    payload = tool_payload(
        _call(
            app_harness,
            mint_token(sub=SHARED_SUB),
            "solstice_prepare_operation_version",
            {"tenant_slug": TENANT_A, "operation_id": OP_A1, "type": "html"},
        )
    )

    assert payload["s3_key"].endswith(f"{PREPARED_ROW_ID}.html")
    assert payload["message_id"] == PREPARED_ROW_ID
    assert payload["upload_url"].startswith("https://s3.test/")
    assert payload["expires_in"] == 600
    assert _named(app_harness, "prepare_upload")["artifact"] == "creative"


def test_the_actor_is_the_verified_subject_never_an_argument(app_harness, mint_token):
    _call(
        app_harness,
        mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT_A, "operation_id": OP_A1, "type": "html"},
    )

    actor = _named(app_harness, "prepare_upload")["actor"]
    assert (actor.actor_sub, actor.tenant_slug) == (SHARED_SUB, TENANT_A)


def test_commit_chains_from_head_message_id(app_harness, mint_token):
    """A caller passes head_message_id back as base_message_id; the tool has
    always published both, and `id` must agree with it."""
    s3_key = f"cg_operation_msg_html/{OP_A1}/{PREPARED_ROW_ID}.html"

    payload = tool_payload(
        _call(
            app_harness,
            mint_token(sub=SHARED_SUB),
            "solstice_commit_operation_version",
            {
                "tenant_slug": TENANT_A,
                "operation_id": OP_A1,
                "type": "html",
                "s3_key": s3_key,
                "base_message_id": "prior-head",
            },
        )
    )

    assert payload["id"] == payload["head_message_id"] == COMMITTED_HEAD_ID
    assert payload["message_id"] == PREPARED_ROW_ID
    assert payload["s3_key"] == s3_key
    assert payload["asset_url"].endswith(OP_A1)
    body = _named(app_harness, "commit_version")["body"]
    assert body == {
        "kind": "content",
        "creative": {"s3_key": s3_key},
        "confirmed": False,
        "base_message_id": "prior-head",
    }


def test_approve_reports_what_the_publish_unlocked(app_harness, mint_token):
    payload = tool_payload(
        _call(
            app_harness,
            mint_token(sub=SHARED_SUB),
            "solstice_approve_operation_version",
            {"tenant_slug": TENANT_A, "operation_id": OP_A1, "message_id": COMMITTED_HEAD_ID},
        )
    )

    assert payload["intent"] == "final"
    assert payload["already_final"] is False
    assert (payload["change_requests_resolved"], payload["requests_completed"]) == (2, 1)
    assert payload["asset_url"].endswith(OP_A1)


def test_a_conflict_reaches_the_agent_as_the_string_it_was_taught(app_harness, mint_token):
    """Agents are instructed to re-read the head on this exact wording."""
    from solstice_mcp.repositories.solstice_backend.errors import BackendStatusError

    app_harness.prc_backend.raises = BackendStatusError(
        status=409, code="content_conflict", detail="head moved"
    )

    response = _call(
        app_harness,
        mint_token(sub=SHARED_SUB),
        "solstice_commit_operation_version",
        {
            "tenant_slug": TENANT_A,
            "operation_id": OP_A1,
            "type": "html",
            "s3_key": f"cg_operation_msg_html/{OP_A1}/{PREPARED_ROW_ID}.html",
        },
    )

    result = response.json()["result"]
    assert result.get("isError") is True, result
    assert result["content"][0]["text"].endswith("conflict: not_latest_document")


def test_a_bake_prepare_targets_the_proof_prefix(app_harness, mint_token):
    from conftest import BRAND_A1

    payload = tool_payload(
        _call(
            app_harness,
            mint_token(sub=SHARED_SUB),
            "solstice_prepare_prc_template_bake",
            {
                "tenant_slug": TENANT_A,
                "brand_id": BRAND_A1,
                "operation_id": OP_A1,
                "content_type": "email",
            },
        )
    )

    assert payload["prc_template_s3_key"].startswith("cg_operation_prc_template/")
    assert _named(app_harness, "prepare_upload")["artifact"] == "proof"


@pytest.mark.parametrize("kind", ["pdf", "source"])
def test_non_html_uploads_never_reach_the_backend(app_harness, mint_token, kind):
    """Only documents moved. A pdf or source upload routed remotely would be
    prepared against a key the local commit then rejects."""
    _call(
        app_harness,
        mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT_A, "operation_id": OP_A1, "type": kind, "file_name": f"a.{kind}"},
    )

    assert app_harness.prc_backend.calls == []
