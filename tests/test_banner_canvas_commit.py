"""A banner canvas commit must not persist merged dimensions (SOL-3238).

A canvas is one complete ``<!DOCTYPE html>`` document per dimension. A
document that arrives without its declaration is read as a continuation of its
predecessor by every consumer that splits on the declaration, so the canvas
silently loses a size — the FE keeps rendering it (its splitter falls back to
``</html>``), and the defect only surfaces when the editing agent cannot open
the asset.

This server writes the tenant bucket and message table directly rather than
through Backend-Server, so it is its own gate.
"""

from __future__ import annotations

from typing import Any

from conftest import PROJECT_P2, STAFF_SUB, AppHarness
from test_server import rpc, tool_payload

TENANT = "tenant_a"
BUCKET = "test-bucket-a"


def _call(harness: AppHarness, token: str, name: str, args: dict[str, Any]):
    return rpc(harness, "tools/call", token=token, params={"name": name, "arguments": args})


def _result(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()["result"]


def doc(dim: str, body: str = "x", doctype: bool = True) -> str:
    width, height = dim.split("x")
    html = (
        f'<html lang="en"><head><meta name="ad.size" content="width={width},height={height}">'
        f"</head><body>{body}</body></html>"
    )
    return f"<!DOCTYPE html>\n{html}" if doctype else html


def _operation(harness: AppHarness, token: str, content_type: str) -> str:
    payload = tool_payload(_call(
        harness, token, "solstice_create_edit_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "canvas.html",
         "kind": "html", "content_type": content_type},
    ))
    return payload["operation_id"]


def _commit(harness: AppHarness, token: str, op_id: str, canvas: str):
    prep = tool_payload(_call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "html", "file_name": "canvas.html"},
    ))
    harness.s3.put(BUCKET, prep["s3_key"], canvas.encode("utf-8"))
    return _call(
        harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "canvas.html"},
    )


def test_rejects_a_banner_canvas_with_a_merged_dimension(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER")

    canvas = "\n\n".join([doc("300x250"), doc("320x50", doctype=False), doc("300x50")])
    result = _result(_commit(app_harness, token, op_id, canvas))

    assert result.get("isError") is True, result
    text = result["content"][0]["text"]
    assert "3 HTML document(s)" in text
    assert "2 <!DOCTYPE html>" in text


def test_accepts_a_well_formed_banner_canvas(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER")

    canvas = "\n\n".join([doc("300x250"), doc("728x90"), doc("160x600")])
    payload = tool_payload(_commit(app_harness, token, op_id, canvas))

    assert payload["version_number"] == 1


def test_accepts_a_single_document_banner(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER")

    payload = tool_payload(_commit(app_harness, token, op_id, doc("300x250")))

    assert payload["version_number"] == 1


def test_rejects_a_lone_document_with_no_declaration(app_harness: AppHarness, mint_token):
    """Nothing can merge into it yet — but the next adapt run appends a size,
    and by then the dimension is already lost."""
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER")

    result = _result(_commit(app_harness, token, op_id, doc("300x250", doctype=False)))

    assert result.get("isError") is True, result


def test_a_correction_to_a_broken_asset_still_commits(app_harness: AppHarness, mint_token):
    """The gate must never strand an asset it just rejected."""
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER")

    broken = "\n\n".join([doc("300x250"), doc("320x50", doctype=False)])
    assert _result(_commit(app_harness, token, op_id, broken)).get("isError") is True

    fixed = "\n\n".join([doc("300x250"), doc("320x50")])
    assert tool_payload(_commit(app_harness, token, op_id, fixed))["version_number"] == 1


def test_social_canvases_are_gated_too(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "SOCIAL")

    canvas = "\n\n".join([doc("1080x1080"), doc("1080x1920", doctype=False)])

    assert _result(_commit(app_harness, token, op_id, canvas)).get("isError") is True


def test_email_documents_are_not_gated(app_harness: AppHarness, mint_token):
    """Email is a single document; the multi-doc rule must not touch it."""
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "EMAIL")

    payload = tool_payload(_commit(app_harness, token, op_id, "<html><body>no doctype</body></html>"))

    assert payload["version_number"] == 1


def test_whitespace_padded_banner_type_is_still_gated(app_harness: AppHarness, mint_token):
    """Create persists the raw content_type; the gate must strip before matching."""
    token = mint_token(sub=STAFF_SUB)
    op_id = _operation(app_harness, token, "BANNER ")

    canvas = "\n\n".join([doc("300x250"), doc("320x50", doctype=False)])
    result = _result(_commit(app_harness, token, op_id, canvas))

    assert result.get("isError") is True, result
