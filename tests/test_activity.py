from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from conftest import BRAND_A1, BRAND_A3, OP_A1, PROJECT_P1, SHARED_SUB, AppHarness
from test_server import audit_events, rpc, tool_payload

from solstice_mcp.audit import AUDIT_LOGGER_NAME


def _call(harness: AppHarness, token: str, name: str, arguments: dict[str, Any]):
    return rpc(
        harness,
        "tools/call",
        token=token,
        params={"name": name, "arguments": arguments},
    )


def _activity_calls(harness: AppHarness) -> list[dict[str, Any]]:
    return [call for call in harness.backend_opener.calls if call["path"] == "/api/internal/agent-memory/activity"]


def test_success_denied_and_error_emit_exact_redacted_activity(
    app_harness: AppHarness,
    mint_token,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    token = mint_token()

    assert (
        tool_payload(
            _call(
                app_harness,
                token,
                "solstice_operation_info",
                {"tenant_slug": "tenant_a", "operation_id": OP_A1},
            )
        )["status"]
        == "ok"
    )
    _call(
        app_harness,
        token,
        "solstice_brand_info",
        {"tenant_slug": "tenant_a", "brand_id": BRAND_A3},
    )
    _call(
        app_harness,
        token,
        "solstice_operation_html",
        {
            "tenant_slug": "tenant_a",
            "operation_id": OP_A1,
            "message_id": "missing",
        },
    )

    calls = _activity_calls(app_harness)
    assert len(calls) == 3
    events = {
        event["tool"]: event
        for event in audit_events(caplog)
        if event["tool"]
        in {
            "solstice_operation_info",
            "solstice_brand_info",
            "solstice_operation_html",
        }
    }
    expected_resources = {
        "solstice_operation_info": {
            "brand_id": BRAND_A1,
            "project_id": PROJECT_P1,
            "operation_id": OP_A1,
        },
        "solstice_brand_info": {"brand_id": BRAND_A3},
        "solstice_operation_html": {
            "operation_id": OP_A1,
            "message_id": "missing",
        },
    }
    expected_outcomes = {
        "solstice_operation_info": "success",
        "solstice_brand_info": "denied",
        "solstice_operation_html": "error",
    }
    for call in calls:
        assert call["method"] == "POST"
        assert call["headers"]["Authorization"] == "Bearer m2m-bearer"
        assert call["headers"]["X-tenant-slug"] == "tenant_a"
        body = json.loads(call["body"])
        event = events[body["tool_name"]]
        assert body == {
            "actor_sub": SHARED_SUB,
            "tenant_slug": "tenant_a",
            "tool_name": body["tool_name"],
            "outcome": expected_outcomes[body["tool_name"]],
            "occurred_at": event["timestamp"],
            "idempotency_key": event["event_id"],
            **expected_resources[body["tool_name"]],
        }


def test_activity_failure_preserves_success_and_logs_redacted_warning(
    app_harness: AppHarness,
    mint_token,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.WARNING, logger=AUDIT_LOGGER_NAME)
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/activity")] = (
        500,
        b'{"detail":"secret telemetry failure"}',
    )

    payload = tool_payload(
        _call(
            app_harness,
            mint_token(),
            "solstice_whoami",
            {"tenant_slug": "tenant_a"},
        )
    )

    assert payload["status"] == "ok"
    warning = json.loads(caplog.records[-1].message)
    assert set(warning) == {"event_id", "tool", "error_type"}
    assert warning["tool"] == "solstice_whoami"
    assert warning["error_type"] == "MemoryClientUnavailable"
    assert "secret" not in caplog.records[-1].message


def test_activity_failure_preserves_original_tool_error(
    app_harness: AppHarness,
    mint_token,
):
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/activity")] = (
        500,
        b"{}",
    )

    response = _call(
        app_harness,
        mint_token(),
        "solstice_operation_html",
        {
            "tenant_slug": "tenant_a",
            "operation_id": OP_A1,
            "message_id": "missing",
        },
    )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "not_found" in result["content"][0]["text"]


def test_memory_tools_do_not_emit_activity(app_harness: AppHarness, mint_token):
    app_harness.backend_opener.responses[("GET", "/api/internal/agent-memory")] = (
        200,
        b'{"brand":[],"personal":[],"tenant_personal":[]}',
    )
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory")] = (
        200,
        b'{"memory_id":"mem-1","status":"active"}',
    )
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/mem-1/supersede")] = (
        200,
        b'{"memory_id":"mem-2","status":"active"}',
    )
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/mem-1/forget")] = (
        200,
        b'{"memory_id":"mem-1","status":"forgotten"}',
    )
    app_harness.backend_opener.responses[("POST", "/api/internal/agent-memory/observations")] = (
        202,
        b'{"observation_id":"obs-1","processing_state":"pending"}',
    )
    token = mint_token()

    tool_payload(
        _call(
            app_harness,
            token,
            "solstice_memory_recall",
            {"tenant_slug": "tenant_a", "brand_id": BRAND_A1},
        )
    )
    tool_payload(
        _call(
            app_harness,
            token,
            "solstice_memory_observe",
            {
                "tenant_slug": "tenant_a",
                "brand_id": BRAND_A1,
                "scope": "personal",
                "observation": "The user prefers concise summaries.",
            },
        )
    )
    tool_payload(
        _call(
            app_harness,
            token,
            "solstice_memory_remember",
            {
                "tenant_slug": "tenant_a",
                "brand_id": BRAND_A1,
                "scope": "personal",
                "fact_type": "preference",
                "statement": "secret statement",
            },
        )
    )
    tool_payload(
        _call(
            app_harness,
            token,
            "solstice_memory_replace",
            {
                "tenant_slug": "tenant_a",
                "brand_id": BRAND_A1,
                "memory_id": "mem-1",
                "scope": "personal",
                "fact_type": "preference",
                "statement": "replacement",
            },
        )
    )
    tool_payload(
        _call(
            app_harness,
            token,
            "solstice_memory_forget",
            {
                "tenant_slug": "tenant_a",
                "brand_id": BRAND_A1,
                "memory_id": "mem-1",
                "scope": "personal",
            },
        )
    )

    assert _activity_calls(app_harness) == []
