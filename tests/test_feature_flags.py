"""Flags must never be the reason a tool call fails, and the override must
work when the provider is what has gone wrong."""

from __future__ import annotations

import pytest

from solstice_mcp import feature_flags


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", raising=False)


def test_flag_returns_its_default_without_a_provider():
    assert feature_flags.get_flag("anything", default=False) is False
    assert feature_flags.get_flag("anything", default=True) is True


def test_a_provider_that_raises_does_not_reach_the_caller(monkeypatch):
    class _Boom:
        def get_boolean_value(self, *_, **__):
            raise RuntimeError("remote config unavailable")

    monkeypatch.setattr(feature_flags, "get_flag", feature_flags.get_flag)
    import openfeature.api as api

    monkeypatch.setattr(api, "get_client", lambda: _Boom())

    assert feature_flags.get_flag("prc_writes_via_backend", default=False) is False


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("off", False), ("no", False)])
def test_environment_override_wins_over_the_provider(monkeypatch, value, expected):
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", value)

    assert feature_flags.prc_writes_via_backend(tenant_slug="acme") is expected


def test_an_unset_override_falls_through(monkeypatch):
    monkeypatch.setenv("MCP_FLAG_PRC_WRITES_VIA_BACKEND", "  ")

    assert feature_flags.prc_writes_via_backend(tenant_slug="acme") is False


def test_init_is_safe_without_the_agent():
    feature_flags.init_feature_flags()
