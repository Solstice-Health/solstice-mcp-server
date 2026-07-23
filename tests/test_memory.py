"""Tests for the Solstice MCP memory tools and Backend client."""

from __future__ import annotations

import io
import json
import logging
import urllib.parse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from conftest import (
    BRAND_A1,
    BRAND_A2,
    BRAND_A3,
    BRAND_B1,
    OTHER_SUB,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
    FakeBackendOpener,
)
from test_server import rpc, tool_payload

from solstice_mcp.audit import AUDIT_EVENT_NAME, AUDIT_LOGGER_NAME
from solstice_mcp.memory_client import (
    Auth0ClientCredentials,
    BackendMemoryClient,
    MemoryClientConflict,
    MemoryClientInvalidArgument,
    MemoryClientNotFound,
    MemoryClientUnauthorized,
    MemoryClientUnavailable,
)

TENANT = "tenant_a"
TENANT_B = "tenant_b"


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness,
        "tools/call",
        token=token,
        params={"name": name, "arguments": args},
    )


def _ok(response) -> dict[str, Any]:
    return tool_payload(response)


def _tool_error_text(response) -> str:
    result = response.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _set_recall_response(
    opener,
    brand: list[dict] | None = None,
    personal: list[dict] | None = None,
    tenant_personal: list[dict] | None = None,
) -> None:
    body = json.dumps(
        {"brand": brand or [], "personal": personal or [], "tenant_personal": tenant_personal or []}
    ).encode("utf-8")
    opener.responses[("GET", "/api/internal/agent-memory")] = (200, body)


def _set_remember_response(opener, memory_id: str = "mem-1", mutation_id: str = "mut-1") -> None:
    opener.responses[("POST", "/api/internal/agent-memory")] = (
        200,
        json.dumps({"memory_id": memory_id, "mutation_id": mutation_id, "status": "active"}).encode("utf-8"),
    )


def _set_replace_response(opener, memory_id: str = "mem-2") -> None:
    opener.responses[("POST", "/api/internal/agent-memory/mem-1/supersede")] = (
        200,
        json.dumps(
            {"memory_id": memory_id, "superseded_id": "mem-1", "mutation_id": "mut-2", "status": "active"}
        ).encode("utf-8"),
    )


def _set_forget_response(opener) -> None:
    opener.responses[("POST", "/api/internal/agent-memory/mem-1/forget")] = (
        200,
        json.dumps({"memory_id": "mem-1", "mutation_id": "mut-3", "status": "forgotten"}).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# tool wiring and annotations
# ---------------------------------------------------------------------------


def test_memory_tools_listed_with_correct_annotations(app_harness: AppHarness, mint_token):
    tools = rpc(app_harness, "tools/list", token=mint_token()).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    for name in (
        "solstice_memory_recall",
        "solstice_list_recent_work",
        "solstice_memory_remember",
        "solstice_memory_replace",
        "solstice_memory_forget",
    ):
        assert name in by_name
    assert by_name["solstice_memory_recall"]["annotations"] == {
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": False,
    }
    assert by_name["solstice_list_recent_work"]["annotations"] == by_name[
        "solstice_memory_recall"
    ]["annotations"]
    for name in ("solstice_memory_remember", "solstice_memory_replace", "solstice_memory_forget"):
        assert by_name[name]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        }


# ---------------------------------------------------------------------------
# role gates
# ---------------------------------------------------------------------------


def test_recall_succeeds_for_member(app_harness: AppHarness, mint_token):
    _set_recall_response(
        app_harness.backend_opener,
        brand=[{"memory_id": "b1"}],
        personal=[{"memory_id": "p1"}],
        tenant_personal=[{"memory_id": "tp1"}],
    )
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1})
    payload = _ok(response)
    assert payload["status"] == "ok"
    assert payload["brand"] == [{"memory_id": "b1"}]
    assert payload["personal"] == [{"memory_id": "p1"}]
    assert payload["tenant_personal"] == [{"memory_id": "tp1"}]


