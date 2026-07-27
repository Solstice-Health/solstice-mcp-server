"""Staff user administration: password resets, brand roles, and onboarding.

Mirrors the Backend-Server admin flows (see
``Backend-Server/src/company_management/routes/api/admin_user_routes.py``)
without importing them:

- Password reset, ``mode="email"``: POST Auth0 ``/dbconnections/change_password``
  — the Authentication API endpoint that actually dispatches the "Change
  Password" email (a Management API ticket alone does NOT email the user).
- Password reset, ``mode="temp_password"``: PATCH the Auth0 user with a
  generated strong password and return it plaintext to the caller. Used when
  the reset email doesn't arrive (corporate mail quarantine). The audit event
  records the mode; tool inputs/outputs are never logged.
- Onboarding: Auth0 create (``email_verified=True`` because creation is
  admin-initiated and the Auth0 tenant has a force-email-verification Action;
  409 → lookup + re-verify), then upsert of the canonical central-auth ``users``
  row, then the tenant ``users`` row with the same id, then an optional
  ``brand_team_members`` upsert, then the optional reset email. Auth0 comes
  first so a failure there leaves no partial DB rows.

Authorization: authority comes only from the verified OAuth subject, resolved
against ``brand_team_members`` — ``require_staff_in_tenant`` for tenant-wide
actions, ``require_brand_role(min_role=SOLSTICE_STAFF)`` for brand-scoped
ones. No tool argument grants anything.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from solstice_mcp.brands import Brand, BrandTeamMember, UserRole, require_brand_role
from solstice_mcp.memory_client import Auth0ClientCredentials, MemoryClientError
from solstice_mcp.requests import require_staff_in_tenant
from solstice_mcp.tenants import (
    SessionFactory,
    TenantRegistry,
    User,
    tenant_session,
)

logger = logging.getLogger(__name__)

RESET_MODES = ("email", "temp_password")
AUTH0_DB_CONNECTION = "Username-Password-Authentication"

CentralSessionFactory = Callable[[], Session]


class Auth0ConflictError(ToolError):
    """Auth0 returned HTTP 409 (resource already exists, e.g. duplicate user)."""

# Readable but strong alphabet: no ambiguous characters (0/O, 1/l/I).
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
_DIGITS = "0123456789"


def generate_temp_password() -> str:
    """16-char upper/lower/digit/symbol password, e.g. ``eDzjGF-619-49QqFP!``."""
    return (
        "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(6))
        + "-"
        + "".join(secrets.choice(_DIGITS) for _ in range(3))
        + "-"
        + "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(6))
        + "!"
    )


class Auth0UserAdmin:
    """Thin Auth0 Management + Authentication API client (urllib, no new deps).

    The management token is a cached client-credentials grant against
    ``/api/v2/``; ``send_reset_email`` uses the Authentication API and needs
    only the client_id. Errors are surfaced as ``ToolError`` with a stable
    code prefix and never embed Auth0 response bodies (they can echo the
    request payload, which for password calls would leak the password).
    """

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        timeout: float = 15.0,
        opener: urllib.request.OpenerDirector | None = None,
        token_acquirer: Any | None = None,
    ) -> None:
        if not (domain and client_id and client_secret):
            raise ValueError("Auth0UserAdmin requires domain, client_id, and client_secret")
        self._domain = domain.strip().rstrip("/")
        self._client_id = client_id
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._token_acquirer = token_acquirer or Auth0ClientCredentials(
            token_endpoint=f"https://{self._domain}/oauth/token",
            client_id=client_id,
            client_secret=client_secret,
            audience=f"https://{self._domain}/api/v2/",
            scope="",
            timeout=timeout,
        )

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        params = urllib.parse.urlencode({"email": email.lower()})
        matches = self._call(
            "GET", f"/api/v2/users-by-email?{params}", authenticated=True
        )
        if not isinstance(matches, list) or not matches:
            return None
        return matches[0]

    def create_user(self, *, email: str, name: str, password: str) -> dict[str, Any]:
        payload = {
            "email": email,
            "name": name,
            "connection": AUTH0_DB_CONNECTION,
            # Admin-initiated creation: mark verified up front or the Auth0
            # force-email-verification Action blocks first login.
            "email_verified": True,
            "verify_email": False,
            "password": password,
        }
        created = self._call("POST", "/api/v2/users", body=payload, authenticated=True)
        if not isinstance(created, dict):
            raise ToolError("auth0_error: unexpected create-user response shape")
        return created

    def ensure_email_verified(self, auth0_id: str) -> None:
        quoted = urllib.parse.quote(auth0_id, safe="")
        self._call(
            "PATCH",
            f"/api/v2/users/{quoted}",
            body={"email_verified": True},
            authenticated=True,
        )

    def set_password(self, auth0_id: str, password: str) -> None:
        quoted = urllib.parse.quote(auth0_id, safe="")
        self._call(
            "PATCH",
            f"/api/v2/users/{quoted}",
            body={"password": password, "connection": AUTH0_DB_CONNECTION},
            authenticated=True,
        )

    def send_reset_email(self, email: str) -> None:
        """Trigger the Auth0 "Change Password" email (Authentication API)."""
        self._call(
            "POST",
            "/dbconnections/change_password",
            body={
                "client_id": self._client_id,
                "email": email,
                "connection": AUTH0_DB_CONNECTION,
            },
            authenticated=False,
        )

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        authenticated: bool,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if authenticated:
            try:
                headers["Authorization"] = f"Bearer {self._token_acquirer.get_token()}"
            except MemoryClientError as exc:
                raise ToolError(f"auth0_unavailable: management token fetch failed ({exc.code})") from exc
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"https://{self._domain}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Surface the status but never the body — Auth0 error bodies can
            # echo the request payload, including passwords. 409 gets a typed
            # error so add_user can fall back to lookup on a create race.
            message = f"auth0_error: {method} {path.split('?')[0]} returned HTTP {exc.code}"
            if exc.code == 409:
                raise Auth0ConflictError(message) from exc
            raise ToolError(message) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ToolError("auth0_unavailable: Auth0 could not be reached") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # /dbconnections/change_password returns a plain-text sentence.
            return {"message": raw.decode("utf-8", errors="replace")}


def central_session_factory_from_url(url: str) -> CentralSessionFactory:
    """Lazy singleton sessionmaker for the central auth database."""
    lock = threading.Lock()
    factory: list[sessionmaker[Session] | None] = [None]

    def open_session() -> Session:
        with lock:
            if factory[0] is None:
                engine = create_engine(
                    url,
                    pool_pre_ping=True,
                    pool_size=2,
                    max_overflow=3,
                    pool_recycle=300,
                )
                factory[0] = sessionmaker(engine, expire_on_commit=False)
        return factory[0]()

    return open_session


def _live_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(
        select(User).where(
            func.lower(User.email) == email.strip().lower(),
            User.deleted_at.is_(None),
        )
    )


def _coerce_role(new_role: str) -> UserRole:
    try:
        return UserRole(new_role.strip().upper())
    except ValueError:
        raise ToolError(
            f"invalid_arguments: role must be one of {', '.join(r.value for r in UserRole)}"
        ) from None


def _auth0_user_or_error(auth0: Auth0UserAdmin, email: str) -> dict[str, Any]:
    auth0_user = auth0.find_user_by_email(email)
    if auth0_user is None or not auth0_user.get("user_id"):
        raise ToolError(
            "not_found: no Auth0 account for this email — onboard the user first with solstice_add_user"
        )
    return auth0_user


def reset_password(
    subject: str,
    tenant_slug: str,
    email: str,
    mode: str = "email",
    *,
    auth0: Auth0UserAdmin,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Send the Auth0 reset email or set (and return) a temp password.

    Gated by ``require_staff_in_tenant`` plus a live target-user row in that
    tenant's DB, so staff on tenant A cannot reset credentials of a user who
    only exists on tenant B.
    """
    if mode not in RESET_MODES:
        raise ToolError(f"invalid_arguments: mode must be one of {', '.join(RESET_MODES)}")
    require_staff_in_tenant(subject, tenant_slug, registry=registry, session_factory=session_factory)
    email = email.strip()
    with tenant_session(tenant_slug, session_factory) as session:
        target = _live_user_by_email(session, email)
    if target is None:
        raise ToolError("not_found: no live user with this email in this tenant")

    auth0_user = _auth0_user_or_error(auth0, email)
    result: dict[str, Any] = {
        "email": target.email,
        "user_id": str(target.id),
        "auth0_id": auth0_user["user_id"],
        "tenant_slug": tenant_slug,
        "mode": mode,
    }
    if mode == "email":
        auth0.send_reset_email(email)
        result["reset_email_sent"] = True
        return result

    temp_password = generate_temp_password()
    auth0.set_password(auth0_user["user_id"], temp_password)
    result["temp_password"] = temp_password
    result["note"] = (
        "Deliver this password to the user through a secure channel and have "
        "them change it after first login. Any previously sent reset-email "
        "link will still work and would overwrite this password if clicked."
    )
    return result


