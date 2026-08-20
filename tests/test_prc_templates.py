from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
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
from solstice_mcp.operations import CgOperation, CgOperationMessage, PrcTemplateVersion

PINNED_EMAIL = "00000000-0000-0000-0000-000000000701"
OPERATION_EMAIL = "00000000-0000-0000-0000-000000000702"
ENV_EMAIL = "00000000-0000-0000-0000-000000000703"
DEFAULT_BANNER = "00000000-0000-0000-0000-000000000704"
BRAND_EMAIL_V1 = "00000000-0000-0000-0000-000000000705"
BRAND_EMAIL_V2 = "00000000-0000-0000-0000-000000000706"
OPERATION_BAKE_EMAIL = (
    '<!doctype html><html><head>'
    '<meta name="sol-prc-contract" content="v2" data-profile="email">'
    '<style id="sol-prc-export-style"></style></head>'
    '<body class="sol-prc-export" data-sol-prc-proof="email"><main data-sol-prc-pages>'
    '<section data-sol-prc-page="page_desktop" data-sol-prc-page-type="render">'
    '<iframe data-sol-prc-creative="desktop" '
    'srcdoc="&lt;!doctype html&gt;&lt;html&gt;creative&lt;/html&gt;"></iframe>'
    "</section></main></body></html>"
)


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
        "confirmed": True,
        "config_schema": {"fields": [{"id": "jobCode"}]},
        "default_field_values": {"jobCode": "ABC-123"},
    }
    first = tool_payload(_create_call(app_harness, mint_token, **arguments))

    assert first["version_number"] == 1
    assert first["template_status"] == "published"
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
                "status": "draft",
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
        ({"template_key": "brand_other_email"}, "reserved auto-resolving prefix"),
        ({"template_key": "environment_default_email"}, "reserved auto-resolving prefix"),
        ({"template_key": "platform_default_email"}, "reserved auto-resolving prefix"),
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


def _doc_rules(profile: str) -> dict[str, dict[str, str]]:
    """Collect a profile's rules straight from the shipped contract markdown.

    Derived independently of the tool's parser so a hardcoded Python rule list,
    or a doc edit the payload never picks up, fails this test.
    """
    text = (
        Path(__file__).parents[1]
        / "plugins/solstice-platform/skills/prc-template-recreation/references/renderer-contract.md"
    ).read_text()
    block = text.split("<!-- PRC_RULES_START -->")[1].split("<!-- PRC_RULES_END -->")[0]
    sections = re.split(r"^### ", block, flags=re.MULTILINE)[1:]
    wanted = {"all profiles", profile}
    rules = {"must": {}, "should": {}, "must_not": {}}
    bucket_names = {"MUST": "must", "SHOULD": "should", "MUST NOT": "must_not"}
    for section in sections:
        if section.splitlines()[0].strip().lower() not in wanted:
            continue
        for heading, contents in re.findall(
            r"^#### (MUST|SHOULD|MUST NOT)\n(.*?)(?=^#### |\Z)",
            section,
            flags=re.MULTILINE | re.DOTALL,
        ):
            bucket = rules[bucket_names[heading]]
            bucket.update(re.findall(r"^- `([^`]+)`: (.+)$", contents, flags=re.MULTILINE))
    return rules


@pytest.mark.parametrize("profile", ["email", "banner", "social", "website"])
def test_prc_template_rules_serves_contract_v2_per_profile(
    app_harness: AppHarness,
    mint_token,
    profile: str,
):
    payload = tool_payload(
        rpc(
            app_harness,
            "tools/call",
            token=mint_token(sub=SHARED_SUB),
            params={"name": "solstice_prc_template_rules", "arguments": {"profile": profile}},
        )
    )

    assert payload["status"] == "ok"
    assert payload["contract_version"] == "v2"
    assert payload["profile"] == profile
    assert set(payload["rules"]) == {"must", "should", "must_not"}
    assert all(payload["rules"][bucket] for bucket in payload["rules"])

    served_rules = {
        bucket: {rule["id"]: rule["text"] for rule in rules}
        for bucket, rules in payload["rules"].items()
    }
    served = {rule_id for rules in served_rules.values() for rule_id in rules}
    assert served_rules == _doc_rules(profile)
    assert all(rule_id.startswith(("common.", f"{profile}.")) for rule_id in served)
    assert all(
        rule["text"].strip() and rule["text"][0].isupper()
        for bucket in payload["rules"].values()
        for rule in bucket
    )
    # Templates never draw annotations: the prohibition must reach every profile.
    assert "common.callout_chrome" in {rule["id"] for rule in payload["rules"]["must_not"]}


def test_prc_template_rules_rejects_an_unknown_profile(app_harness: AppHarness, mint_token):
    response = rpc(
        app_harness,
        "tools/call",
        token=mint_token(sub=SHARED_SUB),
        params={"name": "solstice_prc_template_rules", "arguments": {"profile": "pdf"}},
    )

    assert "invalid_argument: profile must be one of" in _tool_error_text(response)


