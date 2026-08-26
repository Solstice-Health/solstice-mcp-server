from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    OP_A1,
    OP_A2,
    OP_A3,
    OTHER_SUB,
    SHARED_SUB,
    STAFF_SUB,
    AppHarness,
)
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperationMessage

TENANT = "tenant_a"
BUCKET = "test-bucket-a"

# OP_A1 documents: m2 (final), m3 (staff draft). A non-staff caller's visible
# head is m2; staff see m3. Each must declare the head IT read.
MEMBER_BASE = "m2"
STAFF_BASE = "m3"


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


def _live_rows(harness: AppHarness, op_id: str):
    """Live rows in production timeline order: created_at, then id."""
    with harness.session_factory(TENANT) as session:
        rows = session.scalars(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.deleted_at.is_(None),
            ).order_by(CgOperationMessage.created_at, CgOperationMessage.id)
        ).all()
        return [(r.type, r.intent, r.content, r.message_id) for r in rows]


def _metadata_for(harness: AppHarness, op_id: str, message_id: str):
    with harness.session_factory(TENANT) as session:
        row = session.scalar(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == op_id,
                CgOperationMessage.message_id == message_id,
                CgOperationMessage.deleted_at.is_(None),
            )
        )
        assert row is not None
        return dict(row.message_metadata or {})


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_keys_the_upload_by_message_id(app_harness: AppHarness, mint_token):
    # The key carries the minted message_id and no row count, so a version
    # landing before the commit cannot invalidate it (SOL-3251).
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "file_name": "op_a1.html"},
    )
    payload = tool_payload(response)
    assert payload["type"] == "html"
    assert payload["s3_key"] == f"cg_operation_msg_html/{OP_A1}/{payload['message_id']}.html"
    assert payload["upload_url"].startswith("https://fake-s3/")
    assert payload["expires_in"] > 0
    # A presigned PUT was issued against the tenant bucket.
    assert any(c[1] == payload["s3_key"] for c in app_harness.s3.presign_put_calls)


def test_prepare_key_shape_is_the_same_with_no_document_versions(
    app_harness: AppHarness, mint_token
):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html"},
    )
    payload = tool_payload(response)
    assert payload["s3_key"] == f"cg_operation_msg_html/{OP_A2}/{payload['message_id']}.html"


def test_prepare_pdf_key_uses_approved_pdfs_prefix(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "pdf", "file_name": "doc.pdf"},
    )
    payload = tool_payload(response)
    # The message_id segment is what makes two concurrent pdf prepares distinct.
    assert payload["s3_key"] == f"approved_pdfs/{OP_A1}/{payload['message_id']}_doc.pdf"


def test_prepare_denied_for_non_member(app_harness: AppHarness, mint_token):
    # SHARED is not on BRAND_A3 (which owns OP_A3).
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A3, "type": "html"},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_prepare_rejects_unknown_type(app_harness: AppHarness, mint_token):
    # Anything outside html/pdf must be rejected up front — previously an
    # unknown type fell through to the pdf key shape and was presigned.
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "docx", "file_name": "doc.docx"},
    )
    assert "invalid_arguments" in _tool_error_text(response)
    assert app_harness.s3.presign_put_calls == []


def test_commit_rejects_unknown_type(app_harness: AppHarness, mint_token):
    response = _call(
        app_harness, mint_token(sub=SHARED_SUB),
        "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "docx",
         "s3_key": f"approved_pdfs/{OP_A1}/v3_doc.docx"},
    )
    assert "invalid_arguments" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def _prepare_and_upload(harness: AppHarness, token: str, op_id: str, kind: str, file_name: str | None):
    response = _call(
        harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": kind, "file_name": file_name},
    )
    prep = tool_payload(response)
    harness.s3.put(BUCKET, prep["s3_key"], b"<html>new</html>" if kind == "html" else b"%PDF-1.4")
    return prep


