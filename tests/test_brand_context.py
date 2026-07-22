from __future__ import annotations

from typing import Any

from conftest import BRAND_A1, BRAND_A3, BRAND_B1, OTHER_SUB, SHARED_SUB, AppHarness
from test_server import rpc, tool_payload

TENANT = "tenant_a"


def _result(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool_error_text(response) -> str:
    result = _result(response)
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(
        harness, "tools/call", token=token,
        params={"name": name, "arguments": args},
    )


def test_brand_rules_returns_guidelines_and_admin_fields(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(
        _call(
            app_harness, token, "solstice_brand_rules",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1},
        )
    )
    assert payload["brand_id"] == BRAND_A1
    assert payload["design_bible"] == {"palette": "blue"}
    assert payload["isi"] == {"text": "See ISI"}
    assert payload["drug_info"] == {"name": "Drug A"}
    assert payload["count"] == 1
    assert payload["rules"][0]["name"] == "No unapproved claims"
    assert "Check claim library" in (payload["rules"][0]["implementation_steps"] or "")


def test_brand_design_assets_presigns_s3_key(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(
        _call(
            app_harness, token, "solstice_brand_design_assets",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1},
        )
    )
    assert payload["count"] == 1
    asset = payload["assets"][0]
    assert asset["image_file_name"] == "logo.png"
    assert asset["s3_key"] == "design_library/logo.png"
    assert asset["url"] == "https://fake-s3/test-bucket-a/design_library/logo.png?expires=600"
    assert ("test-bucket-a", "design_library/logo.png", 600) in app_harness.s3.presign_calls


def test_brand_claims_defaults_to_extracted_only(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(
        _call(
            app_harness, token, "solstice_brand_claims",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1},
        )
    )
    assert payload["extracted_only"] is True
    assert payload["count"] == 1
    assert payload["claims"][0]["claim_text"] == "Drug A reduced symptoms by 40%"


def test_brand_claims_can_include_unextracted(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    payload = tool_payload(
        _call(
            app_harness, token, "solstice_brand_claims",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1, "extracted_only": False},
        )
    )
    assert payload["count"] == 2
    texts = {c["claim_text"] for c in payload["claims"]}
    assert "Draft unextracted claim" in texts


def test_brand_context_denies_non_member(app_harness: AppHarness, mint_token):
    # SHARED is not a member of BRAND_A3 (OTHER is).
    token = mint_token(sub=SHARED_SUB)
    for tool in (
        "solstice_brand_rules",
        "solstice_brand_design_assets",
        "solstice_brand_claims",
    ):
        err = _tool_error_text(
            _call(app_harness, token, tool, {"tenant_slug": TENANT, "brand_id": BRAND_A3})
        )
        assert "not_authorized" in err


def test_brand_context_denies_cross_tenant_brand(app_harness: AppHarness, mint_token):
    # SHARED is ADMIN on BRAND_B1 in tenant_b, but BRAND_B1 is not in tenant_a.
    token = mint_token(sub=SHARED_SUB)
    err = _tool_error_text(
        _call(
            app_harness, token, "solstice_brand_rules",
            {"tenant_slug": TENANT, "brand_id": BRAND_B1},
        )
    )
    assert "not_authorized" in err


def test_member_role_can_read_brand_context(app_harness: AppHarness, mint_token):
    # OTHER is MEMBER on BRAND_A1.
    token = mint_token(sub=OTHER_SUB)
    payload = tool_payload(
        _call(
            app_harness, token, "solstice_brand_rules",
            {"tenant_slug": TENANT, "brand_id": BRAND_A1},
        )
    )
    assert payload["count"] == 1
