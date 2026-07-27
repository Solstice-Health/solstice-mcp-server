"""User-admin tools: solstice_reset_password, solstice_change_brand_role, solstice_add_user."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest
from conftest import (
    BRAND_A1,
    BRAND_A2,
    BRAND_B1,
    CENTRAL_USER_SHARED,
    COMPANY_A,
    OTHER_SUB,
    SHARED_SUB,
    STAFF_SUB,
    USER_A_OTHER,
    USER_A_SHARED,
    AppHarness,
)
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select
from test_server import audit_events, rpc, tool_payload

from solstice_mcp.audit import AUDIT_LOGGER_NAME
from solstice_mcp.brands import BrandTeamMember
from solstice_mcp.tenants import User
from solstice_mcp.user_admin import _resolve_company_id

TENANT = "tenant_a"
AUTH0_OTHER = [{"user_id": OTHER_SUB, "email": "other@a.test", "email_verified": True}]
TEMP_PASSWORD_RE = re.compile(r"^[A-Za-z2-9]{6}-\d{3}-[A-Za-z2-9]{6}!$")


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(harness, "tools/call", token=token, params={"name": name, "arguments": args})


def _tool_error_text(response) -> str:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _set_auth0_response(harness: AppHarness, method: str, path: str, payload: Any, status: int = 200) -> None:
    harness.auth0_opener.responses[(method, path)] = (status, json.dumps(payload).encode())


def _auth0_calls(harness: AppHarness, method: str, path: str) -> list[dict[str, Any]]:
    return [c for c in harness.auth0_opener.calls if c["method"] == method and c["path"] == path]


# ---------------------------------------------------------------------------
# solstice_reset_password
# ---------------------------------------------------------------------------


def test_reset_password_email_mode(app_harness: AppHarness, mint_token):
    _set_auth0_response(app_harness, "GET", "/api/v2/users-by-email", AUTH0_OTHER)
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password", {"tenant_slug": TENANT, "email": "other@a.test"},
    ))
    assert payload["reset_email_sent"] is True
    assert payload["mode"] == "email"
    assert payload["auth0_id"] == OTHER_SUB
    assert "temp_password" not in payload

    sends = _auth0_calls(app_harness, "POST", "/dbconnections/change_password")
    assert len(sends) == 1
    body = json.loads(sends[0]["body"])
    assert body["email"] == "other@a.test"
    assert body["client_id"] == "test-mgmt-client"
    # Authentication API call carries no management bearer.
    assert not any(k.lower() == "authorization" for k in sends[0]["headers"])


def test_reset_password_temp_mode_patches_auth0_and_returns_password(app_harness: AppHarness, mint_token):
    _set_auth0_response(app_harness, "GET", "/api/v2/users-by-email", AUTH0_OTHER)
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password",
        {"tenant_slug": TENANT, "email": "other@a.test", "mode": "temp_password"},
    ))
    assert TEMP_PASSWORD_RE.match(payload["temp_password"]), payload["temp_password"]

    patches = _auth0_calls(app_harness, "PATCH", f"/api/v2/users/{OTHER_SUB.replace('|', '%7C')}")
    assert len(patches) == 1
    body = json.loads(patches[0]["body"])
    assert body["password"] == payload["temp_password"]
    assert body["connection"] == "Username-Password-Authentication"
    # No reset email was dispatched in temp mode.
    assert not _auth0_calls(app_harness, "POST", "/dbconnections/change_password")


def test_reset_password_denied_without_staff_role(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN+MEMBER in tenant_a — no SOLSTICE_STAFF row anywhere.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_reset_password", {"tenant_slug": TENANT, "email": "other@a.test"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert not app_harness.auth0_opener.calls


def test_reset_password_requires_target_in_tenant(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password", {"tenant_slug": TENANT, "email": "nobody@a.test"},
    )
    assert "not_found" in _tool_error_text(response)
    assert not app_harness.auth0_opener.calls


def test_reset_password_rejects_unknown_mode(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password",
        {"tenant_slug": TENANT, "email": "other@a.test", "mode": "carrier_pigeon"},
    )
    assert "invalid_arguments" in _tool_error_text(response)


def test_reset_password_errors_when_auth0_account_missing(app_harness: AppHarness, mint_token):
    # Default fake Auth0 response is a JSON object -> "no user found".
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password", {"tenant_slug": TENANT, "email": "other@a.test"},
    )
    assert "not_found" in _tool_error_text(response)


def test_reset_password_audit_records_mode_but_never_the_password(
    app_harness: AppHarness, mint_token, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    _set_auth0_response(app_harness, "GET", "/api/v2/users-by-email", AUTH0_OTHER)
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_reset_password",
        {"tenant_slug": TENANT, "email": "other@a.test", "mode": "temp_password"},
    ))
    event = audit_events(caplog)[-1]
    assert event["tool"] == "solstice_reset_password"
    assert event["resources"]["mode"] == "temp_password"
    assert event["outcome"] == "success"
    dumped = json.dumps(event)
    assert payload["temp_password"] not in dumped
    assert "other@a.test" not in dumped


# ---------------------------------------------------------------------------
# solstice_change_brand_role
# ---------------------------------------------------------------------------


def _brand_role(harness: AppHarness, brand_id: str, user_id: str) -> str:
    with harness.session_factory(TENANT) as session:
        row = session.scalar(
            select(BrandTeamMember).where(
                BrandTeamMember.brand_id == brand_id, BrandTeamMember.user_id == user_id
            )
        )
        assert row is not None
        return row.user_role


def test_change_brand_role_updates_membership(app_harness: AppHarness, mint_token):
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_change_brand_role",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1, "email": "other@a.test", "new_role": "ADMIN"},
    ))
    assert payload["previous_role"] == "MEMBER"
    assert payload["new_role"] == "ADMIN"
    assert _brand_role(app_harness, BRAND_A1, USER_A_OTHER) == "ADMIN"


def test_change_brand_role_can_grant_staff(app_harness: AppHarness, mint_token):
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_change_brand_role",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1, "email": "other@a.test", "new_role": "SOLSTICE_STAFF"},
    ))
    assert payload["new_role"] == "SOLSTICE_STAFF"
    assert _brand_role(app_harness, BRAND_A1, USER_A_OTHER) == "SOLSTICE_STAFF"


def test_change_brand_role_requires_staff_on_that_brand(app_harness: AppHarness, mint_token):
    # OTHER_SUB is SOLSTICE_STAFF on BRAND_A2 but only MEMBER on BRAND_A1 —
    # staff elsewhere in the tenant is not enough.
    response = _call(
        app_harness, mint_token(sub=OTHER_SUB),
        "solstice_change_brand_role",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1, "email": "alice@a.test", "new_role": "ADMIN"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert _brand_role(app_harness, BRAND_A1, USER_A_SHARED) == "ADMIN"  # unchanged


def test_change_brand_role_rejects_target_not_on_brand(app_harness: AppHarness, mint_token):
    # OTHER_SUB is staff on BRAND_A2; staff@a.test has no membership there.
    response = _call(
        app_harness, mint_token(sub=OTHER_SUB),
        "solstice_change_brand_role",
        {"tenant_slug": TENANT, "brand_id": BRAND_A2, "email": "staff@a.test", "new_role": "MEMBER"},
    )
    assert "not_found" in _tool_error_text(response)


def test_change_brand_role_rejects_unknown_role(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_change_brand_role",
        {"tenant_slug": TENANT, "brand_id": BRAND_A1, "email": "other@a.test", "new_role": "SUPERUSER"},
    )
    assert "invalid_arguments" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_add_user
# ---------------------------------------------------------------------------


def test_add_user_full_onboarding(app_harness: AppHarness, mint_token):
    _set_auth0_response(app_harness, "POST", "/api/v2/users", {"user_id": "auth0|new"})
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_add_user",
        {
            "tenant_slug": TENANT,
            "email": "new.person@pfizer.test",
            "name": "New Person",
            "brand_id": BRAND_A1,
            "role": "MEMBER",
        },
    ))
    assert payload["created_in_auth0"] is True
    assert payload["auth0_id"] == "auth0|new"
    assert payload["company_id"] == COMPANY_A
    assert payload["brand_role"] == "MEMBER"
    assert payload["reset_email_sent"] is True
    # Canonical id: the fresh central row id is reused for the tenant row.
    assert payload["user_id"] == payload["central_user_id"]

    created = json.loads(_auth0_calls(app_harness, "POST", "/api/v2/users")[0]["body"])
    assert created["email_verified"] is True
    assert created["verify_email"] is False
    with app_harness.central_session_factory() as session:
        central = session.scalar(select(User).where(User.email == "new.person@pfizer.test"))
        assert central is not None and str(central.id) == payload["central_user_id"]
        assert central.auth0_id == "auth0|new"
    with app_harness.session_factory(TENANT) as session:
        tenant_row = session.scalar(select(User).where(User.email == "new.person@pfizer.test"))
        assert tenant_row is not None and str(tenant_row.company_id) == COMPANY_A
        membership = session.scalar(
            select(BrandTeamMember).where(
                BrandTeamMember.brand_id == BRAND_A1, BrandTeamMember.user_id == tenant_row.id
            )
        )
        assert membership is not None and membership.user_role == "MEMBER"
    assert len(_auth0_calls(app_harness, "POST", "/dbconnections/change_password")) == 1


def test_add_user_existing_everywhere_completes_missing_pieces(app_harness: AppHarness, mint_token):
    # Alice exists in Auth0, central auth, and tenant_a (as MEMBER on A2).
    # Re-adding with role=ADMIN on A2 keeps both row ids and updates the role.
    _set_auth0_response(
        app_harness, "GET", "/api/v2/users-by-email",
        [{"user_id": SHARED_SUB, "email": "alice@a.test", "email_verified": False}],
    )
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_add_user",
        {
            "tenant_slug": TENANT,
            "email": "alice@a.test",
            "name": "Alice",
            "brand_id": BRAND_A2,
            "role": "ADMIN",
            "send_reset_email": False,
        },
    ))
    assert payload["created_in_auth0"] is False
    assert payload["user_id"] == USER_A_SHARED  # tenant row keeps its id
    assert payload["central_user_id"] == CENTRAL_USER_SHARED  # central row keeps its id
    assert _brand_role(app_harness, BRAND_A2, USER_A_SHARED) == "ADMIN"
    # Existing Auth0 user is re-verified, no create, no reset email.
    patches = _auth0_calls(app_harness, "PATCH", f"/api/v2/users/{SHARED_SUB.replace('|', '%7C')}")
    assert json.loads(patches[0]["body"]) == {"email_verified": True}
    assert not _auth0_calls(app_harness, "POST", "/api/v2/users")
    assert not _auth0_calls(app_harness, "POST", "/dbconnections/change_password")


def test_add_user_recovers_from_auth0_create_race(app_harness: AppHarness, mint_token):
    # First lookup finds nothing, create hits 409 (another writer won the
    # race), fallback lookup finds the account -> onboarding still completes.
    app_harness.auth0_opener.responses[("GET", "/api/v2/users-by-email")] = [
        (200, b"{}"),
        (200, json.dumps([{"user_id": "auth0|raced", "email": "raced@a.test"}]).encode()),
    ]
    _set_auth0_response(app_harness, "POST", "/api/v2/users", {"error": "user exists"}, status=409)
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_add_user",
        {"tenant_slug": TENANT, "email": "raced@a.test", "name": "Raced", "send_reset_email": False},
    ))
    assert payload["created_in_auth0"] is False
    assert payload["auth0_id"] == "auth0|raced"
    # The raced account is re-verified and the DB rows still land.
    patches = _auth0_calls(app_harness, "PATCH", "/api/v2/users/auth0%7Craced")
    assert json.loads(patches[0]["body"]) == {"email_verified": True}
    with app_harness.session_factory(TENANT) as session:
        assert session.scalar(select(User).where(User.email == "raced@a.test")) is not None


def test_add_user_reports_email_failure_without_hiding_created_rows(app_harness: AppHarness, mint_token):
    # All rows commit, then the reset-email send fails: the tool must return
    # the success payload with reset_email_sent=false, not raise.
    _set_auth0_response(app_harness, "POST", "/api/v2/users", {"user_id": "auth0|emailfail"})
    _set_auth0_response(app_harness, "POST", "/dbconnections/change_password", {"error": "boom"}, status=500)
    payload = tool_payload(_call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_add_user",
        {"tenant_slug": TENANT, "email": "emailfail@a.test", "name": "Email Fail"},
    ))
    assert payload["reset_email_sent"] is False
    assert "HTTP 500" in payload["reset_email_error"]
    assert payload["auth0_id"] == "auth0|emailfail"
    assert payload["user_id"] == payload["central_user_id"]
    with app_harness.session_factory(TENANT) as session:
        assert session.scalar(select(User).where(User.email == "emailfail@a.test")) is not None


def test_add_user_denied_without_staff_role(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_add_user",
        {"tenant_slug": TENANT, "email": "x@y.test", "name": "X"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert not app_harness.auth0_opener.calls


def test_add_user_rejects_invalid_email_and_role(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    bad_email = _call(
        app_harness, token, "solstice_add_user",
        {"tenant_slug": TENANT, "email": "not-an-email", "name": "X"},
    )
    assert "invalid_arguments" in _tool_error_text(bad_email)
    bad_role = _call(
        app_harness, token, "solstice_add_user",
        {"tenant_slug": TENANT, "email": "x@y.test", "name": "X", "role": "OVERLORD"},
    )
    assert "invalid_arguments" in _tool_error_text(bad_role)


def test_resolve_company_id_errors_without_a_resolvable_company(app_harness: AppHarness):
    # tenant_b's only brand has no company_id: both resolution paths must
    # fail loudly instead of inserting a user row with a bogus company.
    with app_harness.session_factory("tenant_b") as session:
        with pytest.raises(ToolError, match="zero or multiple companies"):
            _resolve_company_id(session, None)
        with pytest.raises(ToolError, match="no company_id"):
            _resolve_company_id(session, BRAND_B1)
