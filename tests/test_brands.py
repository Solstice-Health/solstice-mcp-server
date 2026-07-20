from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import (
    BRAND_A1,
    BRAND_A2,
    BRAND_A3,
    BRAND_A4,
    BRAND_A5,
    BRAND_B1,
    OTHER_SUB,
    SHARED_SUB,
    AppHarness,
)
from test_server import rpc, tool_payload

from solstice_mcp.brands import (
    UserRole,
    list_brands_for_user,
    require_brand_role,
    resolve_brand_role,
)


def _result(response) -> dict[str, Any]:
    """Return the full JSON-RPC result object (so callers can inspect isError)."""
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool_error_text(response) -> str:
    """Assert the tool call returned an MCP error result and return its text."""
    result = _result(response)
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# solstice_list_brands — end-to-end via the MCP protocol
# ---------------------------------------------------------------------------


def test_list_brands_returns_per_brand_roles_for_same_tenant(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_brands", "arguments": {"tenant_slug": "tenant_a"}},
    )
    payload = tool_payload(response)
    assert payload["tenant_slug"] == "tenant_a"
    by_id = {b["brand_id"]: b["role"] for b in payload["brands"]}
    # SHARED is ADMIN on a1, MEMBER on a2; a4 (soft-deleted membership) and
    # a5 (soft-deleted brand) are excluded.
    assert by_id == {BRAND_A1: "ADMIN", BRAND_A2: "MEMBER"}
    assert payload["count"] == 2


def test_list_brands_returns_staff_role_for_other_user(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=OTHER_SUB),
        params={"name": "solstice_list_brands", "arguments": {"tenant_slug": "tenant_a"}},
    )
    payload = tool_payload(response)
    by_id = {b["brand_id"]: b["role"] for b in payload["brands"]}
    # OTHER is MEMBER on a1, SOLSTICE_STAFF on a2, MEMBER on a3.
    assert by_id == {BRAND_A1: "MEMBER", BRAND_A2: "SOLSTICE_STAFF", BRAND_A3: "MEMBER"}


def test_list_brands_empty_for_tenant_with_no_brands(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_brands", "arguments": {"tenant_slug": "tenant_prod"}},
    )
    payload = tool_payload(response)
    assert payload == {"tenant_slug": "tenant_prod", "brands": [], "count": 0}


def test_list_brands_empty_when_not_a_tenant_member(app_harness: AppHarness, mint_token):
    # OTHER is not a member of tenant_b at all.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=OTHER_SUB),
        params={"name": "solstice_list_brands", "arguments": {"tenant_slug": "tenant_b"}},
    )
    payload = tool_payload(response)
    assert payload == {"tenant_slug": "tenant_b", "brands": [], "count": 0}


def test_list_brands_works_cross_tenant_when_authorized(app_harness: AppHarness, mint_token):
    # SHARED is also a member of tenant_b and holds ADMIN on brand_b1.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_list_brands", "arguments": {"tenant_slug": "tenant_b"}},
    )
    payload = tool_payload(response)
    assert {b["brand_id"]: b["role"] for b in payload["brands"]} == {BRAND_B1: "ADMIN"}


# ---------------------------------------------------------------------------
# solstice_brand_info — the gated tool (min_role = MEMBER)
# ---------------------------------------------------------------------------


def test_brand_info_ok_for_member(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A1},
        },
    )
    payload = tool_payload(response)
    assert payload["status"] == "ok"
    assert payload["brand_id"] == BRAND_A1
    assert payload["brand_name"] == "Brand A1"
    assert payload["role"] == "ADMIN"
    assert payload["tenant_slug"] == "tenant_a"


def test_brand_info_ok_for_staff(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=OTHER_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A2},
        },
    )
    payload = tool_payload(response)
    assert payload["status"] == "ok"
    assert payload["role"] == "SOLSTICE_STAFF"


def test_brand_info_denied_for_brand_the_user_is_not_on(app_harness: AppHarness, mint_token):
    # SHARED is not on BRAND_A3 (only OTHER is). Passing its brand_id must not
    # grant access — the server re-derives the role from SHARED's own row.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A3},
        },
    )
    assert "not_authorized" in _tool_error_text(response)


def test_brand_info_denied_for_soft_deleted_membership(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A4},
        },
    )
    assert "not_authorized" in _tool_error_text(response)


def test_brand_info_denied_for_soft_deleted_brand(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_A5},
        },
    )
    assert "not_authorized" in _tool_error_text(response)


def test_brand_info_denied_cross_tenant(app_harness: AppHarness, mint_token):
    # OTHER is not a tenant_b member, so the tenant gate fails before the
    # brand check even runs.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=OTHER_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_b", "brand_id": BRAND_B1},
        },
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# Injection-resistance: a brand_id argument never grants authority
# ---------------------------------------------------------------------------


