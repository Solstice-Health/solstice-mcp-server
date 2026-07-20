# Solstice Platform Cursor plugin

This plugin connects Cursor to the production Solstice Streamable HTTP MCP
server through Auth0 Authorization Code with PKCE.

It exposes server metadata, tenant discovery, tenant identity, and four
truthful non-operational Slack stubs. The public OAuth client ID in `mcp.json`
has no secret. Auth0 must allow these Cursor callbacks:

- `https://www.cursor.com/agents/mcp/oauth/callback`
- `http://localhost:8787/callback`

The packaged configuration targets `https://api.solsticehealth.co/mcp`. Edit
the URL to use another environment. The selected URL must match the token
audience. See `docs/MCP_CLIENT_SETUP.md` in the solstice-mcp-server repository.
