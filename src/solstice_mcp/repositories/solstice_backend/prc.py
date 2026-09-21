"""The Backend's PRC write and validation routes.

The MCP authenticates as a service and names the person it acts for as
``actor_sub`` in the request body, the way the agent-memory plane already does.
The Backend revalidates that subject against the tenant and derives write
intent from their brand role; it is a selector, never a credential, and it
comes from the verified end-user token — never from a tool argument and never
from model output.
"""

from __future__ import annotations

from typing import Any

from solstice_mcp.repositories.solstice_backend.session import BackendSession


class PrcRepository:
    def __init__(self, session: BackendSession) -> None:
        self._session = session

    def close(self) -> None:
        self._session.close()

    # -- reads ----------------------------------------------------------------

    def template_rules(self, *, profile: str) -> dict[str, Any]:
        return self._session.request(
            "GET", "/api/v2/prc-template-rules", params={"profile": profile}
        )

    # -- writes ---------------------------------------------------------------

    def prepare_upload(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, artifact: str
    ) -> dict[str, Any]:
        return self._session.request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions/prepare",
            tenant_slug=tenant_slug,
            json_body={"artifact": artifact, "actor_sub": actor_sub},
        )

    def commit_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._session.request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions",
            tenant_slug=tenant_slug,
            json_body={**body, "actor_sub": actor_sub},
        )

    def publish_version(
        self, *, tenant_slug: str, actor_sub: str, operation_id: str, message_id: str
    ) -> dict[str, Any]:
        """Mark a version final.

        ``qc_override`` keeps the behaviour agents have today: the QC gate is a
        reviewer workflow, and an agent approval has no reviewer behind it.
        ``unlock_for_viewers`` closes the change requests and tracker rows that
        would otherwise leave the asset reading "being prepared" to the people
        it was approved for. The app closes those through the notification
        surface, which also emails; agents never have.
        """
        return self._session.request(
            "POST",
            f"/api/v2/operations/{operation_id}/versions/{message_id}/publish",
            tenant_slug=tenant_slug,
            params={"qc_override": "true", "unlock_for_viewers": "true"},
            json_body={"actor_sub": actor_sub},
        )
