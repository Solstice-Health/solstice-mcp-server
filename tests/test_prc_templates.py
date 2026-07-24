from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import (
    BRAND_A1,
    BRAND_A3,
    OP_A1,
    SHARED_SUB,
    STAFF_SUB,
    USER_A_STAFF,
    AppHarness,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
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


def _create_call(
    app_harness: AppHarness,
    mint_token,
    *,
    sub: str = STAFF_SUB,
    **arguments: Any,
):
    return rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=sub),
        params={"name": "solstice_create_prc_template_version", "arguments": arguments},
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


def test_create_prc_template_version_appends_and_preserves_prior_version(
    app_harness: AppHarness,
    mint_token,
):
    arguments = {
        "tenant_slug": "tenant_a",
        "brand_id": BRAND_A1,
        "template_key": "custom_email",
        "content_type": "EMAIL",
        "name": "Custom Email",
        "description": "First version",
        "html_template": "\n<!doctype html><html>v1</html>\n",
        "status": "draft",
        "confirmed": True,
        "config_schema": {"fields": [{"id": "jobCode"}]},
        "default_field_values": {"jobCode": "ABC-123"},
    }
    first = tool_payload(_create_call(app_harness, mint_token, **arguments))

    assert first["version_number"] == 1
    assert first["brand_selection_updated"] is False
    assert first["html_size_bytes"] == len(arguments["html_template"].encode())

    with app_harness.session_factory("tenant_a") as session:
        first_row = session.get(PrcTemplateVersion, first["id"])
        assert first_row is not None
        assert first_row.created_by == USER_A_STAFF
        original = {
            "html_template": first_row.html_template,
            "description": first_row.description,
            "status": first_row.status,
            "config_schema": first_row.config_schema,
            "default_field_values": first_row.default_field_values,
        }

    second = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            **{
                **arguments,
                "description": "Second version",
                "html_template": "<!doctype html><html>v2</html>",
                "status": "published",
            },
        )
    )
    assert second["version_number"] == 2
    assert second["id"] != first["id"]

    with app_harness.session_factory("tenant_a") as session:
        rows = session.scalars(
            select(PrcTemplateVersion)
            .where(
                PrcTemplateVersion.template_key == "custom_email",
                PrcTemplateVersion.content_type == "email",
            )
            .order_by(PrcTemplateVersion.version_number)
        ).all()
        assert [row.version_number for row in rows] == [1, 2]
        assert {
            "html_template": rows[0].html_template,
            "description": rows[0].description,
            "status": rows[0].status,
            "config_schema": rows[0].config_schema,
            "default_field_values": rows[0].default_field_values,
        } == original
        brand = session.get(Brand, BRAND_A1)
        assert brand is not None
        assert brand.brand_metadata is None


def test_create_prc_template_version_requires_staff_and_confirmation(
    app_harness: AppHarness,
    mint_token,
):
    arguments = {
        "tenant_slug": "tenant_a",
        "brand_id": BRAND_A1,
        "template_key": "staff_only_email",
        "content_type": "email",
        "name": "Staff Only",
        "html_template": "<html></html>",
        "status": "draft",
        "confirmed": True,
    }
    denied = _create_call(app_harness, mint_token, sub=SHARED_SUB, **arguments)
    assert "required role SOLSTICE_STAFF" in _tool_error_text(denied)

    unconfirmed = _create_call(
        app_harness,
        mint_token,
        **{**arguments, "confirmed": False},
    )
    assert "confirmation_required" in _tool_error_text(unconfirmed)

    with app_harness.session_factory("tenant_a") as session:
        assert session.scalar(
            select(PrcTemplateVersion).where(
                PrcTemplateVersion.template_key == "staff_only_email"
            )
        ) is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"content_type": "print"}, "content_type must be one of"),
        ({"status": "active"}, "status must be one of"),
        ({"html_template": "   "}, "html_template is required"),
        ({"html_template": "x" * 2_000_001}, "too_large"),
    ],
)
def test_create_prc_template_version_rejects_invalid_rows(
    app_harness: AppHarness,
    mint_token,
    overrides: dict[str, Any],
    error: str,
):
    response = _create_call(
        app_harness,
        mint_token,
        **{
            "tenant_slug": "tenant_a",
            "brand_id": BRAND_A1,
            "template_key": "invalid_email",
            "content_type": "email",
            "name": "Invalid",
            "html_template": "<html></html>",
            "status": "draft",
            "confirmed": True,
            **overrides,
        },
    )
    assert error in _tool_error_text(response)


def test_create_prc_template_version_returns_typed_concurrent_conflict(
    app_harness: AppHarness,
    mint_token,
    monkeypatch: pytest.MonkeyPatch,
):
    original_commit = Session.commit

    def conflict_on_template_insert(session: Session) -> None:
        if any(isinstance(row, PrcTemplateVersion) for row in session.new):
            raise IntegrityError("insert", {}, Exception("unique conflict"))
        original_commit(session)

    monkeypatch.setattr(Session, "commit", conflict_on_template_insert)
    response = _create_call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        template_key="concurrent_email",
        content_type="email",
        name="Concurrent",
        html_template="<html></html>",
        status="draft",
        confirmed=True,
    )

    assert "conflict: another PRC template version was created concurrently" in _tool_error_text(
        response
    )
    with app_harness.session_factory("tenant_a") as session:
        assert session.scalar(
            select(PrcTemplateVersion).where(
                PrcTemplateVersion.template_key == "concurrent_email"
            )
        ) is None
