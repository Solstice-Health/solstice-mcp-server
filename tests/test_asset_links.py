"""asset_url on operation write responses (create / edit-create / commit / approve).

Every successful covered write returns a clickable Solstice asset link so an
agent can hand the user a URL instead of a bare operation UUID:

    https://www.<tenant-host>.solsticehealth.co/home/assets/<operation_id>

where the tenant host is the tenant slug with underscores converted to
hyphens (sanofi_sandbox -> sanofi-sandbox). prepare_operation_version is
deliberately NOT covered: preparation does not mean the upload committed.
"""

from __future__ import annotations

from typing import Any

from conftest import (
    OP_A1,
    PROJECT_P2,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
)
from test_server import rpc, tool_payload

from solstice_mcp.operations import build_asset_url

TENANT = "tenant_a"
BUCKET = "test-bucket-a"


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness, "tools/call", token=token,
        params={"name": name, "arguments": args},
    )


def _expected(op_id: str) -> str:
    # tenant_a -> tenant-a in the subdomain.
    return f"https://www.tenant-a.solsticehealth.co/home/assets/{op_id}"


def _prepare_and_upload(
    harness: AppHarness, token: str, op_id: str, kind: str, file_name: str | None
) -> dict[str, Any]:
    prep = tool_payload(_call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": kind, "file_name": file_name},
    ))
    harness.s3.put(BUCKET, prep["s3_key"], b"file-bytes")
    return prep


# ---------------------------------------------------------------------------
# URL builder unit tests
# ---------------------------------------------------------------------------


def test_build_asset_url_converts_underscores_to_hyphens():
    assert build_asset_url("sanofi_sandbox", "op-123") == (
        "https://www.sanofi-sandbox.solsticehealth.co/home/assets/op-123"
    )


def test_build_asset_url_plain_slug_unchanged():
    assert build_asset_url("incyte", "op-123") == (
        "https://www.incyte.solsticehealth.co/home/assets/op-123"
    )


# ---------------------------------------------------------------------------
# create / edit-create
# ---------------------------------------------------------------------------


def test_create_operation_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1 (owns P2)
    payload = tool_payload(_call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "linked.html",
         "content_type": "EMAIL"},
    ))
    assert payload["asset_url"] == _expected(payload["operation_id"])


def test_create_edit_operation_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(_call(
        app_harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "edit.html",
         "kind": "html", "content_type": "EMAIL"},
    ))
    assert payload["asset_url"] == _expected(payload["operation_id"])


# ---------------------------------------------------------------------------
# commit (html / pdf / source)
# ---------------------------------------------------------------------------


def test_commit_html_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "op_a1.html"},
    ))
    assert payload["asset_url"] == _expected(OP_A1)


def test_commit_pdf_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "pdf", "op_a1.pdf")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "pdf",
         "s3_key": prep["s3_key"], "file_name": "op_a1.pdf"},
    ))
    assert payload["asset_url"] == _expected(OP_A1)


def test_commit_source_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    created = tool_payload(_call(
        app_harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "with-source.pdf",
         "kind": "pdf", "content_type": "EMAIL"},
    ))
    op_id = created["operation_id"]
    prep = _prepare_and_upload(app_harness, token, op_id, "source", "design.zip")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "source",
         "s3_key": prep["s3_key"], "file_name": "design.zip"},
    ))
    assert payload["asset_url"] == _expected(op_id)


# ---------------------------------------------------------------------------
# approve (first flip + idempotent already-final)
# ---------------------------------------------------------------------------


def test_approve_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF on BRAND_A1
    payload = tool_payload(_call(
        app_harness, token, "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m3"},  # draft
    ))
    assert payload["already_final"] is False
    assert payload["asset_url"] == _expected(OP_A1)


def test_approve_already_final_returns_asset_url(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    payload = tool_payload(_call(
        app_harness, token, "solstice_approve_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "message_id": "m2"},  # final
    ))
    assert payload["already_final"] is True
    assert payload["asset_url"] == _expected(OP_A1)