def test_recall_denied_for_non_member(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # not on BRAND_A3
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A3})
    assert "not_authorized" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


def test_personal_write_succeeds_for_member(app_harness: AppHarness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "preference", "statement": "prefer short emails"})
    payload = _ok(response)
    assert payload["scope"] == "personal"
    assert payload["memory_id"] == "mem-1"


def test_tenant_personal_write_omits_partition_brand(app_harness: AppHarness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness,
        token,
        "solstice_memory_remember",
        {
            "tenant_slug": TENANT,
            "brand_id": BRAND_A1,
            "scope": "tenant_personal",
            "fact_type": "preference",
            "statement": "prefer concise responses",
        },
    )

    payload = _ok(response)
    assert payload["scope"] == "tenant_personal"
    assert payload["brand_id"] is None
    assert json.loads(app_harness.backend_opener.calls[-1]["body"])["brand_id"] is None


def test_brand_write_denied_for_member(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # MEMBER on BRAND_A2
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A2, "scope": "brand",
                     "fact_type": "convention", "statement": "use sentence case"})
    assert "not_authorized" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


def test_brand_write_succeeds_for_admin(app_harness: AppHarness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "brand",
                     "fact_type": "decision", "statement": "ship Q3"})
    assert _ok(response)["scope"] == "brand"


def test_brand_write_succeeds_for_solstice_staff(app_harness: AppHarness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF on BRAND_A1
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "brand",
                     "fact_type": "decision", "statement": "staff decision"})
    assert _ok(response)["scope"] == "brand"


def test_invalid_scope_rejected(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "global",
                     "fact_type": "preference", "statement": "x"})
    assert "invalid_argument" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


# ---------------------------------------------------------------------------
# cross-tenant / cross-brand denial
# ---------------------------------------------------------------------------


def test_cross_tenant_recall_denied(app_harness: AppHarness, mint_token):
    # OTHER is a tenant_a user but not a tenant_b user.
    token = mint_token(sub=OTHER_SUB)
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT_B, "brand_id": BRAND_B1})
    assert "not_authorized" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


def test_cross_brand_recall_denied(app_harness: AppHarness, mint_token):
    # SHARED is not a member of BRAND_A3 in tenant_a.
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A3})
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# actor derivation + exact Backend request schemas
# ---------------------------------------------------------------------------


def _headers(call: dict[str, Any]) -> dict[str, Any]:
    return {k.lower(): v for k, v in call["headers"].items()}


def test_recall_request_schema_matches_backend_contract(app_harness: AppHarness, mint_token):
    _set_recall_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1, user_id USER_A_SHARED
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1,
                     "fact_type": "preference", "q": "email", "limit": 5})
    assert _ok(response)["status"] == "ok"

    call = app_harness.backend_opener.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/api/internal/agent-memory"
    # All three partition keys are query params (Backend revalidates against the M2M token + X-Tenant-Slug).
    assert f"brand_id={BRAND_A1}" in call["url"]
    assert f"actor_sub={urllib.parse.quote(SHARED_SUB, safe='')}" in call["url"]
    assert f"tenant_slug={TENANT}" in call["url"]
    assert "fact_type=preference" in call["url"]
    assert "q=email" in call["url"]
    assert "limit=5" in call["url"]
    assert call["body"] is None
    headers = _headers(call)
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == TENANT
    # No actor header carries authority; user_id/role never cross the wire.
    assert "x-solstice-actor" not in headers
    assert "x-solstice-user-id" not in headers
    assert "x-solstice-role" not in headers


