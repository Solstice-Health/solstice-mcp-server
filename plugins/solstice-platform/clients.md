# Client compatibility

The portable pieces are the production Streamable HTTP endpoint and `skills/solstice-platform/SKILL.md`. Plugin manifests, MCP configuration, OAuth fields, and marketplace catalogs are host-specific.

## Support matrix

**Cursor**

- Plugin package: supported by `.cursor-plugin/plugin.json` and `mcp.json`
- Local loading: supported through `~/.cursor/plugins/local`
- Team distribution: supported through the private Cursor team marketplace
- OAuth: configured for the existing public Cursor PKCE client

**Claude Code**

- Plugin package: supported by `.claude-plugin/plugin.json` and `.mcp.json`
- Local loading: supported with `claude --plugin-dir`
- Team distribution: supported through the private Claude marketplace
- OAuth: configured for a local pilot; the dedicated Claude public PKCE client is pending Terraform provision and sync

**Other MCP clients**

- Plugin package: not provided
- MCP: may work if the client supports remote Streamable HTTP and OAuth 2.1 Authorization Code with PKCE
- Agent Skill: copy or install the shared `SKILL.md` only if the client supports the Agent Skills format
- Support status: manual and unverified until a named client joins the acceptance matrix

## Manual fallback

For another MCP-capable client:

1. Configure `https://api.solsticehealth.co/mcp` as a remote Streamable HTTP server.
2. Use a registered public Auth0 client with PKCE, the same URL as the audience, and scopes `mcp:connect openid email`.
3. Register the client's exact OAuth callback URL in Auth0.
4. Complete OAuth as the individual user.
5. If the client supports Agent Skills, copy `skills/solstice-platform/SKILL.md` and its `references/` directory to that client's documented skill location.

Do not reuse the Cursor or Claude manifest in another client. Do not place a client secret in local MCP configuration. A client without compatible OAuth cannot use the protected production endpoint.
