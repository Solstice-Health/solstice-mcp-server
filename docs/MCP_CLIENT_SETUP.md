# MCP client setup

The Solstice server uses Streamable HTTP, OAuth 2.1 Authorization Code with
PKCE, RFC 9728 protected-resource metadata, and RFC 8707 resource indicators.

## Endpoints

- Production: `https://api.solsticehealth.co/mcp`
- Staging: `https://api-staging.solsticehealth.co/mcp`
- Platform testing: `https://api-platform-testing.solsticehealth.co/mcp`
- Dev: `https://api-dev.solsticehealth.co/mcp`

Each URL is also its Auth0 audience. The issuer is
`https://login-solstice.us.auth0.com/`, and clients request `mcp:connect`.
Dynamic Client Registration is disabled. Public clients use PKCE and have no
client secret.

Cursor callback URLs:

- `https://www.cursor.com/agents/mcp/oauth/callback`
- `http://localhost:8787/callback`

Clients discover authorization metadata from the RFC 9728 URL advertised in a
401 response. Direct Auth0 endpoints are `/authorize`, `/oauth/token`, and
`/.well-known/jwks.json` under the issuer above.

## Tenant selection

After OAuth:

1. Call `solstice_list_tenants`.
2. Select the only result without prompting.
3. If there are multiple results, ask the user which tenant to use.
4. If there are no results, stop and request tenant access.
5. Pass `tenant_slug` to every tenant-bound call.

The server is stateless. Never assume the first tenant or reuse a tenant choice
from another session.

## Cursor

The packaged plugin is in `integrations/cursor/solstice-platform`. Its
`mcp.json` targets production. A manual project or global Cursor configuration
is equivalent:

```json
{
  "mcpServers": {
    "solstice-platform": {
      "url": "https://api.solsticehealth.co/mcp",
      "auth": {
        "CLIENT_ID": "<SOLSTICE_MCP_CURSOR_CLIENT_ID>",
        "scopes": ["mcp:connect"]
      }
    }
  }
}
```

## Other clients

Claude Code needs its own registered public Auth0 client and fixed callback:

```bash
claude mcp add-json solstice-platform \
  '{"type":"http","url":"https://api.solsticehealth.co/mcp","oauth":{"clientId":"<CLIENT_ID>","callbackPort":8080,"scopes":"mcp:connect"}}'
```

VS Code can use a separately registered public client:

```json
{
  "servers": {
    "solstice-platform": {
      "type": "http",
      "url": "https://api.solsticehealth.co/mcp",
      "oauth": {"clientId": "<CLIENT_ID>"}
    }
  }
}
```

## Troubleshooting

A 401 means the token is missing, malformed, expired, has the wrong issuer, or
has the wrong audience. Reconnect the client to repeat OAuth. A 403 on the MCP
endpoint means the token lacks `mcp:connect`.

`solstice_whoami` returns `not_member` for an unknown tenant, an environment
mismatch, a cross-tenant subject, or a soft-deleted user. Re-run
`solstice_list_tenants`.

All four `solstice_slack_*` tools return `not_connected`. They contact no Slack
API and perform no side effect.
