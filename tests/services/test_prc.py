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
from solstice_mcp.repositories.solstice_backend.prc import CommittedVersion
from solstice_mcp.services.prc import PrcService, committed_response, row_id_from_key, tool_error
from solstice_mcp.tenants import TenantRegistry


def _service(repository=None) -> PrcService:
    """The local path's dependencies are never reached here; the flag decides."""
    return PrcService(
        repository,
        registry=TenantRegistry(),
        session_factory=lambda _slug: None,
        s3=None,
    )


@pytest.fixture
def via_backend(monkeypatch):
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", "true")


def _commit(service):
    return service.commit_version(
        subject="a",
        tenant_slug="acme",
        operation_id="op",
        kind="html",
        s3_key="cg_operation_msg_html/op/row.html",
        file_name=None,
        show_source_on_ui=False,
        base_message_id=None,
        confirmed=False,
    )


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
def test_backend_codes_map_to_the_strings_the_tools_name(via_backend, status, code, expected):
    service = _service(
        _Repo(raises=BackendStatusError(status=status, code=code, detail="a newer version exists"))
    )

    with pytest.raises(ToolError) as raised:
        _commit(service)

    assert str(raised.value) == expected


@pytest.mark.parametrize(
    "status,expected_prefix",
    [(401, "not_authorized:"), (403, "not_authorized:"), (404, "not_found:"), (400, "invalid_arguments:")],
)
def test_uncoded_statuses_map_by_status(status, expected_prefix):
    service = _service(_Repo(raises=BackendStatusError(status=status, code=None, detail="nope")))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith(expected_prefix)


def test_an_uncoded_422_is_a_body_shape_problem_not_a_domain_rule():
    """FastAPI answers a malformed body with an uncoded 422; the agent should
    hear that its arguments were wrong, not that its proof was rejected."""
    service = _service(_Repo(raises=BackendStatusError(status=422, code=None, detail="field required")))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith("invalid_arguments:")


@pytest.mark.parametrize(
    "failure", [BackendUnreachable("backend_unreachable"), BackendUnauthenticated("auth0_token_endpoint_failed")]
)
def test_a_failure_that_never_reached_the_backend_is_not_available(failure):
    """A token the task could not mint used to escape the client untranslated."""
    service = _service(_Repo(raises=failure))

    with pytest.raises(ToolError) as raised:
        service.template_rules("email")

    assert str(raised.value).startswith("not_available:")


def test_an_unknown_profile_never_reaches_the_backend():
    repo = _Repo()
    service = _service(repo)

    with pytest.raises(ToolError):
        service.template_rules("newsletter")

    assert repo.calls == []


def test_the_local_path_stays_in_use_without_credentials(monkeypatch):
    """The flag alone cannot route a write somewhere this task cannot reach."""
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", "true")

    assert _service(None)._handles("acme") is False
    assert _service(_Repo())._handles("acme") is True


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
        CommittedVersion(
            head_message_id="head-1",
            intent="draft",
            creative_s3_key="cg_operation_msg_html/op-1/row-1.html",
            prc_template_s3_key="cg_operation_prc_template/op-1/head-1.html",
            asset_url="https://app.test/assets/op-1",
        ),
        operation_id="op-1",
        s3_key="cg_operation_msg_html/op-1/row-1.html",
        message_id="row-1",
    )

    assert committed.model_dump() == {
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
        CommittedVersion(
            head_message_id="head-9",
            intent="final",
            creative_s3_key=None,
            prc_template_s3_key=None,
            asset_url="u",
        ),
        operation_id="op",
        s3_key="cg_operation_msg_html/op/row.html",
        message_id="row",
    )

    assert committed.id == committed.head_message_id == "head-9"


class _CommitRepo:
    """Answers a commit the way the Backend does, with its own model."""

    def __init__(self, committed: CommittedVersion) -> None:
        self.committed = committed
        self.bodies: list[dict] = []

    def commit_version(self, *, actor, operation_id, body):
        self.bodies.append(body)
        return self.committed


def test_a_bake_publishes_the_creative_the_proof_wraps():
    """The local path answers with the operation's creative key, so the Backend
    path must too — it was reading a field the commit response cannot carry."""
    repo = _CommitRepo(
        CommittedVersion(
            head_message_id="head-1",
            intent="draft",
            creative_s3_key="cg_operation_msg_html/op-1/creative.html",
            prc_template_s3_key="cg_operation_prc_template/op-1/head-1.html",
            asset_url="https://app.test/assets/op-1",
        )
    )

    baked = _service(repo)._commit_bake_via_backend(
        tenant_slug="acme",
        actor_sub="auth0|person",
        operation_id="op-1",
        proof_s3_key="cg_operation_prc_template/op-1/row-1.html",
    )

    assert baked.s3_key == "cg_operation_msg_html/op-1/creative.html"
    assert baked.message_id == "row-1"
    assert baked.id == "head-1"
    assert repo.bodies == [{"kind": "proof", "proof": {"s3_key": "cg_operation_prc_template/op-1/row-1.html"}}]


