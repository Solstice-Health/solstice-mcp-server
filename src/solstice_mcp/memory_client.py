"""Confidential Backend client for the Solstice agent-memory domain.

The MCP server stays stateless: it validates the end-user OAuth subject,
rechecks tenant/brand membership via ``require_brand_role``, then calls the
Backend-Server internal memory routes with an RS256 Auth0 client-credentials
bearer and a server-derived actor envelope. The MCP never touches the
tenant Postgres store; Backend is the sole trust root for memory writes.

The actor fields in the request query/body are revalidated against the
tenant DB; they never grant access on their own. Caller-supplied roles are
never sent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_SCOPE_PERSONAL = "personal"
MEMORY_SCOPE_TENANT_PERSONAL = "tenant_personal"
MEMORY_SCOPE_BRAND = "brand"
MEMORY_SCOPES = (
    MEMORY_SCOPE_TENANT_PERSONAL,
    MEMORY_SCOPE_PERSONAL,
    MEMORY_SCOPE_BRAND,
)

_TOKEN_SKEW_SECONDS = 60.0


class MemoryClientError(Exception):
    """Redacted Backend error. Carries a stable code; never embeds response bodies."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class MemoryClientUnauthorized(MemoryClientError):
    """Backend rejected the M2M token or actor envelope."""


class MemoryClientNotFound(MemoryClientError):
    """Backend reported the memory_id is unknown for this partition."""


class MemoryClientConflict(MemoryClientError):
    """Backend reported a partition or version conflict (e.g. supersession)."""


class MemoryClientInvalidArgument(MemoryClientError):
    """Backend rejected the payload shape (422). Bodies are never surfaced."""


class MemoryClientUnavailable(MemoryClientError):
    """Backend returned 5xx or could not be reached."""


