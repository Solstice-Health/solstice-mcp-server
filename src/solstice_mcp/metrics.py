"""Lightweight DogStatsD emit for MCP tool metrics (no APM / ddtrace).

Fire-and-forget UDP to the Datadog agent sidecar. Failures are swallowed with
a debug log — metrics must never break a tool call. Disabled when
``DD_DOGSTATSD_DISABLE=true`` or when the agent host is unset and we are not
in a known ECS-style environment (defaults to ``127.0.0.1:8125``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket

logger = logging.getLogger(__name__)

_sock: socket.socket | None = None


def _enabled() -> bool:
    return os.getenv("DD_DOGSTATSD_DISABLE", "").strip().lower() not in {"1", "true", "yes"}


def _endpoint() -> tuple[str, int]:
    host = os.getenv("DD_AGENT_HOST", os.getenv("DD_DOGSTATSD_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port_raw = os.getenv("DD_DOGSTATSD_PORT", "8125").strip() or "8125"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8125
    return host, port


def _socket() -> socket.socket:
    global _sock
    if _sock is None:
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.setblocking(False)
    return _sock


def _escape_tag(value: str) -> str:
    return value.replace("|", "_").replace(",", "_").replace(":", "_")[:128]


def emit_tool_metrics(*, tool: str, outcome: str, duration_ms: float) -> None:
    if not _enabled():
        return
    tags = [f"tool:{_escape_tag(tool)}", f"outcome:{_escape_tag(outcome)}"]
    tag_str = ",".join(tags)
    lines = [
        f"mcp.tool.calls:1|c|#{tag_str}",
        f"mcp.tool.duration_ms:{max(duration_ms, 0):.3f}|h|#{tag_str}",
    ]
    payload = "\n".join(lines).encode("utf-8")
    try:
        _socket().sendto(payload, _endpoint())
    except OSError as exc:
        # Metrics are best-effort; never fail the request path.
        logger.debug("dogstatsd emit failed: %s", exc)


def reset_for_tests() -> None:
    """Close the cached socket (unit tests)."""
    global _sock
    if _sock is not None:
        with contextlib.suppress(OSError):
            _sock.close()
        _sock = None


__all__ = ["emit_tool_metrics", "reset_for_tests"]
