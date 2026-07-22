"""Admin-request triage tools: solstice_list_requests, solstice_dismiss_request."""

from __future__ import annotations

from typing import Any

from conftest import (
    BRAND_A1,
    BRAND_A3,
    OTHER_SUB,
    REQ_COMPLETED_A1,
    REQ_ORPHAN_A1,
    REQ_PENDING_A1,
    REQ_PENDING_A3,
    SHARED_SUB,
    STAFF_SUB,
    USER_A_STAFF,
    AppHarness,
)
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.requests import AdminRequest

TENANT = "tenant_a"


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness, "tools/call", token=token,
        params={"name": name, "arguments": args},
    )


def _tool_error_text(response) -> str:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _row(harness: AppHarness, request_id: str) -> AdminRequest:
    with harness.session_factory(TENANT) as session:
        row = session.scalar(select(AdminRequest).where(AdminRequest.id == request_id))
        assert row is not None
        return row


# ---------------------------------------------------------------------------
# solstice_list_requests
# ---------------------------------------------------------------------------


def test_list_pending_is_tenant_wide_for_staff(app_harness: AppHarness, mint_token):
    # STAFF_SUB is SOLSTICE_STAFF on BRAND_A1 only, but the read covers the
    # whole tenant queue — including the BRAND_A3 pending row.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_list_requests", {"tenant_slug": TENANT},
    )
    payload = tool_payload(response)
    ids = [r["id"] for r in payload["requests"]]
    # Newest first: orphan (Jan 7), A3 approval (Jan 6), A1 initial (Jan 5).
    assert ids == [REQ_ORPHAN_A1, REQ_PENDING_A3, REQ_PENDING_A1]
    by_id = {r["id"]: r for r in payload["requests"]}
    a1 = by_id[REQ_PENDING_A1]
    assert a1["brand_name"] == "Brand A1"
    assert a1["requester"]["email"] == "alice@a.test"
    assert a1["assigned_to"]["user_id"] == USER_A_STAFF
    assert a1["priority"] == "high"
    a3 = by_id[REQ_PENDING_A3]
    assert a3["brand_id"] == BRAND_A3
    assert a3["message"] == "please approve for Veeva"
    orphan = by_id[REQ_ORPHAN_A1]
    assert orphan["operation_deleted"] is True


def test_list_status_and_brand_filters(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    completed = tool_payload(_call(
        app_harness, token, "solstice_list_requests",
        {"tenant_slug": TENANT, "status": "completed"},
    ))
    assert [r["id"] for r in completed["requests"]] == [REQ_COMPLETED_A1]
    assert completed["requests"][0]["comment_count"] == 1
    assert completed["requests"][0]["additional_comment"] == "asap"
    assert completed["requests"][0]["resolved_version_number"] == 2

    everything = tool_payload(_call(
        app_harness, token, "solstice_list_requests",
        {"tenant_slug": TENANT, "status": "all"},
    ))
    assert everything["count"] == 4

    brand_scoped = tool_payload(_call(
        app_harness, token, "solstice_list_requests",
        {"tenant_slug": TENANT, "status": "all", "brand_id": BRAND_A1},
    ))
    assert {r["brand_id"] for r in brand_scoped["requests"]} == {BRAND_A1}
    assert brand_scoped["count"] == 3


def test_list_rejects_unknown_status(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_list_requests", {"tenant_slug": TENANT, "status": "bogus"},
    )
    assert "invalid_arguments" in _tool_error_text(response)


def test_list_denied_without_staff_role_anywhere(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on A1 + MEMBER on A2 — no SOLSTICE_STAFF row in tenant_a.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_list_requests", {"tenant_slug": TENANT},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_list_allowed_for_staff_on_any_brand(app_harness: AppHarness, mint_token):
    # OTHER is SOLSTICE_STAFF on BRAND_A2 (a brand with no requests) — that
    # alone opens the tenant queue.
    response = _call(
        app_harness, mint_token(sub=OTHER_SUB),
        "solstice_list_requests", {"tenant_slug": TENANT},
    )
    assert tool_payload(response)["count"] == 3


def test_list_denied_in_tenant_without_staff_role(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on BRAND_B1 in tenant_b — still not staff.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_list_requests", {"tenant_slug": "tenant_b"},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# solstice_dismiss_request
# ---------------------------------------------------------------------------


def test_dismiss_pending_request(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_PENDING_A1,
         "reason_category": "duplicate", "reason_text": "raised twice"},
    )
    payload = tool_payload(response)
    assert payload["status"] == "dismissed"
    assert payload["reason_category"] == "duplicate"
    row = _row(app_harness, REQ_PENDING_A1)
    assert row.status == "dismissed"
    assert row.resolved_by_user_id == USER_A_STAFF
    assert row.resolved_at is not None
    dismissal = row.request_metadata["dismissal"]
    assert dismissal["category"] == "duplicate"
    assert dismissal["text"] == "raised twice"
    assert dismissal["dismissed_by_user_id"] == USER_A_STAFF
    # Pre-existing metadata keys are preserved (merge, not replace).
    assert row.request_metadata["project_name"] == "Project P1"


def test_dismiss_orphaned_request_whose_operation_is_gone(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_ORPHAN_A1, "reason_category": "invalid"},
    )
    assert tool_payload(response)["status"] == "dismissed"
    row = _row(app_harness, REQ_ORPHAN_A1)
    assert row.status == "dismissed"
    # The row is dismissed, never deleted, and keeps its GC stamp.
    assert row.deleted_at is None
    assert row.request_metadata["operation_deleted"] is True


def test_dismiss_requires_staff_on_the_rows_own_brand(app_harness: AppHarness, mint_token):
    # STAFF_SUB is staff on BRAND_A1 but the A3 request belongs to BRAND_A3:
    # tenant-wide read does NOT imply tenant-wide dismiss.
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_PENDING_A3, "reason_category": "other"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert _row(app_harness, REQ_PENDING_A3).status == "pending"


def test_dismiss_rejects_non_pending_row(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=STAFF_SUB),
        "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_COMPLETED_A1, "reason_category": "other"},
    )
    assert "invalid_request" in _tool_error_text(response)
    assert _row(app_harness, REQ_COMPLETED_A1).status == "completed"


def test_dismiss_rejects_bad_category_and_long_text(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    bad_category = _call(
        app_harness, token, "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_PENDING_A1, "reason_category": "meh"},
    )
    assert "invalid_arguments" in _tool_error_text(bad_category)
    long_text = _call(
        app_harness, token, "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_PENDING_A1,
         "reason_category": "other", "reason_text": "x" * 501},
    )
    assert "invalid_arguments" in _tool_error_text(long_text)
    assert _row(app_harness, REQ_PENDING_A1).status == "pending"


def test_dismiss_denied_for_admin(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on BRAND_A1 — below the staff gate.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_dismiss_request",
        {"tenant_slug": TENANT, "request_id": REQ_PENDING_A1, "reason_category": "other"},
    )
    assert "not_authorized" in _tool_error_text(response)
    assert _row(app_harness, REQ_PENDING_A1).status == "pending"
