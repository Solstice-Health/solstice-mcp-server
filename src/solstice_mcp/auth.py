"""Auth0 RS256 verification for the MCP resource server."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

import anyio.to_thread
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


def fetch_jwks(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"JWKS endpoint returned HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ValueError("JWKS response must contain a keys array")
    return payload


class JWKSCache:
    """Thread-safe, short-lived cache for one Auth0 JWKS document."""

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float = 300.0,
        timeout: float = 5.0,
        initial: dict[str, Any] | None = None,
    ) -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self._value = initial
        self._expires_at = float("inf") if initial is not None else 0.0
        self._lock = threading.Lock()

    def get(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._value is not None and time.monotonic() < self._expires_at:
                return self._value
            self._value = fetch_jwks(self.url, timeout=self.timeout)
            self._expires_at = time.monotonic() + self.ttl_seconds
            return self._value


class MCPAccessTokenVerifier(TokenVerifier):
    """Validate Auth0 tokens and expose their claims to FastMCP."""

    def __init__(self, *, audience: str, issuer: str, jwks_cache: JWKSCache | None = None) -> None:
        self.audience = audience
        self.issuer = issuer
        self.jwks_cache = jwks_cache or JWKSCache(f"{issuer.rstrip('/')}/.well-known/jwks.json")

    def _decode(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise jwt.InvalidTokenError("Token must use RS256 and include kid")

        jwks = self.jwks_cache.get()
        key_data = next((key for key in jwks["keys"] if key.get("kid") == header["kid"]), None)
        if key_data is None:
            jwks = self.jwks_cache.get(refresh=True)
            key_data = next((key for key in jwks["keys"] if key.get("kid") == header["kid"]), None)
        if key_data is None:
            raise jwt.InvalidTokenError("Signing key not found")

        return jwt.decode(
            token,
            jwt.PyJWK.from_dict(key_data).key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # Offloaded to a thread: a JWKS cache miss does a blocking HTTP
            # fetch that must not stall the event loop.
            payload = await anyio.to_thread.run_sync(self._decode, token)
            raw_scope = payload.get("scope") or ""
            scopes = raw_scope.split() if isinstance(raw_scope, str) else list(raw_scope)
            return AccessToken(
                token=token,
                client_id=payload.get("azp") or payload.get("client_id") or "unknown",
                scopes=scopes,
                expires_at=int(payload["exp"]),
                resource=self.audience,
                subject=payload["sub"],
                claims=payload,
            )
        except Exception as exc:
            # Any fetch or validation failure safely denies access through
            # FastMCP's 401 response — but never silently: a JWKS outage or an
            # issuer/audience misconfig must be distinguishable from a bad
            # token in the logs. The token itself is never logged.
            logger.info("Token verification failed: %s: %s", type(exc).__name__, exc)
            return None
