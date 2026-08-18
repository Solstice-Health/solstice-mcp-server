"""SOL-3165 hardening: rate limit, JWKS refresh throttle, auth-deny audit."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from conftest import BRAND_A1, SHARED_SUB
from mcp.server.fastmcp.exceptions import ToolError
from test_memory import TENANT, _call, _set_remember_response, _tool_error_text

from solstice_mcp.audit import AUDIT_LOGGER_NAME, AUTH_DENY_EVENT_NAME
from solstice_mcp.auth import JWKSCache, MCPAccessTokenVerifier
from solstice_mcp.rate_limit import RateLimiter


def test_rate_limiter_blocks_after_budget():
    limiter = RateLimiter(default_rpm=2, strict_rpm=1, window_seconds=60.0)
    limiter.check(subject="sub", client_id="c", tool_name="solstice_whoami")
    limiter.check(subject="sub", client_id="c", tool_name="solstice_whoami")
    with pytest.raises(ToolError, match="rate_limited"):
        limiter.check(subject="sub", client_id="c", tool_name="solstice_whoami")


def test_rate_limiter_strict_tools_use_separate_budget():
    limiter = RateLimiter(default_rpm=100, strict_rpm=1, window_seconds=60.0)
    limiter.check(subject="sub", client_id="c", tool_name="solstice_list_tenants")
    with pytest.raises(ToolError, match="rate_limited"):
        limiter.check(subject="sub", client_id="c", tool_name="solstice_list_tenants")
    # Default-tier tools remain available.
    limiter.check(subject="sub", client_id="c", tool_name="solstice_whoami")


def test_rate_limiter_prunes_idle_keys():
    limiter = RateLimiter(default_rpm=100, strict_rpm=100, window_seconds=60.0)
    for i in range(63):
        limiter.check(subject=f"sub-{i}", client_id="c", tool_name="solstice_whoami")
    assert len(limiter._hits) == 63
    for bucket in limiter._hits.values():
        bucket.clear()
        bucket.append(0.0)  # expired relative to monotonic now
    limiter._checks = 63
    limiter.check(subject="alive", client_id="c", tool_name="solstice_whoami")
    assert "alive\0c\0*" in limiter._hits
    assert all(not k.startswith("sub-") for k in limiter._hits)


def test_jwks_unknown_kid_refresh_throttled():
    fetches: list[str] = []

    def fake_fetch(url: str, *, timeout: float = 5.0) -> dict:
        fetches.append(url)
        return {"keys": [{"kid": "real", "kty": "RSA", "n": "x", "e": "AQAB"}]}

    cache = JWKSCache("https://example.test/jwks.json", ttl_seconds=300.0, initial=None)
    with patch("solstice_mcp.auth.fetch_jwks", side_effect=fake_fetch):
        first = cache.get()
        assert first["keys"][0]["kid"] == "real"
        assert len(fetches) == 1
        # Forced refresh within TTL is a no-op after the first successful fetch.
        second = cache.get(refresh=True)
        assert second is first
        assert len(fetches) == 1
        third = cache.get(refresh=True)
        assert third is first
        assert len(fetches) == 1


def test_verify_token_emits_auth_denied_event(caplog):
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    verifier = MCPAccessTokenVerifier(
        audience="https://example.test/mcp",
        issuer="https://example.test/",
        jwks_cache=JWKSCache(
            "https://example.test/jwks.json",
            initial={"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]},
        ),
    )
    result = asyncio.run(verifier.verify_token("not-a-jwt"))
    assert result is None
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME
    ]
    assert any(e.get("event") == AUTH_DENY_EVENT_NAME for e in events)
    deny = next(e for e in events if e.get("event") == AUTH_DENY_EVENT_NAME)
    assert deny["outcome"] == "denied"
    assert "token" not in deny
    assert deny["error_code"] == "invalid_token"


def test_remember_rejects_secret_shaped_statement(app_harness, mint_token):
    _set_remember_response(app_harness.backend_opener)
    response = _call(
        app_harness,
        mint_token(sub=SHARED_SUB),
        "solstice_memory_remember",
        {
            "tenant_slug": TENANT,
            "brand_id": BRAND_A1,
            "scope": "personal",
            "fact_type": "preference",
            "statement": "api_key=super-secret-value",
        },
    )
    assert "invalid_argument" in _tool_error_text(response)
    assert "credentials" in _tool_error_text(response)
    assert app_harness.backend_opener.calls == []