def test_create_prc_template_version_points_authors_at_the_contract(app_harness: AppHarness, mint_token):
    tools = rpc(app_harness, "tools/list", token=mint_token()).json()["result"]["tools"]
    description = next(
        tool for tool in tools if tool["name"] == "solstice_create_prc_template_version"
    )["description"]

    assert "Contract v2" in description
    assert "solstice_prc_template_rules" in description
    assert "publish_target" in description
    assert "operation_bake_html" in description
    assert "operation_bake_s3_key" in description
    assert "solstice_prepare_prc_template_bake" in description
    assert "raw" in description and "catalog" in description
    assert "prc_template_s3_key" in description
    assert "producer-neutral" in description
    assert "frontend-composed" not in description


def test_create_prc_template_bakes_a_draft_operation_version(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "EMAIL"
        head = session.get(CgOperationMessage, "00000000-0000-0000-0000-000000000503")
        assert head is not None
        head.prc_template_s3_key = f"cg_operation_prc_template/{OP_A1}/old.html"
        session.commit()

    resolved = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
            operation_id=OP_A1,
        )
    )
    assert resolved["operation_bake"]["prc_template_s3_key"].endswith("/old.html")
    assert resolved["publish_targets"] == ["operation", "library", "both"]

    baked = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="",
            content_type="email",
            name="",
            confirmed=True,
            operation_bake_html=OPERATION_BAKE_EMAIL,
            publish_target="operation",
            operation_id=OP_A1,
        )
    )
    assert baked["publish_target"] == "operation"
    assert baked["version_number"] == 3
    assert baked["intent"] == "draft"
    assert baked["prc_template_s3_key"].startswith(f"cg_operation_prc_template/{OP_A1}/")
    proof = app_harness.s3.objects[("test-bucket-a", baked["prc_template_s3_key"])]
    assert proof == OPERATION_BAKE_EMAIL.encode()
    creative = app_harness.s3.objects[("test-bucket-a", baked["s3_key"])]
    assert creative == b"<html>draft v2 body</html>"

    with app_harness.session_factory("tenant_a") as session:
        catalog = session.scalar(
            select(PrcTemplateVersion).where(PrcTemplateVersion.template_key == "")
        )
        assert catalog is None
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == OP_A1,
                CgOperationMessage.version_number == 3,
            )
        )
        assert row is not None
        assert row.prc_template_s3_key == baked["prc_template_s3_key"]
        assert row.intent == "draft"
        feedback = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == OP_A1,
                CgOperationMessage.type == "text",
                CgOperationMessage.content == "PRC template update",
            )
        )
        assert feedback is not None
        assert feedback.version_number is None
        assert feedback.position == row.position - 1
        assert feedback.message_metadata["kind"] == "user_feedback"


@pytest.mark.parametrize(
    ("operation_bake_html", "error"),
    [
        (None, "operation_bake_html or operation_bake_s3_key is required"),
        (
            '<!doctype html><html><head><meta name="sol-prc-contract" content="v2"></head>'
            '<body data-sol-prc-proof="email"><main data-sol-prc-pages>'
            '<section data-sol-prc-page="page_desktop" data-sol-prc-page-type="render">'
            '<iframe data-sol-prc-creative="desktop"></iframe></section></main></body></html>',
            "catalog template shell is not an operation bake",
        ),
        (
            '<!doctype html><html><head>'
            '<meta name="sol-prc-contract" content="v2" data-profile="email"></head>'
            '<body data-sol-prc-proof="email"><main data-sol-prc-pages>'
            '<section data-sol-prc-page="page_desktop" data-sol-prc-page-type="render">'
            '<iframe data-sol-prc-creative="desktop" '
            'srcdoc="&lt;html&gt;creative&lt;/html&gt;"></iframe></section></main></body></html>',
            "export marker",
        ),
    ],
)
def test_create_prc_template_operation_target_requires_a_contract_v2_bake(
    app_harness: AppHarness,
    mint_token,
    operation_bake_html: str | None,
    error: str,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "email"
        session.commit()

    response = _create_call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        template_key="",
        content_type="email",
        name="",
        confirmed=True,
        operation_bake_html=operation_bake_html,
        publish_target="operation",
        operation_id=OP_A1,
    )

    assert error in _tool_error_text(response)


def test_bake_copies_newest_created_at_html_not_highest_version_number(
    app_harness: AppHarness,
    mint_token,
):
    later_key = f"cg_operation_msg_html/{OP_A1}/unnumbered.html"
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "email"
        session.add(
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000599",
                operation_id=OP_A1,
                message_id="m-later",
                author_id=None,
                type="html",
                content=later_key,
                version_number=None,
                intent="draft",
                position=99,
                created_at=datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
                deleted_at=None,
            )
        )
        session.commit()
    app_harness.s3.put("test-bucket-a", later_key, b"<html>later unnumbered</html>")

    baked = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="",
            content_type="email",
            name="",
            confirmed=True,
            operation_bake_html=OPERATION_BAKE_EMAIL,
            publish_target="operation",
            operation_id=OP_A1,
        )
    )
    creative = app_harness.s3.objects[("test-bucket-a", baked["s3_key"])]
    assert creative == b"<html>later unnumbered</html>"


