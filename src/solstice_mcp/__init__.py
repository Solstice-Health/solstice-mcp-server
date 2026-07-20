"""Standalone Solstice MCP server."""

from solstice_mcp.app import build_asgi_app, build_mcp_app

__all__ = ["build_asgi_app", "build_mcp_app"]