def test_remember_request_schema_matches_backend_contract(app_harness: AppHarness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=OTHER_SUB)  # MEMBER on BRAND_A1, user_id USER_A_OTHER
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "finding_disposition",
                     "statement": "claim C is unsupported for this brand",
                     "source_refs": [{"source_type": "claim", "source_id": "claim-7"}],
                     "entity_refs": [{"entity_type": "brand", "entity_id": BRAND_A1}],
                     "expires_at": "2027-01-01T00:00:00Z",
                     "reason": "user confirmed"})
    assert _ok(response)["scope"] == "personal"

    call = app_harness.backend_opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory"
    headers = _headers(call)
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == TENANT
    assert "x-solstice-actor" not in headers
    body = json.loads(call["body"])
    assert body == {
        "brand_id": BRAND_A1,
        "scope": "personal",
        "fact_type": "finding_disposition",
        "statement": "claim C is unsupported for this brand",
        "source_refs": [{"source_type": "claim", "source_id": "claim-7"}],
        "entity_refs": [{"entity_type": "brand", "entity_id": BRAND_A1}],
        "expires_at": "2027-01-01T00:00:00Z",
        "reason": "user confirmed",
        "actor_sub": OTHER_SUB,
        "tenant_slug": TENANT,
    }


def test_replace_request_schema(app_harness: AppHarness, mint_token):
    _set_replace_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1 -> brand write ok
    response = _call(app_harness, token, "solstice_memory_replace",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "memory_id": "mem-1",
                     "scope": "brand", "fact_type": "decision",
                     "statement": "ship Q4 instead", "reason": "replan"})
    payload = _ok(response)
    assert payload["superseded_id"] == "mem-1"

    call = app_harness.backend_opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory/mem-1/supersede"
    headers = _headers(call)
    assert headers["x-tenant-slug"] == TENANT
    assert "x-solstice-actor" not in headers
    body = json.loads(call["body"])
    assert body == {
        "brand_id": BRAND_A1,
        "scope": "brand",
        "fact_type": "decision",
        "statement": "ship Q4 instead",
        "actor_sub": SHARED_SUB,
        "tenant_slug": TENANT,
        "reason": "replan",
    }


def test_forget_request_schema(app_harness: AppHarness, mint_token):
    _set_forget_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1 -> brand write ok
    response = _call(app_harness, token, "solstice_memory_forget",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "memory_id": "mem-1",
                     "scope": "brand", "reason": "obsolete"})
    payload = _ok(response)
    assert payload["status"] == "forgotten"

    call = app_harness.backend_opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory/mem-1/forget"
    headers = _headers(call)
    assert headers["x-tenant-slug"] == TENANT
    assert "x-solstice-actor" not in headers
    body = json.loads(call["body"])
    assert body == {
        "brand_id": BRAND_A1,
        "scope": "brand",
        "actor_sub": SHARED_SUB,
        "tenant_slug": TENANT,
        "reason": "obsolete",
    }


def test_tenant_personal_replace_and_forget_omit_partition_brand(
    app_harness: AppHarness,
    mint_token,
):
    _set_replace_response(app_harness.backend_opener)
    _set_forget_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)

    replace_response = _call(
        app_harness,
        token,
        "solstice_memory_replace",
        {
            "tenant_slug": TENANT,
            "brand_id": BRAND_A1,
            "memory_id": "mem-1",
            "scope": "tenant_personal",
            "fact_type": "preference",
            "statement": "prefer short responses",
        },
    )
    assert _ok(replace_response)["brand_id"] is None
    assert json.loads(app_harness.backend_opener.calls[-1]["body"])["brand_id"] is None

    forget_response = _call(
        app_harness,
        token,
        "solstice_memory_forget",
        {
            "tenant_slug": TENANT,
            "brand_id": BRAND_A1,
            "memory_id": "mem-1",
            "scope": "tenant_personal",
        },
    )
    assert _ok(forget_response)["brand_id"] is None
    assert json.loads(app_harness.backend_opener.calls[-1]["body"])["brand_id"] is None


def test_recall_entity_id_filter_is_passed_through(app_harness: AppHarness, mint_token):
    _set_recall_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)
    _call(app_harness, token, "solstice_memory_recall",
          {"tenant_slug": TENANT, "brand_id": BRAND_A1, "entity_id": "op-123"})
    call = app_harness.backend_opener.calls[-1]
    assert "entity_id=op-123" in call["url"]


