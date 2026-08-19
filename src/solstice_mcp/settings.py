"""Runtime settings read from the small MCP environment contract."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_configured(value: str) -> bool:
    """True when a setting is present and not a Terraform/deploy stub.

    ECS task defs historically inject ``unused`` for required-but-irrelevant
    keys so the process can boot; treat that the same as empty so env-gated
    tools (user-admin) never half-register on a stub secret.
    """
    cleaned = value.strip()
    return bool(cleaned) and cleaned.lower() != "unused"


@dataclass(frozen=True)
class Settings:
    ENV: str = "development"
    AUTH0_DOMAIN: str = ""
    # Management-capable Auth0 application (create:users/read:users/update:users
    # on the /api/v2/ audience). Mirrors the Backend-Server env names. Empty
    # values disable the user-admin tools.
    AUTH0_CLIENT_ID: str = ""
    AUTH0_CLIENT_SECRET: str = ""
    # Central auth database URL (canonical users rows). Empty disables the
    # user-admin tools.
    CENTRAL_AUTH_DB: str = ""
    MCP_RESOURCE_URL: str = ""
    TENANT_CONFIG_PATH: str = "config/tenants.json"
    ALLOWED_EMAIL_DOMAIN: str = "@solsticehealth.co"
    SIBLING_MCP_CONFIG_PATH: str = "config/sibling_mcps.json"
    DATABASE_URL_TEMPLATE: str = ""
    DATABASE_URL_TEMPLATE_DEV: str = ""
    DATABASE_URL_TEMPLATE_PROD: str = ""
    AWS_REGION: str = "us-east-1"
    S3_PRESIGN_EXPIRY_SECONDS: int = 600
    S3_MAX_INLINE_BYTES: int = 2_000_000
    # Backend-Server internal memory routes. Empty base URL disables memory tools.
    SOLSTICE_BACKEND_BASE_URL: str = ""
    SOLSTICE_BACKEND_TIMEOUT_SECONDS: int = 10
    SOLSTICE_BACKEND_AUTH0_CLIENT_ID: str = ""
    SOLSTICE_BACKEND_AUTH0_CLIENT_SECRET: str = ""
    SOLSTICE_BACKEND_AUTH0_AUDIENCE: str = ""
    SOLSTICE_BACKEND_AUTH0_SCOPE: str = "memory:invoke"
    SOLSTICE_BACKEND_AUTH0_TOKEN_TIMEOUT_SECONDS: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        values = {name: os.getenv(name, field.default) for name, field in cls.__dataclass_fields__.items()}
        values["S3_PRESIGN_EXPIRY_SECONDS"] = int(str(values["S3_PRESIGN_EXPIRY_SECONDS"]))
        values["S3_MAX_INLINE_BYTES"] = int(str(values["S3_MAX_INLINE_BYTES"]))
        values["SOLSTICE_BACKEND_TIMEOUT_SECONDS"] = int(str(values["SOLSTICE_BACKEND_TIMEOUT_SECONDS"]))
        token_timeout_key = "SOLSTICE_BACKEND_AUTH0_TOKEN_TIMEOUT_SECONDS"
        values[token_timeout_key] = int(str(values[token_timeout_key]))
        return cls(**values)  # type: ignore[arg-type]

    @property
    def user_admin_auth0_configured(self) -> bool:
        return _env_configured(self.AUTH0_CLIENT_ID) and _env_configured(self.AUTH0_CLIENT_SECRET)

    @property
    def central_auth_db_configured(self) -> bool:
        return _env_configured(self.CENTRAL_AUTH_DB)

    @property
    def tenant_environment(self) -> str:
        return "production" if self.ENV.strip().lower() in {"prod", "production"} else "development"

    @property
    def database_url_template(self) -> str:
        if self.DATABASE_URL_TEMPLATE:
            return self.DATABASE_URL_TEMPLATE
        if self.tenant_environment == "production":
            return self.DATABASE_URL_TEMPLATE_PROD
        return self.DATABASE_URL_TEMPLATE_DEV

    @property
    def database_url_templates(self) -> dict[str, str]:
        """Per-env URL templates for cross-environment tenant discovery.

        The MCP task probes tenant databases in any environment it has a template
        for; access is gated by the ``users`` table in each tenant DB, not by the
        task's own environment.
        """
        templates: dict[str, str] = {}
        if self.DATABASE_URL_TEMPLATE:
            templates[self.tenant_environment] = self.DATABASE_URL_TEMPLATE
        if self.DATABASE_URL_TEMPLATE_DEV:
            templates["development"] = self.DATABASE_URL_TEMPLATE_DEV
        if self.DATABASE_URL_TEMPLATE_PROD:
            templates["production"] = self.DATABASE_URL_TEMPLATE_PROD
        return templates

    @property
    def issuer_url(self) -> str:
        return f"https://{self.AUTH0_DOMAIN.strip().rstrip('/')}/"


settings = Settings.from_env()