def test_brand_id_argument_does_not_grant_access(app_harness: AppHarness, mint_token):
    # SHARED calls brand_info with BRAND_B1's id but in tenant_a, where no such
    # brand exists / SHARED has no row. Even though SHARED legitimately owns
    # BRAND_B1 in tenant_b, the tenant_a+BRAND_B1 triple resolves to no row.
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={
            "name": "solstice_brand_info",
            "arguments": {"tenant_slug": "tenant_a", "brand_id": BRAND_B1},
        },
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# Resolver / enforcement unit tests (the injection-proof core)
# ---------------------------------------------------------------------------


def test_resolve_brand_role_returns_correct_role(app_harness: AppHarness):
    identity = resolve_brand_role(
        SHARED_SUB,
        "tenant_a",
        BRAND_A1,
        registry=app_harness.registry,
        session_factory=app_harness.session_factory,
    )
    assert identity is not None
    assert identity.role is UserRole.ADMIN
    assert identity.brand_name == "Brand A1"


def test_resolve_brand_role_none_for_non_member_brand(app_harness: AppHarness):
    assert (
        resolve_brand_role(
            SHARED_SUB,
            "tenant_a",
            BRAND_A3,
            registry=app_harness.registry,
            session_factory=app_harness.session_factory,
        )
        is None
    )


def test_resolve_brand_role_none_cross_tenant(app_harness: AppHarness):
    # OTHER is not a tenant_b member.
    assert (
        resolve_brand_role(
            OTHER_SUB,
            "tenant_b",
            BRAND_B1,
            registry=app_harness.registry,
            session_factory=app_harness.session_factory,
        )
        is None
    )


def test_require_brand_role_denies_escalation(app_harness: AppHarness):
    # SHARED is ADMIN on BRAND_A1. ADMIN does not satisfy a SOLSTICE_STAFF gate.
    with pytest.raises(Exception, match="not_authorized"):
        require_brand_role(
            SHARED_SUB,
            "tenant_a",
            BRAND_A1,
            min_role=UserRole.SOLSTICE_STAFF,
            registry=app_harness.registry,
            session_factory=app_harness.session_factory,
        )


def test_require_brand_role_allows_staff_to_satisfy_lower_gate(app_harness: AppHarness):
    # OTHER is SOLSTICE_STAFF on BRAND_A2; that satisfies an ADMIN gate.
    identity = require_brand_role(
        OTHER_SUB,
        "tenant_a",
        BRAND_A2,
        min_role=UserRole.ADMIN,
        registry=app_harness.registry,
        session_factory=app_harness.session_factory,
    )
    assert identity.role is UserRole.SOLSTICE_STAFF


def test_require_brand_role_denies_missing_brand(app_harness: AppHarness):
    with pytest.raises(Exception, match="not_authorized"):
        require_brand_role(
            SHARED_SUB,
            "tenant_a",
            BRAND_A3,
            min_role=UserRole.MEMBER,
            registry=app_harness.registry,
            session_factory=app_harness.session_factory,
        )


def test_list_brands_for_user_excludes_soft_deleted(app_harness: AppHarness):
    memberships = list_brands_for_user(
        SHARED_SUB,
        "tenant_a",
        registry=app_harness.registry,
        session_factory=app_harness.session_factory,
    )
    by_id = {m.brand_id: m.role for m in memberships}
    assert by_id == {BRAND_A1: UserRole.ADMIN, BRAND_A2: UserRole.MEMBER}


# ---------------------------------------------------------------------------
# Context injection — solstice_server_info advertises the RBAC model
# ---------------------------------------------------------------------------


def test_server_info_advertises_rbac_model(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={"name": "solstice_server_info", "arguments": {}},
    )
    payload = tool_payload(response)
    assert payload["rbac"]["roles"] == ["MEMBER", "ADMIN", "SOLSTICE_STAFF"]
    assert payload["rbac"]["super_user"] == "SOLSTICE_STAFF (brand-scoped, not tenant-wide)"
    assert "solstice_list_brands" in payload["tools"]
    assert "solstice_brand_info" in payload["tools"]


def test_json_dump_of_rbac_payload_is_stable(app_harness: AppHarness, mint_token):
    # Guards against accidentally returning a non-serializable UserRole enum
    # in a tool payload (which would surface as a 500 to the client).
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(),
        params={"name": "solstice_server_info", "arguments": {}},
    )
    text = response.json()["result"]["content"][0]["text"]
    assert json.loads(text)["rbac"]["roles"] == ["MEMBER", "ADMIN", "SOLSTICE_STAFF"]
