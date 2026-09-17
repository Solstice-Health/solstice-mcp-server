"""Routing a write to the Backend must not change what the tool returns.

Agents are instructed against these response fields by name — `head_message_id`
becomes the next `base_message_id`, `asset_url` goes in the reply — so a field
that changes shape or disappears breaks callers without breaking any signature.
"""

from __future__ import annotations

import pytest

from solstice_mcp.prc_client import PrcBackendError
from solstice_mcp.tools.content import _backend_call, _committed_response, _row_id_from_key


@pytest.mark.parametrize(
    "key",
    [
        "cg_operation_msg_html/op-1/11111111-2222-3333-4444-555555555555.html",
        "cg_operation_prc_template/op-1/11111111-2222-3333-4444-555555555555.html",
    ],
)
def test_the_row_id_is_read_from_the_key_both_artifacts_use(key):
    assert _row_id_from_key(key) == "11111111-2222-3333-4444-555555555555"


def test_commit_response_keeps_every_field_the_tool_has_always_published():
    committed = _committed_response(
        {
            "head_message_id": "head-1",
            "intent": "draft",
            "prc_template_s3_key": "cg_operation_prc_template/op-1/head-1.html",
            "asset_url": "https://app.test/assets/op-1",
        },
        operation_id="op-1",
        kind="html",
        s3_key="cg_operation_msg_html/op-1/row-1.html",
        message_id="row-1",
    )

    assert committed == {
        "operation_id": "op-1",
        "type": "html",
        "intent": "draft",
        "id": "head-1",
        "head_message_id": "head-1",
        "message_id": "row-1",
        "s3_key": "cg_operation_msg_html/op-1/row-1.html",
        "prc_template_s3_key": "cg_operation_prc_template/op-1/head-1.html",
        "asset_url": "https://app.test/assets/op-1",
    }


def test_id_and_head_message_id_agree_so_the_next_commit_can_chain():
    """A caller passes `head_message_id` back as `base_message_id`; the two
    fields diverging would break the compare-and-swap silently."""
    committed = _committed_response(
        {"head_message_id": "head-9", "intent": "final", "prc_template_s3_key": None, "asset_url": "u"},
        operation_id="op",
        kind="html",
        s3_key="cg_operation_msg_html/op/row.html",
        message_id="row",
    )

    assert committed["id"] == committed["head_message_id"] == "head-9"


def test_a_backend_failure_reaches_the_agent_as_the_string_it_was_taught():
    def _fails(**_):
        raise PrcBackendError("conflict: not_latest_document", status=409, code="content_conflict")

    with pytest.raises(Exception) as exc:
        _backend_call(_fails)

    assert str(exc.value) == "conflict: not_latest_document"


def test_a_backend_success_passes_straight_through():
    assert _backend_call(lambda **_: {"ok": True}) == {"ok": True}
