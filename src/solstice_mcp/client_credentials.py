"""Auth0 client-credentials token acquisition.

Not Solstice-specific: the Backend repositories and the Auth0 Management API
client both hold one of these, against different audiences.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
from typing import Any

import httpx

from solstice_mcp import http_client

_TOKEN_SKEW_SECONDS = 60.0


class CredentialsError(Exception):
    """Redacted token-grant failure. Carries a stable code, never a body."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class CredentialsRejected(CredentialsError):
    """Auth0 refused the grant or answered with something unusable."""


class CredentialsUnavailable(CredentialsError):
    """The token endpoint could not be reached."""


class Auth0ClientCredentials:
    """Thread-safe, short-lived cache of one Auth0 client-credentials access token.

    ponytail: one token per process, refreshed ~60s before expiry. Replace with
    a shared token cache if multiple workers duplicate the fetch under load.
    """

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        audience: str,
        scope: str,
        timeout: float = 5.0,
        opener: Any | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        if not (token_endpoint and client_id and client_secret and audience):
            raise ValueError("Auth0 client-credentials requires endpoint, client id, secret, and audience")
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._scope = scope
        self._timeout = timeout
        # opener set → urllib (tests). opener None → pooled httpx (production).
        self._opener = opener
        self._http = http if opener is None else None
        self._owns_http = False
        if self._opener is None and self._http is None:
            self._http = httpx.Client(timeout=timeout)
            self._owns_http = True
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def get_token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            token, expires_in = self._fetch()
            self._token = token
            # Refresh before the real expiry so a slow request never carries an expired token.
            self._expires_at = time.monotonic() + max(expires_in - _TOKEN_SKEW_SECONDS, 1.0)
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> tuple[str, float]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "audience": self._audience,
                "scope": self._scope,
            }
        ).encode("utf-8")
        try:
            # client_credentials is safe to retry; Auth0 issues a new token
            # without side effects. Mutating Backend/Auth0 admin POSTs must NOT
            # set retry_mutations (http_client defaults to no retries there).
            response = http_client.request(
                "POST",
                self._token_endpoint,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                content=body,
                timeout=self._timeout,
                retry_mutations=True,
                opener=self._opener,
                client=self._http,
            )
        except (httpx.TransportError, TimeoutError, urllib.error.URLError, OSError) as exc:
            raise CredentialsUnavailable("auth0_token_endpoint_unreachable") from exc

        if response.status_code >= 400:
            raise CredentialsRejected("auth0_token_endpoint_failed", status=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise CredentialsRejected("auth0_token_response_invalid") from exc
        if not isinstance(payload, dict):
            raise CredentialsRejected("auth0_token_response_invalid")

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, (int, float)):
            raise CredentialsRejected("auth0_token_response_invalid")
        return token, float(expires_in)


__all__ = [
    "Auth0ClientCredentials",
    "CredentialsError",
    "CredentialsRejected",
    "CredentialsUnavailable",
]
