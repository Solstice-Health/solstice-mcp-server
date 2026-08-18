"""Per-caller in-process rate limits for MCP tool invocations.

Keyed by (subject, client_id) with an optional stricter budget for fan-out /
privileged tools. Per-worker only (2 gunicorn workers ~ 2x effective budget);
shared Redis is a deliberate non-goal for v1.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from mcp.server.fastmcp.exceptions import ToolError

# Tools that fan out across tenants or mutate credentials get a tighter cap.
STRICT_TOOL_NAMES = frozenset(
    {
        "solstice_list_tenants",
        "solstice_reset_password",
        "solstice_change_brand_role",
        "solstice_add_user",
    }
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


class RateLimiter:
    """Sliding-window limiter. ``rpm=0`` disables that tier."""

    def __init__(
        self,
        *,
        default_rpm: int | None = None,
        strict_rpm: int | None = None,
        window_seconds: float = 60.0,
    ) -> None:
        self.default_rpm = (
            _env_int("MCP_RATE_LIMIT_RPM", 60) if default_rpm is None else max(0, default_rpm)
        )
        self.strict_rpm = (
            _env_int("MCP_RATE_LIMIT_STRICT_RPM", 10) if strict_rpm is None else max(0, strict_rpm)
        )
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, *, subject: str, client_id: str, tool_name: str) -> None:
        limit = self.strict_rpm if tool_name in STRICT_TOOL_NAMES else self.default_rpm
        if limit <= 0:
            return
        key = f"{subject}\0{client_id}\0{tool_name if tool_name in STRICT_TOOL_NAMES else '*'}"
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                raise ToolError(
                    f"rate_limited: too many calls to {tool_name} "
                    f"(limit {limit} per {int(self.window_seconds)}s); retry shortly"
                )
            bucket.append(now)

    def reset(self) -> None:
        """Clear all buckets (tests)."""
        with self._lock:
            self._hits.clear()


# Process-wide limiter shared by audited_tool wrappers on this worker.
default_limiter = RateLimiter()
