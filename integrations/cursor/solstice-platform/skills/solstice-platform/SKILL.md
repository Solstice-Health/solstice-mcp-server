---
name: solstice-platform
description: >-
  Use when connected to the Solstice Platform MCP server. Discover permitted
  tenants after OAuth, select one safely, pass tenant_slug explicitly, and
  treat every Slack tool as a non-operational stub.
---

# Solstice Platform MCP

The server is stateless. The bearer token identifies the Auth0 user, while
every tenant-bound call requires an explicit `tenant_slug`.

## Start every session

1. Call `solstice_list_tenants` after OAuth.
2. If one tenant is returned, select it without prompting.
3. If multiple tenants are returned, ask the user to choose. Do not default to
   the first result.
4. If no tenants are returned, stop and tell the user an admin must grant
   access.
5. Call `solstice_whoami` with the selected slug.

Do not cache tenant selection across sessions or use a slug that was not
returned by `solstice_list_tenants`.

## Available tools

- `solstice_server_info`
- `solstice_list_tenants`
- `solstice_whoami`, which requires `tenant_slug`
- `solstice_slack_search`
- `solstice_slack_read`
- `solstice_slack_send`
- `solstice_slack_react`

The Slack tools always return `not_connected`. Never say a message was sent,
a channel was read, or a reaction was added. Do not fabricate Slack content
or work around the stubs.

A 401 requires reconnecting OAuth. A 403 means the token lacks
`mcp:connect`. A `not_member` result from `solstice_whoami` means the slug is
unknown, belongs to another runtime environment, or the user has no live row
in that tenant.

See `docs/MCP_CLIENT_SETUP.md` in the solstice-mcp-server repository.
