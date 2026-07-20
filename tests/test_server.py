from __future__ import annotations

import io
import json
from typing import Any

import pytest
from conftest import DELETED_SUB, OTHER_SUB, SHARED_SUB, TEST_ISSUER, TEST_RESOURCE, AppHarness

from solstice_mcp.auth import fetch_jwks
from solstice_mcp.tenants import (
    TenantMembershipCache,
    current_tenant,
    discover_tenants_for_sub,
    resolve_tenant_identity,
)

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def test_fetch_jwks_reads_response_before_close(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    response = Response(b'{"keys": [{"kid": "one"}]}')
    monkeypatch.setattr("solstice_mcp.auth.urllib.request.urlopen", lambda *_args, **_kwargs: response)

    assert fetch_jwks("https://auth.example/.well-known/jwks.json") == {"keys": [{"kid": "one"}]}
    assert response.closed


def rpc(
    harness: AppHarness,
    method: str,
    *,
    token: str | None,
    params: dict[str, Any] | None = None,
):
    headers = {**MCP_HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return harness.client.post("/mcp", headers=headers, json=body)


def tool_payload(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_health_and_protected_resource_metadata_are_public(app_harness: AppHarness):
    health = app_harness.client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "solstice-mcp", "version": "1.0.0"}

    metadata = app_harness.client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == TEST_RESOURCE
    assert TEST_ISSUER in metadata.json()["authorization_servers"]
    assert "mcp:connect" in metadata.json()["scopes_supported"]
    assert set(app_harness.registry.slugs) == {"tenant_a", "tenant_b", "tenant_prod"}


@pytest.mark.parametrize(
    ("token_kwargs", "status"),
    [
        ({"aud": "https://wrong.test/mcp"}, 401),
        ({"exp_delta": -60}, 401),
        ({"scope": ""}, 403),
    ],
)
def test_auth_rejects_wrong_audience_expiry_and_scope(app_harness: AppHarness, mint_token, token_kwargs, status):
    response = rpc(app_harness, "tools/list", token=mint_token(**token_kwargs))
    assert response.status_code == status
    challenge = response.headers["www-authenticate"]
    assert "invalid_token" in challenge if status == 401 else "insufficient_scope" in challenge


def test_missing_token_advertises_rfc9728_metadata(app_harness: AppHarness):
    response = rpc(app_harness, "tools/list", token=None)
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_initialize_and_tool_discovery(app_harness: AppHarness, mint_token):
    initialized = rpc(
        app_harness,
        "initialize",
        token=mint_token(),
        params={
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "solstice-mcp"

    tools = rpc(app_harness, "tools/list", token=mint_token())
    names = {tool["name"] for tool in tools.json()["result"]["tools"]}
    assert names == {
        "solstice_server_info",
        "solstice_list_tenants",
        "solstice_whoami",
        "solstice_slack_search",
        "solstice_slack_read",
        "solstice_slack_send",
        "solstice_slack_react",
    }


def test_multi_tenant_discovery_filters_runtime_environment(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_tenants", "arguments": {}},
    )
    payload = tool_payload(response)
    assert {tenant["slug"] for tenant in payload["tenants"]} == {"tenant_a", "tenant_b"}
    assert payload["count"] == 2


def test_cross_tenant_membership_is_rejected(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=OTHER_SUB),
        params={"name": "solstice_whoami", "arguments": {"tenant_slug": "tenant_b"}},
    )
    assert tool_payload(response)["status"] == "not_member"


def test_soft_deleted_user_is_excluded(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=DELETED_SUB),
        params={"name": "solstice_list_tenants", "arguments": {}},
    )
    assert tool_payload(response) == {"tenants": [], "count": 0}


def test_membership_cache_avoids_second_scan(app_harness: AppHarness, mint_token):
    params = {"name": "solstice_list_tenants", "arguments": {}}
    assert rpc(app_harness, "tools/call", token=mint_token(), params=params).status_code == 200
    calls_after_first = app_harness.calls.copy()
    assert rpc(app_harness, "tools/call", token=mint_token(), params=params).status_code == 200
    assert app_harness.calls == calls_after_first


def test_context_is_cleared_when_database_factory_fails(app_harness: AppHarness):
    def fail(_slug: str):
        raise RuntimeError("database unavailable")

    current_tenant.set("stale")
    memberships = discover_tenants_for_sub(
        SHARED_SUB,
        registry=app_harness.registry,
        session_factory=fail,
        cache=TenantMembershipCache(ttl_seconds=60, max_entries=2),
        tenant_environment="development",
        slugs=["tenant_a"],
    )
    assert memberships == []
    assert current_tenant.get() is None

    with pytest.raises(RuntimeError, match="database unavailable"):
        resolve_tenant_identity(
            SHARED_SUB,
            "tenant_a",
            registry=app_harness.registry,
            session_factory=fail,
            tenant_environment="development",
        )
    assert current_tenant.get() is None


@pytest.mark.parametrize(
    ("name", "arguments", "false_field", "empty_field"),
    [
        ("solstice_slack_search", {"query": "q"}, None, "results"),
        ("solstice_slack_read", {"channel": "C1"}, None, "messages"),
        ("solstice_slack_send", {"channel": "C1", "message": "hello"}, "sent", None),
        (
            "solstice_slack_react",
            {"channel": "C1", "timestamp": "1.2", "emoji": "thumbsup"},
            "reacted",
            None,
        ),
    ],
)
def test_slack_stubs_are_truthful(app_harness: AppHarness, mint_token, name, arguments, false_field, empty_field):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={"name": name, "arguments": arguments},
    )
    payload = tool_payload(response)
    assert payload["status"] == "not_connected"
    assert payload["connected"] is False
    if false_field:
        assert payload[false_field] is False
    if empty_field:
        assert payload[empty_field] == []
