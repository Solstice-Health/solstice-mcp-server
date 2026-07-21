"""Gunicorn ASGI entry point with raw JSON audit events."""

import logging

from solstice_mcp.app import build_asgi_app
from solstice_mcp.audit import configure_audit_logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
configure_audit_logging()

app = build_asgi_app()
