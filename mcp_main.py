"""Gunicorn ASGI entry point with raw JSON audit events."""

import logging

from solstice_mcp.app import build_asgi_app
from solstice_mcp.audit import configure_audit_logging
from solstice_mcp.feature_flags import init_feature_flags

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
configure_audit_logging()
# Per worker, and before the app: without it OpenFeature keeps its NoOp
# provider, every flag reads its code default, and a staged rollout has no dial.
init_feature_flags()

app = build_asgi_app()
