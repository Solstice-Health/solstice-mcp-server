import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "solstice-platform"
PLUGIN_NAME = "solstice-platform"
PLUGIN_VERSION = "0.3.6"
PRODUCTION_URL = "https://solstice-mcp-l6apghhxpf.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
PRODUCTION_AUDIENCE = PRODUCTION_URL
CURSOR_CLIENT_ID = "uoOiEXHZxyDBkkBEfnOQEp6IhqcnAgTP"
SCOPES = {"mcp:connect", "openid", "email"}
CODEX_CALLBACK_ID = "TL-8G9qfe5UK"


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
    assert set(cursor["auth"]["scopes"]) == set(claude["auth"]["scopes"]) == SCOPES
    assert set(claude["oauth"]["scopes"].split()) == set(codex["scopes"]) == SCOPES
    assert claude["type"] == "http"
    assert codex["auth"] == "oauth"
    assert codex["oauth_resource"] == PRODUCTION_AUDIENCE
    assert codex["default_tools_approval_mode"] == "writes"

    # Static PKCE client (no secret); DCR is disabled on the Auth0 tenant, so
    # every host config must carry the pre-registered client ID in its own
    # dialect. CRITICAL: .mcp.json must carry BOTH blocks, because Cursor loads
    # the dotfile .mcp.json in preference to mcp.json (proven twice: e7c5616 on
    # Jul 21 and the 0.3.4 regression on Jul 23 — oauth-only .mcp.json makes
    # Cursor fall back to DCR, which Auth0 rejects with 400, and the login
    # browser never opens). Cursor reads auth.CLIENT_ID; Claude reads
    # oauth.clientId (its documented no-DCR mechanism — it ignores the
    # Cursor-style auth block); Codex reads oauth.client_id in codex.mcp.json.
    # Each host ignores the other's key. Claude's loopback callback port 8787
    # matches the http://localhost:8787/callback redirect registered at Auth0.
    assert cursor["auth"]["CLIENT_ID"] == claude["auth"]["CLIENT_ID"] == CURSOR_CLIENT_ID
    assert claude["oauth"]["clientId"] == codex["oauth"]["client_id"] == CURSOR_CLIENT_ID
    assert claude["oauth"]["callbackPort"] == 8787


def test_shared_skill_is_portable_and_action_focused() -> None:
    fields, body = skill_frontmatter()
    assert set(fields) == {"name", "description"}
    assert fields["name"] == PLUGIN_NAME

    body_lower = body.lower()
    for action in (
        "my workspaces",
        "my brands",
        "brand context",
        "projects",
        "content reviews",
        "review activity",
        "open/read document",
    ):
        assert action in body_lower
    for language in ("fetch=false", "fetch=true", "ask", "untrusted user content", "no change was made"):
        assert language in body_lower

    skill_dir = PLUGIN / "skills" / PLUGIN_NAME
    references = {
        "actions.md",
        "data-model.md",
        "errors.md",
        "memory.md",
        "operation-types.md",
        "request-triage.md",
    }
    assert {path.name for path in (skill_dir / "references").glob("*.md")} == references
    for reference in references:
        assert f"(references/{reference})" in body


def test_figma_to_solstice_skill_is_portable_and_human_in_loop() -> None:
    skill_name = "figma-to-solstice"
    text = (PLUGIN / "skills" / skill_name / "SKILL.md").read_text()
    marker, frontmatter, body = text.split("---", 2)
    assert marker == ""
    fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
    assert set(fields) == {"name", "description"}
    assert fields["name"] == skill_name
    assert "figma" in fields["description"].lower()

    body_lower = body.lower()
    for phrase in (
        "no write until approval",
        "human-in-loop",
        "solstice_brand_rules",
        "solstice_brand_design_assets",
        "solstice_brand_claims",
        "solstice_create_operation",
        "untrusted content",
    ):
        assert phrase in body_lower

    references = {"conversion-workflow.md"}
    skill_dir = PLUGIN / "skills" / skill_name
    assert {path.name for path in (skill_dir / "references").glob("*.md")} == references
    assert "(references/conversion-workflow.md)" in body
    workflow = (skill_dir / "references" / "conversion-workflow.md").read_text().lower()
    assert "only after approval" in workflow
    assert "solstice_prepare_operation_version" in workflow
    assert "solstice_commit_operation_version" in workflow


def test_isi_replacement_skill_is_portable_and_human_in_loop() -> None:
    skill_name = "isi-replacement"
    text = (PLUGIN / "skills" / skill_name / "SKILL.md").read_text()
    marker, frontmatter, body = text.split("---", 2)
    assert marker == ""
    fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
    assert set(fields) == {"name", "description"}
    assert fields["name"] == skill_name
    assert "isi" in fields["description"].lower()

    body_lower = body.lower()
    for phrase in (
        "no write until approval",
        "human-in-loop",
        "verbatim",
        "solstice_brand_rules",
        "solstice_operation_html",
        "untrusted content",
        "append-only",
    ):
        assert phrase in body_lower

    references = {"isi-workflow.md"}
    skill_dir = PLUGIN / "skills" / skill_name
    assert {path.name for path in (skill_dir / "references").glob("*.md")} == references
    assert "(references/isi-workflow.md)" in body
    workflow = (skill_dir / "references" / "isi-workflow.md").read_text().lower()
    assert "only after approval" in workflow
    assert "solstice_prepare_operation_version" in workflow
    assert "solstice_commit_operation_version" in workflow
    assert "solstice_approve_operation_version" in workflow
    # The wizard-parity checklist: every input group the admin UI collects.
    for input_group in (
        "brand isi",
        "pasted html",
        "docx",
        "date / copyright",
        "subject / preheader",
        "veeva job codes",
        "find → replace",
    ):
        assert input_group in workflow


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
