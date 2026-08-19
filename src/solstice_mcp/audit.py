"""Structured audit events for authenticated MCP tool calls.

Events identify the Auth0 subject, OAuth client, tool, outcome, duration, and
non-content resource selectors. Tool inputs and outputs are never logged.

Field names align with Backend Datadog Logs facets (service, env, tenant_slug,
user_email, user_id, request_id) so MCP usage is browsable in Logs Explorer
alongside ``service:solstice-backend``.

Performance: identity comes only from the already-verified access token
(claims in memory). No DB, HTTP, or Datadog API calls are made on the audit
path — one JSON line to stdout.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from solstice_mcp.metrics import emit_tool_metrics
from solstice_mcp.rate_limit import default_limiter

P = ParamSpec("P")
R = TypeVar("R")

AUDIT_LOGGER_NAME = "solstice_mcp.audit"
AUDIT_EVENT_NAME = "mcp_tool_audit"
AUTH_DENY_EVENT_NAME = "mcp_auth_denied"
# Tool-argument keys that are safe identity/resource selectors (never payloads).
AUDIT_RESOURCE_FIELDS = {
    "tenant_slug",
    "brand_id",
    "project_id",
    "operation_id",
    "message_id",
    "request_id",
    "status",
    "template_key",
    "content_type",
    "reason_category",
    "type",
    "fetch",
    "memory_id",
    "scope",
    # User-admin tools: audit the password-reset variant and the granted role
    # (argument values only — tool inputs/outputs stay unlogged).
    "mode",
    "new_role",
    "role",
}

logger = logging.getLogger(AUDIT_LOGGER_NAME)


def configure_audit_logging() -> None:
    """Write audit records as raw JSON independently of Gunicorn formatting."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _claim_str(claims: dict[str, Any] | None, *keys: str) -> str:
    if not isinstance(claims, dict):
        return ""
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _audit_env() -> str:
    explicit = os.getenv("DD_ENV", "").strip()
    if explicit:
        return explicit
    runtime = os.getenv("ENV", "").strip().lower()
    return "prod" if runtime in {"prod", "production"} else "dev"


def _build_audit_event(
    *,
    token: Any,
    tool_name: str,
    bound_arguments: dict[str, Any],
) -> dict[str, Any]:
    request_id = str(uuid4())
    claims = getattr(token, "claims", None)
    if not isinstance(claims, dict):
        claims = {}

    # Flatten safe selectors to top-level (Backend-style facets). Nested
    # ``resources`` is kept for CloudWatch Insights queries that still use it.
    resources = {
        name: value
        for name, value in bound_arguments.items()
        if name in AUDIT_RESOURCE_FIELDS and isinstance(value, (str, bool))
    }

    event: dict[str, Any] = {
        "service": os.getenv("DD_SERVICE", "solstice-mcp").strip() or "solstice-mcp",
        "env": _audit_env(),
        "event": AUDIT_EVENT_NAME,
        "event_id": request_id,
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "subject": token.subject,
        "client_id": token.client_id,
        # Token claims only — never resolve users from DB on the audit path.
        "user_id": _claim_str(claims, "https://solsticehealth.co/user_id", "user_id"),
        "user_email": _claim_str(claims, "email"),
        "user_name": _claim_str(claims, "name", "nickname"),
        "tool": tool_name,
        "resources": resources,
    }
    # Promote resource selectors for Datadog tag remappers (tenant_slug, etc.).
    for key, value in resources.items():
        if key == "request_id":
            # Tool-arg request_id (admin requests) must not overwrite audit request_id.
            event["admin_request_id"] = value
            continue
        event[key] = value
    return event


def emit_auth_denied(*, reason: str, error_type: str) -> None:
    """Structured auth failure for Datadog (never includes the bearer token)."""
    event = {
        "service": os.getenv("DD_SERVICE", "solstice-mcp").strip() or "solstice-mcp",
        "env": _audit_env(),
        "event": AUTH_DENY_EVENT_NAME,
        "event_id": str(uuid4()),
        "request_id": str(uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "outcome": "denied",
        "error_code": "invalid_token",
        "error_type": error_type,
        "reason": reason,
    }
    logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))


def audited_tool(
    mcp: FastMCP,
    require_access_token: Callable[[], Any],
    *,
    annotations: ToolAnnotations,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a tool that emits one payload-free audit event per invocation."""

    def register(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @wraps(function)
        async def audited(*args: P.args, **kwargs: P.kwargs) -> R:
            started_at = time.monotonic()
            token = require_access_token()
            bound = signature.bind_partial(*args, **kwargs)
            event = _build_audit_event(
                token=token,
                tool_name=function.__name__,
                bound_arguments=bound.arguments,
            )

            try:
                default_limiter.check(
                    subject=str(getattr(token, "subject", "") or ""),
                    client_id=str(getattr(token, "client_id", "") or "unknown"),
                    tool_name=function.__name__,
                )
                # Tool bodies do blocking I/O (SQLAlchemy, boto3). The MCP SDK
                # calls sync tools inline on the event loop, so offload to a
                # worker thread to keep one slow DB/S3 call from stalling every
                # concurrent request on this worker.
                result = await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
            except Exception as exc:
                # Re-raise after recording the outcome; tool error behavior is unchanged.
                error_code = str(exc).partition(":")[0]
                duration_ms = round((time.monotonic() - started_at) * 1000, 3)
                outcome = (
                    "denied"
                    if error_code in {"not_authorized", "rate_limited"}
                    else "error"
                )
                event.update(
                    outcome=outcome,
                    error_code=error_code,
                    error_type=type(exc).__name__,
                    duration_ms=duration_ms,
                )
                logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
                emit_tool_metrics(
                    tool=function.__name__, outcome=outcome, duration_ms=duration_ms
                )
                raise

            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            event.update(
                outcome="success",
                duration_ms=duration_ms,
            )
            logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
            emit_tool_metrics(
                tool=function.__name__, outcome="success", duration_ms=duration_ms
            )
            return result

        return mcp.tool(annotations=annotations)(audited)

    return register
