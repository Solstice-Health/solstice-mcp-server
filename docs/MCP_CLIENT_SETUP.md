# MCP client setup

The Solstice server uses Streamable HTTP, OAuth 2.1 Authorization Code with PKCE, RFC 9728 protected-resource metadata, and RFC 8707 resource indicators. Dynamic Client Registration is disabled. Every client is public, uses PKCE, and has no client secret.

## Endpoints and token contract

- Production: `https://api.solsticehealth.co/mcp`
- Staging: `https://api-staging.solsticehealth.co/mcp`
- Platform testing: `https://api-platform-testing.solsticehealth.co/mcp`
- Dev: `https://api-dev.solsticehealth.co/mcp`

Each MCP URL is also its Auth0 audience. The issuer is `https://login-solstice.us.auth0.com/`. Clients request `mcp:connect openid email`.

Auth0 mints RS256 access tokens. The server verifies the signature against Auth0 JWKS, issuer, audience, expiry, subject, and the required `mcp:connect` scope. Tenant membership, brand membership, roles, and draft visibility are then checked from server-side data. The `email` claim supports the internal sibling-MCP access gate; see [AUTH0_EMAIL_CLAIM.md](AUTH0_EMAIL_CLAIM.md).

Clients discover authorization metadata from the RFC 9728 URL advertised in a 401 response. Direct Auth0 endpoints are `/authorize`, `/oauth/token`, and `/.well-known/jwks.json` under the issuer.

## Cursor authentication round trip

The Cursor adapter is `plugins/solstice-platform/mcp.json`. It uses the existing public Cursor Auth0 client and the production MCP URL. Auth0 allows these callbacks:

- `https://www.cursor.com/agents/mcp/oauth/callback`
- `http://localhost:8787/callback`

The round trip is:

1. Cursor connects to the production MCP URL and receives protected-resource metadata.
2. Cursor starts Authorization Code with PKCE for the configured public client, production audience, and scopes `mcp:connect openid email`.
3. Auth0 redirects to an allowed Cursor callback and Cursor exchanges the code plus PKCE verifier for an RS256 access token.
4. Cursor sends the access token as a Bearer credential to the MCP endpoint.
5. The MCP server validates the token contract, then applies tenant and brand authorization to each tool call.

If validation fails, the server returns 401 for a missing, malformed, expired, wrong-issuer, or wrong-audience token. A valid token without `mcp:connect` receives 403. Reconnect OAuth rather than editing credentials.

## Claude Code authentication round trip

The Claude adapter is `plugins/solstice-platform/.mcp.json`. It uses Streamable HTTP, callback `http://localhost:8787/callback`, and scopes `mcp:connect openid email`.

For the local pilot, the adapter temporarily uses the existing public Cursor client ID. After the Backend-Server Terraform change is applied, retrieve `claude_client_id` and update `.mcp.json` through a reviewed pull request. The callback, audience, URL, and scopes stay unchanged.

The round trip is:

1. Claude Code loads the plugin and connects to the production MCP URL.
2. Claude Code starts Authorization Code with PKCE for the configured public client, production audience, and scopes `mcp:connect openid email`.
3. Auth0 redirects to `http://localhost:8787/callback`, then Claude Code exchanges the code plus PKCE verifier for an RS256 access token.
4. Claude Code stores the user's token in its credential flow and sends it as a Bearer credential to the MCP endpoint.
5. The MCP server performs the same token, tenant, brand, role, and draft-visibility checks used for Cursor.

Authenticate from `/mcp`, or use `claude mcp login solstice-platform` when available in the installed Claude Code version. OAuth tokens are per user and are never included in the plugin.

## Codex authentication round trip

The Codex adapter is `plugins/solstice-platform/codex.mcp.json`. It uses Streamable HTTP, a pre-registered public client ID, the production audience, and scopes `mcp:connect openid email`. Codex 0.142.0 or newer is required for static MCP OAuth client IDs.

Set `mcp_oauth_callback_port = 8788` at the top level of `~/.codex/config.toml`. For the production MCP URL, Codex derives the callback `http://127.0.0.1:8788/callback/rNbuDm0IFnc1`; Auth0 must contain that exact value.

For the local pilot, the adapter temporarily uses the existing public Cursor client ID after Terraform adds the Codex callback to its allowlist. Terraform also provisions a dedicated Codex client. After apply, retrieve `codex_client_id` and update `codex.mcp.json` through a reviewed pull request.

The round trip is:

1. Codex loads the plugin's MCP server and connects to the production URL.
2. `codex mcp login solstice-platform` starts Authorization Code with PKCE using the configured public client ID, production audience, and scopes.
3. Auth0 redirects to the fixed loopback callback and Codex exchanges the code plus PKCE verifier for an RS256 access token.
4. Codex stores the token in its configured credential store and sends it as a Bearer credential to the MCP endpoint.
5. The MCP server performs the same token, tenant, brand, role, and draft-visibility checks used for Cursor and Claude Code.

Codex's plugin policy uses `writes` approval mode, so read-only tools follow normal policy while the prepare and commit version-write tools prompt for approval.

## Workspace selection

After OAuth:

1. Call `solstice_list_tenants`.
2. Select the only result without prompting.
3. If there are multiple results, ask the user which workspace to use.
4. If there are no results, stop and request access.
5. Pass the selected `tenant_slug` to every workspace-bound call.

The server is stateless. Never assume the first workspace or reuse a workspace choice from another session.

## Other clients

Other clients need their own registered public Auth0 client, exact callback URL, PKCE support, and the token contract above. The Cursor, Claude, and Codex manifests are not portable client configuration. See [the plugin compatibility guide](../plugins/solstice-platform/clients.md) for the manual MCP and Agent Skill fallback.

## Troubleshooting

- **401:** reconnect OAuth. Check that the token's issuer and audience match the selected MCP environment.
- **403:** reauthorize with `mcp:connect`.
- **Callback failure:** confirm the client uses an Auth0-registered callback. Claude Code must use port `8787`; Codex must use port `8788` and the production callback ID shown above.
- **`not_member`:** call `solstice_list_tenants` again. The workspace may be unknown, belong to another environment, or no longer contain a live membership.
- **Access denied or not found:** do not infer that an inaccessible resource exists. Re-list the parent collection and choose only from returned results.
