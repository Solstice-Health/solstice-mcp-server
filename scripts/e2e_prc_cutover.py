#!/usr/bin/env python3
"""Cutover driver: the MCP writing PRC documents through a real Backend.

Everything in the test suite answers from a double this repo wrote. This
drives real JSON-RPC tool calls against a real MCP, which mints a real Auth0
machine token and calls a real Backend, which writes real rows. It is the only
thing that can catch the two planes disagreeing — the class of bug where both
suites are green and the field is null on the wire.

    export E2E_MCP_URL=http://localhost:8001/mcp
    export E2E_MCP_TOKEN=<a user's access token for that MCP audience>
    export E2E_TENANT_SLUG=sanofi_sandbox
    export E2E_OPERATION_ID=<an operation in that tenant>
    ./scripts/e2e_prc_cutover.py

The MCP must run with MCP_FLAG_PRC_WRITES_VIA_BACKEND=true and its
SOLSTICE_BACKEND_* settings pointed at the Backend under test.

Writes real version rows and S3 objects, and publishes. Nothing is cleaned up:
the ids it created are printed at the end.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from typing import Any

import httpx

MCP_URL = os.getenv("E2E_MCP_URL", "http://localhost:8001/mcp")
TOKEN = os.getenv("E2E_MCP_TOKEN", "")
TENANT_SLUG = os.getenv("E2E_TENANT_SLUG", "sanofi_sandbox")
OPERATION_ID = os.getenv("E2E_OPERATION_ID", "")
BRAND_ID = os.getenv("E2E_BRAND_ID", "")
TIMEOUT = float(os.getenv("E2E_TIMEOUT", "90"))

CREATIVE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>e2e</title></head>"
    "<body><h1>e2e cutover {marker}</h1><p>Written by e2e_prc_cutover.</p></body></html>"
)

_failures: list[str] = []
_created: dict[str, str] = {}


def check(name: str, condition: bool, detail: str = "") -> bool:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}{('  — ' + detail) if detail and not condition else ''}")
    if not condition:
        _failures.append(f"{name}: {detail}")
    return condition


def claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying — diagnostics only."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class Mcp:
    """Minimal streamable-HTTP JSON-RPC client."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.session = httpx.Client(timeout=TIMEOUT)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
        )
        self._id = 0
        self._session_id: str | None = None

    def _post(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        response = self.session.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
            headers=headers,
        )
        if "Mcp-Session-Id" in response.headers:
            self._session_id = response.headers["Mcp-Session-Id"]
        if response.status_code >= 400:
            raise SystemExit(f"{method} -> HTTP {response.status_code}: {response.text[:400]}")
        return _parse(response.text)

    def initialize(self) -> None:
        self._post(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-prc-cutover", "version": "1"},
            },
        )
        self.session.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": self._session_id} if self._session_id else {},
        )

    def tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """(payload, error_text) — a tool error is an outcome, not an exception."""
        result = self._post("tools/call", {"name": name, "arguments": arguments}).get("result", {})
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            return None, text
        try:
            return json.loads(text), None
        except ValueError:
            return {"_text": text}, None


