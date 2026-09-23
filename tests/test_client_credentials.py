"""Token acquisition: caching, and the codes a grant failure carries.

Both the Backend repositories and the Auth0 Management client hold one of
these, so a change here is not a memory change.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from solstice_mcp import http_client
from solstice_mcp.client_credentials import (
    Auth0ClientCredentials,
    CredentialsRejected,
    CredentialsUnavailable,
)


def _make_acquirer() -> Auth0ClientCredentials:
    return Auth0ClientCredentials(
        token_endpoint="https://test.auth0.local/oauth/token",
        client_id="m2m-client",
        client_secret="m2m-secret",
        audience="https://backend.test",
        scope="memory:invoke",
        timeout=2.0,
    )


def _fake_token_request(body: bytes, *, status: int = 200):
    from solstice_mcp.http_client import HttpResponse

    def _request(method, url, **kwargs):
        assert method == "POST"
        assert url == "https://test.auth0.local/oauth/token"
        headers = kwargs.get("headers") or {}
        content = kwargs.get("content") or b""
        assert headers.get("Content-Type") == "application/x-www-form-urlencoded"
        decoded = content.decode("utf-8")
        assert "grant_type=client_credentials" in decoded
        assert "client_id=m2m-client" in decoded
        return HttpResponse(status_code=status, content=body)

    return _request


def test_client_credentials_caches_until_near_expiry(monkeypatch):
    calls: list[int] = []

    def fake_request(method, url, **kwargs):
        calls.append(1)
        return _fake_token_request(
            b'{"access_token":"tok-1","expires_in":3600,"token_type":"Bearer"}'
        )(method, url, **kwargs)

    monkeypatch.setattr("solstice_mcp.client_credentials.http_client.request", fake_request)
    acquirer = _make_acquirer()
    assert acquirer.get_token() == "tok-1"
    assert acquirer.get_token() == "tok-1"
    assert len(calls) == 1


def test_client_credentials_refetches_after_expiry(monkeypatch):
    calls: list[int] = []

    def fake_request(method, url, **kwargs):
        calls.append(1)
        return _fake_token_request(
            b'{"access_token":"tok-N","expires_in":3600,"token_type":"Bearer"}'
        )(method, url, **kwargs)

    monkeypatch.setattr("solstice_mcp.client_credentials.http_client.request", fake_request)
    acquirer = _make_acquirer()
    acquirer.get_token()
    assert len(calls) == 1
    acquirer._expires_at = 0.0  # force expiry
    acquirer.get_token()
    assert len(calls) == 2


def test_client_credentials_rejects_invalid_response(monkeypatch):
    monkeypatch.setattr(
        "solstice_mcp.client_credentials.http_client.request",
        _fake_token_request(b'{"access_token":"tok"}'),  # no expires_in
    )
    acquirer = _make_acquirer()
    with pytest.raises(CredentialsRejected, match="auth0_token_response_invalid"):
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
    monkeypatch.setattr(
        "solstice_mcp.client_credentials.http_client.request",
        _fake_token_request(b'{"error":"invalid_client"}', status=401),
    )
    acquirer = _make_acquirer()
    with pytest.raises(CredentialsRejected, match="auth0_token_endpoint_failed"):
        acquirer.get_token()


def test_client_credentials_maps_unreachable(monkeypatch):
    def fake_request(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("solstice_mcp.client_credentials.http_client.request", fake_request)
    acquirer = _make_acquirer()
    with pytest.raises(CredentialsUnavailable, match="auth0_token_endpoint_unreachable"):
        acquirer.get_token()


def test_client_credentials_reuses_pooled_httpx_client(monkeypatch):
    calls: list[Any] = []

    def fake_once(method, url, *, headers, content, timeout, client):
        calls.append(client)
        return http_client.HttpResponse(
            status_code=200,
            content=b'{"access_token": "tok", "expires_in": 3600}',
        )

    monkeypatch.setattr(http_client, "_httpx_once", fake_once)
    acquirer = _make_acquirer()
    acquirer.get_token()
    acquirer.invalidate()
    acquirer.get_token()

    assert len(calls) == 2
    assert calls[0] is not None  # real pooled client, not client=None
    assert calls[0] is calls[1]  # same instance reused, not recreated per fetch
