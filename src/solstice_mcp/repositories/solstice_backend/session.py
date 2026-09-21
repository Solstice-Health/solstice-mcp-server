"""One authenticated conversation with the Solstice Backend.

Every repository below shares this: the machine bearer, the tenant header
TenantMiddleware requires, JSON encoding, and the ``{"detail": {"code",
"message"}}`` envelope the Backend answers errors with. Domains differ only in
paths and payloads, so they own nothing of this.

The pooled ``httpx.Client`` lives here rather than in each repository, so a
domain cannot acquire one by accident or forget to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from typing import Any

import httpx

from solstice_mcp import http_client
from solstice_mcp.client_credentials import CredentialsError
from solstice_mcp.repositories.solstice_backend.errors import (
    BackendInvalidResponse,
    BackendStatusError,
    BackendUnauthenticated,
    BackendUnreachable,
)


class BackendSession:
    def __init__(
        self,
        *,
        base_url: str,
        token_acquirer: Any,
        timeout: float = 10.0,
        opener: Any | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Backend base URL is required")
        self._base_url = base_url.rstrip("/")
        self._token_acquirer = token_acquirer
        self._timeout = timeout
        # opener set → urllib (tests). opener None → pooled httpx (production).
        self._opener = opener
        self._http = http if opener is None else None
        self._owns_http = False
        if self._opener is None and self._http is None:
            self._http = httpx.Client(timeout=timeout)
            self._owns_http = True

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def request(
        self,
        method: str,
        path: str,
        *,
        tenant_slug: str | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._base_url + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        try:
            token = self._token_acquirer.get_token()
        except CredentialsError as exc:
            raise BackendUnauthenticated(exc.code) from exc

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        # Omitted only by reads the Backend exempts from TenantMiddleware;
        # sending a slug it will not use would invite one to be invented.
        if tenant_slug is not None:
            headers["X-Tenant-Slug"] = tenant_slug
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            response = http_client.request(
                method,
                url,
                headers=headers,
                content=data,
                timeout=self._timeout,
                opener=self._opener,
                client=self._http,
            )
        except (httpx.TransportError, TimeoutError, urllib.error.URLError, OSError) as exc:
            raise BackendUnreachable("backend_unreachable") from exc

        if response.status_code >= 400:
            code, detail = _error_detail(response.content)
            raise BackendStatusError(status=response.status_code, code=code, detail=detail)
        return _payload(response.content)


def _payload(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackendInvalidResponse("backend_response_invalid_json") from exc
    if not isinstance(parsed, dict):
        raise BackendInvalidResponse("backend_response_invalid_shape")
    return parsed


def _error_detail(body: bytes) -> tuple[str | None, str]:
    """``(code, message)`` from an error body, tolerating any shape."""
    try:
        parsed = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return None, "backend error"
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message") or "backend error"
        return (code if isinstance(code, str) else None), str(message)
    if isinstance(detail, str):
        return None, detail
    return None, "backend error"
