from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
from conftest import BRAND_A3, DELETED_SUB, OP_A1, OTHER_SUB, SHARED_SUB, TEST_ISSUER, TEST_RESOURCE, AppHarness

from solstice_mcp.audit import AUDIT_EVENT_NAME, AUDIT_LOGGER_NAME
from solstice_mcp.auth import fetch_jwks
from solstice_mcp.gate import SolsticeAccessGate
from solstice_mcp.settings import Settings
from solstice_mcp.tenants import (
    TenantDatabaseFactory,
    TenantMembershipCache,
    TenantRegistry,
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


def test_settings_from_env_coerces_numeric_values(monkeypatch):
    monkeypatch.setenv("S3_PRESIGN_EXPIRY_SECONDS", "900")
    monkeypatch.setenv("S3_MAX_INLINE_BYTES", "1234")

    parsed = Settings.from_env()

    assert parsed.S3_PRESIGN_EXPIRY_SECONDS == 900
    assert parsed.S3_MAX_INLINE_BYTES == 1234


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


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME and json.loads(record.message).get("event") == AUDIT_EVENT_NAME
    ]


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
    listed_tools = tools.json()["result"]["tools"]
    names = {tool["name"] for tool in listed_tools}
    assert names == {
        "solstice_server_info",
        "solstice_list_tenants",
        "solstice_whoami",
        "solstice_check_access",
        "solstice_list_sibling_mcps",
        "solstice_list_brands",
        "solstice_brand_info",
        "solstice_brand_rules",
        "solstice_brand_design_assets",
        "solstice_brand_claims",
        "solstice_list_projects",
        "solstice_project_info",
        "solstice_list_operations",
        "solstice_operation_info",
        "solstice_operation_messages",
        "solstice_operation_html",
        "solstice_create_operation",
        "solstice_create_edit_operation",
        "solstice_prepare_operation_version",
        "solstice_commit_operation_version",
        "solstice_list_brand_users",
        "solstice_update_operation",
        "solstice_approve_operation_version",
        "solstice_list_requests",
        "solstice_dismiss_request",
        "solstice_memory_recall",
        "solstice_list_recent_work",
        "solstice_memory_observe",
        "solstice_memory_remember",
        "solstice_memory_replace",
        "solstice_memory_forget",
    }
    non_destructive_writes = {
        "solstice_create_operation",
        "solstice_create_edit_operation",
        "solstice_prepare_operation_version",
        "solstice_commit_operation_version",
        "solstice_memory_observe",
        "solstice_memory_remember",
        "solstice_memory_replace",
        "solstice_memory_forget",
    }
    updates_in_place = {
        "solstice_update_operation",
        "solstice_approve_operation_version",
        "solstice_dismiss_request",
    }
    # Dismiss is a one-way status flip: retrying is rejected, so unlike the
    # other in-place updates it is not idempotent.
    non_idempotent_updates = {"solstice_dismiss_request"}
    for tool in listed_tools:
        is_write = tool["name"] in non_destructive_writes
        is_update = tool["name"] in updates_in_place
        assert tool["annotations"] == {
            "readOnlyHint": not (is_write or is_update),
            "destructiveHint": is_update,
            "idempotentHint": not (is_write or tool["name"] in non_idempotent_updates),
            "openWorldHint": False,
        }


def test_tool_audit_logs_identity_resources_and_outcome_without_payloads(
    app_harness: AppHarness,
    mint_token,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(email="private@example.com"),
        params={"name": "solstice_whoami", "arguments": {"tenant_slug": "tenant_a"}},
    )
    assert tool_payload(response)["status"] == "ok"

    event = audit_events(caplog)[-1]
    assert event["subject"] == SHARED_SUB
    assert event["client_id"] == "cursor-test-client"
    assert event["tool"] == "solstice_whoami"
    assert event["resources"] == {"tenant_slug": "tenant_a"}
    assert event["outcome"] == "success"
    assert "private@example.com" not in json.dumps(event)


def test_tool_audit_classifies_authorization_denials(
    app_harness: AppHarness,
    mint_token,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

    rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A3},
        },
    )

    event = audit_events(caplog)[-1]
    assert event["tool"] == "solstice_brand_info"
    assert event["outcome"] == "denied"
    assert event["error_code"] == "not_authorized"
    assert event["resources"] == {"tenant_slug": "tenant_a", "brand_id": BRAND_A3}


