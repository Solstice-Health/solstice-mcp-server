"""Failures a Backend call can raise, carrying facts rather than advice.

``BackendStatusError`` keeps the Backend's own ``code`` and message so the
service above can decide what the agent should do about it. Nothing here names
a tool-facing string; a repository that knew those would make the agent
contract depend on transport.
"""

from __future__ import annotations


class BackendError(Exception):
    """A Backend call that produced no usable payload."""


class BackendUnreachable(BackendError):
    """Transport failed after retries; the request may or may not have landed."""


class BackendUnauthenticated(BackendError):
    """No machine token could be obtained, so the call was never made."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackendInvalidResponse(BackendError):
    """A 2xx whose body was not a JSON object."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackendStatusError(BackendError):
    """A 4xx or 5xx. ``code`` is the Backend's own error code when it sent one."""

    def __init__(self, *, status: int, code: str | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
