"""HTTP client for the Backend's PRC write and validation routes.

The MCP authenticates as a service and names the person it acts for in
``X-Actor-Sub``; the Backend revalidates that subject against the tenant and
derives write intent from their brand role. The header is a selector, never a
credential, and it comes from the verified end-user token — never from a tool
argument and never from model output.

Backend errors arrive as ``{"detail": {"code", "message"}}`` and are mapped back
to the ``ToolError`` strings the tool descriptions already name. That mapping is
part of the tool contract: an agent told to re-read on ``not_latest_document``
will retry blindly instead if a conflict arrives as something else.

"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import httpx

from solstice_mcp import http_client
from solstice_mcp.memory_client import Auth0ClientCredentials


class PrcBackendError(Exception):
    """A Backend response mapped to the tool-facing message for that failure."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# Backend code -> the string the tool descriptions teach the model to act on.
# `content_conflict` is the backend's name for what the tools call
# `not_latest_document`; the tool wording wins, because agents are instructed
# against it.
_CODE_MESSAGES = {
    "content_conflict": "conflict: not_latest_document",
    "not_latest_document": "conflict: not_latest_document",
}


def _tool_message(status: int, code: str | None, detail: str) -> str:
    if code in _CODE_MESSAGES:
        return _CODE_MESSAGES[code]
    if code == "confirmation_required":
        return f"confirmation_required: {detail}"
    if code == "no_template":
        return f"invalid_state: {detail}"
    if code == "stale_proof":
        return f"invalid_state: {detail}"
    if code == "invalid_proof":
        return f"invalid_request: operation bake must satisfy baked contract v2: {detail}"
    if code == "invalid_request":
        return f"invalid_request: {detail}"
    if code == "bake_unavailable":
        return f"not_available: {detail}"
    if status in (401, 403):
        return f"not_authorized: {detail}"
    if status == 404:
        return f"not_found: {detail}"
    if status == 400:
        return f"invalid_arguments: {detail}"
    if status == 422:
        # An uncoded 422 is FastAPI rejecting the body shape, not a domain rule.
        return f"invalid_arguments: {detail}"
    return f"not_available: {detail}"


def _detail(body: bytes) -> tuple[str | None, str]:
    """(code, message) from a Backend error body, tolerating any shape."""
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return None, "backend error"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message") or "backend error"
        return (code if isinstance(code, str) else None), str(message)
    if isinstance(detail, str):
        return None, detail
    return None, "backend error"


class PrcBackendClient:
    """Calls the Backend's ``/api/v2`` PRC routes on behalf of one person."""

    def __init__(
        self,
        *,
        base_url: str,
        token_acquirer: Auth0ClientCredentials,
        timeout: float = 30.0,
        opener: Any | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_acquirer = token_acquirer
        self._timeout = timeout
        self._opener = opener
        self._http = http

    def _request(
        self,
        method: str,
        path: str,
        *,
        tenant_slug: str,
        actor_sub: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token_acquirer.get_token()}",
            "Accept": "application/json",
            # TenantMiddleware requires this on every Backend API request.
            "X-Tenant-Slug": tenant_slug,
            "X-Actor-Sub": actor_sub,
        }
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            response = http_client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                content=data,
                timeout=self._timeout,
                opener=self._opener,
                client=self._http,
            )
        except (httpx.TransportError, TimeoutError, urllib.error.URLError, OSError) as exc:
            raise PrcBackendError("not_available: backend unreachable") from exc

        if response.status_code >= 400:
            code, detail = _detail(response.content)
            raise PrcBackendError(
                _tool_message(response.status_code, code, detail),
                status=response.status_code,
                code=code,
            )
        return json.loads(response.content or b"{}")

    # -- writes ---------------------------------------------------------------

    def prepare_upload(self, *, tenant_slug: str, actor_sub: str, operation_id: str, artifact: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions/prepare",
            tenant_slug=tenant_slug,
            actor_sub=actor_sub,
            json_body={"artifact": artifact},
        )

    def commit_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions",
            tenant_slug=tenant_slug,
            actor_sub=actor_sub,
            json_body=body,
        )

    def publish_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, message_id: str
    ) -> dict[str, Any]:
        """Mark a version final.

        ``qc_override`` keeps the behaviour agents have today: the QC gate is a
        reviewer workflow, and an agent approval has no reviewer behind it.
        """
        return self._request(
            "POST",
            # unlock_for_viewers closes the change requests and tracker rows that
            # would otherwise leave the asset reading "being prepared" to the
            # people it was approved for. The app closes those through the
            # notification surface, which also emails; agents never have.
            f"/api/v2/operations/{operation_id}/versions/{message_id}/publish?qc_override=true&unlock_for_viewers=true",
            tenant_slug=tenant_slug,
            actor_sub=actor_sub,
        )

    # -- validation -----------------------------------------------------------

    def validate_proof(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions/validate",
            tenant_slug=tenant_slug,
            actor_sub=actor_sub,
            json_body={"proof": payload},
        )

    def validate_template(
        self, *, tenant_slug: str, actor_sub: str, brand_id: str, html_template: str, content_type: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v2/brands/{brand_id}/prc-templates/validate",
            tenant_slug=tenant_slug,
            actor_sub=actor_sub,
            json_body={"html_template": html_template, "content_type": content_type},
        )