def change_brand_role(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    email: str,
    new_role: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Set a user's role on one brand. SOLSTICE_STAFF on that brand required.

    Only updates a live existing membership — attaching a user to a brand is
    ``add_user``'s job. Setting the already-held role is an idempotent no-op.
    """
    role = _coerce_role(new_role)
    require_brand_role(
        subject, tenant_slug, brand_id,
        min_role=UserRole.SOLSTICE_STAFF,
        registry=registry, session_factory=session_factory,
    )
    with tenant_session(tenant_slug, session_factory) as session:
        target = _live_user_by_email(session, email)
        if target is None:
            raise ToolError("not_found: no live user with this email in this tenant")
        membership = session.scalar(
            select(BrandTeamMember).where(
                BrandTeamMember.brand_id == brand_id,
                BrandTeamMember.user_id == target.id,
                BrandTeamMember.deleted_at.is_(None),
            )
        )
        if membership is None:
            raise ToolError(
                "not_found: user is not a member of this brand — attach them with solstice_add_user"
            )
        previous_role = membership.user_role
        membership.user_role = role.value
        session.commit()
    return {
        "email": target.email,
        "user_id": str(target.id),
        "brand_id": brand_id,
        "tenant_slug": tenant_slug,
        "previous_role": previous_role,
        "new_role": role.value,
    }


def _resolve_company_id(session: Session, brand_id: str | None) -> str:
    if brand_id is not None:
        brand = session.scalar(
            select(Brand).where(Brand.id == brand_id, Brand.deleted_at.is_(None))
        )
        if brand is None:
            raise ToolError("not_found: unknown or deleted brand")
        if not brand.company_id:
            raise ToolError("invalid_state: brand has no company_id")
        return str(brand.company_id)
    company_ids = {
        str(row)
        for row in session.scalars(
            select(Brand.company_id).where(Brand.deleted_at.is_(None), Brand.company_id.is_not(None)).distinct()
        )
    }
    if len(company_ids) != 1:
        raise ToolError(
            "invalid_arguments: tenant has zero or multiple companies — pass brand_id so the company can be resolved"
        )
    return company_ids.pop()


def _upsert_user_row(
    session: Session,
    *,
    email: str,
    name: str,
    auth0_id: str,
    company_id: str,
    preferred_id: str,
) -> str:
    """Insert or update one ``users`` row by email; returns the row id.

    Mirrors the Backend-Server ``ON CONFLICT (email) DO UPDATE`` upsert: an
    existing row keeps its id (downstream FKs depend on it) and gets its
    auth0_id/name/company_id synced; a new row uses ``preferred_id``.
    """
    existing = session.scalar(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    if existing is not None:
        existing.auth0_id = auth0_id
        existing.name = name
        existing.company_id = company_id
        existing.deleted_at = None
        session.commit()
        return str(existing.id)
    session.add(
        User(
            id=preferred_id,
            auth0_id=auth0_id,
            name=name,
            email=email,
            company_id=company_id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    return preferred_id


def add_user(
    subject: str,
    tenant_slug: str,
    email: str,
    name: str,
    brand_id: str | None = None,
    role: str = "MEMBER",
    send_reset_email: bool = True,
    *,
    auth0: Auth0UserAdmin,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    central_session_factory: CentralSessionFactory,
) -> dict[str, Any]:
    """Full onboarding: Auth0 + central auth row + tenant row (+ brand, + email).

    Every step is an upsert, so re-running for a user who exists in a subset of
    the systems (the classic partial-onboarding failure) completes the missing
    pieces instead of erroring.
    """
    email = email.strip()
    name = name.strip()
    if not email or "@" not in email:
        raise ToolError("invalid_arguments: a valid email is required")
    if not name:
        raise ToolError("invalid_arguments: name is required")
    member_role = _coerce_role(role)
    require_staff_in_tenant(subject, tenant_slug, registry=registry, session_factory=session_factory)

    with tenant_session(tenant_slug, session_factory) as session:
        company_id = _resolve_company_id(session, brand_id)

    # 1. Auth0 first: a failure here leaves no partial DB rows.
    created_in_auth0 = False
    existing = auth0.find_user_by_email(email)
    if existing is not None and existing.get("user_id"):
        auth0_id = existing["user_id"]
        auth0.ensure_email_verified(auth0_id)
    else:
        try:
            created = auth0.create_user(email=email, name=name, password=generate_temp_password())
            auth0_id = created.get("user_id")
            created_in_auth0 = True
        except Auth0ConflictError:
            # Create raced with another writer (or eventual-consistent search
            # missed the account): fall back to lookup + re-verify, matching
            # the Backend-Server bulk-create 409 path.
            raced = auth0.find_user_by_email(email)
            auth0_id = raced.get("user_id") if raced else None
            if auth0_id:
                auth0.ensure_email_verified(auth0_id)
        if not auth0_id:
            raise ToolError("auth0_error: Auth0 did not return a user_id on create")

    # 2. Central auth row is canonical: its id is reused for a new tenant row.
    central_session = central_session_factory()
    try:
        central_user_id = _upsert_user_row(
            central_session,
            email=email, name=name, auth0_id=auth0_id,
            company_id=company_id, preferred_id=str(uuid4()),
        )
    finally:
        central_session.close()

    # 3. Tenant row, same id when newly created.
    with tenant_session(tenant_slug, session_factory) as session:
        tenant_user_id = _upsert_user_row(
            session,
            email=email, name=name, auth0_id=auth0_id,
            company_id=company_id, preferred_id=central_user_id,
        )

        # 4. Optional brand membership (revives a soft-deleted row).
        brand_role_set: str | None = None
        if brand_id is not None:
            membership = session.scalar(
                select(BrandTeamMember).where(
                    BrandTeamMember.brand_id == brand_id,
                    BrandTeamMember.user_id == tenant_user_id,
                )
            )
            if membership is not None:
                membership.user_role = member_role.value
                membership.deleted_at = None
            else:
                session.add(
                    BrandTeamMember(
                        brand_id=brand_id,
                        user_id=tenant_user_id,
                        user_role=member_role.value,
                        deleted_at=None,
                    )
                )
            session.commit()
            brand_role_set = member_role.value

    # 5. Optional reset email so the user can set their own password. A send
    # failure is reported in the payload, NOT raised: at this point Auth0 +
    # central + tenant (+ brand) rows are all committed, and raising would
    # make the caller believe onboarding failed and hide the created ids.
    # Degrades to reset_email_sent=false + reset_email_error, which the
    # caller resolves by re-triggering via solstice_reset_password.
    reset_email_sent = False
    reset_email_error: str | None = None
    if send_reset_email:
        try:
            auth0.send_reset_email(email)
            reset_email_sent = True
        except ToolError as exc:
            logger.warning("add_user: onboarding succeeded but reset email failed: %s", exc)
            reset_email_error = str(exc)

    return {
        "email": email,
        "name": name,
        "tenant_slug": tenant_slug,
        "auth0_id": auth0_id,
        "created_in_auth0": created_in_auth0,
        "user_id": tenant_user_id,
        "central_user_id": central_user_id,
        "company_id": company_id,
        "brand_id": brand_id,
        "brand_role": brand_role_set,
        "reset_email_sent": reset_email_sent,
        "reset_email_error": reset_email_error,
    }


__all__ = [
    "AUTH0_DB_CONNECTION",
    "RESET_MODES",
    "Auth0ConflictError",
    "Auth0UserAdmin",
    "CentralSessionFactory",
    "add_user",
    "central_session_factory_from_url",
    "change_brand_role",
    "generate_temp_password",
    "reset_password",
]
