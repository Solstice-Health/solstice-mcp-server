"""Per-brand RBAC for the Solstice MCP server.

Role determination is the trust root for every future brand-bound tool, so it
is deliberately constructed to be impossible to bypass via tool arguments:

- The only input that grants authority is the verified OAuth subject returned
  by ``require_subject()`` in ``app.py``. Everything here is derived from it.
- ``brand_id`` / ``tenant_slug`` arguments only *select* a resource; they are
  re-validated against the subject's own ``brand_team_members`` row before any
  role decision is made.
- No tool accepts ``user_id`` or ``role`` as an authorization input. There is
  no code path in this module where a caller-supplied value becomes the role.

The role model mirrors ``Backend-Server``:
``brand_team_members(user_id, brand_id, user_role)`` with
``UserRole`` ∈ {``MEMBER``, ``ADMIN``, ``SOLSTICE_STAFF``}. ``ADMIN`` and
``MEMBER`` are normal tenant users; ``SOLSTICE_STAFF`` is the brand-scoped
super user. A user may hold different roles on different brands within the
same tenant.
"""

from __future__ import annotations

import enum
import logging
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import JSON, DateTime, String, Uuid, select
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.tenants import (
    Base,
    SessionFactory,
    TenantRegistry,
    resolve_tenant_identity,
    tenant_session,
)

logger = logging.getLogger(__name__)

# Per-request brand context. Downstream code reads role from here, never from a
# handler argument. Mirrors ``tenants.current_tenant``.
current_brand_role: ContextVar[BrandIdentity | None] = ContextVar(
    "current_brand_role", default=None
)


class UserRole(enum.StrEnum):
    """Per-brand roles. Order matters: ``_ROLE_RANK`` defines the privilege lattice."""

    MEMBER = "MEMBER"
    ADMIN = "ADMIN"
    SOLSTICE_STAFF = "SOLSTICE_STAFF"


# Privilege ordering used by ``require_brand_role(min_role=...)``.
# SOLSTICE_STAFF > ADMIN > MEMBER. A staff user satisfies any lower gate.
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.MEMBER: 0,
    UserRole.ADMIN: 1,
    UserRole.SOLSTICE_STAFF: 2,
}


def role_satisfies(held: UserRole, min_role: UserRole) -> bool:
    return _ROLE_RANK[held] >= _ROLE_RANK[min_role]


class Brand(Base):
    """Mapping of the tenant ``brands`` table.

    Read paths use identity columns; brand-context tools also read the admin
    JSON fields (``design_bible``, ``isi``, ``drug_info``).
    """

    __tablename__ = "brands"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String)
    design_bible: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    isi: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    drug_info: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BrandTeamMember(Base):
    """Read-only mapping of the tenant ``brand_team_members`` table.

    Composite PK (brand_id, user_id). ``user_role`` is stored as a postgres enum
    in production; we map it as ``String`` for read-only access that works
    uniformly across SQLite (tests) and Postgres (prod), and convert to
    ``UserRole`` in Python.
    """

    __tablename__ = "brand_team_members"

    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    user_role: Mapped[str] = mapped_column(String)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@dataclass(frozen=True)
class BrandMembership:
    """A user's access to a single brand, with their per-brand role."""

    brand_id: str
    brand_name: str
    role: UserRole

    def as_dict(self) -> dict[str, str]:
        return {**asdict(self), "role": self.role.value}


@dataclass(frozen=True)
class BrandIdentity:
    """Resolved role for one (subject, tenant, brand) triple."""

    user_id: str
    brand_id: str
    brand_name: str
    role: UserRole
    tenant_slug: str
    env: str

    def as_dict(self) -> dict[str, str]:
        return {**asdict(self), "role": self.role.value}


def _coerce_role(value: str) -> UserRole | None:
    try:
        return UserRole(value)
    except ValueError:
        logger.warning("Unknown user_role value in brand_team_members", extra={"value": value})
        return None


