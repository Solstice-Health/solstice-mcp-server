"""Shared outbound HTTP with retries (httpx) and a urllib fallback for tests.

Production callers leave ``opener=None`` and get a pooled ``httpx.Client`` with
bounded retries on 5xx / transport errors for idempotent methods. Mutating
methods (POST/PATCH/PUT/DELETE) are never retried by default — a late 502 after
the upstream already applied the write must not duplicate memory writes or
Auth0 password emails. Callers may opt in with ``retry_mutations=True`` only
for known-safe POSTs (e.g. Auth0 client-credentials token grants).

Tests inject a urllib-style opener (``FakeBackendOpener`` / Auth0 fake) and
keep the existing call-recording shape.

HTTP status codes are returned on ``HttpResponse`` — callers map them. Only
transport failures raise after retries are exhausted.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 3
RETRYABLE_STATUS = frozenset({502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HttpResponse:
    """Minimal response shape shared by httpx and urllib paths."""

    def __init__(self, *, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        if not self.content:
            return {}
        return json.loads(self.content)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = 10.0,
    retries: int = DEFAULT_RETRIES,
    retry_mutations: bool = False,
    opener: Any | None = None,
    client: httpx.Client | None = None,
) -> HttpResponse:
    """Perform one HTTP request with optional retries on transient failures.

    Retries apply to idempotent methods (GET/HEAD/OPTIONS) by default. Mutating
    methods only retry when ``retry_mutations=True`` (opt-in for safe POSTs).
    """
    headers = dict(headers or {})
    method_u = method.upper()
    attempts = max(1, retries) if method_u in IDEMPOTENT_METHODS or retry_mutations else 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            if opener is not None:
                response = _urllib_once(
                    method, url, headers=headers, content=content, timeout=timeout, opener=opener
                )
            else:
                response = _httpx_once(
                    method, url, headers=headers, content=content, timeout=timeout, client=client
                )
        except (httpx.TransportError, TimeoutError, urllib.error.URLError, OSError) as exc:
            # urllib.error.HTTPError subclasses URLError — exclude it so 4xx
            # never enters the transport-retry path (handled below via status).
            if isinstance(exc, urllib.error.HTTPError):
                response = HttpResponse(status_code=int(exc.code), content=_read_http_error_body(exc))
            else:
                last_error = exc
                if attempt >= attempts - 1:
                    raise
                _backoff(attempt)
                continue

        if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
            _backoff(attempt)
            continue
        return response

    assert last_error is not None
    raise last_error


def _backoff(attempt: int) -> None:
    time.sleep(min(0.05 * (2**attempt), 0.4))


def _read_http_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() if hasattr(exc, "read") else b""
    except Exception:  # body drain is best-effort; status mapping does not need it
        return b""


def _urllib_once(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: bytes | None,
    timeout: float,
    opener: Any,
) -> HttpResponse:
    req = urllib.request.Request(url, data=content, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read()
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            return HttpResponse(status_code=status, content=body)
    except urllib.error.HTTPError as exc:
        return HttpResponse(status_code=int(exc.code), content=_read_http_error_body(exc))


def _httpx_once(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: bytes | None,
    timeout: float,
    client: httpx.Client | None,
) -> HttpResponse:
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.request(method, url, headers=headers, content=content)
        return HttpResponse(status_code=response.status_code, content=response.content)
    finally:
        if owns_client:
            http.close()
