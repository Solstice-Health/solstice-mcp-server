# Auth0 email claim

`solstice-mcp` is a **discovery and authorization hub**. It tells the agent
which sibling MCPs (Linear, Jam, Slack) a Solstice internal user is allowed to
use and how to connect them in their IDE. The IDE (Cursor) connects to each
sibling MCP directly and calls them directly — `solstice-mcp` holds zero
sibling credentials, relays zero calls, and stores zero per-user credentials.

The authorization check that decides whether to reveal the sibling MCP
directory is the user's email domain. The hub reads the `email` claim from
the verified Auth0 RS256 access token and allows iff it ends with
`@solsticehealth.co` (see `src/solstice_mcp/gate.py`). The claim must therefore
be present in the access token, not just the ID token.

## Why the email claim is needed

`MCPAccessTokenVerifier` validates the JWT (issuer, audience, expiry, RS256
signature, `mcp:connect` scope) and exposes the decoded payload as
`AccessToken.claims`. `solstice_check_access` and `solstice_list_sibling_mcps`
read `claims["email"]` and pass it to `SolsticeAccessGate.evaluate`. If the
claim is missing the gate fails closed: the caller sees an empty sibling MCP
list. The directory is the authorization surface — non-Solstice users see
nothing.

The Cursor client requests `mcp:connect openid email` scopes (see
`integrations/cursor/solstice-platform/mcp.json`). Auth0 mints the `email`
claim into the access token only when an Auth0 Action adds it; the default
behavior puts `email` in the ID token, not the access token. The Action below
is the bridge.

## Auth0 Action code

The Action runs on `post-login` and only mints the claim when the request
audience is one of the MCP resource identifiers (dev, staging,
platform-testing, prod, localhost). Tokens minted for other audiences are
left untouched.

```javascript
exports.onExecutePostLogin = async (event, api) => {
  const mcpAudiences = ${MCP_AUDIENCES_JSON};
  if (!mcpAudiences.includes(event.request.audience)) {
    return;
  }
  if (event.user && event.user.email) {
    api.accessToken.setCustomClaim("email", event.user.email);
  }
  if (event.user && typeof event.user.email_verified === "boolean") {
    api.accessToken.setCustomClaim("email_verified", event.user.email_verified);
  }
};
```

`${MCP_AUDIENCES_JSON}` is interpolated by Terraform from
`jsonencode(values(var.mcp_resource_identifiers))` so the audience list stays
in sync with the resource servers already declared in
`terraform/environments/mcp/main.tf`.

## Install path (Terraform-managed, opt-in)

The Action is provisioned by `auth0_action.mint_email_claim` and
`auth0_trigger_action.mint_email_claim` in
`Backend-Server/terraform/environments/mcp/main.tf`. It is gated behind a new
variable:

```hcl
variable "enable_email_claim_action" {
  description = "Opt in to mint the email claim into MCP access tokens via an Auth0 post-login Action."
  type        = bool
  default     = false
}
```

Default is `false`. Enable it in the `mcp` environment's tfvars only after
importing the Action and confirming the plan changes no other Auth0 resources.
The Action uses `node18` runtime, `v3` post-login trigger, and `deploy = true`
so the code is live after `terraform apply`.

## End-to-end verification

1. Apply with `enable_email_claim_action = true`.
2. In Cursor, reconnect the `solstice-platform` MCP server (Settings → MCP →
   reconnect) so a fresh access token is minted with the `email` claim.
3. From the agent, call `solstice_check_access` with no arguments.
4. Expect:
   ```json
   {
     "allowed": true,
     "email": "<your>@solsticehealth.co",
     "reason": "email domain allowed",
     "allowed_domain": "@solsticehealth.co"
   }
   ```
5. Call `solstice_list_sibling_mcps` and confirm the directory is non-empty.
6. (Negative path) A token minted for a non-Solstice email returns
   `{"allowed": false, "reason": "email domain not allowed", "sibling_mcps": []}`
   from `solstice_list_sibling_mcps` — the directory stays hidden.