def test_tool_audit_classifies_tool_errors(
    app_harness: AppHarness,
    mint_token,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

    # An unknown message on an operation the caller CAN access is a plain
    # not_found (unknown project/operation ids are uniform not_authorized
    # denies, so they cannot be used to exercise the "error" outcome).
    rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={
            "name": "solstice_operation_html",
            "arguments": {
                "tenant_slug": "tenant_a",
                "operation_id": OP_A1,
                "message_id": "no-such-message",
            },
        },
    )

    event = audit_events(caplog)[-1]
    assert event["tool"] == "solstice_operation_html"
    assert event["outcome"] == "error"
    assert event["error_code"] == "not_found"
    assert event["resources"] == {
        "tenant_slug": "tenant_a",
        "operation_id": OP_A1,
        "message_id": "no-such-message",
        "fetch": False,
    }


def test_cross_environment_discovery_returns_all_tenants_with_user(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_tenants", "arguments": {}},
    )
    payload = tool_payload(response)
    assert {tenant["slug"] for tenant in payload["tenants"]} == {
        "tenant_a",
        "tenant_b",
        "tenant_prod",
    }
    assert payload["count"] == 3
    envs = {tenant["env"] for tenant in payload["tenants"]}
    assert envs == {"development", "production"}


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


def test_unreachable_database_is_skipped_in_discovery(app_harness: AppHarness):
    def fail(_slug: str):
        raise RuntimeError("database unavailable")

    memberships = discover_tenants_for_sub(
        SHARED_SUB,
        registry=app_harness.registry,
        session_factory=fail,
        cache=TenantMembershipCache(ttl_seconds=60, max_entries=2),
        slugs=["tenant_a"],
    )
    assert memberships == []

    with pytest.raises(RuntimeError, match="database unavailable"):
        resolve_tenant_identity(
            SHARED_SUB,
            "tenant_a",
            registry=app_harness.registry,
            session_factory=fail,
        )


def test_tenant_database_factory_routes_by_env(tmp_path):
    import json

    config_path = tmp_path / "tenants.json"
    config_path.write_text(
        json.dumps(
            {
                "tenant_dev": {"db_name": "tenant_dev", "env": "development"},
                "tenant_prod": {"db_name": "tenant_prod", "env": "production"},
            }
        )
    )
    registry = TenantRegistry()
    registry.load(config_path)

    seen: dict[str, str] = {}

    class _RecordingEngine:
        def __init__(self, url: str) -> None:
            self.url = url

        def connect(self, *_args, **_kwargs):  # pragma: no cover - not exercised
            raise RuntimeError("connect should not be called in this test")

    import sqlalchemy

    real_create_engine = sqlalchemy.create_engine

    def fake_create_engine(url: str, **_kwargs):
        seen[url] = url
        # Return a real in-memory sqlite engine so sessionmaker() works downstream.
        return real_create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    from sqlalchemy.pool import StaticPool

    factory = TenantDatabaseFactory(
        registry,
        {
            "development": "postgresql+psycopg://dev-host:5432/{db_name}",
            "production": "postgresql+psycopg://prod-host:5432/{db_name}",
        },
    )

    import solstice_mcp.tenants as tenants_mod

    original = tenants_mod.create_engine
    tenants_mod.create_engine = fake_create_engine
    try:
        factory("tenant_dev")
        factory("tenant_prod")
        # Re-call should reuse the cached sessionmaker, not create a new engine.
        factory("tenant_dev")
    finally:
        tenants_mod.create_engine = original

    assert seen == {
        "postgresql+psycopg://dev-host:5432/tenant_dev": "postgresql+psycopg://dev-host:5432/tenant_dev",
        "postgresql+psycopg://prod-host:5432/tenant_prod": "postgresql+psycopg://prod-host:5432/tenant_prod",
    }


def test_tenant_database_factory_rejects_missing_env(tmp_path):
    import json

    config_path = tmp_path / "tenants.json"
    config_path.write_text(json.dumps({"tenant_dev": {"db_name": "tenant_dev", "env": "development"}}))
    registry = TenantRegistry()
    registry.load(config_path)

    factory = TenantDatabaseFactory(
        registry,
        {"production": "postgresql+psycopg://prod-host:5432/{db_name}"},
    )
    with pytest.raises(ValueError, match="No database URL template registered for env 'development'"):
        factory("tenant_dev")


def test_tenant_database_factory_rejects_empty_templates(tmp_path):
    registry = TenantRegistry()
    with pytest.raises(ValueError, match="At least one database URL template is required"):
        TenantDatabaseFactory(registry, {})