def test_prc_template_offers_operation_bake_before_first_bake(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        session.add(
            _template(
                ENV_EMAIL,
                key="environment_default_email",
                content_type="email",
                html="<html>environment shell</html>",
            )
        )
        operation = session.get(CgOperation, OP_A1)
        assert operation is not None
        operation.content_type = "email"
        session.commit()

    payload = tool_payload(
        _call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            content_type="email",
            operation_id=OP_A1,
        )
    )
    assert payload["id"] == ENV_EMAIL
    assert payload["operation_bake"] is None
    assert payload["publish_targets"] == ["operation", "library", "both"]


def test_create_prc_template_both_does_not_commit_library_if_bake_fails(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "email"
        session.commit()
    app_harness.s3.mark_missing_on_download(
        "test-bucket-a", f"cg_operation_msg_html/{OP_A1}/v2/m3/v2.html"
    )

    response = _create_call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        template_key="both_email",
        content_type="email",
        name="Both Email",
        html_template="<!doctype html><html>both</html>",
        confirmed=True,
        operation_bake_html=OPERATION_BAKE_EMAIL,
        publish_target="both",
        operation_id=OP_A1,
    )
    assert "not_found: current html object missing in s3" in _tool_error_text(response)
    with app_harness.session_factory("tenant_a") as session:
        assert session.scalar(
            select(PrcTemplateVersion).where(PrcTemplateVersion.template_key == "both_email")
        ) is None


def test_create_prc_template_bake_rejects_content_type_mismatch(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "BANNER"
        session.commit()

    response = _create_call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        template_key="",
        content_type="email",
        name="",
        confirmed=True,
        operation_bake_html=OPERATION_BAKE_EMAIL,
        publish_target="operation",
        operation_id=OP_A1,
    )
    assert "operation content_type does not match the proof template" in _tool_error_text(
        response
    )


def test_create_prc_template_both_bakes_then_appends_library(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "email"
        session.commit()

    payload = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="both_email",
            content_type="email",
            name="Both Email",
            html_template="<!doctype html><html>both</html>",
            confirmed=True,
            operation_bake_html=OPERATION_BAKE_EMAIL,
            publish_target="both",
            operation_id=OP_A1,
        )
    )
    assert payload["publish_target"] == "both"
    assert payload["template_key"] == "both_email"
    assert payload["version_number"] == 1
    assert payload["operation_bake"]["prc_template_s3_key"].startswith(
        f"cg_operation_prc_template/{OP_A1}/"
    )
    with app_harness.session_factory("tenant_a") as session:
        catalog = session.scalar(
            select(PrcTemplateVersion).where(PrcTemplateVersion.template_key == "both_email")
        )
        assert catalog is not None
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == OP_A1,
                CgOperationMessage.version_number == 3,
            )
        )
        assert row is not None
        assert row.prc_template_s3_key == payload["operation_bake"]["prc_template_s3_key"]


def _prepare_prc_bake_call(
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
        params={"name": "solstice_prepare_prc_template_bake", "arguments": arguments},
    )


def test_prepare_prc_template_bake_returns_presigned_put(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "EMAIL"
        session.commit()

    payload = tool_payload(
        _prepare_prc_bake_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            operation_id=OP_A1,
            content_type="email",
        )
    )
    assert payload["prc_template_s3_key"].startswith(f"cg_operation_prc_template/{OP_A1}/")
    assert payload["prc_template_s3_key"].endswith(".html")
    assert payload["upload_url"]
    assert payload["expires_in"] == 600


def test_create_prc_template_bakes_from_presigned_s3_key(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "EMAIL"
        session.commit()

    prepared = tool_payload(
        _prepare_prc_bake_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            operation_id=OP_A1,
            content_type="email",
        )
    )
    bake_key = prepared["prc_template_s3_key"]
    app_harness.s3.objects[("test-bucket-a", bake_key)] = OPERATION_BAKE_EMAIL.encode()

    baked = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="",
            content_type="email",
            name="",
            confirmed=True,
            operation_bake_s3_key=bake_key,
            publish_target="operation",
            operation_id=OP_A1,
        )
    )
    assert baked["prc_template_s3_key"] == bake_key
    assert app_harness.s3.objects[("test-bucket-a", bake_key)] == OPERATION_BAKE_EMAIL.encode()


def test_create_prc_template_rejects_missing_presigned_upload(
    app_harness: AppHarness,
    mint_token,
):
    with app_harness.session_factory("tenant_a") as session:
        op = session.get(CgOperation, OP_A1)
        assert op is not None
        op.content_type = "EMAIL"
        session.commit()

    prepared = tool_payload(
        _prepare_prc_bake_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            operation_id=OP_A1,
            content_type="email",
        )
    )
    response = _create_call(
        app_harness,
        mint_token,
        tenant_slug="tenant_a",
        brand_id=BRAND_A1,
        template_key="",
        content_type="email",
        name="",
        confirmed=True,
        operation_bake_s3_key=prepared["prc_template_s3_key"],
        publish_target="operation",
        operation_id=OP_A1,
    )
    assert "PUT the bake HTML to upload_url first" in _tool_error_text(response)
