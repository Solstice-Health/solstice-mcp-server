"""SOL-3166 reliability: httpx retry, metrics, readiness, engine warm."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
from conftest import TEST_ISSUER, TEST_RESOURCE, AppHarness
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from solstice_mcp import http_client
from solstice_mcp.app import build_mcp_app
from solstice_mcp.auth import JWKSCache
from solstice_mcp.metrics import emit_tool_metrics, reset_for_tests
from solstice_mcp.settings import Settings
from solstice_mcp.tenants import TenantConfig, TenantDatabaseFactory, TenantRegistry


def test_http_client_retries_retryable_status_then_succeeds(monkeypatch):
    attempts: list[int] = []

    def fake_once(method, url, *, headers, content, timeout, client):
        attempts.append(1)
        if len(attempts) < 3:
            return http_client.HttpResponse(status_code=503, content=b"busy")
        return http_client.HttpResponse(status_code=200, content=b'{"ok":true}')

    monkeypatch.setattr(http_client, "_httpx_once", fake_once)
    monkeypatch.setattr(http_client, "_backoff", lambda _n: None)

    response = http_client.request("GET", "https://example.test/x", retries=3)
    assert response.status_code == 200
    assert json.loads(response.content)["ok"] is True
    assert len(attempts) == 3


def test_http_client_does_not_retry_client_errors(monkeypatch):
    attempts: list[int] = []

    def fake_once(method, url, *, headers, content, timeout, client):
        attempts.append(1)
        return http_client.HttpResponse(status_code=404, content=b"missing")

    monkeypatch.setattr(http_client, "_httpx_once", fake_once)

    response = http_client.request("GET", "https://example.test/x", retries=3)
    assert response.status_code == 404
    assert len(attempts) == 1


def test_http_client_retries_transport_errors(monkeypatch):
    attempts: list[int] = []

    def fake_once(method, url, *, headers, content, timeout, client):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("boom")
        return http_client.HttpResponse(status_code=200, content=b"{}")

    monkeypatch.setattr(http_client, "_httpx_once", fake_once)
    monkeypatch.setattr(http_client, "_backoff", lambda _n: None)

    response = http_client.request("GET", "https://example.test/x", retries=3)
    assert response.status_code == 200
    assert len(attempts) == 2


def test_emit_tool_metrics_sends_dogstatsd_payload(monkeypatch):
    reset_for_tests()
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSock:
        def sendto(self, payload: bytes, endpoint: tuple[str, int]) -> int:
            sent.append((payload, endpoint))
            return len(payload)

        def setblocking(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.delenv("DD_DOGSTATSD_DISABLE", raising=False)
    monkeypatch.setenv("DD_AGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("DD_DOGSTATSD_PORT", "8125")
    monkeypatch.setattr("solstice_mcp.metrics._socket", lambda: FakeSock())

    emit_tool_metrics(tool="solstice_whoami", outcome="success", duration_ms=12.5)
    assert sent
    body = sent[0][0].decode("utf-8")
    assert "mcp.tool.calls:1|c" in body
    assert "tool:solstice_whoami" in body
    assert "outcome:success" in body
    assert "mcp.tool.duration_ms:12.500|h" in body
    reset_for_tests()


def test_emit_tool_metrics_disabled(monkeypatch):
    reset_for_tests()
    monkeypatch.setenv("DD_DOGSTATSD_DISABLE", "true")
    with patch("solstice_mcp.metrics._socket") as sock_factory:
        emit_tool_metrics(tool="solstice_whoami", outcome="success", duration_ms=1.0)
        sock_factory.assert_not_called()


def test_ready_returns_ready_when_db_ping_ok(app_harness: AppHarness):
    response = app_harness.client.get("/mcp/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "solstice-mcp"
    assert body["probe_tenant"] in app_harness.registry.slugs


def test_ready_returns_503_when_session_factory_raises(tmp_path, signing_material):
    _private_pem, jwks = signing_material
    tenant_file = tmp_path / "tenants.json"
    tenant_file.write_text(
        json.dumps(
            {
                "tenant_a": {
                    "db_name": "tenant_a",
                    "env": "development",
                    "s3_bucket": "bucket-a",
                }
            }
        ),
        encoding="utf-8",
    )
    registry = TenantRegistry()
    registry.load(tenant_file)

    def boom(_slug: str) -> Session:
        raise RuntimeError("db down")

    settings = Settings(
        ENV="development",
        AUTH0_DOMAIN="test.auth0.local",
        MCP_RESOURCE_URL=TEST_RESOURCE,
        TENANT_CONFIG_PATH=str(tenant_file),
    )
    mcp = build_mcp_app(
        runtime_settings=settings,
        registry=registry,
        session_factory=boom,
        jwks_cache=JWKSCache(f"{TEST_ISSUER}.well-known/jwks.json", initial=jwks),
    )
    with TestClient(mcp.streamable_http_app(), base_url="https://mcp.test.local") as client:
        response = client.get("/mcp/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["reason"] == "db_unreachable"


def test_tenant_database_factory_warm_skips_failures(tmp_path):
    registry = TenantRegistry()
    registry._tenants = {
        "ok": TenantConfig(slug="ok", db_name="ok", env="development"),
        "bad": TenantConfig(slug="bad", db_name="bad", env="development"),
    }
    factory = TenantDatabaseFactory(
        registry,
        {"development": f"sqlite:///{tmp_path}/{{db_name}}.db"},
    )
    original = factory.__call__

    def flaky(slug: str) -> Session:
        if slug == "bad":
            raise RuntimeError("nope")
        return original(slug)

    factory.__call__ = flaky  # type: ignore[method-assign]
    factory.warm()  # must not raise
    assert "ok" in factory._sessions


def test_health_still_shallow_without_ready_fields(app_harness: AppHarness):
    health = app_harness.client.get("/mcp/health")
    assert health.status_code == 200
    assert "probe_tenant" not in health.json()
    assert health.json()["status"] == "ok"
