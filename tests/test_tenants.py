from __future__ import annotations

import json
from pathlib import Path

from solstice_mcp.tenants import TenantRegistry


def _registry(tmp_path: Path, payload: dict[str, object]) -> TenantRegistry:
    config = tmp_path / "tenants.json"
    config.write_text(json.dumps(payload))
    registry = TenantRegistry()
    registry.load(config)
    return registry


def test_get_exact_slug(tmp_path: Path):
    registry = _registry(
        tmp_path,
        {"sanofi_sandbox": {"db_name": "sanofi_sandbox", "s3_bucket": "sanofi-sandbox", "env": "development"}},
    )
    assert registry.get("sanofi_sandbox") is not None
    assert registry.get("sanofi_sandbox").s3_bucket == "sanofi-sandbox"


def test_get_normalizes_hyphens_to_underscores(tmp_path: Path):
    # Asset URL subdomain uses hyphens (sanofi-sandbox.solsticehealth.co);
    # slug uses underscores (sanofi_sandbox). The registry must accept either.
    registry = _registry(
        tmp_path,
        {"sanofi_sandbox": {"db_name": "sanofi_sandbox", "s3_bucket": "sanofi-sandbox", "env": "development"}},
    )
    assert registry.get("sanofi-sandbox") is not None
    assert registry.get("sanofi-sandbox").db_name == "sanofi_sandbox"


def test_get_normalizes_underscores_to_hyphens(tmp_path: Path):
    # Symmetric: a slug stored with hyphens resolves from an underscore input.
    registry = _registry(
        tmp_path,
        {"real-chem": {"db_name": "real_chem", "s3_bucket": "rc", "env": "production"}},
    )
    assert registry.get("real_chem") is not None
    assert registry.get("real_chem").db_name == "real_chem"


def test_get_returns_none_for_unknown(tmp_path: Path):
    registry = _registry(
        tmp_path,
        {"sanofi_sandbox": {"db_name": "sanofi_sandbox", "s3_bucket": "sanofi-sandbox", "env": "development"}},
    )
    assert registry.get("nonexistent") is None
    assert registry.get("") is None
    assert registry.get("sanofi_sandbox-extra") is None