def test_solstice_access_gate_allows_solstice_domain():
    gate = SolsticeAccessGate(allowed_domain="@solsticehealth.co")
    decision = gate.evaluate("auth0|alice", "alice@solsticehealth.co")
    assert decision.allowed is True
    assert decision.email == "alice@solsticehealth.co"
    assert decision.reason == "email domain allowed"


def test_solstice_access_gate_denies_other_domain():
    gate = SolsticeAccessGate(allowed_domain="@solsticehealth.co")
    decision = gate.evaluate("auth0|eve", "eve@example.com")
    assert decision.allowed is False
    assert decision.email == "eve@example.com"
    assert decision.reason == "email domain not allowed"


def test_solstice_access_gate_denies_missing_email():
    gate = SolsticeAccessGate(allowed_domain="@solsticehealth.co")
    decision = gate.evaluate("auth0|anon", None)
    assert decision.allowed is False
    assert decision.email is None
    assert decision.reason == "missing email claim"


def test_solstice_access_gate_is_case_insensitive():
    gate = SolsticeAccessGate(allowed_domain="@solsticehealth.co")
    decision = gate.evaluate("auth0|bob", "Bob@SolsticeHealth.co")
    assert decision.allowed is True
    assert decision.email == "Bob@SolsticeHealth.co"


def test_solstice_access_gate_cache_hit_returns_same_decision():
    gate = SolsticeAccessGate(allowed_domain="@solsticehealth.co", ttl_seconds=60, max_entries=8)
    first = gate.evaluate("auth0|alice", "alice@solsticehealth.co")
    second = gate.evaluate("auth0|alice", "alice@solsticehealth.co")
    assert first is second


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"allowed_domain": "", "ttl_seconds": 60, "max_entries": 8}, "allowed_domain is required"),
        ({"allowed_domain": "@x.co", "ttl_seconds": 0, "max_entries": 8}, "Cache TTL and size must be positive"),
        ({"allowed_domain": "@x.co", "ttl_seconds": 60, "max_entries": 0}, "Cache TTL and size must be positive"),
    ],
)
def test_solstice_access_gate_rejects_invalid_config(kwargs, match):
    with pytest.raises(ValueError, match=match):
        SolsticeAccessGate(**kwargs)


@pytest.mark.parametrize(
    ("email", "allowed"),
    [
        ("alice@solsticehealth.co", True),
        ("Alice@SolsticeHealth.co", True),
        ("eve@example.com", False),
    ],
)
def test_solstice_check_access_integration(app_harness: AppHarness, mint_token, email, allowed):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(email=email),
        params={"name": "solstice_check_access", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is allowed
    assert payload["allowed_domain"] == "@solsticehealth.co"
    if allowed:
        assert payload["email"] == email
        assert payload["reason"] == "email domain allowed"
    else:
        assert payload["email"] == email
        assert payload["reason"] == "email domain not allowed"


def test_solstice_check_access_falls_back_to_tenant_db_email(app_harness: AppHarness, mint_token):
    # Gateway-minted tokens carry no email claim; the gate must resolve the
    # email from the subject's tenant user row (same source as whoami) instead
    # of denying with "missing email claim". SHARED_SUB is alice@a.test in
    # tenant_a — resolvable, but outside the allowed domain.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(email=None),
        params={"name": "solstice_check_access", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is False
    assert payload["email"] == "alice@a.test"
    assert payload["reason"] == "email domain not allowed"


def test_solstice_check_access_missing_everywhere(app_harness: AppHarness, mint_token):
    # No email claim AND no tenant membership (soft-deleted user) -> the
    # fallback finds nothing and the gate reports the missing claim.
    from conftest import DELETED_SUB

    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=DELETED_SUB, email=None),
        params={"name": "solstice_check_access", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is False
    assert payload["email"] is None
    assert payload["reason"] == "missing email claim"


def test_solstice_list_sibling_mcps_allowed_user_sees_directory(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(email="alice@solsticehealth.co"),
        params={"name": "solstice_list_sibling_mcps", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is True
    names = {entry["name"] for entry in payload["sibling_mcps"]}
    assert names == {"linear", "slack"}
    assert payload["count"] == 2


def test_solstice_list_sibling_mcps_denied_user_sees_empty_list(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(email="eve@example.com"),
        params={"name": "solstice_list_sibling_mcps", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is False
    assert payload["sibling_mcps"] == []


def test_solstice_list_sibling_mcps_missing_email_sees_empty_list(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={"name": "solstice_list_sibling_mcps", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["allowed"] is False
    assert payload["sibling_mcps"] == []
