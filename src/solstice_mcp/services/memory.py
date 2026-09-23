"""Memory policy: who may write what, for whom, and what a failure sounds like.

Authorization is the only thing that grants authority. ``tenant_slug`` and
``brand_id`` reaching these methods select a partition; the role behind them is
resolved here against the tenant database, never accepted from a caller.

Backend failures are translated here, and the Backend's own message is dropped
on the way: an error body can carry internal detail, and none of it tells an
agent anything it can act on. That is a decision about the string a person's
agent reads, which is why it lives beside the rest of the policy rather than in
the repository.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError

from solstice_mcp.brands import BrandIdentity, UserRole, require_brand_role, role_satisfies
from solstice_mcp.repositories.solstice_backend.errors import BackendError, BackendStatusError
from solstice_mcp.repositories.solstice_backend.memory import (
    MEMORY_SCOPE_BRAND,
    MEMORY_SCOPE_TENANT_PERSONAL,
    ActorEnvelope,
    MemoryRepository,
)
from solstice_mcp.tenants import SessionFactory, TenantRegistry, resolve_tenant_identity

_BRAND_WRITE_MIN_ROLE = UserRole.ADMIN


class _BackendObserveResult(BaseModel):
    """Backend observe response fields used to define the stable MCP response."""

    model_config = ConfigDict(extra="ignore")

    outcome: Literal["activated", "reinforced", "suppressed", "ineligible"]
    fact: dict[str, Any] | None


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        registry: TenantRegistry,
        session_factory: SessionFactory,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._session_factory = session_factory

    def recall(
        self,
        *,
        subject: str,
        tenant_slug: str,
        brand_id: str,
        fact_type: str | None = None,
        entity_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        actor = self._member(subject, tenant_slug, brand_id)
        result = self._call(
            self._repository.recall,
            conflict_scope="recall",
            actor=actor,
            fact_type=fact_type,
            entity_id=entity_id,
            q=q,
            limit=limit,
        )
        return {"status": "ok", "tenant_slug": tenant_slug, "brand_id": brand_id, **result}

    def observe(
        self,
        *,
        subject: str,
        tenant_slug: str,
        scope: str,
        brand_id: str | None,
        statement: str,
        fact_type: str,
        semantic_subject: str,
        entity_refs: list[dict[str, Any]],
        source_refs: list[dict[str, Any]],
        occurred_at: str,
        host_correlation_id: str | None,
    ) -> dict[str, Any]:
        # Tenant-personal facts have no brand to be a member of, so membership
        # is the whole check; a brand-partitioned one still needs the role.
        if brand_id is None:
            if resolve_tenant_identity(
                subject,
                tenant_slug,
                registry=self._registry,
                session_factory=self._session_factory,
            ) is None:
                raise ToolError("not_authorized: no active tenant membership")
        else:
            self._member(subject, tenant_slug, brand_id)

        result = self._call(
            self._repository.record_observation,
            conflict_scope=scope,
            actor_sub=subject,
            tenant_slug=tenant_slug,
            scope=scope,
            brand_id=brand_id,
            statement=statement,
            fact_type=fact_type,
            semantic_subject=semantic_subject,
            entity_refs=entity_refs,
            source_refs=source_refs,
            occurred_at=occurred_at,
            host_correlation_id=host_correlation_id,
        )
        try:
            observed = _BackendObserveResult.model_validate(result)
        except ValidationError as exc:
            raise ToolError(
                "internal_error: memory backend returned an unexpected observation result"
            ) from exc
        return {
            "outcome": observed.outcome,
            "fact": observed.fact,
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "scope": scope,
        }

    def remember(
        self,
        *,
        subject: str,
        tenant_slug: str,
        brand_id: str,
        scope: str,
        fact_type: str,
        statement: str,
        source_refs: list[dict[str, Any]] | None,
        entity_refs: list[dict[str, Any]] | None,
        expires_at: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        actor = self._writer(subject, tenant_slug, brand_id, scope)
        result = self._call(
            self._repository.remember,
            conflict_scope=scope,
            actor=actor,
            scope=scope,
            fact_type=fact_type,
            statement=statement,
            source_refs=source_refs,
            entity_refs=entity_refs,
            expires_at=expires_at,
            reason=reason,
        )
        return self._partition(result, tenant_slug=tenant_slug, brand_id=brand_id, scope=scope)

    def replace(
        self,
        *,
        subject: str,
        tenant_slug: str,
        brand_id: str,
        memory_id: str,
        scope: str,
        fact_type: str,
        statement: str,
        source_refs: list[dict[str, Any]] | None,
        entity_refs: list[dict[str, Any]] | None,
        expires_at: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        actor = self._writer(subject, tenant_slug, brand_id, scope)
        result = self._call(
            self._repository.replace,
            conflict_scope=scope,
            actor=actor,
            memory_id=memory_id,
            scope=scope,
            fact_type=fact_type,
            statement=statement,
            source_refs=source_refs,
            entity_refs=entity_refs,
            expires_at=expires_at,
            reason=reason,
        )
        return self._partition(result, tenant_slug=tenant_slug, brand_id=brand_id, scope=scope)

    def forget(
        self,
        *,
        subject: str,
        tenant_slug: str,
        brand_id: str,
        memory_id: str,
        scope: str,
        reason: str | None,
    ) -> dict[str, Any]:
        actor = self._writer(subject, tenant_slug, brand_id, scope)
        result = self._call(
            self._repository.forget,
            conflict_scope=scope,
            actor=actor,
            memory_id=memory_id,
            scope=scope,
            reason=reason,
        )
        return self._partition(result, tenant_slug=tenant_slug, brand_id=brand_id, scope=scope)

    # -- policy ---------------------------------------------------------------

    def _identity(self, subject: str, tenant_slug: str, brand_id: str) -> BrandIdentity:
        return require_brand_role(
            subject,
            tenant_slug,
            brand_id,
            min_role=UserRole.MEMBER,
            registry=self._registry,
            session_factory=self._session_factory,
        )

    def _member(self, subject: str, tenant_slug: str, brand_id: str) -> ActorEnvelope:
        return _actor_for(self._identity(subject, tenant_slug, brand_id), subject)

    def _writer(self, subject: str, tenant_slug: str, brand_id: str, scope: str) -> ActorEnvelope:
        identity = self._identity(subject, tenant_slug, brand_id)
        if scope == MEMORY_SCOPE_BRAND and not role_satisfies(identity.role, _BRAND_WRITE_MIN_ROLE):
            raise ToolError("not_authorized: brand memory writes require ADMIN or SOLSTICE_STAFF")
        return _actor_for(identity, subject)

    @staticmethod
    def _partition(
        result: dict[str, Any], *, tenant_slug: str, brand_id: str, scope: str
    ) -> dict[str, Any]:
        return {
            **result,
            "tenant_slug": tenant_slug,
            "brand_id": None if scope == MEMORY_SCOPE_TENANT_PERSONAL else brand_id,
            "scope": scope,
        }

    @staticmethod
    def _call(fn: Any, conflict_scope: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(**kwargs)
        except BackendError as exc:
            raise tool_error(exc, scope=conflict_scope) from exc


def _actor_for(identity: BrandIdentity, subject: str) -> ActorEnvelope:
    return ActorEnvelope(
        actor_sub=subject,
        tenant_slug=identity.tenant_slug,
        brand_id=identity.brand_id,
        user_id=identity.user_id,
    )


def tool_error(exc: BackendError, *, scope: str) -> ToolError:
    """A Backend failure as one of the strings the tool descriptions name.

    The Backend's message is deliberately dropped: it may carry internal detail,
    and an agent can act on the category alone.
    """
    if not isinstance(exc, BackendStatusError):
        # No token, an unreachable Backend, or a 2xx that was not a payload —
        # the Backend never ruled on the request.
        return ToolError("service_unavailable: memory backend unavailable; retry later")
    if exc.status == 422:
        return ToolError("invalid_argument: backend rejected the memory payload")
    if exc.status in (401, 403):
        return ToolError("not_authorized: backend rejected the memory request")
    if exc.status == 404:
        return ToolError("not_found: memory fact not found in this partition")
    if exc.status == 409:
        return ToolError(f"conflict: memory {scope} write conflicted; restate and retry")
    if 500 <= exc.status < 600:
        return ToolError("service_unavailable: memory backend unavailable; retry later")
    return ToolError("internal_error: memory backend returned an unexpected result")
