"""Truthful Slack stubs with no credentials or network access."""

from __future__ import annotations

from typing import Any

_NOT_CONNECTED: dict[str, Any] = {
    "status": "not_connected",
    "connected": False,
    "message": "Slack is not connected. No Slack API call or side effect was performed.",
}


def slack_search(query: str, *, channel: str | None = None, limit: int = 20) -> dict[str, Any]:
    return {**_NOT_CONNECTED, "query": query, "channel": channel, "limit": limit, "results": []}


def slack_read(channel: str, *, latest: str | None = None, limit: int = 50) -> dict[str, Any]:
    return {**_NOT_CONNECTED, "channel": channel, "latest": latest, "limit": limit, "messages": []}


def slack_send(channel: str, message: str, *, thread_ts: str | None = None) -> dict[str, Any]:
    return {**_NOT_CONNECTED, "channel": channel, "thread_ts": thread_ts, "sent": False}


def slack_react(channel: str, timestamp: str, emoji: str) -> dict[str, Any]:
    return {**_NOT_CONNECTED, "channel": channel, "timestamp": timestamp, "emoji": emoji, "reacted": False}
