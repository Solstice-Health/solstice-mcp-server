"""The memory service decides what an agent hears when the Backend refuses.

Redaction is the point: a Backend error body can carry internal detail, so the
category travels and the message does not.
"""

from __future__ import annotations

import pytest

from solstice_mcp.repositories.solstice_backend.errors import (
    BackendInvalidResponse,
    BackendStatusError,
    BackendUnauthenticated,
    BackendUnreachable,
)
from solstice_mcp.services.memory import tool_error


@pytest.mark.parametrize(
    "status,expected",
    [
        (422, "invalid_argument: backend rejected the memory payload"),
        (401, "not_authorized: backend rejected the memory request"),
        (403, "not_authorized: backend rejected the memory request"),
        (404, "not_found: memory fact not found in this partition"),
        (409, "conflict: memory brand write conflicted; restate and retry"),
        (500, "service_unavailable: memory backend unavailable; retry later"),
        (418, "internal_error: memory backend returned an unexpected result"),
    ],
)
def test_statuses_map_to_the_strings_the_tools_name(status, expected):
    failure = BackendStatusError(status=status, code=None, detail="x")

    assert str(tool_error(failure, scope="brand")) == expected


@pytest.mark.parametrize(
    "failure",
    [
        BackendUnreachable("backend_unreachable"),
        BackendUnauthenticated("auth0_token_endpoint_failed"),
        BackendInvalidResponse("backend_response_invalid_json"),
    ],
)
def test_a_failure_that_never_reached_the_backend_is_unavailable(failure):
    assert str(tool_error(failure, scope="personal")).startswith("service_unavailable:")


def test_the_backend_message_never_reaches_the_agent():
    """An error body may carry internal detail; none of it helps an agent."""
    failure = BackendStatusError(status=500, code=None, detail="internal db creds leak")

    assert "internal db creds leak" not in str(tool_error(failure, scope="personal"))
