"""Email-domain access gate for the Solstice discovery hub."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    email: str | None
    reason: str


class SolsticeAccessGate:
    """Thread-safe, short-TTL cache of allow/deny decisions per Auth0 subject.

    The gate decides whether to reveal the sibling MCP directory to a caller.
    Allow iff the verified JWT's email claim ends with the allowed domain.
    The IDE connects to sibling MCPs directly; this gate holds no sibling
    credentials and relays no calls.

    ponytail: per-subject cache mirrors TenantMembershipCache. Replace with a
    central policy service if domain rules grow beyond a single suffix match.
    """

    def __init__(
        self,
        *,
        allowed_domain: str,
        ttl_seconds: float = 300.0,
        max_entries: int = 1024,
    ) -> None:
        if not allowed_domain:
            raise ValueError("allowed_domain is required")
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("Cache TTL and size must be positive")
        self.allowed_domain = allowed_domain.lower()
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, AccessDecision]] = {}
        self._lock = threading.Lock()

    def evaluate(self, subject: str, email: str | None) -> AccessDecision:
        if not subject:
            raise ValueError("subject is required")
        cached = self._get(subject)
        if cached is not None:
            return cached

        if not email:
            decision = AccessDecision(allowed=False, email=None, reason="missing email claim")
        elif email.lower().endswith(self.allowed_domain):
            decision = AccessDecision(allowed=True, email=email, reason="email domain allowed")
        else:
            decision = AccessDecision(allowed=False, email=email, reason="email domain not allowed")
        self._set(subject, decision)
        return decision

    def _get(self, subject: str) -> AccessDecision | None:
        with self._lock:
            entry = self._store.get(subject)
            if entry is None:
                return None
            expires_at, decision = entry
            if time.monotonic() >= expires_at:
                self._store.pop(subject, None)
                return None
            return decision

    def _set(self, subject: str, decision: AccessDecision) -> None:
        with self._lock:
            if subject not in self._store and len(self._store) >= self.max_entries:
                self._store.pop(next(iter(self._store)))
            self._store[subject] = (time.monotonic() + self.ttl_seconds, decision)