def list_brands_for_user(
    subject: str,
    tenant_slug: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> list[BrandMembership]:
    """Return the brands the subject can access in one tenant, with per-brand role.

    Access is exactly the set of live ``brand_team_members`` rows for the
    subject's user_id in that tenant's database. A user who is MEMBER on brand
    A and SOLSTICE_STAFF on brand B (same tenant) gets both, each with its own
    role. Soft-deleted memberships and soft-deleted brands are excluded.
    """
    identity = resolve_tenant_identity(
        subject, tenant_slug, registry=registry, session_factory=session_factory
    )
    if identity is None:
        return []

    with tenant_session(tenant_slug, session_factory) as session:
        rows = session.execute(
            select(BrandTeamMember.brand_id, Brand.name, BrandTeamMember.user_role)
            .join(Brand, Brand.id == BrandTeamMember.brand_id)
            .where(
                BrandTeamMember.user_id == identity.user_id,
                BrandTeamMember.deleted_at.is_(None),
                Brand.deleted_at.is_(None),
            )
        ).all()

    memberships: list[BrandMembership] = []
    for brand_id, brand_name, role_value in rows:
        role = _coerce_role(role_value)
        if role is None:
            continue
        memberships.append(BrandMembership(str(brand_id), brand_name, role))
    return memberships


def resolve_brand_role(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> BrandIdentity | None:
    """Resolve the subject's role on one brand.

    Chains the existing tenant-membership gate (``resolve_tenant_identity``)
    with a ``brand_team_members`` lookup. Returns ``None`` if the subject is
    not a tenant member, not on the brand, or the brand/membership is
    soft-deleted. Never raises on a missing row — callers raise via
    ``require_brand_role``.
    """
    identity = resolve_tenant_identity(
        subject, tenant_slug, registry=registry, session_factory=session_factory
    )
    if identity is None:
        return None

    with tenant_session(tenant_slug, session_factory) as session:
        row = session.execute(
            select(BrandTeamMember.user_role, Brand.name)
            .join(Brand, Brand.id == BrandTeamMember.brand_id)
            .where(
                BrandTeamMember.user_id == identity.user_id,
                BrandTeamMember.brand_id == brand_id,
                BrandTeamMember.deleted_at.is_(None),
                Brand.deleted_at.is_(None),
            )
        ).first()

    if row is None:
        return None
    role = _coerce_role(row.user_role)
    if role is None:
        return None
    return BrandIdentity(
        user_id=identity.user_id,
        brand_id=str(brand_id),
        brand_name=row.name,
        role=role,
        tenant_slug=tenant_slug,
        env=identity.env,
    )


def require_brand_role(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    min_role: UserRole,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> BrandIdentity:
    """Single authorization gate for every brand-bound tool.

    Raises ``ToolError`` (a protocol-level tool error, not a dict the agent can
    misread or retry around) when the subject is not a tenant member, not on the
    brand, or holds a role below ``min_role``. On success, sets
    ``current_brand_role`` so downstream logic reads role from context rather
    than from handler arguments.

    This is the only function brand-bound tools should call for authorization.
    Keeping it centralized is what makes role determination bypass-proof
    against future tool additions.
    """
    identity = resolve_brand_role(
        subject,
        tenant_slug,
        brand_id,
        registry=registry,
        session_factory=session_factory,
    )
    if identity is None:
        raise ToolError(
            "not_authorized: unknown tenant, unknown brand, or subject is not a member of this brand"
        )
    if not role_satisfies(identity.role, min_role):
        raise ToolError(
            f"not_authorized: role {identity.role.value} does not satisfy required role {min_role.value}"
        )
    current_brand_role.set(identity)
    return identity


def reset_brand_role() -> None:
    """Clear ``current_brand_role``. Call in a ``finally`` after the handler returns."""
    current_brand_role.set(None)


__all__ = [
    "Brand",
    "BrandIdentity",
    "BrandMembership",
    "BrandTeamMember",
    "UserRole",
    "current_brand_role",
    "list_brands_for_user",
    "require_brand_role",
    "reset_brand_role",
    "resolve_brand_role",
    "role_satisfies",
]
