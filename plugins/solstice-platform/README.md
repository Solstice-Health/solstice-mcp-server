# Solstice Platform plugin

This private plugin connects Cursor and Claude Code to the production Solstice MCP. It provides one shared Agent Skill for workspaces, brands, projects, content reviews, review activity, documents, and append-only HTML or PDF document versions. Each user signs in through Auth0. No secret or environment setup is required.

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

## Temporary Claude OAuth client

The Claude adapter temporarily uses the existing public Cursor Auth0 client ID for the local pilot. It uses callback `http://localhost:8787/callback` and requests `mcp:connect openid email`. This client is public and has no secret.

After Backend-Server Terraform provisions the dedicated Claude client, retrieve its `claude_client_id` output and update `.mcp.json` through a reviewed pull request. The production MCP URL and audience remain `https://api.solsticehealth.co/mcp`.

See [clients.md](clients.md) for other MCP clients and [the repository setup guide](../../docs/MCP_CLIENT_SETUP.md) for the complete authentication contract.
