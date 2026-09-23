"""The Backend's agent-memory routes.

The MCP server stays stateless: it validates the end-user OAuth subject,
rechecks tenant/brand membership via ``require_brand_role``, then calls these
routes with a server-derived actor envelope. The MCP never touches the tenant
Postgres store; Backend is the sole trust root for memory writes.

The actor fields in the request query/body are revalidated against the tenant
DB; they never grant access on their own. Caller-supplied roles are never sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solstice_mcp.repositories.solstice_backend.session import BackendSession

MEMORY_SCOPE_PERSONAL = "personal"
MEMORY_SCOPE_TENANT_PERSONAL = "tenant_personal"
MEMORY_SCOPE_BRAND = "brand"
MEMORY_SCOPES = (
    MEMORY_SCOPE_TENANT_PERSONAL,
    MEMORY_SCOPE_PERSONAL,
    MEMORY_SCOPE_BRAND,
)


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


class MemoryRepository:
    """The ``/api/internal/agent-memory`` routes."""

    def __init__(self, session: BackendSession) -> None:
        self._session = session

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
        return self._session.request(
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
        return self._session.request(
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
        return self._session.request(
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
        return self._session.request(
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
        statement: str,
        fact_type: str,
        semantic_subject: str,
        entity_refs: list[dict[str, Any]],
        source_refs: list[dict[str, Any]],
        occurred_at: str,
        brand_id: str | None = None,
        host_correlation_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "actor_sub": actor_sub,
            "tenant_slug": tenant_slug,
            "scope": scope,
            "statement": statement,
            "fact_type": fact_type,
            "semantic_subject": semantic_subject,
            "entity_refs": entity_refs,
            "source_refs": source_refs,
            "occurred_at": occurred_at,
        }
        if brand_id is not None:
            body["brand_id"] = brand_id
        if host_correlation_id is not None:
            body["host_correlation_id"] = host_correlation_id
        return self._session.request(
            "POST",
            "/api/internal/agent-memory/observations",
            tenant_slug=tenant_slug,
            json_body=body,
        )


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


__all__ = [
    "MEMORY_SCOPES",
    "MEMORY_SCOPE_BRAND",
    "MEMORY_SCOPE_PERSONAL",
    "MEMORY_SCOPE_TENANT_PERSONAL",
    "ActorEnvelope",
    "MemoryRepository",
]
