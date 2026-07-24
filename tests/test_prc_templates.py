from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from conftest import BRAND_A1, BRAND_A3, OP_A1, SHARED_SUB, AppHarness
from test_server import rpc, tool_payload

from solstice_mcp.brands import Brand
from solstice_mcp.operations import CgOperation, PrcTemplateVersion

PINNED_EMAIL = "00000000-0000-0000-0000-000000000701"
OPERATION_EMAIL = "00000000-0000-0000-0000-000000000702"
ENV_EMAIL = "00000000-0000-0000-0000-000000000703"
DEFAULT_BANNER = "00000000-0000-0000-0000-000000000704"
BRAND_EMAIL_V1 = "00000000-0000-0000-0000-000000000705"
BRAND_EMAIL_V2 = "00000000-0000-0000-0000-000000000706"


def _tool_error_text(response) -> str:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def _template(
    template_id: str,
    *,
    key: str,
    content_type: str,
    html: str,
    status: str = "published",
    version_number: int = 1,
) -> PrcTemplateVersion:
    now = datetime.now(UTC)
    return PrcTemplateVersion(
        id=template_id,
        template_key=key,
        version_number=version_number,
        content_type=content_type,
        name=key,
        description=None,
        html_template=html,
        config_schema={"fields": []},
        default_field_values={},
        status=status,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


def _call(
    app_harness: AppHarness,
    mint_token,
    **arguments: Any,
):
    return rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_prc_template", "arguments": arguments},
    )


def test_prc_template_resolves_brand_pin_and_fetches_html(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add(
            _template(
                PINNED_EMAIL,
                key="custom_email",
                content_type="email",
                html="<html>brand email shell</html>",
                status="draft",
            )
        )
        brand = session.get(Brand, BRAND_A1)
        assert brand is not None
        brand.brand_metadata = {
            "prc_templates": {
                "email": {"enabled": True, "template_version_id": PINNED_EMAIL}
            }
        }
        session.commit()

    metadata = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="EMAIL",
        )
    )
    assert metadata["id"] == PINNED_EMAIL
    assert metadata["resolved_tier"] == "brand"
    assert metadata["html_template"] is None

    fetched = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
            fetch=True,
        )
    )
    assert fetched["html_template"] == "<html>brand email shell</html>"
    assert fetched["html_size_bytes"] > 0


def test_prc_template_operation_override_requires_matching_brand_and_content_type(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add_all(
            [
                _template(
                    OPERATION_EMAIL,
                    key="operation_email",
                    content_type="email",
                    html="<html>operation shell</html>",
                ),
                _template(
                    ENV_EMAIL,
                    key="environment_default_email",
                    content_type="email",
                    html="<html>environment shell</html>",
                ),
                _template(
                    DEFAULT_BANNER,
                    key="platform_default_banner",
                    content_type="banner",
                    html="<html>platform banner</html>",
                ),
            ]
        )
        operation = session.get(CgOperation, OP_A1)
        assert operation is not None
        operation.content_type = "email"
        operation.operation_metadata = {"prc_template_version_id": OPERATION_EMAIL}
        session.commit()

    payload = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
            operation_id=OP_A1,
            fetch=True,
        )
    )
    assert payload["id"] == OPERATION_EMAIL
    assert payload["resolved_tier"] == "operation"

    fallback = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="banner",
            operation_id=OP_A1,
            fetch=False,
        )
    )
    assert fallback["id"] == DEFAULT_BANNER
    assert fallback["content_type"] == "banner"


def test_prc_template_fallback_stays_within_exact_content_type(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add_all(
            [
                _template(
                    ENV_EMAIL,
                    key="environment_default_email",
                    content_type="email",
                    html="<html>environment email</html>",
                ),
                _template(
                    DEFAULT_BANNER,
                    key="platform_default_banner",
                    content_type="banner",
                    html="<html>platform banner</html>",
                ),
            ]
        )
        session.commit()

    email = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
        )
    )
    assert email["id"] == ENV_EMAIL
    assert email["resolved_tier"] == "environment"

    banner = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="banner",
        )
    )
    assert banner["id"] == DEFAULT_BANNER
    assert banner["resolved_tier"] == "default"


def test_prc_template_prefers_latest_matching_brand_template(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add_all(
            [
                _template(
                    BRAND_EMAIL_V1,
                    key="brand_a1_email",
                    content_type="email",
                    html="<html>brand v1</html>",
                ),
                _template(
                    BRAND_EMAIL_V2,
                    key="brand_a1_email",
                    content_type="email",
                    html="<html>brand v2</html>",
                    version_number=2,
                ),
                _template(
                    ENV_EMAIL,
                    key="environment_default_email",
                    content_type="email",
                    html="<html>environment email</html>",
                ),
            ]
        )
        session.commit()

    payload = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
            fetch=True,
        )
    )
    assert payload["id"] == BRAND_EMAIL_V2
    assert payload["resolved_tier"] == "brand"
    assert payload["html_template"] == "<html>brand v2</html>"


def test_prc_template_brand_opt_out_blocks_operation_and_default_fallbacks(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add_all(
            [
                _template(
                    OPERATION_EMAIL,
                    key="operation_email",
                    content_type="email",
                    html="<html>operation shell</html>",
                ),
                _template(
                    ENV_EMAIL,
                    key="environment_default_email",
                    content_type="email",
                    html="<html>environment email</html>",
                ),
            ]
        )
        brand = session.get(Brand, BRAND_A1)
        operation = session.get(CgOperation, OP_A1)
        assert brand is not None
        assert operation is not None
        brand.brand_metadata = {
            "prc_templates": {
                "email": {"enabled": False, "template_version_id": None}
            }
        }
        operation.content_type = "email"
        operation.operation_metadata = {"prc_template_version_id": OPERATION_EMAIL}
        session.commit()

    response = _call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        content_type="email",
        operation_id=OP_A1,
    )
    assert "not_found" in _tool_error_text(response)


def test_prc_template_denies_brand_non_member(
    app_harness: AppHarness,
    mint_token,
):
    response = _call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A3,
        content_type="email",
    )
    assert "not_authorized" in _tool_error_text(response)
