import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "solstice-platform"
PLUGIN_NAME = "solstice-platform"
PLUGIN_VERSION = "0.3.0"
PRODUCTION_URL = "https://api.solsticehealth.co/mcp"
PRODUCTION_AUDIENCE = PRODUCTION_URL
CURSOR_CLIENT_ID = "uoOiEXHZxyDBkkBEfnOQEp6IhqcnAgTP"
SCOPES = {"mcp:connect", "openid", "email"}
CODEX_CALLBACK_ID = "rNbuDm0IFnc1"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def skill_frontmatter() -> tuple[dict[str, str], str]:
    text = (PLUGIN / "skills" / PLUGIN_NAME / "SKILL.md").read_text()
    marker, frontmatter, body = text.split("---", 2)
    assert marker == ""
    fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
    return fields, body


def test_host_manifests_and_catalogs_stay_aligned() -> None:
    cursor_manifest = load_json(PLUGIN / ".cursor-plugin" / "plugin.json")
    claude_manifest = load_json(PLUGIN / ".claude-plugin" / "plugin.json")
    codex_manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
    cursor_catalog = load_json(ROOT / ".cursor-plugin" / "marketplace.json")
    claude_catalog = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    codex_catalog = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")

    assert cursor_manifest["name"] == claude_manifest["name"] == codex_manifest["name"] == PLUGIN_NAME
    assert cursor_manifest["version"] == claude_manifest["version"] == codex_manifest["version"] == PLUGIN_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", PLUGIN_VERSION)
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["mcpServers"] == "./codex.mcp.json"

    cursor_entry = cursor_catalog["plugins"][0]
    claude_entry = claude_catalog["plugins"][0]
    codex_entry = codex_catalog["plugins"][0]
    assert cursor_catalog["name"] == claude_catalog["name"] == codex_catalog["name"] == "solstice-tools"
    assert cursor_entry["name"] == claude_entry["name"] == PLUGIN_NAME
    assert cursor_entry["version"] == claude_entry["version"] == PLUGIN_VERSION
    assert cursor_entry["source"] == "plugins/solstice-platform"
    assert claude_entry["source"] == "./plugins/solstice-platform"
    assert codex_entry["name"] == PLUGIN_NAME
    assert codex_entry["source"] == {
        "source": "local",
        "path": "./plugins/solstice-platform",
    }
    assert codex_entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }


def test_mcp_configs_share_the_production_auth_contract() -> None:
    cursor = load_json(PLUGIN / "mcp.json")["mcpServers"][PLUGIN_NAME]
    claude = load_json(PLUGIN / ".mcp.json")["mcpServers"][PLUGIN_NAME]
    codex = load_json(PLUGIN / "codex.mcp.json")["mcp_servers"][PLUGIN_NAME]

    assert cursor["url"] == claude["url"] == codex["url"] == PRODUCTION_URL == PRODUCTION_AUDIENCE
    assert set(cursor["auth"]["scopes"]) == set(claude["oauth"]["scopes"].split()) == set(codex["scopes"]) == SCOPES
    assert claude["type"] == "http"
    assert claude["oauth"]["callbackPort"] == 8787
    assert codex["auth"] == "oauth"
    assert codex["oauth_resource"] == PRODUCTION_AUDIENCE
    assert codex["default_tools_approval_mode"] == "writes"

    # Temporary pilot fallback; reviewed PRs replace host IDs after Terraform apply.
    assert cursor["auth"]["CLIENT_ID"] == claude["oauth"]["clientId"] == codex["oauth"]["client_id"] == CURSOR_CLIENT_ID


def test_shared_skill_is_portable_and_action_focused() -> None:
    fields, body = skill_frontmatter()
    assert set(fields) == {"name", "description"}
    assert fields["name"] == PLUGIN_NAME

    body_lower = body.lower()
    for action in (
        "my workspaces",
        "my brands",
        "projects",
        "content reviews",
        "review activity",
        "open/read document",
    ):
        assert action in body_lower
    for language in ("fetch=false", "fetch=true", "ask", "untrusted user content", "no change was made"):
        assert language in body_lower

    skill_dir = PLUGIN / "skills" / PLUGIN_NAME
    references = {"actions.md", "data-model.md", "errors.md"}
    assert {path.name for path in (skill_dir / "references").glob("*.md")} == references
    for reference in references:
        assert f"(references/{reference})" in body


def test_codex_callback_contract_stays_aligned() -> None:
    callback_id = base64.urlsafe_b64encode(hashlib.sha256(PRODUCTION_URL.encode()).digest()[:9]).decode().rstrip("=")
    readme = (PLUGIN / "README.md").read_text()

    assert callback_id == CODEX_CALLBACK_ID
    assert "mcp_oauth_callback_port = 8788" in readme
    assert f"http://127.0.0.1:8788/callback/{CODEX_CALLBACK_ID}" in readme


def test_package_has_no_secrets_placeholders_or_duplicate_skill() -> None:
    forbidden = re.compile(
        r"<[^>]+>|client[_-]?secret|api[_-]?key|BEGIN (?:RSA |EC )?PRIVATE KEY|\$\{[^}]+\}",
        re.IGNORECASE,
    )
    package_paths = [
        *PLUGIN.rglob("*"),
        ROOT / ".cursor-plugin" / "marketplace.json",
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
    ]
    for path in package_paths:
        if path.is_file():
            # ponytail: skip binary assets (e.g. logo.png) — secrets scan is for text.
            if b"\x00" in path.read_bytes()[:2048]:
                continue
            assert forbidden.search(path.read_text()) is None, path

    assert not (ROOT / "integrations" / "cursor" / "solstice-platform").exists()
    assert list(ROOT.glob("**/skills/solstice-platform/SKILL.md")) == [
        PLUGIN / "skills" / PLUGIN_NAME / "SKILL.md"
    ]
