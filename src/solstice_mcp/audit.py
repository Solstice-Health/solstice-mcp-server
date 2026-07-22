"""Structured audit events for authenticated MCP tool calls.

Events identify the Auth0 subject, OAuth client, tool, outcome, duration, and
non-content resource selectors. Tool inputs and outputs are never logged.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

P = ParamSpec("P")
R = TypeVar("R")

AUDIT_LOGGER_NAME = "solstice_mcp.audit"
AUDIT_EVENT_NAME = "mcp_tool_audit"
AUDIT_RESOURCE_FIELDS = {
    "tenant_slug",
    "brand_id",
    "project_id",
    "operation_id",
    "message_id",
    "request_id",
    "status",
    "reason_category",
    "type",
    "fetch",
}

logger = logging.getLogger(AUDIT_LOGGER_NAME)


def configure_audit_logging() -> None:
    """Write audit records as raw JSON independently of Gunicorn formatting."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


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
        def audited(*args: P.args, **kwargs: P.kwargs) -> R:
            started_at = time.monotonic()
            token = require_access_token()
            bound = signature.bind_partial(*args, **kwargs)
            resources = {
                name: value
                for name, value in bound.arguments.items()
                if name in AUDIT_RESOURCE_FIELDS and isinstance(value, (str, bool))
            }
            event = {
                "event": AUDIT_EVENT_NAME,
                "event_id": str(uuid4()),
                "timestamp": datetime.now(UTC).isoformat(),
                "subject": token.subject,
                "client_id": token.client_id,
                "tool": function.__name__,
                "resources": resources,
            }

            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                # Re-raise after recording the outcome; tool error behavior is unchanged.
                error_code = str(exc).partition(":")[0]
                event.update(
                    outcome="denied" if error_code == "not_authorized" else "error",
                    error_code=error_code,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000, 3),
                )
                logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
                raise

            event.update(
                outcome="success",
                duration_ms=round((time.monotonic() - started_at) * 1000, 3),
            )
            logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
            return result

        return mcp.tool(annotations=annotations)(audited)

    return register