def test_commit_member_creates_final_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1
    before = _live_rows(app_harness, OP_A1)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    # OP_A1's head (m3) is a staff draft this caller cannot see, so the commit
    # needs an explicit confirmation. See test_commit_refused_when_head_is_hidden_draft.
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "op_a1.html",
         "base_message_id": MEMBER_BASE, "confirmed": True},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "final"
    assert payload["message_id"] == prep["message_id"]
    assert payload["s3_key"] == prep["s3_key"]
    # Existing rows untouched (append-only); two new rows appended (pill + doc).
    after = _live_rows(app_harness, OP_A1)
    assert len(after) - len(before) == 2
    assert before == after[: len(before)]
    doc = after[-1]
    assert doc[0] == "html"
    assert doc[1] == "final"
    assert doc[2] == prep["s3_key"]
    pill = after[-2]
    assert pill[0] == "text"
    assert pill[1] is None
    assert pill[2] == "Save new version"
    # SOL-3234: the pill must sort before the document it introduces. Ordering is
    # created_at then id, so the pair cannot share a timestamp.
    with app_harness.session_factory(TENANT) as session:
        ordered = session.scalars(
            select(CgOperationMessage).where(
                CgOperationMessage.operation_id == OP_A1,
                CgOperationMessage.deleted_at.is_(None),
            ).order_by(CgOperationMessage.created_at, CgOperationMessage.id)
        ).all()
        assert ordered[-2].created_at < ordered[-1].created_at


def test_commit_staff_creates_draft_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=STAFF_SUB)  # SOLSTICE_STAFF on BRAND_A1
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": STAFF_BASE},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "draft"
    after = _live_rows(app_harness, OP_A1)
    assert after[-1][1] == "draft"


def test_commit_v1_when_no_versions(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html", "s3_key": prep["s3_key"]},
    )
    payload = tool_payload(response)
    assert payload["intent"] == "final"


def test_consecutive_commits_chain_the_base(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep1 = _prepare_and_upload(app_harness, token, OP_A1, "html", "a.html")
    first = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep1["s3_key"], "base_message_id": MEMBER_BASE, "confirmed": True},
    ))
    prep2 = _prepare_and_upload(app_harness, token, OP_A1, "html", "b.html")
    # The first commit is now the caller's head, so it is the next base — and no
    # confirmation is needed, because that head is one they can read.
    second = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep2["s3_key"], "base_message_id": first["message_id"]},
    ))
    assert second["message_id"] == prep2["message_id"]
    assert _live_rows(app_harness, OP_A1)[-1][2] == prep2["s3_key"]


@pytest.mark.parametrize("kind", ["html", "pdf"])
def test_two_prepares_never_share_a_key(app_harness: AppHarness, mint_token, kind):
    # The P0 this key shape fixes: the old v{n} segment came from a row count, so
    # two prepares before either commit produced the SAME key. For pdf, whose key
    # carried no message_id, the second upload silently overwrote the first
    # caller's committed document — potentially an MLR-approved PDF.
    token = mint_token(sub=SHARED_SUB)
    a = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": kind,
         "file_name": f"same.{kind}"},
    ))
    b = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": kind,
         "file_name": f"same.{kind}"},  # identical name, no commit in between
    ))
    assert a["message_id"] != b["message_id"]
    assert a["s3_key"] != b["s3_key"]


def test_prepared_key_survives_a_version_landing_first(
    app_harness: AppHarness, mint_token
):
    # A key must not be invalidated by someone else's commit: the caller already
    # uploaded to it. Previously the key embedded a live count, so a concurrent
    # write made it fail validation as invalid_key — hiding the real cause.
    staff = mint_token(sub=STAFF_SUB)
    slow = _prepare_and_upload(app_harness, staff, OP_A1, "html", "slow.html")
    interloper = _prepare_and_upload(app_harness, staff, OP_A1, "html", "fast.html")
    landed = tool_payload(_call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": interloper["s3_key"], "base_message_id": STAFF_BASE},
    ))
    # The stale base is still caught, and as a conflict — not as a bogus key.
    response = _call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": slow["s3_key"], "base_message_id": STAFF_BASE},
    )
    error = _tool_error_text(response)
    assert "not_latest_document" in error
    assert "invalid_key" not in error
    # And the same untouched key commits cleanly against the new head.
    ok = tool_payload(_call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": slow["s3_key"], "base_message_id": landed["message_id"]},
    ))
    assert ok["message_id"] == slow["message_id"]


def test_commit_pdf_row(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "pdf", "doc.pdf")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "pdf",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE, "confirmed": True},
    )
    payload = tool_payload(response)
    assert payload["s3_key"] == f"approved_pdfs/{OP_A1}/{prep['message_id']}_doc.pdf"
    # The id handed out at prepare is the id that landed — pdf keys used to carry
    # none, so commit minted a second one and the prepare id named nothing.
    assert payload["message_id"] == prep["message_id"]
    after = _live_rows(app_harness, OP_A1)
    assert after[-1][0] == "pdf"
    assert after[-1][2] == payload["s3_key"]


