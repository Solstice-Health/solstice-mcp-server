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
PYTHONPATH=src gunicorn -w 2 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 mcp_main:app
```

## Infrastructure ownership

Terraform remains in Backend-Server because its existing state owns the Auth0,
ECR, ECS, ALB, security group, and RDS wiring. This repository only builds and
deploys the application image to those resources.
