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
from typing import Any, Literal, TypeVar

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from solstice_mcp import feature_flags
from solstice_mcp.repositories.solstice_backend.errors import (
    BackendError,
    BackendStatusError,
    BackendUnreachable,
)
from solstice_mcp.repositories.solstice_backend.prc import (
    CommittedVersion,
    PrcActor,
    PrcProfile,
    PrcRepository,
    RuleSet,
)

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


class ToolResponse(BaseModel):
    """A tool result. Field names are the agent contract — tool descriptions
    instruct the model by them — so the shapes are pinned here rather than
    assembled ad hoc, and nothing extra leaks into one."""

    model_config = ConfigDict(extra="forbid")


class TemplateRulesResponse(ToolResponse):
    status: Literal["ok"] = "ok"
    contract_version: str
    profile: PrcProfile
    rules: RuleSet
    document: str


class PreparedVersionResponse(ToolResponse):
    operation_id: str
    type: Literal["html"] = "html"
    # The row id the Backend minted is embedded in the key it returned; the
    # tool has always published it as message_id.
    message_id: str
    s3_key: str
    upload_url: str
    expires_in: int


class PreparedBakeResponse(ToolResponse):
    operation_id: str
    prc_template_s3_key: str
    upload_url: str
    expires_in: int


class CommittedVersionResponse(ToolResponse):
    """The tool's long-standing response shape, from the Backend's envelope.

    The Backend answers with what it durably knows; everything else here the
    caller already supplied or the prepare step already returned, so the tool
    contract holds without the Backend echoing it back.
    """

    operation_id: str
    type: Literal["html"] = "html"
    intent: str | None
    # `id` and `head_message_id` are the same row: a caller passes the value
    # back as `base_message_id`, and the two diverging would break the
    # compare-and-swap silently.
    id: str
    head_message_id: str
    message_id: str
    s3_key: str
    prc_template_s3_key: str | None
    asset_url: str


class CommittedBakeResponse(ToolResponse):
    operation_id: str
    intent: str | None
    message_id: str
    id: str
    s3_key: str | None
    prc_template_s3_key: str | None
    # Not reported by the Backend, which stores the proof rather than measuring
    # it. Zero rather than a guess.
    html_size_bytes: int = 0
    asset_url: str


class PublishedVersionResponse(ToolResponse):
    operation_id: str
    id: str
    message_id: str
    intent: Literal["final"] = "final"
    already_final: Literal[False] = False
    change_requests_resolved: int
    requests_completed: int
    asset_url: str


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

    def template_rules(self, profile: str) -> TemplateRulesResponse:
        parsed = _profile(profile)
        rules = self._call(self._backend().template_rules, profile=parsed)
        return TemplateRulesResponse(
            contract_version=rules.contract_version,
            profile=rules.profile,
            rules=rules.rules,
            document=rules.document,
        )

    def prepare_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str
    ) -> PreparedVersionResponse:
        prepared = self._call(
            self._backend().prepare_upload,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            artifact="creative",
        )
        return PreparedVersionResponse(
            operation_id=operation_id,
            message_id=row_id_from_key(prepared.s3_key),
            s3_key=prepared.s3_key,
            upload_url=prepared.upload_url,
            expires_in=prepared.expires_in,
        )

    def prepare_bake(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str
    ) -> PreparedBakeResponse:
        prepared = self._call(
            self._backend().prepare_upload,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            artifact="proof",
        )
        return PreparedBakeResponse(
            operation_id=operation_id,
            prc_template_s3_key=prepared.s3_key,
            upload_url=prepared.upload_url,
            expires_in=prepared.expires_in,
        )

    def commit_bake(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, proof_s3_key: str
    ) -> CommittedBakeResponse:
        """An operation bake IS a proof edit: the proof is supplied, and the
        creative it wraps is the one the operation already holds."""
        committed = self._call(
            self._backend().commit_version,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            body={"kind": "proof", "proof": {"s3_key": proof_s3_key}},
        )
        return CommittedBakeResponse(
            operation_id=operation_id,
            intent=committed.intent,
            message_id=row_id_from_key(proof_s3_key),
            id=committed.head_message_id,
            s3_key=committed.creative_s3_key,
            prc_template_s3_key=committed.prc_template_s3_key,
            asset_url=committed.asset_url,
        )

    def commit_version(
        self,
        *,
        tenant_slug: str,
        actor_sub: str,
        operation_id: str,
        s3_key: str,
        base_message_id: str | None,
        confirmed: bool,
    ) -> CommittedVersionResponse:
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
    ) -> PublishedVersionResponse:
        published = self._call(
            self._backend().publish_version,
            actor=PrcActor(tenant_slug, actor_sub),
            operation_id=operation_id,
            message_id=message_id,
        )
        return PublishedVersionResponse(
            operation_id=operation_id,
            id=message_id,
            message_id=message_id,
            change_requests_resolved=published.change_requests_resolved,
            requests_completed=published.requests_completed,
            asset_url=asset_url,
        )

    @staticmethod
    def _call(fn: Callable[..., _Answer], **kwargs: Any) -> _Answer:
        try:
            return fn(**kwargs)
        except BackendError as exc:
            raise tool_error(exc) from exc

    def _backend(self) -> PrcRepository:
        if self._repository is None:  # pragma: no cover - guarded by handles()
            raise ToolError("not_configured: PRC backend client is unavailable")
        return self._repository


_Answer = TypeVar("_Answer")


def _profile(value: str) -> PrcProfile:
    try:
        return PrcProfile(value.strip().lower())
    except ValueError:
        raise ToolError(
            f"invalid_argument: profile must be one of {', '.join(PrcProfile)}"
        ) from None


def row_id_from_key(s3_key: str) -> str:
    """The row id the Backend embedded in an artifact key.

    Both artifact prefixes end ``/{operation_id}/{row_id}.html``. The tool has
    always published this as ``message_id``, and callers pass it back, so it is
    read from the key rather than invented here.
    """
    return s3_key.rsplit("/", 1)[-1].removesuffix(".html")


def committed_response(
    committed: CommittedVersion,
    *,
    operation_id: str,
    s3_key: str,
    message_id: str,
) -> CommittedVersionResponse:
    return CommittedVersionResponse(
        operation_id=operation_id,
        intent=committed.intent,
        id=committed.head_message_id,
        head_message_id=committed.head_message_id,
        message_id=message_id,
        s3_key=s3_key,
        prc_template_s3_key=committed.prc_template_s3_key,
        asset_url=committed.asset_url,
    )


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
