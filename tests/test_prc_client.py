"""The error mapping is the part of this client that is a contract.

Tool descriptions instruct the model by name — re-read the head on
`not_latest_document`, ask the user on `confirmation_required` — so a backend
code that maps to the wrong string changes agent behaviour without changing any
signature. One case per code.
"""

from __future__ import annotations

import json

import pytest

from solstice_mcp.prc_client import PrcBackendClient, PrcBackendError


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.content = json.dumps(payload).encode() if payload is not None else b""


class _Opener:
    """Records the request and replays a canned response, the way the memory
    client's tests drive their transport."""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def open(self, request, timeout=None):  # pragma: no cover - shape only
        raise AssertionError("http_client should be driven through the patched request")


class _Token:
    def get_token(self) -> str:
        return "m2m-token"


@pytest.fixture
def client(monkeypatch):
    sent: dict = {}

    def _request(method, url, *, headers, content, timeout, opener=None, client=None):
        sent.update(method=method, url=url, headers=headers, content=content)
        return sent["response"]

    monkeypatch.setattr("solstice_mcp.prc_client.http_client.request", _request)
    made = PrcBackendClient(base_url="https://backend.test", token_acquirer=_Token())
    made.sent = sent  # type: ignore[attr-defined]
    return made


def _respond(client, status, payload):
    client.sent["response"] = _Response(status, payload)


def test_a_successful_call_carries_tenant_and_actor(client):
    """Tenant travels in a header, the actor in the body — matching the
    agent-memory plane rather than inventing a second convention."""
    _respond(client, 200, {"artifact": "proof", "s3_key": "k", "upload_url": "u", "expires_in": 600})

    result = client.prepare_upload(tenant_slug="acme", actor_sub="auth0|person", operation_id="op-1", artifact="proof")

    assert result["s3_key"] == "k"
    assert client.sent["headers"]["X-Tenant-Slug"] == "acme"
    assert client.sent["headers"]["Authorization"] == "Bearer m2m-token"
    assert "X-Actor-Sub" not in client.sent["headers"]
    assert json.loads(client.sent["content"]) == {"artifact": "proof", "actor_sub": "auth0|person"}


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
    ],
)
def test_backend_codes_map_to_the_strings_the_tools_name(client, status, code, expected):
    _respond(client, status, {"detail": {"code": code, "message": "a newer version exists"}})

    with pytest.raises(PrcBackendError) as exc:
        client.commit_version(tenant_slug="acme", actor_sub="a", operation_id="op", body={})

    assert str(exc.value) == expected
    assert exc.value.code == code


@pytest.mark.parametrize(
    "status,expected_prefix",
    [(401, "not_authorized:"), (403, "not_authorized:"), (404, "not_found:"), (400, "invalid_arguments:")],
)
def test_uncoded_statuses_map_by_status(client, status, expected_prefix):
    _respond(client, status, {"detail": "nope"})

    with pytest.raises(PrcBackendError) as exc:
        client.commit_version(tenant_slug="acme", actor_sub="a", operation_id="op", body={})

    assert str(exc.value).startswith(expected_prefix)


def test_an_uncoded_422_is_a_body_shape_problem_not_a_domain_rule(client):
    """FastAPI answers a malformed body with an uncoded 422; the agent should
    hear that its arguments were wrong, not that its proof was rejected."""
    _respond(client, 422, {"detail": [{"loc": ["body", "kind"], "msg": "field required"}]})

    with pytest.raises(PrcBackendError) as exc:
        client.commit_version(tenant_slug="acme", actor_sub="a", operation_id="op", body={})

    assert str(exc.value).startswith("invalid_arguments:")


def test_an_unparseable_body_still_produces_a_tool_message(client):
    _respond(client, 500, None)

    with pytest.raises(PrcBackendError) as exc:
        client.commit_version(tenant_slug="acme", actor_sub="a", operation_id="op", body={})

    assert str(exc.value).startswith("not_available:")


def test_the_rules_read_sends_neither_tenant_nor_actor(client):
    """It acts for nobody and the payload is the same for every tenant. Sending
    a slug the Backend will not use would mean inventing one."""
    _respond(client, 200, {"contract_version": "v2", "profile": "email", "rules": {}, "source": "doc"})

    result = client.template_rules(profile="email")

    assert result["profile"] == "email"
    assert client.sent["url"].endswith("/api/v2/prc-template-rules?profile=email")
    assert "X-Tenant-Slug" not in client.sent["headers"]
    assert "X-Actor-Sub" not in client.sent["headers"]
    assert client.sent["headers"]["Authorization"] == "Bearer m2m-token"


def test_an_unreadable_contract_keeps_its_own_wording(client):
    """The tool raised `contract_error:` from its own image before the move; a
    deployment that drops the document must still say so in those words."""
    _respond(client, 500, {"detail": {"code": "contract_error", "message": "renderer contract is not readable"}})

    with pytest.raises(PrcBackendError) as raised:
        client.template_rules(profile="email")

    assert str(raised.value) == "contract_error: renderer contract is not readable"


def test_publish_carries_a_body_only_to_name_its_actor(client):
    """Publish took no body before this; it has one so the actor can travel the
    same way it does on every other write."""
    _respond(client, 200, {"operation_id": "op-1", "message_id": "m-1", "intent": "final"})

    client.publish_version(tenant_slug="acme", actor_sub="auth0|person", operation_id="op-1", message_id="m-1")

    assert json.loads(client.sent["content"]) == {"actor_sub": "auth0|person"}
    assert "qc_override=true" in client.sent["url"]
