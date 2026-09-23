"""What the memory repository puts on the wire.

Direct contract smoke tests: the Backend's routes are pinned here rather than
only through the tool face, so a query-string or body change is a failing test
and not a runtime 422.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import BRAND_A1, FakeBackendOpener

from solstice_mcp.repositories.solstice_backend.memory import ActorEnvelope, MemoryRepository
from solstice_mcp.repositories.solstice_backend.session import BackendSession


class _FakeAcquirer:
    def get_token(self) -> str:
        return "m2m-bearer"

    def invalidate(self) -> None:
        pass


def _repo(opener: FakeBackendOpener) -> MemoryRepository:
    return MemoryRepository(
        BackendSession(
            base_url="https://backend.test",
            token_acquirer=_FakeAcquirer(),
            timeout=5.0,
            opener=opener,
        )
    )


def _headers(call: dict[str, Any]) -> dict[str, Any]:
    return {k.lower(): v for k, v in call["headers"].items()}


def _actor() -> ActorEnvelope:
    return ActorEnvelope(actor_sub="sub-1", tenant_slug="tenant_a", brand_id=BRAND_A1, user_id="u-1")


def test_recall_emits_exact_query_and_headers():
    opener = FakeBackendOpener()
    opener.responses[("GET", "/api/internal/agent-memory")] = (
        200,
        json.dumps({"brand": [], "personal": [], "tenant_personal": []}).encode("utf-8"),
    )
    _repo(opener).recall(actor=_actor(), fact_type="preference", entity_id="op-9", q="email", limit=25)

    call = opener.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/api/internal/agent-memory"
    assert f"brand_id={BRAND_A1}" in call["url"]
    assert "actor_sub=sub-1" in call["url"]
    assert "tenant_slug=tenant_a" in call["url"]
    assert "fact_type=preference" in call["url"]
    assert "entity_id=op-9" in call["url"]
    assert "q=email" in call["url"]
    assert "limit=25" in call["url"]
    headers = _headers(call)
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == "tenant_a"
    assert "x-solstice-actor" not in headers
    assert call["body"] is None


def test_remember_emits_exact_body_and_headers():
    opener = FakeBackendOpener()
    opener.responses[("POST", "/api/internal/agent-memory")] = (
        200,
        json.dumps({"fact": {"memory_id": "mem-1", "status": "active"}}).encode("utf-8"),
    )
    _repo(opener).remember(
        actor=_actor(),
        scope="personal",
        fact_type="decision",
        statement="ship Q3",
        source_refs=[{"source_type": "claim", "source_id": "c1", "source_version": "v1", "fingerprint": "fp"}],
        entity_refs=[{"entity_type": "brand", "entity_id": BRAND_A1, "entity_version": "v1"}],
        expires_at="2027-01-01T00:00:00Z",
        reason="user confirmed",
    )

    call = opener.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/internal/agent-memory"
    headers = _headers(call)
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer m2m-bearer"
    assert headers["x-tenant-slug"] == "tenant_a"
    assert "x-solstice-actor" not in headers
    assert json.loads(call["body"]) == {
        "brand_id": BRAND_A1,
        "scope": "personal",
        "fact_type": "decision",
        "statement": "ship Q3",
        "source_refs": [{"source_type": "claim", "source_id": "c1", "source_version": "v1", "fingerprint": "fp"}],
        "entity_refs": [{"entity_type": "brand", "entity_id": BRAND_A1, "entity_version": "v1"}],
        "expires_at": "2027-01-01T00:00:00Z",
        "reason": "user confirmed",
        "actor_sub": "sub-1",
        "tenant_slug": "tenant_a",
    }
