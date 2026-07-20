"""Registry of sibling MCPs the discovery hub can advertise."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SiblingMCPRegistry:
    """Load and list sibling MCP directory entries from a flat JSON file.

    Each entry: {name, url, auth_type, scopes, setup_instructions}.
    Keys starting with "_" are skipped, mirroring TenantRegistry.

    ponytail: flat config file, not a framework. If directory entries grow
    beyond a static list, swap for a typed provider with schema validation.
    """

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []

    def load(self, path: str | Path) -> None:
        with Path(path).open(encoding="utf-8") as config_file:
            raw = json.load(config_file)
        entries: list[dict[str, Any]] = []
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            entries.append(
                {
                    "name": key,
                    "url": value.get("url", ""),
                    "auth_type": value.get("auth_type", ""),
                    "scopes": value.get("scopes", []),
                    "setup_instructions": value.get("setup_instructions", ""),
                }
            )
        self._entries = entries
        logger.info("Loaded %d sibling MCP entries", len(self._entries))

    def list(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._entries]
