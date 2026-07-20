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
    DATABASE_URL_TEMPLATE: str = ""
    DATABASE_URL_TEMPLATE_DEV: str = ""
    DATABASE_URL_TEMPLATE_PROD: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(**{name: os.getenv(name, field.default) for name, field in cls.__dataclass_fields__.items()})

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
