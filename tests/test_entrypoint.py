"""The module gunicorn actually loads.

Every other suite builds the app through ``build_mcp_app``. Production runs
``mcp_main:app``, and the process-level setup — audit logging, the flag
provider — exists only there. Nothing covered it, and an unregistered flag
provider fails silently: every flag keeps reading its code default.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def entrypoint(monkeypatch):
    """Import mcp_main with its three side effects recorded, not performed."""
    called: list[str] = []
    monkeypatch.syspath_prepend(str(_ROOT))
    for module, name in (
        ("solstice_mcp.audit", "configure_audit_logging"),
        ("solstice_mcp.feature_flags", "init_feature_flags"),
        ("solstice_mcp.app", "build_asgi_app"),
    ):
        monkeypatch.setattr(
            importlib.import_module(module), name, lambda _n=name: called.append(_n)
        )
    sys.modules.pop("mcp_main", None)
    importlib.import_module("mcp_main")
    sys.modules.pop("mcp_main", None)
    return called


def test_the_entry_point_performs_every_startup_step(entrypoint):
    assert set(entrypoint) == {
        "configure_audit_logging",
        "init_feature_flags",
        "build_asgi_app",
    }


def test_setup_runs_before_the_app_is_built(entrypoint):
    """A flag read during app construction must not see the NoOp provider."""
    assert entrypoint.index("init_feature_flags") < entrypoint.index("build_asgi_app")
    assert entrypoint.index("configure_audit_logging") < entrypoint.index("build_asgi_app")