@dataclass(frozen=True)
class ActorEnvelope:
    """Server-derived actor fields for Backend's internal memory routes.

    Backend revalidates ``actor_sub`` against the tenant DB and requires
    ``tenant_slug`` to match ``X-Tenant-Slug``. ``user_id`` is kept locally for
    audit/debug and is never sent as authority.
    """

    actor_sub: str
    tenant_slug: str
    brand_id: str
    user_id: str


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
    ) -> None:
        if not (token_endpoint and client_id and client_secret and audience):
            raise ValueError("Auth0 client-credentials requires endpoint, client id, secret, and audience")
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._scope = scope
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

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
        request = urllib.request.Request(
            self._token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = _read_json(response)
        except urllib.error.HTTPError as exc:
            raise MemoryClientUnauthorized("auth0_token_endpoint_failed", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MemoryClientUnavailable("auth0_token_endpoint_unreachable") from exc

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, (int, float)):
            raise MemoryClientUnauthorized("auth0_token_response_invalid")
        return token, float(expires_in)


class BackendMemoryClient:
    """HTTP client for the Backend-Server ``/api/internal/agent-memory`` routes."""

    def __init__(
        self,
        *,
        base_url: str,
        token_acquirer: Auth0ClientCredentials,
        timeout: float = 10.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Backend base URL is required for memory tools")
        self._base_url = base_url.rstrip("/")
        self._token_acquirer = token_acquirer
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def recall(
        self,
        *,
        actor: ActorEnvelope,
        fact_type: str | None = None,
        entity_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "brand_id": actor.brand_id,
            "actor_sub": actor.actor_sub,
            "tenant_slug": actor.tenant_slug,
        }
        if fact_type:
            params["fact_type"] = fact_type
        if entity_id:
            params["entity_id"] = entity_id
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = str(limit)
        return self._request(
            "GET",
            "/api/internal/agent-memory",
            tenant_slug=actor.tenant_slug,
            params=params,
        )

    def remember(
        self,
        *,
        actor: ActorEnvelope,
        scope: str,
        fact_type: str,
        statement: str,
        source_refs: list[dict[str, Any]] | None = None,
        entity_refs: list[dict[str, Any]] | None = None,
        expires_at: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body = _mutation_body(
            actor,
            scope=scope,
            fact_type=fact_type,
            statement=statement,
            source_refs=source_refs,
            entity_refs=entity_refs,
            expires_at=expires_at,
            reason=reason,
        )
        return self._request(
            "POST",
            "/api/internal/agent-memory",
            tenant_slug=actor.tenant_slug,
            json_body=body,
        )

    def replace(
        self,
        *,
        actor: ActorEnvelope,
        memory_id: str,
        scope: str,
        fact_type: str,
        statement: str,
        source_refs: list[dict[str, Any]] | None = None,
        entity_refs: list[dict[str, Any]] | None = None,
        expires_at: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body = _mutation_body(
            actor,
            scope=scope,
            fact_type=fact_type,
            statement=statement,
            source_refs=source_refs,
            entity_refs=entity_refs,
            expires_at=expires_at,
            reason=reason,
        )
        return self._request(
            "POST",
            f"/api/internal/agent-memory/{memory_id}/supersede",
            tenant_slug=actor.tenant_slug,
            json_body=body,
        )

    def forget(
        self,
        *,
        actor: ActorEnvelope,
        memory_id: str,
        scope: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "brand_id": _brand_id_for_scope(actor, scope),
            "scope": scope,
            "actor_sub": actor.actor_sub,
            "tenant_slug": actor.tenant_slug,
        }
        if reason is not None:
            body["reason"] = reason
        return self._request(
            "POST",
            f"/api/internal/agent-memory/{memory_id}/forget",
            tenant_slug=actor.tenant_slug,
            json_body=body,
        )

    def record_observation(
        self,
        *,
        actor_sub: str,
        tenant_slug: str,
        scope: str,
        observation: str,
        entity_refs: list[dict[str, Any]],
        source_refs: list[dict[str, Any]],
        occurred_at: str,
        idempotency_key: str,
        brand_id: str | None = None,
        host_correlation_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "actor_sub": actor_sub,
            "tenant_slug": tenant_slug,
            "scope": scope,
            "observation": observation,
            "entity_refs": entity_refs,
            "source_refs": source_refs,
            "occurred_at": occurred_at,
            "idempotency_key": idempotency_key,
        }
        if brand_id is not None:
            body["brand_id"] = brand_id
        if host_correlation_id is not None:
            body["host_correlation_id"] = host_correlation_id
        return self._request(
            "POST",
            "/api/internal/agent-memory/observations",
            tenant_slug=tenant_slug,
            json_body=body,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        tenant_slug: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._base_url + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        token = self._token_acquirer.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # TenantMiddleware requires this on every Backend API request.
            "X-Tenant-Slug": tenant_slug,
        }
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return _read_json(response)
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MemoryClientUnavailable("backend_unreachable") from exc


def _mutation_body(
    actor: ActorEnvelope,
    *,
    scope: str,
    fact_type: str,
    statement: str,
    source_refs: list[dict[str, Any]] | None,
    entity_refs: list[dict[str, Any]] | None,
    expires_at: str | None,
    reason: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "brand_id": _brand_id_for_scope(actor, scope),
        "scope": scope,
        "fact_type": fact_type,
        "statement": statement,
        "actor_sub": actor.actor_sub,
        "tenant_slug": actor.tenant_slug,
    }
    if source_refs is not None:
        body["source_refs"] = source_refs
    if entity_refs is not None:
        body["entity_refs"] = entity_refs
    if expires_at is not None:
        body["expires_at"] = expires_at
    if reason is not None:
        body["reason"] = reason
    return body


def _brand_id_for_scope(actor: ActorEnvelope, scope: str) -> str | None:
    return None if scope == MEMORY_SCOPE_TENANT_PERSONAL else actor.brand_id


def _read_json(response: Any) -> dict[str, Any]:
    raw = response.read()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryClientUnavailable("backend_response_invalid_json") from exc
    if not isinstance(payload, dict):
        raise MemoryClientUnavailable("backend_response_invalid_shape")
    return payload


def _map_http_error(exc: urllib.error.HTTPError) -> MemoryClientError:
    status = exc.code
    # The response body may carry backend-internal detail; never surface it.
    try:
        body = exc.read()
        logger.debug("backend memory error body", extra={"status": status, "body_len": len(body)})
    except Exception:  # reading the error body is best-effort; never surfaced
        pass
    if status in (401, 403):
        return MemoryClientUnauthorized("backend_unauthorized", status=status)
    if status == 404:
        return MemoryClientNotFound("backend_not_found", status=status)
    if status == 409:
        return MemoryClientConflict("backend_conflict", status=status)
    if status == 422:
        return MemoryClientInvalidArgument("backend_invalid_argument", status=status)
    if 500 <= status < 600:
        return MemoryClientUnavailable("backend_unavailable", status=status)
    return MemoryClientError("backend_unexpected_status", status=status)


__all__ = [
    "MEMORY_SCOPES",
    "MEMORY_SCOPE_BRAND",
    "MEMORY_SCOPE_PERSONAL",
    "MEMORY_SCOPE_TENANT_PERSONAL",
    "ActorEnvelope",
    "Auth0ClientCredentials",
    "BackendMemoryClient",
    "MemoryClientConflict",
    "MemoryClientError",
    "MemoryClientInvalidArgument",
    "MemoryClientNotFound",
    "MemoryClientUnauthorized",
    "MemoryClientUnavailable",
]
