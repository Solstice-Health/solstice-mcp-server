"""Gunicorn ASGI entry point."""

import logging

from solstice_mcp.app import build_asgi_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = build_asgi_app()
