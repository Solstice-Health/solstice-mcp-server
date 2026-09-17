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
    _respond(client, 200, {"artifact": "proof", "s3_key": "k", "upload_url": "u", "expires_in": 600})

    result = client.prepare_upload(tenant_slug="acme", actor_sub="auth0|person", operation_id="op-1", artifact="proof")

    assert result["s3_key"] == "k"
    assert client.sent["headers"]["X-Tenant-Slug"] == "acme"
    assert client.sent["headers"]["X-Actor-Sub"] == "auth0|person"
    assert client.sent["headers"]["Authorization"] == "Bearer m2m-token"


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
