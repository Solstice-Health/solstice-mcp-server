"""The service owns two contracts an agent depends on.

Tool descriptions instruct the model by name — re-read the head on
`not_latest_document`, ask the user on `confirmation_required` — so a Backend
code that maps to the wrong string changes agent behaviour without changing any
signature. And the response fields predate the cutover: `head_message_id`
becomes the next `base_message_id`, `asset_url` goes in the reply, so a field
that changes shape breaks callers without breaking a signature either.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from solstice_mcp.repositories.solstice_backend.errors import (
    BackendStatusError,
    BackendUnauthenticated,
    BackendUnreachable,
)
from solstice_mcp.services.prc import PrcService, committed_response, row_id_from_key


class _Repo:
    """A repository that answers every method with one canned result or raise."""

    def __init__(self, result=None, raises=None):
        self.result = result or {}
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def _call(**kwargs):
            self.calls.append((name, kwargs))
            if self.raises is not None:
                raise self.raises
            return self.result

        return _call


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (409, "content_conflict", "conflict: not_latest_document"),
        (409, "confirmation_required", "confirmation_required: a newer version exists"),
        (409, "no_template", "invalid_state: a newer version exists"),
        (
            422,
            "invalid_proof",
            "invalid_request: operation bake must satisfy baked contract v2: a newer version exists",
        ),
        (422, "stale_proof", "invalid_state: a newer version exists"),
        (422, "invalid_request", "invalid_request: a newer version exists"),
        (503, "bake_unavailable", "not_available: a newer version exists"),
        (500, "contract_error", "contract_error: a newer version exists"),
    ],
)
def test_backend_codes_map_to_the_strings_the_tools_name(status, code, expected):
    service = PrcService(
        _Repo(raises=BackendStatusError(status=status, code=code, detail="a newer version exists"))
    )

    with pytest.raises(ToolError) as raised:
        service.commit_version(
            tenant_slug="acme",
            actor_sub="a",
            operation_id="op",
            s3_key="cg_operation_msg_html/op/row.html",
            base_message_id=None,
            confirmed=False,
        )

    assert str(raised.value) == expected


@pytest.mark.parametrize(
    "status,expected_prefix",
    [(401, "not_authorized:"), (403, "not_authorized:"), (404, "not_found:"), (400, "invalid_arguments:")],
)
def test_uncoded_statuses_map_by_status(status, expected_prefix):
    service = PrcService(_Repo(raises=BackendStatusError(status=status, code=None, detail="nope")))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith(expected_prefix)


def test_an_uncoded_422_is_a_body_shape_problem_not_a_domain_rule():
    """FastAPI answers a malformed body with an uncoded 422; the agent should
    hear that its arguments were wrong, not that its proof was rejected."""
    service = PrcService(_Repo(raises=BackendStatusError(status=422, code=None, detail="field required")))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith("invalid_arguments:")


@pytest.mark.parametrize(
    "failure", [BackendUnreachable("backend_unreachable"), BackendUnauthenticated("auth0_token_endpoint_failed")]
)
def test_a_failure_that_never_reached_the_backend_is_not_available(failure):
    """A token the task could not mint used to escape the client untranslated."""
    service = PrcService(_Repo(raises=failure))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith("not_available:")


def test_an_unknown_profile_never_reaches_the_backend():
    repo = _Repo()
    service = PrcService(repo)

    with pytest.raises(ToolError):
        service.template_rules("newsletter")

    assert repo.calls == []


def test_the_local_path_stays_in_use_without_credentials(monkeypatch):
    """The flag alone cannot route a write somewhere this task cannot reach."""
    monkeypatch.setattr(
        "solstice_mcp.feature_flags.prc_writes_via_backend", lambda **_: True
    )

    assert PrcService(None).handles("acme") is False
    assert PrcService(_Repo()).handles("acme") is True


@pytest.mark.parametrize(
    "key",
    [
        "cg_operation_msg_html/op-1/11111111-2222-3333-4444-555555555555.html",
        "cg_operation_prc_template/op-1/11111111-2222-3333-4444-555555555555.html",
    ],
)
def test_the_row_id_is_read_from_the_key_both_artifacts_use(key):
    assert row_id_from_key(key) == "11111111-2222-3333-4444-555555555555"


def test_commit_response_keeps_every_field_the_tool_has_always_published():
    committed = committed_response(
        {
            "head_message_id": "head-1",
            "intent": "draft",
            "prc_template_s3_key": "cg_operation_prc_template/op-1/head-1.html",
            "asset_url": "https://app.test/assets/op-1",
        },
        operation_id="op-1",
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
    committed = committed_response(
        {"head_message_id": "head-9", "intent": "final", "prc_template_s3_key": None, "asset_url": "u"},
        operation_id="op",
        s3_key="cg_operation_msg_html/op/row.html",
        message_id="row",
    )

    assert committed["id"] == committed["head_message_id"] == "head-9"
