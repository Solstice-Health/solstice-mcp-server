"""PRC writes: where they go, and what the agent hears when they fail.

Two decisions live here. Which plane serves a write — the Backend when the
tenant's flag says so and credentials exist, the local path otherwise — and how
a Backend refusal becomes one of the ``ToolError`` strings the tool
descriptions instruct the model against. That mapping is part of the agent
contract: an agent told to re-read on ``not_latest_document`` will retry
blindly instead if a conflict arrives as anything else.

Response shaping is here too, because the tool's published fields predate the
Backend and must survive the cutover unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from solstice_mcp import feature_flags
from solstice_mcp.repositories.solstice_backend.errors import (
    BackendError,
    BackendStatusError,
    BackendUnreachable,
)
from solstice_mcp.repositories.solstice_backend.prc import PrcActor, PrcRepository

PRC_TEMPLATE_PROFILES = ("email", "banner", "social", "website")

# Backend code -> the string the tool descriptions teach the model to act on.
# `content_conflict` is the Backend's name for what the tools call
# `not_latest_document`; the tool wording wins, because agents are instructed
# against it.
_CODE_PREFIXES = {
    "confirmation_required": "confirmation_required",
    "no_template": "invalid_state",
    "stale_proof": "invalid_state",
    "contract_error": "contract_error",
    "invalid_request": "invalid_request",
    "bake_unavailable": "not_available",
}

_STATUS_PREFIXES = {
    401: "not_authorized",
    403: "not_authorized",
    404: "not_found",
    400: "invalid_arguments",
    # An uncoded 422 is FastAPI rejecting the body shape, not a domain rule.
    422: "invalid_arguments",
}


class PrcService:
    def __init__(self, repository: PrcRepository | None) -> None:
        self._repository = repository

    def handles(self, tenant_slug: str, brand_id: str | None = None) -> bool:
        """True when this tenant's PRC writes belong to the Backend.

        Credentials gate the flag, not the other way round: a task without them
        keeps the local path however the flag is set, so enabling a tenant
        cannot route a write somewhere this process cannot reach.
        """
        if self._repository is None:
            return False
        return feature_flags.prc_writes_via_backend(tenant_slug=tenant_slug, brand_id=brand_id)

    def template_rules(self, profile: str) -> dict[str, Any]:
        normalized = profile.strip().lower()
        if normalized not in PRC_TEMPLATE_PROFILES:
            allowed = ", ".join(PRC_TEMPLATE_PROFILES)
            raise ToolError(f"invalid_argument: profile must be one of {allowed}")
        rules = self._call(self._backend().template_rules, profile=normalized)
        return {"status": "ok", **rules}

    def prepare_version(self, *, tenant_slug: str, actor_sub: str, operation_id: str) -> dict[str, Any]:
        prepared = self._call(
            self._backend().prepare_upload,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            artifact="creative",
        )
        return {
            "operation_id": operation_id,
            "type": "html",
            # The row id the Backend minted is embedded in the key it returned;
            # the tool has always published it as message_id.
            "message_id": row_id_from_key(prepared["s3_key"]),
            "s3_key": prepared["s3_key"],
            "upload_url": prepared["upload_url"],
            "expires_in": prepared["expires_in"],
        }

    def prepare_bake(self, *, tenant_slug: str, actor_sub: str, operation_id: str) -> dict[str, Any]:
        prepared = self._call(
            self._backend().prepare_upload,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            artifact="proof",
        )
        return {
            "operation_id": operation_id,
            "prc_template_s3_key": prepared["s3_key"],
            "upload_url": prepared["upload_url"],
            "expires_in": prepared["expires_in"],
        }

    def commit_bake(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, proof_s3_key: str
    ) -> dict[str, Any]:
        """An operation bake IS a proof edit: the proof is supplied, and the
        creative it wraps is the one the operation already holds."""
        committed = self._call(
            self._backend().commit_version,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            body={"kind": "proof", "proof": {"s3_key": proof_s3_key}},
        )
        return {
            "operation_id": operation_id,
            "intent": committed.get("intent"),
            "message_id": row_id_from_key(proof_s3_key),
            "id": str(committed.get("head_message_id") or ""),
            "s3_key": committed.get("content"),
            "prc_template_s3_key": committed.get("prc_template_s3_key"),
            # Not reported by the Backend, which stores the proof rather than
            # measuring it. Zero rather than a guess.
            "html_size_bytes": 0,
            "asset_url": committed.get("asset_url"),
        }

    def commit_version(
        self,
        *,
        tenant_slug: str,
        actor_sub: str,
        operation_id: str,
        s3_key: str,
        base_message_id: str | None,
        confirmed: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": "content",
            "creative": {"s3_key": s3_key},
            "confirmed": confirmed,
        }
        if base_message_id:
            body["base_message_id"] = base_message_id
        committed = self._call(
            self._backend().commit_version,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            body=body,
        )
        return committed_response(
            committed,
            operation_id=operation_id,
            s3_key=s3_key,
            message_id=row_id_from_key(s3_key),
        )

    def publish_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, message_id: str, asset_url: str
    ) -> dict[str, Any]:
        published = self._call(
            self._backend().publish_version,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            message_id=message_id,
        )
        return {
            "operation_id": operation_id,
            "id": message_id,
            "message_id": message_id,
            "intent": "final",
            "already_final": False,
            "change_requests_resolved": published.get("change_requests_resolved", 0),
            "requests_completed": published.get("requests_completed", 0),
            "asset_url": asset_url,
        }

    @staticmethod
    def _call(fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(**kwargs)
        except BackendError as exc:
            raise tool_error(exc) from exc

    def _backend(self) -> PrcRepository:
        if self._repository is None:  # pragma: no cover - guarded by handles()
            raise ToolError("not_configured: PRC backend client is unavailable")
        return self._repository


def row_id_from_key(s3_key: str) -> str:
    """The row id the Backend embedded in an artifact key.

    Both artifact prefixes end ``/{operation_id}/{row_id}.html``. The tool has
    always published this as ``message_id``, and callers pass it back, so it is
    read from the key rather than invented here.
    """
    return s3_key.rsplit("/", 1)[-1].removesuffix(".html")


def committed_response(
    committed: dict[str, Any],
    *,
    operation_id: str,
    s3_key: str,
    message_id: str,
) -> dict[str, Any]:
    """The tool's long-standing response shape, from the Backend's envelope.

    The Backend answers with what it durably knows; everything else here the
    caller already supplied or the prepare step already returned, so the tool
    contract holds without the Backend echoing it back.
    """
    head = str(committed.get("head_message_id") or "")
    return {
        "operation_id": operation_id,
        "type": "html",
        "intent": committed.get("intent"),
        "id": head,
        "head_message_id": head,
        "message_id": message_id,
        "s3_key": s3_key,
        "prc_template_s3_key": committed.get("prc_template_s3_key"),
        "asset_url": committed.get("asset_url"),
    }


def tool_error(exc: BackendError) -> ToolError:
    if isinstance(exc, BackendUnreachable):
        return ToolError("not_available: backend unreachable")
    if not isinstance(exc, BackendStatusError):
        # No token, or a 2xx that was not a payload: the Backend never ruled on
        # the request, so the agent should hear that rather than a domain code.
        return ToolError(f"not_available: {exc}")
    if exc.code in ("content_conflict", "not_latest_document"):
        return ToolError("conflict: not_latest_document")
    if exc.code == "invalid_proof":
        return ToolError(
            f"invalid_request: operation bake must satisfy baked contract v2: {exc.detail}"
        )
    prefix = _CODE_PREFIXES.get(exc.code or "") or _STATUS_PREFIXES.get(exc.status, "not_available")
    return ToolError(f"{prefix}: {exc.detail}")