def test_recent_work_tool_returns_backend_items(app_harness: AppHarness, mint_token):
    items = [
        {
            "brand_id": BRAND_A1,
            "entity_type": "operation",
            "entity_id": "op-7",
            "last_opened_at": "2026-07-23T12:00:00+00:00",
        }
    ]
    app_harness.backend_opener.responses[
        ("GET", "/api/internal/agent-memory/recent-work")
    ] = (200, json.dumps({"items": items}).encode())

    payload = _ok(
        _call(
            app_harness,
            mint_token(sub=SHARED_SUB),
            "solstice_list_recent_work",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1, "limit": 7},
        )
    )

    assert payload == {"tenant_slug": TENANT, "items": items}
    call = next(
        call
        for call in app_harness.backend_opener.calls
        if call["path"] == "/api/internal/agent-memory/recent-work"
    )
    assert call["method"] == "GET"
    assert f"actor_sub={urllib.parse.quote(SHARED_SUB, safe='')}" in call["url"]
    assert f"tenant_slug={TENANT}" in call["url"]
    assert f"brand_id={BRAND_A1}" in call["url"]
    assert "limit=7" in call["url"]


def test_invalid_fact_type_rejected_before_backend(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "rumor", "statement": "x"})
    assert "invalid_argument" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


def test_malformed_entity_ref_rejected_before_backend(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "preference", "statement": "x",
                     "entity_refs": [{"entity_type": "brand"}]})  # missing entity_id
    assert "invalid_argument" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []


# ---------------------------------------------------------------------------
# error redaction
# ---------------------------------------------------------------------------


def test_backend_5xx_redacted(app_harness: AppHarness, mint_token):
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory")] = (
        500,
        b'{"detail":"internal stack trace with secret sql://creds"}',
    )
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "preference", "statement": "x"})
    text = _tool_error_text(response)
    assert "service_unavailable" in text
    assert "internal stack trace" not in text
    assert "secret" not in text


def test_backend_404_maps_to_not_found(app_harness: AppHarness, mint_token):
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/mem-1/forget")] = (
        404, b'{"detail":"no such fact"}',
    )
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_forget",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "memory_id": "mem-1",
                     "scope": "brand"})
    text = _tool_error_text(response)
    assert "not_found" in text
    assert "no such fact" not in text


def test_backend_409_maps_to_conflict(app_harness: AppHarness, mint_token):
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/mem-1/supersede")] = (
        409, b'{"detail":"version mismatch"}',
    )
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_replace",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "memory_id": "mem-1",
                     "scope": "brand", "fact_type": "decision", "statement": "x"})
    text = _tool_error_text(response)
    assert "conflict" in text
    assert "version mismatch" not in text


def test_backend_401_maps_to_not_authorized(app_harness: AppHarness, mint_token):
    app_harness.backend_opener.responses[("GET", "/api/internal/agent-memory")] = (
        401, b'{"detail":"bad m2m token"}',
    )
    token = mint_token(sub=SHARED_SUB)
    response = _call(app_harness, token, "solstice_memory_recall",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1})
    text = _tool_error_text(response)
    assert "not_authorized" in text
    assert "bad m2m token" not in text


# ---------------------------------------------------------------------------
# audit omission of statements
# ---------------------------------------------------------------------------


def test_audit_omits_statement_and_results(app_harness: AppHarness, mint_token,
                                           caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    _set_remember_response(app_harness.backend_opener)
    token = mint_token(sub=SHARED_SUB)
    statement_text = "RECALL_ME_AUDIT_NEEDLE"
    response = _call(app_harness, token, "solstice_memory_remember",
                    {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal",
                     "fact_type": "preference", "statement": statement_text,
                     "reason": "REASON_NEEDLE"})
    payload = _ok(response)
    assert payload["memory_id"] == "mem-1"
    assert payload["scope"] == "personal"

    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME
        and json.loads(record.message).get("event") == AUDIT_EVENT_NAME
    ]
    memory_events = [e for e in events if e["tool"] == "solstice_memory_remember"]
    assert len(memory_events) == 1
    event = memory_events[0]
    blob = json.dumps(event)
    assert statement_text not in blob
    assert "REASON_NEEDLE" not in blob
    assert event["resources"] == {"tenant_slug": TENANT, "brand_id": BRAND_A1, "scope": "personal"}
    assert event["outcome"] == "success"


