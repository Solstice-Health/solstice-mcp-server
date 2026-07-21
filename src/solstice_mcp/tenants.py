"""Tenant registry, database membership lookup, and request context."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, String, Uuid, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)
current_tenant: ContextVar[str | None] = ContextVar("current_tenant", default=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    """Minimal read-only mapping for the existing tenant users table."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    auth0_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@dataclass(frozen=True)
class TenantConfig:
    slug: str
    db_name: str
    env: str
    s3_bucket: str = ""


class TenantRegistry:
    def __init__(self) -> None:
        self._tenants: dict[str, TenantConfig] = {}

    def load(self, path: str | Path) -> None:
        with Path(path).open(encoding="utf-8") as config_file:
            raw = json.load(config_file)
        self._tenants = {
            slug: TenantConfig(
                slug=slug,
                db_name=value["db_name"],
                env=value.get("env", "production"),
                s3_bucket=value.get("s3_bucket", ""),
            )
            for slug, value in raw.items()
            if not slug.startswith("_")
        }
        logger.info("Loaded %d tenant configs", len(self._tenants))

    @property
    def slugs(self) -> list[str]:
        return list(self._tenants)

    def get(self, slug: str) -> TenantConfig | None:
        if not slug:
            return None
        cfg = self._tenants.get(slug)
        if cfg is not None:
            return cfg
        # Solstice asset URLs use hyphens in the subdomain (e.g.
        # sanofi-sandbox.solsticehealth.co) while tenant slugs use underscores
        # (sanofi_sandbox). Accept either form so deep-link parsing tolerates a
        # hyphen/underscore mismatch. The tenant registry mirrors
        # Backend-Server/config/tenants.json (single source of truth).
        for swapped in (slug.replace("-", "_"), slug.replace("_", "-")):
            if swapped != slug:
                found = self._tenants.get(swapped)
                if found is not None:
                    return found
        return None


class TenantDatabaseFactory:
    """Create sessions for configured tenant databases, across multiple environments.

    Each tenant's config declares an ``env`` (``development`` or ``production``);
    the factory picks the URL template matching that env so a single MCP task
    can reach tenant databases in any environment it is allowed to read.
    """

    def __init__(self, registry: TenantRegistry, url_templates: Mapping[str, str]) -> None:
        if not url_templates:
            raise ValueError("At least one database URL template is required")
        normalized = {
            env.strip().lower(): template for env, template in url_templates.items()
        }
        for env, template in normalized.items():
            if "{db_name}" not in template:
                raise ValueError(f"Database URL template for env {env!r} must contain {{db_name}}")
        self.registry = registry
        self.url_templates = normalized
        self._sessions: dict[str, sessionmaker[Session]] = {}
        self._lock = threading.Lock()

    def __call__(self, slug: str) -> Session:
        config = self.registry.get(slug)
        if config is None:
            raise ValueError(f"Unknown tenant slug: {slug!r}")
        env = config.env.strip().lower()
        template = self.url_templates.get(env)
        if template is None:
            raise ValueError(f"No database URL template registered for env {env!r} (tenant {slug!r})")
        with self._lock:
            factory = self._sessions.get(slug)
            if factory is None:
                engine = create_engine(template.format(db_name=config.db_name), pool_pre_ping=True)
                factory = sessionmaker(engine, expire_on_commit=False)
                self._sessions[slug] = factory
        return factory()


@dataclass(frozen=True)
class TenantMembership:
    slug: str
    env: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TenantIdentity:
    user_id: str
    name: str
    email: str
    tenant_slug: str
    env: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class TenantMembershipCache:
    """Bounded short-TTL cache of subject memberships."""

    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 1024) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("Cache TTL and size must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, list[TenantMembership]]] = {}
        self._lock = threading.Lock()

    def get(self, subject: str) -> list[TenantMembership] | None:
        with self._lock:
            entry = self._store.get(subject)
            if entry is None:
                return None
            expires_at, memberships = entry
            if time.monotonic() >= expires_at:
                self._store.pop(subject, None)
                return None
            return list(memberships)

    def set(self, subject: str, memberships: list[TenantMembership]) -> None:
        with self._lock:
            if subject not in self._store and len(self._store) >= self.max_entries:
                self._store.pop(next(iter(self._store)))
            self._store[subject] = (time.monotonic() + self.ttl_seconds, list(memberships))


SessionFactory = Callable[[str], Session]


@contextmanager
def tenant_session(slug: str, session_factory: SessionFactory) -> Iterator[Session]:
    current_tenant.set(slug)
    session: Session | None = None
    try:
        session = session_factory(slug)
        yield session
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            current_tenant.set(None)


def _live_user(session: Session, subject: str) -> User | None:
    return session.scalar(select(User).where(User.auth0_id == subject, User.deleted_at.is_(None)))


def discover_tenants_for_sub(
    subject: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    cache: TenantMembershipCache,
    slugs: Iterable[str] | None = None,
) -> list[TenantMembership]:
    """Return configured tenants containing a live user with this Auth0 subject.

    Cross-environment discovery: every configured tenant is probed regardless of
    the MCP task's own environment. Access is gated entirely by the presence of a
    live row in that tenant's ``users`` table.

    ponytail: each cache miss scans one database per configured tenant, across
    dev and prod RDS. Replace with a central membership directory if tenant count
    makes that costly.
    """
    cached = cache.get(subject)
    if cached is not None:
        return cached

    memberships: list[TenantMembership] = []
    for slug in slugs if slugs is not None else registry.slugs:
        config = registry.get(slug)
        if config is None:
            continue
        try:
            with tenant_session(slug, session_factory) as session:
                if _live_user(session, subject) is not None:
                    memberships.append(TenantMembership(slug, config.env))
        except Exception as exc:
            # An unreachable tenant is omitted; other tenant memberships remain usable.
            logger.warning("Skipping unreachable tenant database", extra={"tenant_slug": slug, "error": str(exc)})

    cache.set(subject, memberships)
    return memberships


def resolve_tenant_identity(
    subject: str,
    tenant_slug: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> TenantIdentity | None:
    config = registry.get(tenant_slug)
    if config is None:
        return None

    with tenant_session(tenant_slug, session_factory) as session:
        user = _live_user(session, subject)
        if user is None:
            return None
        return TenantIdentity(str(user.id), user.name or "", user.email or "", tenant_slug, config.env)