# ---------------------------------------------------------------------------
# Dispatch: which plane serves a write
# ---------------------------------------------------------------------------


@pytest.fixture
def local(monkeypatch):
    """Records which local function the service fell back to."""
    called: dict[str, dict] = {}

    def _record(name):
        def _fn(*args, **kwargs):
            called[name] = {"args": args, **kwargs}
            return {"from": name}

        return _fn

    for name in (
        "prepare_operation_version",
        "commit_operation_version",
        "approve_operation_version",
        "prepare_prc_template_bake",
        "create_prc_template_version",
    ):
        monkeypatch.setattr(f"solstice_mcp.services.prc.{name}", _record(name))
    return called


def test_with_the_flag_off_every_write_stays_local(local):
    repo = _Repo()
    service = _service(repo)

    service.prepare_version(
        subject="a", tenant_slug="acme", operation_id="op", kind="html", file_name=None
    )
    _commit(service)
    service.publish_version(subject="a", tenant_slug="acme", operation_id="op", message_id="m")
    service.prepare_bake(
        subject="a", tenant_slug="acme", brand_id="b", operation_id="op", content_type="email"
    )

    assert set(local) == {
        "prepare_operation_version",
        "commit_operation_version",
        "approve_operation_version",
        "prepare_prc_template_bake",
    }
    assert repo.calls == []


@pytest.mark.parametrize("kind", ["pdf", "source"])
def test_only_html_moves_to_the_backend(via_backend, local, kind):
    """The Backend serves documents; pdf and source uploads never left."""
    repo = _Repo()

    _service(repo).prepare_version(
        subject="a", tenant_slug="acme", operation_id="op", kind=kind, file_name="f.pdf"
    )

    assert "prepare_operation_version" in local
    assert repo.calls == []


def test_both_halves_are_offered_to_the_backend_as_callables(via_backend, local):
    """One function owns the order of bake and catalog append, so each plane
    arrives as an injected callable rather than a branch inside it."""
    _service(_Repo()).create_template_version(
        subject="a", tenant_slug="acme", brand_id="b", template_key="k"
    )

    assert local["create_prc_template_version"]["operation_baker"] is not None
    assert local["create_prc_template_version"]["library_publisher"] is not None


def test_without_credentials_neither_half_is_offered_a_callable(via_backend, local):
    _service(None).create_template_version(
        subject="a", tenant_slug="acme", brand_id="b", template_key="k"
    )

    assert local["create_prc_template_version"]["operation_baker"] is None
    assert local["create_prc_template_version"]["library_publisher"] is None


def test_the_kill_switch_reaches_the_dispatch(monkeypatch, local):
    """The env override is what works when Remote Configuration is what has
    gone wrong, so it must win at the point the plane is chosen."""
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", "off")
    repo = _Repo()

    _service(repo).publish_version(
        subject="a", tenant_slug="acme", operation_id="op", message_id="m"
    )

    assert "approve_operation_version" in local
    assert repo.calls == []


@pytest.mark.parametrize(
    ("code", "opening"),
    [
        ("invalid_template", "invalid_request: missing the Contract v2 declaration"),
        ("invalid_proof", "invalid_request: operation bake must satisfy baked contract v2"),
    ],
)
def test_a_validating_refusal_carries_every_condition_it_failed(code, opening):
    """The refusal is the only place the author learns what to repair, so both
    surfaces name every condition rather than the first one that tripped."""
    failure = BackendStatusError(
        status=400,
        code=code,
        detail="missing the Contract v2 declaration",
        failures=[
            {
                "check": "L0",
                "message": "missing the Contract v2 declaration",
                "hint": 'add <meta name="sol-prc-contract" content="v2">',
                "rule": "common.declaration",
            },
            {
                "check": "L4",
                "message": "missing the config seed",
                "hint": "add <script id=\"sol-prc-config\">",
                "rule": "common.config",
            },
        ],
    )

    message = str(tool_error(failure))

    assert message.startswith(opening)
    assert "[rule: common.declaration]" in message
    assert "[rule: common.config]" in message