def test_commit_rejects_key_with_wrong_operation(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    # Key targets OP_A2 but committed against OP_A1.
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_rejects_key_with_wrong_version_segment(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    # A v99 key for OP_A1 when next is v3.
    bogus = (
        "cg_operation_msg_html/00000000-0000-0000-0000-000000000401/v99/"
        "00000000-0000-0000-0000-000000000099/v99.html"
    )
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": bogus, "base_message_id": MEMBER_BASE},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_rejects_wrong_prefix(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    other = f"approved_pdfs/{OP_A1}/v3_doc.pdf"  # pdf prefix for an html commit
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": other, "base_message_id": MEMBER_BASE},
    )
    assert "invalid_key" in _tool_error_text(response)


def test_commit_before_upload_is_rejected(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html"},
    )
    prep = tool_payload(response)
    # Do NOT upload; head will return None.
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE},
    )
    assert "not_found" in _tool_error_text(response)


def test_commit_denied_for_non_member(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, mint_token(sub=OTHER_SUB), OP_A1, "html", None)
    # SHARED is ADMIN on A1 but try OP_A3 (BRAND_A3, no membership).
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A3, "type": "html", "s3_key": prep["s3_key"]},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_commit_metadata_mirrors_be_shape(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "op_a1.html",
         "base_message_id": MEMBER_BASE, "confirmed": True},
    )
    meta = _metadata_for(app_harness, OP_A1, prep["message_id"])
    # type == "bot" + isFinalDocument are what the FE version stepper
    # (isDocumentVersionMessage) gates on; without them the version is invisible.
    assert meta["type"] == "bot"
    assert meta["isFinalDocument"] is True
    assert meta["versionIntent"] == "final"
    assert meta["finalContentS3Key"] == prep["s3_key"]
    assert meta["fileName"] == "op_a1.html"
    # No numeric version keys: BE build_final_document_bot_metadata stamps only
    # versionIntent since row identity replaced documentVersion. Writing them
    # here would revive a number no reader trusts (SOL-3251).
    assert "documentVersion" not in meta
    assert "htmlDocumentVersion" not in meta
    assert "htmlDocumentLastVersion" not in meta


# ---------------------------------------------------------------------------
# base_message_id compare-and-swap (SOL-3251)
# ---------------------------------------------------------------------------


def test_commit_requires_a_base_when_documents_exist(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    before = _live_rows(app_harness, OP_A1)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "s3_key": prep["s3_key"]},
    )
    assert "base_message_id is required" in _tool_error_text(response)
    assert _live_rows(app_harness, OP_A1) == before


def test_commit_rejects_a_base_that_is_no_longer_the_head(
    app_harness: AppHarness, mint_token
):
    # The race this exists for: the agent read m2, and while it worked a newer
    # version landed. Committing on the stale base would bury the new one.
    token = mint_token(sub=STAFF_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    before = _live_rows(app_harness, OP_A1)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": "m2"},  # head is m3
    )
    assert "not_latest_document" in _tool_error_text(response)
    assert _live_rows(app_harness, OP_A1) == before


def test_commit_conflicts_when_a_version_lands_mid_flight(
    app_harness: AppHarness, mint_token
):
    # End-to-end race: staff reads the head, prepares, and a second writer lands
    # a version before the first one commits.
    staff = mint_token(sub=STAFF_SUB)
    slow = _prepare_and_upload(app_harness, staff, OP_A1, "html", "slow.html")
    interloper = _prepare_and_upload(app_harness, staff, OP_A1, "html", "fast.html")
    landed = tool_payload(_call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": interloper["s3_key"], "base_message_id": STAFF_BASE},
    ))
    # The slow writer's base is now stale.
    response = _call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": slow["s3_key"], "base_message_id": STAFF_BASE},
    )
    assert "not_latest_document" in _tool_error_text(response)
    # Reapplying against the new head succeeds.
    retry = _prepare_and_upload(app_harness, staff, OP_A1, "html", "slow.html")
    ok = tool_payload(_call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": retry["s3_key"], "base_message_id": landed["message_id"]},
    ))
    assert ok["message_id"] == retry["message_id"]


def test_commit_rejects_a_base_on_an_operation_with_no_documents(
    app_harness: AppHarness, mint_token
):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": "m10"},
    )
    assert "omit base_message_id" in _tool_error_text(response)


