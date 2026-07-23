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
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

import anyio.to_thread
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
    "memory_id",
    "scope",
}
ACTIVITY_RESOURCE_FIELDS = (
    "tenant_slug",
    "brand_id",
    "project_id",
    "operation_id",
    "message_id",
)

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
    record_activity: Callable[..., Any] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a tool that emits audit and bounded activity events.

    Activity ingestion is best-effort: personalization telemetry may lag, but
    the completed or failed platform action remains authoritative.
    """

    def register(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @wraps(function)
        async def audited(*args: P.args, **kwargs: P.kwargs) -> R:
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

            async def emit_activity(outcome: str, result: Any = None) -> None:
                if record_activity is None or function.__name__.startswith("solstice_memory_"):
                    return
                activity_resources: dict[str, str] = {}
                for source in (bound.arguments, result if isinstance(result, dict) else {}):
                    activity_resources.update(
                        {
                            name: value
                            for name in ACTIVITY_RESOURCE_FIELDS
                            if isinstance((value := source.get(name)), str) and value
                        }
                    )
                tenant_slug = activity_resources.pop("tenant_slug", None)
                if tenant_slug is None:
                    return
                try:
                    await anyio.to_thread.run_sync(
                        partial(
                            record_activity,
                            actor_sub=token.subject,
                            tenant_slug=tenant_slug,
                            tool_name=function.__name__,
                            outcome=outcome,
                            occurred_at=event["timestamp"],
                            idempotency_key=event["event_id"],
                            **activity_resources,
                        )
                    )
                except Exception as activity_exc:
                    logger.warning(
                        json.dumps(
                            {
                                "event_id": event["event_id"],
                                "tool": function.__name__,
                                "error_type": type(activity_exc).__name__,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )

            try:
                # Tool bodies do blocking I/O (SQLAlchemy, boto3). The MCP SDK
                # calls sync tools inline on the event loop, so offload to a
                # worker thread to keep one slow DB/S3 call from stalling every
                # concurrent request on this worker.
                result = await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
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
                await emit_activity(event["outcome"])
                raise

            event.update(
                outcome="success",
                duration_ms=round((time.monotonic() - started_at) * 1000, 3),
            )
            logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
            await emit_activity(event["outcome"], result)
            return result

        return mcp.tool(annotations=annotations)(audited)

    return register
