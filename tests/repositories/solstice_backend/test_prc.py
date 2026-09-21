"""What the PRC repository puts on the wire, and what it raises when the
Backend refuses. Translating a refusal into agent-facing wording is the
service's job and is tested there.
"""

from __future__ import annotations

import json

import pytest

from solstice_mcp.repositories.solstice_backend.errors import BackendStatusError, BackendUnreachable
from solstice_mcp.repositories.solstice_backend.prc import PrcActor, PrcProfile, PrcRepository
from solstice_mcp.repositories.solstice_backend.session import BackendSession


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.content = json.dumps(payload).encode() if payload is not None else b""


class _Token:
    def get_token(self) -> str:
        return "m2m-token"


@pytest.fixture
def repo(monkeypatch):
    sent: dict = {}

    def _request(method, url, *, headers, content, timeout, opener=None, client=None):
        sent.update(method=method, url=url, headers=headers, content=content)
        return sent["response"]

    monkeypatch.setattr(
        "solstice_mcp.repositories.solstice_backend.session.http_client.request", _request
    )
    made = PrcRepository(BackendSession(base_url="https://backend.test", token_acquirer=_Token()))
    made.sent = sent  # type: ignore[attr-defined]
    return made


def _respond(repo, status, payload):
    repo.sent["response"] = _Response(status, payload)


def test_a_write_carries_tenant_in_a_header_and_the_actor_in_the_body(repo):
    """Matching the agent-memory plane rather than inventing a second convention."""
    _respond(repo, 200, {"s3_key": "k", "upload_url": "u", "expires_in": 600})

    result = repo.prepare_upload(
        actor=PrcActor("acme", "auth0|person"), operation_id="op-1", artifact="proof"
    )

    assert result["s3_key"] == "k"
    assert repo.sent["headers"]["X-Tenant-Slug"] == "acme"
    assert repo.sent["headers"]["Authorization"] == "Bearer m2m-token"
    assert "X-Actor-Sub" not in repo.sent["headers"]
    assert json.loads(repo.sent["content"]) == {"artifact": "proof", "actor_sub": "auth0|person"}


def test_the_rules_read_sends_neither_tenant_nor_actor(repo):
    """It acts for nobody and the payload is the same for every tenant. Sending
    a slug the Backend will not use would mean inventing one."""
    _respond(repo, 200, {"contract_version": "v2", "profile": "email", "rules": {}, "document": "#"})

    result = repo.template_rules(profile=PrcProfile.EMAIL)

    assert result["profile"] == "email"
    assert repo.sent["url"].endswith("/api/v2/prc-template-rules?profile=email")
    assert "X-Tenant-Slug" not in repo.sent["headers"]
    assert repo.sent["content"] is None


def test_publish_carries_a_body_only_to_name_its_actor(repo):
    """Publish took no body before the cutover; it has one so the actor can
    travel the same way it does on every other write."""
    _respond(repo, 200, {"operation_id": "op-1", "intent": "final"})

    repo.publish_version(
        actor=PrcActor("acme", "auth0|person"), operation_id="op-1", message_id="m-1"
    )

    assert json.loads(repo.sent["content"]) == {"actor_sub": "auth0|person"}
    assert "qc_override=true" in repo.sent["url"]
    assert "unlock_for_viewers=true" in repo.sent["url"]


def test_a_refusal_keeps_the_backend_code_and_message_unjudged(repo):
    _respond(repo, 409, {"detail": {"code": "content_conflict", "message": "head moved"}})

    with pytest.raises(BackendStatusError) as raised:
        repo.commit_version(actor=PrcActor("acme", "a"), operation_id="op", body={})

    assert (raised.value.status, raised.value.code, raised.value.detail) == (
        409,
        "content_conflict",
        "head moved",
    )


def test_an_unparseable_error_body_still_yields_a_status(repo):
    _respond(repo, 500, None)

    with pytest.raises(BackendStatusError) as raised:
        repo.commit_version(actor=PrcActor("acme", "a"), operation_id="op", body={})

    assert raised.value.status == 500
    assert raised.value.code is None


def test_a_transport_failure_is_not_a_status(repo, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise TimeoutError("no route")

    monkeypatch.setattr(
        "solstice_mcp.repositories.solstice_backend.session.http_client.request", _boom
    )

    with pytest.raises(BackendUnreachable):
        repo.template_rules(profile=PrcProfile.EMAIL)


def test_the_session_pools_one_httpx_client_across_calls(monkeypatch):
    """Each PRC call used to build and tear down its own client, paying a TLS
    handshake per write while the memory plane pooled."""
    clients: list[object] = []

    def _once(method, url, *, headers, content, timeout, client):
        clients.append(client)
        return _Response(200, {})

    monkeypatch.setattr(
        "solstice_mcp.repositories.solstice_backend.session.http_client._httpx_once", _once
    )
    repo = PrcRepository(BackendSession(base_url="https://backend.test", token_acquirer=_Token()))

    repo.template_rules(profile=PrcProfile.EMAIL)
    repo.template_rules(profile=PrcProfile.BANNER)

    assert clients[0] is not None
    assert clients[0] is clients[1]