def test_commit_accepts_the_row_id_as_a_base(app_harness: AppHarness, mint_token):
    # Legacy rows can have a NULL message_id, so the row id addresses them too.
    token = mint_token(sub=STAFF_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"],
         "base_message_id": "00000000-0000-0000-0000-000000000503"},  # m3's row id
    ))
    assert payload["intent"] == "draft"


# ---------------------------------------------------------------------------
# hidden-draft-head confirmation gate (SOL-3251)
# ---------------------------------------------------------------------------


def test_commit_refused_when_head_is_hidden_draft(app_harness: AppHarness, mint_token):
    # OP_A1's newest document (m3) is a staff draft. A non-staff caller read an
    # older final, so committing would bury the draft's edits under stale content.
    token = mint_token(sub=SHARED_SUB)
    before = _live_rows(app_harness, OP_A1)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE},
    )
    assert "confirmation_required" in _tool_error_text(response)
    # Refused means nothing was written.
    assert _live_rows(app_harness, OP_A1) == before


def test_commit_proceeds_when_hidden_draft_head_is_confirmed(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE, "confirmed": True},
    ))
    assert payload["intent"] == "final"
    assert _live_rows(app_harness, OP_A1)[-1][2] == prep["s3_key"]


def test_commit_refused_when_every_version_is_hidden(app_harness: AppHarness, mint_token):
    # Draft-only operation: staff lands the first version (staff intent = draft),
    # so a non-staff caller sees NO documents at all. They correctly omit
    # base_message_id, and the gate must still catch that a newer version exists.
    staff = mint_token(sub=STAFF_SUB)
    member = mint_token(sub=SHARED_SUB)
    prep_staff = _prepare_and_upload(app_harness, staff, OP_A2, "html", None)
    tool_payload(_call(
        app_harness, staff, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html",
         "s3_key": prep_staff["s3_key"]},
    ))
    # The member cannot see it, so their head is empty and no base is passable.
    assert tool_payload(_call(
        app_harness, member, "solstice_operation_messages",
        {"tenant_slug": TENANT, "operation_id": OP_A2},
    ))["head_message_id"] is None

    before = _live_rows(app_harness, OP_A2)
    prep = _prepare_and_upload(app_harness, member, OP_A2, "html", None)
    response = _call(
        app_harness, member, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html",
         "s3_key": prep["s3_key"]},
    )
    error = _tool_error_text(response)
    assert "confirmation_required" in error
    assert _live_rows(app_harness, OP_A2) == before
    # The refusal must not hand back what the read withheld: it may say a newer
    # version is unreadable, never that it is an unapproved staff draft.
    lowered = error.lower()
    assert "draft" not in lowered
    assert "staff" not in lowered
    assert "unapproved" not in lowered


def test_commit_needs_no_confirmation_for_staff(app_harness: AppHarness, mint_token):
    # Staff see the draft head, so there is nothing hidden to bury.
    token = mint_token(sub=STAFF_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A1, "html", "op_a1.html")
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": STAFF_BASE},
    ))
    assert payload["intent"] == "draft"


def test_commit_needs_no_confirmation_when_visible_head_is_final(
    app_harness: AppHarness, mint_token
):
    # OP_A2 has no documents at all, so no hidden head can exist.
    token = mint_token(sub=SHARED_SUB)
    prep = _prepare_and_upload(app_harness, token, OP_A2, "html", None)
    payload = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A2, "type": "html", "s3_key": prep["s3_key"]},
    ))
    assert payload["intent"] == "final"


def test_commit_reports_upload_and_key_errors_before_asking_to_confirm(
    app_harness: AppHarness, mint_token
):
    # A caller's own bad request must surface as itself, not as a confusing
    # confirmation prompt for a draft that is beside the point.
    token = mint_token(sub=SHARED_SUB)
    prep = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html", "file_name": "op_a1.html"},
    ))  # deliberately never uploaded
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": prep["s3_key"], "base_message_id": MEMBER_BASE},
    )
    assert "not_found" in _tool_error_text(response)

    # A key whose message_id segment is not a UUID is malformed regardless of any
    # row count, so it must still be rejected as a key problem.
    response = _call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": OP_A1, "type": "html",
         "s3_key": f"cg_operation_msg_html/{OP_A1}/not-a-uuid.html",
         "base_message_id": MEMBER_BASE},
    )
    assert "invalid_key" in _tool_error_text(response)
