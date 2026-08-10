# Client compatibility

The portable pieces are the production Streamable HTTP endpoint and `skills/solstice-platform/SKILL.md`. Plugin manifests, MCP configuration, OAuth fields, and marketplace catalogs are host-specific.

## Production endpoints (dual pointing)

| Host | Config file | Endpoint | Why |
|---|---|---|---|
| Cursor | `mcp.json` / `.mcp.json` | `https://api.solsticehealth.co/mcp` (ECS) | Full tool list (clients that skip AgentCore `tools/list` pagination) |
| Claude Code | `.mcp.json` | same ECS URL (shared with Cursor) | Same pagination limitation as Cursor |
| Codex | `codex.mcp.json` | AgentCore gateway (below) | Keeps OBO + Cedar PromptAttack on write args |

AgentCore gateway (Codex + manual fallback):

`https://solstice-mcp-l6apghhxpf.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp`

Each URL is also its Auth0 audience. Scopes stay `mcp:connect openid email`. After switching endpoints, reconnect OAuth (new audience).

## Support matrix

**Cursor**

- Plugin package: supported by `.cursor-plugin/plugin.json` and `mcp.json`
- Local loading: supported through `~/.cursor/plugins/local`
- Team distribution: supported through the private Cursor team marketplace
- OAuth: configured for the existing public Cursor PKCE client
- Endpoint: ECS direct (Cursor prefers `.mcp.json` when both ship)

**Claude Code**

- Plugin package: supported by `.claude-plugin/plugin.json` and `.mcp.json`
- Local loading: supported with `claude --plugin-dir`
- Team distribution: supported through the private Claude marketplace
- OAuth: configured for a local pilot; the dedicated Claude public PKCE client is pending Terraform provision and sync
- Endpoint: ECS direct (shares `.mcp.json` with Cursor)

**Codex**

- Plugin package: supported by `.codex-plugin/plugin.json` and `codex.mcp.json`
- Local loading: supported through the repo marketplace at `.agents/plugins/marketplace.json`
- Team distribution: supported by adding the private repository as a Codex marketplace
- Hosts: plugin installation works in Codex CLI and Codex in the ChatGPT desktop app; the Codex IDE extension supports the shared MCP and skill but not plugin installation
- OAuth: requires Codex 0.142.0 or newer, `mcp_oauth_callback_port = 8788`, and the Terraform-registered fixed callback
- Approval policy: read tools run normally; Codex prompts for the prepare and commit version-write tools
- Endpoint: AgentCore gateway (`oauth_resource` matches the gateway URL)

**Other MCP clients**

- Plugin package: not provided
- MCP: may work if the client supports remote Streamable HTTP and OAuth 2.1 Authorization Code with PKCE
- Agent Skill: copy or install the shared `SKILL.md` only if the client supports the Agent Skills format
- Support status: manual and unverified until a named client joins the acceptance matrix

## Manual fallback

For another MCP-capable client that should keep AgentCore guardrails:

1. Configure `https://solstice-mcp-l6apghhxpf.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` as a remote Streamable HTTP server.
2. Use a registered public Auth0 client with PKCE, the same URL as the audience, and scopes `mcp:connect openid email`.
3. Register the client's exact OAuth callback URL in Auth0.
4. Complete OAuth as the individual user.
5. If the client supports Agent Skills, copy `skills/solstice-platform/SKILL.md` and its `references/` directory to that client's documented skill location.

For a client that cannot follow AgentCore tool-list pagination and needs the full tool inventory, point at `https://api.solsticehealth.co/mcp` instead (audience = that URL). You lose gateway Cedar/PromptAttack on that path; ECS still enforces Auth0 JWT + tenant/brand RBAC.

Do not reuse a Cursor, Claude, or Codex manifest in another client. Do not place a client secret in local MCP configuration. A client without compatible OAuth cannot use the protected production endpoint.
