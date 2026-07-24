# Solstice MCP server

Standalone Python 3.12 service for the Solstice remote MCP endpoint. It serves
stateless Streamable HTTP at `/mcp` and a public liveness check at `/health`.
It does not import the Backend-Server application.

## Authentication contract

Auth0 mints RS256 access tokens for the exact `MCP_RESOURCE_URL` audience.
This service fetches Auth0 JWKS and validates the signature, expiry, issuer,
audience, and subject. FastMCP then requires the `mcp:connect` scope. Invalid
tokens return 401 with RFC 9728 protected-resource metadata; valid tokens
without the scope return 403. Tokens stay inside this process and are never
forwarded.

## Configuration

The service reads only these variables:

- `ENV`, where `prod` and `production` select production tenants and every
  other value selects development tenants
- `AUTH0_DOMAIN`
- `MCP_RESOURCE_URL`
- `TENANT_CONFIG_PATH`, defaulting to `config/tenants.json`
- `DATABASE_URL_TEMPLATE`
- `DATABASE_URL_TEMPLATE_DEV` and `DATABASE_URL_TEMPLATE_PROD`, used only when
  the primary template is empty

Database templates must contain `{db_name}`. Other ECS environment variables
are ignored.

## Memory tools (optional)

The five `solstice_memory_*` tools are registered only when the Backend memory
contract is configured. When `SOLSTICE_BACKEND_BASE_URL` and
`SOLSTICE_BACKEND_AUTH0_CLIENT_ID` are unset, the tools are not exposed and the
MCP remains a pure content-review server. The MCP stays stateless: it validates
the end-user OAuth subject, rechecks tenant/brand membership, then calls the
Backend-Server `/api/internal/agent-memory` routes with an RS256 Auth0
client-credentials bearer and a server-derived actor envelope. The tenant
Postgres store and the mutation audit log live in Backend-Server; the MCP
never touches them.

`solstice_memory_observe` is a cooperative host finalizer for bounded personal
preferences and conventions. It does not capture conversations or automatically
write brand memory; explicit remember, replace, and forget behavior is unchanged.

- `SOLSTICE_BACKEND_BASE_URL` — Backend-Server origin (e.g. `https://api.solsticehealth.co`)
- `SOLSTICE_BACKEND_TIMEOUT_SECONDS` — per-request timeout, default 10
- `SOLSTICE_BACKEND_AUTH0_CLIENT_ID` / `SOLSTICE_BACKEND_AUTH0_CLIENT_SECRET` —
  confidential M2M client credentials
- `SOLSTICE_BACKEND_AUTH0_AUDIENCE` — memory-specific Auth0 audience
- `SOLSTICE_BACKEND_AUTH0_SCOPE` — default `memory:invoke`
- `SOLSTICE_BACKEND_AUTH0_TOKEN_TIMEOUT_SECONDS` — Auth0 token endpoint timeout, default 5

## Local checks

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest
```

Run the service after setting the required environment:

```bash
PYTHONPATH=src gunicorn -w 2 -k uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:8000 mcp_main:app
```

## Infrastructure ownership

Terraform remains in Backend-Server because its existing state owns the Auth0,
ECR, ECS, ALB, security group, and RDS wiring. This repository only builds and
deploys the application image to those resources.
