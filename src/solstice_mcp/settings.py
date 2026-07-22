"""Runtime settings read from the small MCP environment contract."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ENV: str = "development"
    AUTH0_DOMAIN: str = ""
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
        values["S3_PRESIGN_EXPIRY_SECONDS"] = int(values["S3_PRESIGN_EXPIRY_SECONDS"])
        values["S3_MAX_INLINE_BYTES"] = int(values["S3_MAX_INLINE_BYTES"])
        values["SOLSTICE_BACKEND_TIMEOUT_SECONDS"] = int(values["SOLSTICE_BACKEND_TIMEOUT_SECONDS"])
        token_timeout_key = "SOLSTICE_BACKEND_AUTH0_TOKEN_TIMEOUT_SECONDS"
        values[token_timeout_key] = int(values[token_timeout_key])
        return cls(**values)

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