def test_audit_records_denied_brand_write(app_harness: AppHarness, mint_token,
                                          caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    token = mint_token(sub=SHARED_SUB)  # MEMBER on BRAND_A2 -> brand write denied
    _call(app_harness, token, "solstice_memory_remember",
          {"tenant_slug": TENANT, "brand_id": BRAND_A2, "scope": "brand",
           "fact_type": "convention", "statement": "x"})
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME
        and json.loads(record.message).get("event") == AUDIT_EVENT_NAME
    ]
    event = [e for e in events if e["tool"] == "solstice_memory_remember"][-1]
    assert event["outcome"] == "denied"
    assert event["error_code"] == "not_authorized"
    assert event["resources"] == {"tenant_slug": TENANT, "brand_id": BRAND_A2, "scope": "brand"}


# ---------------------------------------------------------------------------
# Auth0 client-credentials caching/validation
# ---------------------------------------------------------------------------


class _FakeTokenResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeTokenResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        self.closed = True
        return False


def _make_acquirer() -> Auth0ClientCredentials:
    return Auth0ClientCredentials(
        token_endpoint="https://test.auth0.local/oauth/token",
        client_id="m2m-client",
        client_secret="m2m-secret",
        audience="https://backend.test",
        scope="memory:invoke",
        timeout=2.0,
    )


def test_client_credentials_caches_until_near_expiry(monkeypatch):
    calls = []

    def fake_urlopen(request: Request, timeout: float | None = None):
        calls.append(request)
        assert request.full_url == "https://test.auth0.local/oauth/token"
        body = request.data.decode("utf-8")
        assert "grant_type=client_credentials" in body
        assert "client_id=m2m-client" in body
        assert "audience=https%3A%2F%2Fbackend.test" in body
        assert "scope=memory%3Ainvoke" in body
        return _FakeTokenResponse(b'{"access_token":"tok-1","expires_in":3600,"token_type":"Bearer"}')

    monkeypatch.setattr("solstice_mcp.memory_client.urllib.request.urlopen", fake_urlopen)
    acquirer = _make_acquirer()
    assert acquirer.get_token() == "tok-1"
    assert acquirer.get_token() == "tok-1"
    assert len(calls) == 1


def test_client_credentials_refetches_after_expiry(monkeypatch):
    calls = []

    def fake_urlopen(_request: Request, timeout: float | None = None):
        calls.append(len(calls))
        return _FakeTokenResponse(b'{"access_token":"tok-N","expires_in":3600,"token_type":"Bearer"}')

    monkeypatch.setattr("solstice_mcp.memory_client.urllib.request.urlopen", fake_urlopen)
    acquirer = _make_acquirer()
    acquirer.get_token()
    assert len(calls) == 1
    acquirer._expires_at = 0.0  # force expiry
    acquirer.get_token()
    assert len(calls) == 2


def test_client_credentials_rejects_invalid_response(monkeypatch):
    monkeypatch.setattr(
        "solstice_mcp.memory_client.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeTokenResponse(b'{"access_token":"tok"}'),  # no expires_in
    )
    acquirer = _make_acquirer()
    with pytest.raises(MemoryClientUnauthorized, match="auth0_token_response_invalid"):
        acquirer.get_token()


def test_client_credentials_rejects_missing_config():
    with pytest.raises(ValueError, match="Auth0 client-credentials requires"):
        Auth0ClientCredentials(
            token_endpoint="https://x/oauth/token",
            client_id="",
            client_secret="",
            audience="",
            scope="memory:invoke",
        )


