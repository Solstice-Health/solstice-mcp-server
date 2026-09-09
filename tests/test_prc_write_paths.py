"""Compose vs catalog write paths (SOL-3356 / SOL-3398).

Creative HTML commit and operation-bake publish both call compose_prc_proof.
Library-only catalog publish does not.
"""

from __future__ import annotations

from conftest import BRAND_A1, OP_A1, OP_A2, SHARED_SUB, AppHarness
from prc_mold import assert_healthy_banner_mold
from test_prc_templates import BANNER_TEMPLATE, EMAIL_TEMPLATE, _create_call, _upload_operation_bake
from test_server import tool_payload
from test_versions import BUCKET, TENANT, _call, _configure_email_prc, _prepare_and_upload

import solstice_mcp.operations as operations


def test_library_publish_does_not_compose(app_harness: AppHarness, mint_token, monkeypatch):
    calls: list[str] = []

    def forbidden(supplied_proof: str, creative: str, content_type: str) -> str:
        calls.append(content_type)
        raise AssertionError("library publish must not compose")

    monkeypatch.setattr(operations, "_compose_supplied_prc_proof", forbidden)

    payload = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="library_no_compose",
            content_type="email",
            name="Library only",
            html_template=EMAIL_TEMPLATE,
            confirmed=True,
            publish_target="library",
        )
    )
    assert payload["publish_target"] == "library"
    assert payload["operation_bake"] is None
    assert calls == []


def test_operation_bake_still_composes_supplied_proof(
    app_harness: AppHarness, mint_token, monkeypatch
):
    """Gap vs 'template save skips compose': operation bake recomposes."""
    calls: list[str] = []
    original = operations._compose_supplied_prc_proof

    def wrapped(supplied_proof: str, creative: str, content_type: str) -> str:
        calls.append(content_type)
        return original(supplied_proof, creative, content_type)

    monkeypatch.setattr(operations, "_compose_supplied_prc_proof", wrapped)

    with app_harness.session_factory("tenant_a") as session:
        op = session.get(operations.CgOperation, OP_A1)
        assert op is not None
        op.content_type = "email"
        session.commit()

    payload = tool_payload(
        _create_call(
            app_harness,
            mint_token,
            tenant_slug="tenant_a",
            brand_id=BRAND_A1,
            template_key="",
            content_type="email",
            name="",
            confirmed=True,
            operation_bake_s3_key=_upload_operation_bake(app_harness, mint_token),
            publish_target="operation",
            operation_id=OP_A1,
        )
    )
    assert payload["publish_target"] == "operation"
    assert calls == ["email"]


def test_html_commit_banner_bake_leaves_mold_empty(app_harness: AppHarness, mint_token):
    _configure_email_prc(
        app_harness,
        OP_A2,
        content_type="banner",
        template_html=BANNER_TEMPLATE,
    )
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    payload = tool_payload(
        _call(
            app_harness,
            token,
            "solstice_commit_operation_version",
            {
                "tenant_slug": TENANT,
                "operation_id": OP_A2,
                "type": "html",
                "s3_key": prep["s3_key"],
            },
        )
    )
    proof = app_harness.s3.objects[(BUCKET, payload["prc_template_s3_key"])].decode()
    assert_healthy_banner_mold(proof)
