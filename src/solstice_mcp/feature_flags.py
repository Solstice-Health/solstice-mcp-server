"""Feature flag plumbing for the MCP, with the same interface the Backend uses.

Flag names, their defaults, and the targeting context each one needs belong to
the service that reads them; this module only knows how to ask.

Flag values arrive through the Datadog agent's Remote Configuration — the same
sidecar this task already sends metrics to — evaluated by OpenFeature. Every
read falls back to the code default: before init, on any provider error, and on
a deployment where the agent is not reachable. A flag must never be the reason a
tool call fails.

Each flag also honours an environment override, checked before the provider.
Remote Configuration is the dial for a staged rollout; the override is the kill
switch that works when Remote Configuration is what has gone wrong, and it is
the only way to pin a value in a local or test process.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _override_key(name: str) -> str:
    return f"MCP_FLAG_{name.upper().replace('-', '_')}"


def _override(name: str) -> bool | None:
    raw = os.getenv(_override_key(name), "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return None


def init_feature_flags() -> None:
    """Register the Datadog OpenFeature provider, or leave the default NoOp.

    Safe to call unconditionally at startup: when ddtrace is absent or the
    provider cannot start, every ``get_flag`` keeps returning its code default,
    which every caller already handles.
    """
    try:
        # Starts ddtrace's products, including the Remote Configuration callback
        # the provider reads from. Without the preload the provider exists but
        # no flag config ever reaches it.
        import ddtrace.bootstrap.preload  # noqa: F401
        from ddtrace import tracer
        from ddtrace.openfeature import DataDogProvider
        from openfeature import api

        tracer.configure()
        api.set_provider(DataDogProvider())
        logger.info("Datadog OpenFeature provider registered")
    except Exception as exc:
        logger.warning("feature flag provider unavailable; flags return defaults: %s", exc)


def get_flag(name: str, *, default: bool, context: dict[str, Any] | None = None) -> bool:
    """Read one boolean flag. Returns ``default`` on any failure.

    ``context`` carries the targeting attributes — ``tenant_slug`` and
    ``brand_id`` — so a rollout can be staged per tenant rather than flipped
    for everyone at once.
    """
    override = _override(name)
    if override is not None:
        return override
    try:
        from openfeature import api
        from openfeature.evaluation_context import EvaluationContext

        return api.get_client().get_boolean_value(
            name, default, evaluation_context=EvaluationContext(attributes=dict(context or {}))
        )
    except Exception as exc:
        logger.debug("flag %r evaluation failed; returning %s: %s", name, default, exc)
        return default