def test_client_credentials_maps_http_error(monkeypatch):
    def fake_urlopen(_request: Request, timeout: float | None = None):
        raise HTTPError("https://x", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"invalid_client"}'))

    monkeypatch.setattr("solstice_mcp.memory_client.urllib.request.urlopen", fake_urlopen)
    acquirer = _make_acquirer()
    with pytest.raises(MemoryClientUnauthorized, match="auth0_token_endpoint_failed"):
        acquirer.get_token()


def test_client_credentials_maps_unreachable(monkeypatch):
    def fake_urlopen(_request: Request, timeout: float | None = None):
        raise URLError("connection refused")

    monkeypatch.setattr("solstice_mcp.memory_client.urllib.request.urlopen", fake_urlopen)
    acquirer = _make_acquirer()
    with pytest.raises(MemoryClientUnavailable, match="auth0_token_endpoint_unreachable"):
        acquirer.get_token()


# ---------------------------------------------------------------------------
# BackendMemoryClient direct error mapping and redaction
# ---------------------------------------------------------------------------


def _direct_client(opener: FakeBackendOpener) -> BackendMemoryClient:
    return BackendMemoryClient(
        base_url="https://backend.test",
        token_acquirer=_FakeAcquirer(),
        timeout=5.0,
        opener=opener,
    )


class _FakeAcquirer:
    def get_token(self) -> str:
        return "m2m-bearer"

    def invalidate(self) -> None:
        pass


