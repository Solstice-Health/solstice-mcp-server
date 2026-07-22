from __future__ import annotations

from typing import Any
from uuid import uuid4

from conftest import (
    BRAND_A1,
    DELETED_SUB,
    PROJECT_P1,
    PROJECT_P2,
    SHARED_SUB,
    AppHarness,
)
from sqlalchemy import select
from test_server import rpc, tool_payload

from solstice_mcp.operations import CgOperation, Project

TENANT = "tenant_a"
BUCKET = "test-bucket-a"


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


def _operation(harness: AppHarness, op_id: str) -> CgOperation | None:
    with harness.session_factory(TENANT) as session:
        return session.scalar(select(CgOperation).where(CgOperation.id == op_id))


def _dir_map(harness: AppHarness, project_id: str) -> dict[str, Any]:
    with harness.session_factory(TENANT) as session:
        project = session.scalar(select(Project).where(Project.id == project_id))
        assert project is not None
        return dict(project.dir_map)


# ---------------------------------------------------------------------------
# create at root
# ---------------------------------------------------------------------------


def test_create_at_root_inserts_row_and_leaf(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1 (owns P2)
    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "New Review.html"},
    )
    payload = tool_payload(response)
    op_id = payload["operation_id"]
    assert payload["project_id"] == PROJECT_P2
    assert payload["brand_id"] == BRAND_A1
    assert payload["status"] == "EDITING"
    assert payload["version_number"] == 1
    assert payload["folder_path"] == ""

    # Row inserted with brand derived from the project + owner = subject's user.
    op = _operation(app_harness, op_id)
    assert op is not None
    assert op.brand_id == BRAND_A1
    assert op.project_id == PROJECT_P2
    assert op.status == "EDITING"
    assert op.version_number == 1
    assert op.file_name == "New Review.html"
    assert op.chat_title == "New Review.html"

    # Leaf appended at root of the (previously empty) project dir_map.
    items = _dir_map(app_harness, PROJECT_P2)["items"]
    assert any(
        i.get("operation_id") == op_id and i.get("name") == "New Review.html"
        for i in items
    )


def test_create_uses_optional_fields(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "leaf.html",
         "content_type": "EMAIL", "chat_title": "My chat", "file_name": "real.html"},
    )
    payload = tool_payload(response)
    op = _operation(app_harness, payload["operation_id"])
    assert op is not None
    assert op.content_type == "EMAIL"
    assert op.chat_title == "My chat"
    assert op.file_name == "real.html"
    leaf = next(
        i for i in _dir_map(app_harness, PROJECT_P2)["items"]
        if i.get("operation_id") == payload["operation_id"]
    )
    assert leaf["content_type"] == "EMAIL"
    assert leaf["veeva_document_number"] is None


# ---------------------------------------------------------------------------
# create into an existing nested folder
# ---------------------------------------------------------------------------


def test_create_into_existing_folder(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    before = _dir_map(app_harness, PROJECT_P1)
    root_names_before = {i.get("name") for i in before["items"]}

    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P1,
         "name": "nested2.html", "folder_path": "Folder"},
    )
    payload = tool_payload(response)
    op_id = payload["operation_id"]

    after = _dir_map(app_harness, PROJECT_P1)
    # Root siblings untouched.
    assert {i.get("name") for i in after["items"]} == root_names_before
    folder = next(i for i in after["items"] if i.get("name") == "Folder")
    names = {i.get("name") for i in folder["items"]}
    assert "nested2.html" in names
    assert "nested.html" in names  # pre-existing sibling preserved
    assert any(i.get("operation_id") == op_id for i in folder["items"])


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_create_unknown_folder_path_rejected(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P1,
         "name": "x.html", "folder_path": "DoesNotExist"},
    )
    assert "not_found" in _tool_error_text(response)


def test_create_unknown_project_rejected(app_harness: AppHarness, mint_token):
    # Uniform not_authorized deny: create must not act as an existence oracle
    # for project ids any more than the read tools do.
    token = mint_token(sub=SHARED_SUB)
    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": str(uuid4()), "name": "x.html"},
    )
    assert "not_authorized" in _tool_error_text(response)


def test_create_denied_for_non_member(app_harness: AppHarness, mint_token):
    # DELETED_SUB maps to a soft-deleted user -> no tenant identity -> not authorized.
    token = mint_token(sub=DELETED_SUB)
    response = _call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "x.html"},
    )
    assert "not_authorized" in _tool_error_text(response)


# ---------------------------------------------------------------------------
# end-to-end: create -> prepare -> upload -> commit (v1)
# ---------------------------------------------------------------------------


def test_create_then_add_v1_version(app_harness: AppHarness, mint_token):
    token = mint_token(sub=SHARED_SUB)  # ADMIN on BRAND_A1 -> final intent
    created = tool_payload(_call(
        app_harness, token, "solstice_create_operation",
        {"tenant_slug": TENANT, "project_id": PROJECT_P2, "name": "doc.html"},
    ))
    op_id = created["operation_id"]

    prep = tool_payload(_call(
        app_harness, token, "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "html", "file_name": "doc.html"},
    ))
    assert prep["version_number"] == 1
    app_harness.s3.put(BUCKET, prep["s3_key"], b"<html>v1</html>")

    committed = tool_payload(_call(
        app_harness, token, "solstice_commit_operation_version",
        {"tenant_slug": TENANT, "operation_id": op_id, "type": "html",
         "s3_key": prep["s3_key"], "file_name": "doc.html"},
    ))
    assert committed["version_number"] == 1
    assert committed["intent"] == "final"