def _parse(body: str) -> dict[str, Any]:
    """Accept a JSON body or an SSE frame carrying one."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise SystemExit(f"unparseable MCP response: {body[:200]}")


def main() -> int:
    if not TOKEN:
        sys.exit("E2E_MCP_TOKEN is required (a user's access token for the MCP audience)")
    if not OPERATION_ID:
        sys.exit("E2E_OPERATION_ID is required (an operation in the tenant under test)")

    payload = claims(TOKEN)
    print(f"token sub={payload.get('sub')} aud={payload.get('aud')}")
    print(f"mcp={MCP_URL} tenant={TENANT_SLUG} operation={OPERATION_ID}\n")

    mcp = Mcp(MCP_URL, TOKEN)
    mcp.initialize()

    # 1. The rules read. Served by the Backend that enforces them, so a 404
    #    here means the Backend is older than the MCP expects.
    print("rules")
    rules, error = mcp.tool("solstice_prc_template_rules", {"profile": "email"})
    if check("rules served", rules is not None, error or ""):
        assert rules is not None
        check("contract_version present", bool(rules.get("contract_version")))
        check("rules bucketed", set(rules.get("rules") or {}) == {"must", "should", "must_not"})
        check(
            "document is the whole contract",
            str(rules.get("document", "")).startswith("# Solstice PRC Template Contract v2"),
            f"got {str(rules.get('document'))[:60]!r}",
        )

    # 2. Prepare. The Backend mints the key and the row id inside it.
    print("\nprepare")
    prepared, error = mcp.tool(
        "solstice_prepare_operation_version",
        {"tenant_slug": TENANT_SLUG, "operation_id": OPERATION_ID, "type": "html"},
    )
    if not check("prepare accepted", prepared is not None, error or ""):
        return _report()
    assert prepared is not None
    s3_key = prepared.get("s3_key", "")
    upload_url = prepared.get("upload_url", "")
    check("key is under the creative prefix", s3_key.startswith("cg_operation_msg_html/"), s3_key)
    check(
        "message_id is the key's row id",
        prepared.get("message_id") == s3_key.rsplit("/", 1)[-1].removesuffix(".html"),
    )
    check("upload_url presigned", "Signature" in upload_url or "X-Amz-Signature" in upload_url)
    _created["prepared_key"] = s3_key

    # 3. The upload the agent would do. A PUT that the Backend then reads back
    #    on commit — the step no in-process test can perform.
    print("\nupload")
    marker = uuid.uuid4().hex[:8]
    body = CREATIVE.format(marker=marker).encode("utf-8")
    put = httpx.put(upload_url, content=body, headers={"Content-Type": "text/html"}, timeout=TIMEOUT)
    if not check("PUT to presigned url", put.status_code in (200, 204), f"HTTP {put.status_code} {put.text[:200]}"):
        return _report()

    # 4. Commit. The Backend composes the proof from what it just read.
    print("\ncommit")
    committed, error = mcp.tool(
        "solstice_commit_operation_version",
        {
            "tenant_slug": TENANT_SLUG,
            "operation_id": OPERATION_ID,
            "type": "html",
            "s3_key": s3_key,
        },
    )
    if not check("commit accepted", committed is not None, error or ""):
        return _report()
    assert committed is not None
    head = committed.get("head_message_id")
    # The fields agents are instructed by. A rename breaks callers silently.
    for field in ("operation_id", "type", "intent", "id", "head_message_id", "message_id", "s3_key", "asset_url"):
        check(f"commit publishes {field}", field in committed, f"missing from {sorted(committed)}")
    check("id agrees with head_message_id", committed.get("id") == head)
    check("s3_key echoes what was committed", committed.get("s3_key") == s3_key)
    check(
        "proof composed",
        bool(committed.get("prc_template_s3_key")),
        "prc_template_s3_key is null — the Backend stored no proof",
    )
    _created["head_message_id"] = str(head)

    # 5. Read it back through a different path. The write claiming success and
    #    the row being visible are separate facts; only a second path shows it.
    print("\nread back")
    messages, error = mcp.tool(
        "solstice_operation_messages",
        {"tenant_slug": TENANT_SLUG, "operation_id": OPERATION_ID},
    )
    if check("messages readable", messages is not None, error or ""):
        assert messages is not None
        rows = messages.get("messages") or []
        check("the committed row is the head", messages.get("head_message_id") == head,
              f"head is {messages.get('head_message_id')}, committed {head}")
        check("the row is visible in the timeline", any(r.get("id") == head for r in rows),
              f"{len(rows)} rows, none with id {head}")

    # 6. A stale base must conflict rather than bury the head.
    print("\nconflict")
    _, error = mcp.tool(
        "solstice_commit_operation_version",
        {
            "tenant_slug": TENANT_SLUG,
            "operation_id": OPERATION_ID,
            "type": "html",
            "s3_key": s3_key,
            "base_message_id": str(uuid.uuid4()),
        },
    )
    check(
        "a stale base is refused as not_latest_document",
        error is not None and "not_latest_document" in error,
        f"got {error!r}",
    )

    # 7. Publish. Flips intent and unlocks what hides the asset from viewers.
    print("\npublish")
    published, error = mcp.tool(
        "solstice_approve_operation_version",
        {"tenant_slug": TENANT_SLUG, "operation_id": OPERATION_ID, "message_id": str(head)},
    )
    if check("publish accepted", published is not None, error or ""):
        assert published is not None
        check("intent is final", published.get("intent") == "final")
        check("asset_url published", bool(published.get("asset_url")))
        for field in ("change_requests_resolved", "requests_completed"):
            check(f"publish reports {field}", field in published)

    return _report()


def _report() -> int:
    print("\ncreated:")
    for name, value in _created.items():
        print(f"  {name} = {value}")
    if _failures:
        print(f"\n{len(_failures)} FAILED:")
        for line in _failures:
            print(f"  - {line}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
