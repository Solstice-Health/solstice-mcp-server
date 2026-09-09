"""Live platform-testing MCP coverage (SOL-3356 / SOL-3398).

Skipped unless STAGING_E2E=1. Hits api-platform-testing, downloads operation
bakes, and asserts #frame-template is not stamped. Mutations require
STAGING_E2E_MUTATIONS=1 and disposable operation ids.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import pytest
from prc_mold import assert_healthy_banner_mold, inspect_prc_mold

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.environ.get("STAGING_E2E") != "1",
        reason="Set STAGING_E2E=1 to hit platform-testing MCP",
    ),
]

MCP_URL = os.environ.get(
    "MCP_STAGING_URL",
    "https://api-platform-testing.solsticehealth.co/mcp",
)
TENANT = os.environ.get("STAGING_TENANT", "platform_testing")
PROJECT_ID = os.environ.get(
    "STAGING_PROJECT_ID",
    "92cfc1cd-0e3c-4b20-bde0-71aa2d489e68",
)
MUTATIONS = os.environ.get("STAGING_E2E_MUTATIONS") == "1"


def _token() -> str:
    token = os.environ.get("MCP_STAGING_TOKEN", "").strip()
    if not token:
        pytest.skip("MCP_STAGING_TOKEN is required for staging E2E")
    return token


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"MCP {method} HTTP {exc.code}: {exc.read()[:400]!r}") from exc
    if payload.get("error"):
        raise AssertionError(f"MCP {method} error: {payload['error']}")
    return payload["result"]


def _tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _rpc("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        raise AssertionError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode()


def _operation_ids(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        op_id = node.get("operation_id")
        if isinstance(op_id, str) and op_id:
            found.append(op_id)
        for value in node.values():
            _operation_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            _operation_ids(item, found)


def _fixtures() -> dict[str, str]:
    found: dict[str, str] = {}
    for kind in ("email", "banner", "social"):
        configured = os.environ.get(f"STAGING_{kind.upper()}_OPERATION_ID", "").strip()
        if configured:
            found[kind] = configured
    if len(found) == 3:
        return found
    project = _tool("solstice_project_info", {"tenant_slug": TENANT, "project_id": PROJECT_ID})
    ids: list[str] = []
    _operation_ids(project.get("dir_map") or project, ids)
    for op_id in ids:
        info = _tool("solstice_operation_info", {"tenant_slug": TENANT, "operation_id": op_id})
        kind = (info.get("content_type") or "").lower()
        if kind in {"email", "banner", "social"} and kind not in found:
            found[kind] = op_id
        if len(found) == 3:
            break
    return found


def _proof_html(operation_id: str) -> str:
    messages = _tool(
        "solstice_operation_messages",
        {"tenant_slug": TENANT, "operation_id": operation_id},
    )
    head_id = messages.get("head_message_id")
    assert head_id, f"{operation_id} has no head_message_id"
    html = _tool(
        "solstice_operation_html",
        {
            "tenant_slug": TENANT,
            "operation_id": operation_id,
            "message_id": head_id,
        },
    )
    proof_url = html.get("prc_proof_url")
    assert proof_url, f"{operation_id} has no prc_proof_url"
    return _get(proof_url)


def test_staging_project_has_banner_fixture():
    fixtures = _fixtures()
    assert fixtures.get("banner"), f"no banner in project {PROJECT_ID}"


@pytest.mark.parametrize("kind", ["email", "banner", "social"])
def test_staging_proof_mold_is_healthy(kind: str):
    operation_id = _fixtures().get(kind)
    if not operation_id:
        pytest.skip(f"no {kind} fixture in project {PROJECT_ID}")
    proof = _proof_html(operation_id)
    inspection = inspect_prc_mold(proof)
    if kind == "banner" or inspection.has_frame_template:
        assert_healthy_banner_mold(proof)
    if kind == "email":
        assert 'data-sol-prc-creative="desktop"' in proof or 'data-sol-prc-creative="mobile"' in proof
    if kind == "social":
        assert 'data-sol-prc-creative="social"' in proof


@pytest.mark.parametrize("kind", ["banner", "social"])
def test_staging_creative_commit_keeps_mold_empty(kind: str):
    if not MUTATIONS:
        pytest.skip("Set STAGING_E2E_MUTATIONS=1 and a disposable duplicate")
    operation_id = os.environ.get(f"STAGING_MUTABLE_{kind.upper()}_OPERATION_ID", "").strip()
    if not operation_id:
        pytest.skip(f"STAGING_MUTABLE_{kind.upper()}_OPERATION_ID is required")
    messages = _tool(
        "solstice_operation_messages",
        {"tenant_slug": TENANT, "operation_id": operation_id},
    )
    head_id = messages.get("head_message_id")
    creative = _tool(
        "solstice_operation_html",
        {"tenant_slug": TENANT, "operation_id": operation_id, "message_id": head_id},
    )
    body = _get(creative["url"])
    if "<!-- e2e-mold -->" not in body:
        body = body.replace("</body>", "<!-- e2e-mold --></body>") if "</body>" in body else body + "<!-- e2e-mold -->"
    prepared = _tool(
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT, "operation_id": operation_id, "type": "html"},
    )
    request = urllib.request.Request(
        prepared["upload_url"],
        data=body.encode(),
        headers={"Content-Type": "text/html"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert 200 <= response.status < 300
    _tool(
        "solstice_commit_operation_version",
        {
            "tenant_slug": TENANT,
            "operation_id": operation_id,
            "type": "html",
            "s3_key": prepared["s3_key"],
            "base_message_id": head_id,
        },
    )
    proof = _proof_html(operation_id)
    inspection = inspect_prc_mold(proof)
    if kind == "banner" or inspection.has_frame_template:
        assert_healthy_banner_mold(proof)