def test_backend_client_redacts_5xx_body():
    opener = FakeBackendOpener()
    opener.responses[("GET", "/api/internal/agent-memory")] = (
        500, b'{"detail":"internal db creds leak"}',
    )
    client = _direct_client(opener)
    from solstice_mcp.memory_client import ActorEnvelope

    actor = ActorEnvelope(actor_sub="sub", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u")
    with pytest.raises(MemoryClientUnavailable) as exc_info:
        client.recall(actor=actor)
    assert "internal db creds leak" not in str(exc_info.value)
    assert exc_info.value.code == "backend_unavailable"


def test_backend_client_maps_404_409_403_422():
    from solstice_mcp.memory_client import ActorEnvelope

    actor = ActorEnvelope(actor_sub="sub", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u")
    for status, exc_type in [(404, MemoryClientNotFound), (409, MemoryClientConflict),
                             (403, MemoryClientUnauthorized),
                             (422, MemoryClientInvalidArgument)]:
        opener = FakeBackendOpener()
        opener.responses[("POST", "/api/internal/agent-memory")] = (status, b'{"detail":"x"}')
        client = _direct_client(opener)
        with pytest.raises(exc_type):
            client.remember(actor=actor, scope="personal", fact_type="preference", statement="s")


def test_backend_client_unreachable_maps_to_unavailable():
    from solstice_mcp.memory_client import ActorEnvelope

    class _FailOpener:
        def open(self, _request, timeout=None):
            raise URLError("refused")

    client = BackendMemoryClient(
        base_url="https://backend.test",
        token_acquirer=_FakeAcquirer(),
        opener=_FailOpener(),
    )
    actor = ActorEnvelope(actor_sub="sub", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u")
    with pytest.raises(MemoryClientUnavailable, match="backend_unreachable"):
        client.recall(actor=actor)


def test_backend_client_recall_emits_exact_query_and_headers():
    """Direct contract smoke test: pins the recall wire shape to the Backend contract."""
    from solstice_mcp.memory_client import ActorEnvelope

    opener = FakeBackendOpener()
    _set_recall_response(opener)
    client = _direct_client(opener)
    actor = ActorEnvelope(actor_sub="sub-1", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u-1")
    client.recall(actor=actor, fact_type="preference", entity_id="op-9", q="email", limit=25)

    call = opener.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/api/internal/agent-memory"
    assert f"brand_id={BRAND_A1}" in call["url"]
    assert "actor_sub=sub-1" in call["url"]
    assert "tenant_slug=tenant_a" in call["url"]
    assert "fact_type=preference" in call["url"]
    assert "entity_id=op-9" in call["url"]
    assert "q=email" in call["url"]
    assert "limit=25" in call["url"]
    headers = _headers(call)
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == "tenant_a"
    assert "x-solstice-actor" not in headers
    assert call["body"] is None


def test_backend_client_remember_emits_exact_body_and_headers():
    """Direct contract smoke test: pins the remember wire shape to the Backend contract."""
    from solstice_mcp.memory_client import ActorEnvelope

    opener = FakeBackendOpener()
    _set_remember_response(opener)
    client = _direct_client(opener)
    actor = ActorEnvelope(actor_sub="sub-1", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u-1")
    client.remember(
        actor=actor,
        scope="personal",
        fact_type="decision",
        statement="ship Q3",
        source_refs=[{"source_type": "claim", "source_id": "c1", "source_version": "v1", "fingerprint": "fp"}],
        entity_refs=[{"entity_type": "brand", "entity_id": BRAND_A1, "entity_version": "v1"}],
        expires_at="2027-01-01T00:00:00Z",
        reason="user confirmed",
    )

    call = opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory"
    headers = _headers(call)
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == "tenant_a"
    assert "x-solstice-actor" not in headers
    body = json.loads(call["body"])
    assert body == {
        "brand_id": BRAND_A1,
        "scope": "personal",
        "fact_type": "decision",
        "statement": "ship Q3",
        "source_refs": [{"source_type": "claim", "source_id": "c1", "source_version": "v1", "fingerprint": "fp"}],
        "entity_refs": [{"entity_type": "brand", "entity_id": BRAND_A1, "entity_version": "v1"}],
        "expires_at": "2027-01-01T00:00:00Z",
        "reason": "user confirmed",
        "actor_sub": "sub-1",
        "tenant_slug": "tenant_a",
    }


def test_backend_client_activity_emits_exact_body_and_headers():
    opener = FakeBackendOpener()
    opener.responses[("POST", "/api/internal/agent-memory/activity")] = (
        200,
        b'{"id":"event-1"}',
    )
    client = _direct_client(opener)

    client.record_activity(
        actor_sub="sub-1",
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        tool_name="solstice_operation_info",
        outcome="success",
        project_id="project-1",
        operation_id="operation-1",
        message_id="message-1",
        occurred_at="2026-07-23T12:00:00+00:00",
        host_correlation_id="host-1",
        idempotency_key="event-1",
    )

    call = opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory/activity"
    assert _headers(call) == {
        "authorization": "Bearer m2m-bearer",
        "accept": "application/json",
        "x-tenant-slug": "tenant_a",
        "content-type": "application/json",
    }
    assert json.loads(call["body"]) == {
        "actor_sub": "sub-1",
        "tenant_slug": "tenant_a",
        "brand_id": BRAND_A1,
        "tool_name": "solstice_operation_info",
        "outcome": "success",
        "project_id": "project-1",
        "operation_id": "operation-1",
        "message_id": "message-1",
        "occurred_at": "2026-07-23T12:00:00+00:00",
        "host_correlation_id": "host-1",
        "idempotency_key": "event-1",
    }


def test_backend_client_recent_work_emits_exact_query_and_headers():
    opener = FakeBackendOpener()
    opener.responses[("GET", "/api/internal/agent-memory/recent-work")] = (
        200,
        b'{"items":[]}',
    )
    client = _direct_client(opener)

    assert client.list_recent_work(
        actor_sub="auth0|sub",
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        limit=20,
    ) == {"items": []}

    call = opener.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/api/internal/agent-memory/recent-work"
    assert "actor_sub=auth0%7Csub" in call["url"]
    assert "tenant_slug=tenant_a" in call["url"]
    assert f"brand_id={BRAND_A1}" in call["url"]
    assert "limit=20" in call["url"]
    assert call["body"] is None
    assert _headers(call) == {
        "authorization": "Bearer m2m-bearer",
        "accept": "application/json",
        "x-tenant-slug": "tenant_a",
    }










