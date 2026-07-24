# Solstice Platform plugin

This private plugin connects Cursor, Claude Code, and Codex to the production Solstice MCP. Its bundled Agent Skills cover platform operations, Figma conversion, PRC template recreation, ISI replacement, and browser fallback. Each user signs in through Auth0; no client secret is distributed.

## Cursor local

From the repository root:

```bash
mkdir -p ~/.cursor/plugins/local
ln -sfn "$(pwd)/plugins/solstice-platform" ~/.cursor/plugins/local/solstice-platform
```

Reload Cursor with **Developer: Reload Window**. Open **Settings > MCP**, select `solstice-platform`, and connect OAuth. To pick up local edits, reload Cursor again.

## Claude Code local

From the repository root:

```bash
claude --plugin-dir ./plugins/solstice-platform
```

Run `/reload-plugins` after local edits. Authenticate `solstice-platform` from `/mcp`, or run `claude mcp login solstice-platform` if your Claude Code version provides that command.

## Codex local

Codex requires a fixed callback port because Auth0 does not accept ephemeral loopback ports. Add this top-level setting to `~/.codex/config.toml`:

```toml
mcp_oauth_callback_port = 8788
```

Then, from the repository root:

```bash
codex plugin marketplace add "$(pwd)"
codex plugin add solstice-platform@solstice-tools
codex mcp login solstice-platform
```

Restart Codex after installing or updating the plugin.

## Cursor team marketplace

An administrator imports the private `solsticehealth/solstice-mcp-server` repository from **Dashboard > Plugins > Team Marketplaces > Add Marketplace > Import from Repo**. Team members install **Solstice Platform** from Cursor's Customize view and connect OAuth in MCP settings.

## Claude Code private marketplace

Configure Git credentials for the private repository, then run:

```text
/plugin marketplace add solsticehealth/solstice-mcp-server
/plugin install solstice-platform@solstice-tools
/reload-plugins
```

Authenticate the MCP from `/mcp`. Plugin installation does not share credentials; every user completes OAuth.

## Codex private marketplace

Configure Git credentials for the private repository, then run:

```bash
codex plugin marketplace add solsticehealth/solstice-mcp-server
codex plugin add solstice-platform@solstice-tools
codex mcp login solstice-platform
```

The fixed callback-port setting from **Codex local** is required for both local and marketplace installs. Codex prompts before the two append-only version-write tools because the bundled MCP policy uses `writes` approval mode.

## Update or uninstall

For Cursor local use, pull repository changes and reload. Remove the symlink to uninstall:

```bash
rm ~/.cursor/plugins/local/solstice-platform
```

Team marketplace users update or uninstall the plugin from Cursor's Customize view. Marketplace administrators can enable auto-refresh after importing the repository.

For Claude Code:

```text
/plugin marketplace update solstice-tools
/plugin update solstice-platform@solstice-tools
/plugin uninstall solstice-platform@solstice-tools
```

Run `/reload-plugins` after an update or uninstall.

For Codex:

```bash
codex plugin marketplace upgrade solstice-tools
codex plugin remove solstice-platform@solstice-tools
```

Restart Codex after an update or uninstall.

## Temporary OAuth client IDs

The Claude adapter temporarily uses the existing public Cursor Auth0 client ID for the local pilot. It uses callback `http://localhost:8787/callback` and requests `mcp:connect openid email`. This client is public and has no secret.

The Codex adapter also temporarily uses the Cursor public client ID. Backend-Server Terraform registers Codex's fixed `http://127.0.0.1:8788/callback/TL-8G9qfe5UK` callback on that pilot client and provisions a dedicated Codex client.

After Terraform apply, retrieve `claude_client_id` and `codex_client_id`, then update `.mcp.json` and `codex.mcp.json` through a reviewed pull request. The production MCP URL and audience are `https://solstice-mcp-l6apghhxpf.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp`.

See [clients.md](clients.md) for other MCP clients and [the repository setup guide](../../docs/MCP_CLIENT_SETUP.md) for the complete authentication contract.
