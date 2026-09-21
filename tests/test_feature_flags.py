"""Flags must never be the reason a tool call fails, and the override must
work when the provider is what has gone wrong."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from solstice_mcp import feature_flags


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("MCP_FLAG_SOME_ROLLOUT", raising=False)


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

    assert feature_flags.get_flag("some_rollout", default=False) is False


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("off", False), ("no", False)])
def test_environment_override_wins_over_the_provider(monkeypatch, value, expected):
    monkeypatch.setenv("MCP_FLAG_SOME_ROLLOUT", value)

    assert feature_flags.get_flag("some_rollout", default=not expected) is expected


def test_an_unset_override_falls_through(monkeypatch):
    monkeypatch.setenv("MCP_FLAG_SOME_ROLLOUT", "  ")

    assert feature_flags.get_flag("some_rollout", default=False) is False


def test_init_is_safe_without_the_agent():
    feature_flags.init_feature_flags()


def test_the_entry_point_registers_the_provider():
    """Nothing else does, and an unregistered provider fails silently: every
    flag keeps reading its code default and the rollout has no dial."""
    entry_point = Path(__file__).resolve().parents[1] / "mcp_main.py"
    assert entry_point.exists(), entry_point

    called = {
        node.func.id
        for node in ast.walk(ast.parse(entry_point.read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "init_feature_flags" in called
