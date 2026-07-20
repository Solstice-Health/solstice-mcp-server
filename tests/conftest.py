from __future__ import annotations

import base64
import json
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from solstice_mcp.app import build_mcp_app
from solstice_mcp.auth import JWKSCache
from solstice_mcp.brands import Brand, BrandTeamMember
from solstice_mcp.operations import CgOperation, CgOperationMessage, Project
from solstice_mcp.settings import Settings
from solstice_mcp.tenants import Base, TenantMembershipCache, TenantRegistry, User

TEST_ISSUER = "https://test.auth0.local/"
TEST_RESOURCE = "https://mcp.test.local/mcp"
TEST_KID = "test-key"
SHARED_SUB = "auth0|shared"
OTHER_SUB = "auth0|other"
DELETED_SUB = "auth0|deleted"
STAFF_SUB = "auth0|staff"

# Fixed UUIDs for cross-test consistency.
USER_A_SHARED = "00000000-0000-0000-0000-000000000001"
USER_A_OTHER = "00000000-0000-0000-0000-000000000002"
USER_B_SHARED = "00000000-0000-0000-0000-000000000004"
USER_A_STAFF = "00000000-0000-0000-0000-000000000010"

BRAND_A1 = "00000000-0000-0000-0000-000000000101"
BRAND_A2 = "00000000-0000-0000-0000-000000000102"
BRAND_A3 = "00000000-0000-0000-0000-000000000103"
BRAND_A4 = "00000000-0000-0000-0000-000000000104"  # membership soft-deleted
BRAND_A5 = "00000000-0000-0000-0000-000000000105"  # brand itself soft-deleted
BRAND_B1 = "00000000-0000-0000-0000-000000000201"

PROJECT_P1 = "00000000-0000-0000-0000-000000000301"
PROJECT_P2 = "00000000-0000-0000-0000-000000000302"
OP_A1 = "00000000-0000-0000-0000-000000000401"
OP_A2 = "00000000-0000-0000-0000-000000000402"
OP_A3 = "00000000-0000-0000-0000-000000000403"


def _b64(value: int) -> str:
    size = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


@pytest.fixture(scope="session")
def signing_material() -> tuple[bytes, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": TEST_KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }
    return private_pem, jwks


@pytest.fixture
def mint_token(signing_material: tuple[bytes, dict[str, Any]]) -> Callable[..., str]:
    private_pem, _jwks = signing_material

    def mint(
        *,
        sub: str = SHARED_SUB,
        aud: str = TEST_RESOURCE,
        scope: str = "mcp:connect",
        exp_delta: int = 300,
        email: str | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "aud": aud,
            "sub": sub,
            "scope": scope,
            "azp": "cursor-test-client",
            "iat": now,
            "exp": now + exp_delta,
        }
        if email is not None:
            payload["email"] = email
        return jwt.encode(
            payload,
            private_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )

    return mint


@dataclass
class AppHarness:
    client: TestClient
    registry: TenantRegistry
    session_factory: Callable[[str], Session]
    calls: Counter[str]


@pytest.fixture
def app_harness(tmp_path: Path, signing_material: tuple[bytes, dict[str, Any]]) -> Iterator[AppHarness]:
    tenant_file = tmp_path / "tenants.json"
    tenant_file.write_text(
        json.dumps(
            {
                "tenant_a": {"db_name": "tenant_a", "env": "development"},
                "tenant_b": {"db_name": "tenant_b", "env": "development"},
                "tenant_prod": {"db_name": "tenant_prod", "env": "production"},
            }
        )
    )

    sibling_file = tmp_path / "sibling_mcps.json"
    sibling_file.write_text(
        json.dumps(
            {
                "_comment": "Test sibling MCP directory.",
                "linear": {
                    "url": "https://mcp.linear.app/mcp",
                    "auth_type": "oauth",
                    "scopes": ["mcp:linear"],
                    "setup_instructions": "OAuth via Linear in Cursor MCP settings.",
                },
                "slack": {
                    "url": "",
                    "auth_type": "bot_token",
                    "scopes": [],
                    "setup_instructions": (
                        "Requires a Slack bot token configured in Cursor MCP settings; "
                        "no public MCP endpoint."
                    ),
                },
            }
        )
    )

    rows = {
        "tenant_a": [
            User(
                id="00000000-0000-0000-0000-000000000001",
                auth0_id=SHARED_SUB,
                name="Alice",
                email="alice@a.test",
                deleted_at=None,
            ),
            User(
                id="00000000-0000-0000-0000-000000000002",
                auth0_id=OTHER_SUB,
                name="Other",
                email="other@a.test",
                deleted_at=None,
            ),
            User(
                id="00000000-0000-0000-0000-000000000003",
                auth0_id=DELETED_SUB,
                name="Deleted",
                email="deleted@a.test",
                deleted_at=datetime.now(UTC),
            ),
            User(
                id=USER_A_STAFF,
                auth0_id=STAFF_SUB,
                name="Staff",
                email="staff@a.test",
                deleted_at=None,
            ),
        ],
        "tenant_b": [
            User(
                id="00000000-0000-0000-0000-000000000004",
                auth0_id=SHARED_SUB,
                name="Alice B",
                email="alice@b.test",
                deleted_at=None,
            ),
        ],
        "tenant_prod": [
            User(
                id="00000000-0000-0000-0000-000000000005",
                auth0_id=SHARED_SUB,
                name="Alice P",
                email="alice@p.test",
                deleted_at=None,
            ),
        ],
    }

    # Per-brand RBAC seed. SHARED holds different roles on different brands in
    # tenant_a (ADMIN on a1, MEMBER on a2); OTHER is SOLSTICE_STAFF on a2.
    # STAFF_SUB is SOLSTICE_STAFF on a1 (used to prove staff sees draft msgs).
    now = datetime.now(UTC)
    brand_rows = {
        "tenant_a": [
            Brand(id=BRAND_A1, name="Brand A1", deleted_at=None),
            Brand(id=BRAND_A2, name="Brand A2", deleted_at=None),
            Brand(id=BRAND_A3, name="Brand A3", deleted_at=None),
            Brand(id=BRAND_A4, name="Brand A4", deleted_at=None),
            Brand(id=BRAND_A5, name="Brand A5", deleted_at=now),
            BrandTeamMember(brand_id=BRAND_A1, user_id=USER_A_SHARED, user_role="ADMIN", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A2, user_id=USER_A_SHARED, user_role="MEMBER", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A4, user_id=USER_A_SHARED, user_role="ADMIN", deleted_at=now),
            BrandTeamMember(brand_id=BRAND_A5, user_id=USER_A_SHARED, user_role="ADMIN", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A1, user_id=USER_A_OTHER, user_role="MEMBER", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A2, user_id=USER_A_OTHER, user_role="SOLSTICE_STAFF", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A3, user_id=USER_A_OTHER, user_role="MEMBER", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_A1, user_id=USER_A_STAFF, user_role="SOLSTICE_STAFF", deleted_at=None),
            # Projects + operations + messages for tenant_a.
            Project(
                id=PROJECT_P1,
                name="Project P1",
                brand_id=BRAND_A1,
                dir_map={
                    "items": [
                        {"name": "op_a1.html", "operation_id": OP_A1},
                        {"name": "Folder", "items": [{"name": "nested.html", "operation_id": OP_A2}]},
                    ]
                },
                deleted_at=None,
            ),
            Project(id=PROJECT_P2, name="Project P2", brand_id=BRAND_A1, dir_map={"items": []}, deleted_at=None),
            CgOperation(
                id=OP_A1, brand_id=BRAND_A1, project_id=PROJECT_P1, status="editing",
                chat_title="Op A1", file_name="op_a1.html", version_number=2,
                created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC), updated_at=None, deleted_at=None,
            ),
            CgOperation(
                id=OP_A2, brand_id=BRAND_A1, project_id=None, status="completed",
                chat_title="Op A2", file_name=None, version_number=1,
                created_at=datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC), updated_at=None, deleted_at=None,
            ),
            CgOperation(
                id=OP_A3, brand_id=BRAND_A3, project_id=None, status="editing",
                chat_title="Op A3", file_name=None, version_number=1,
                created_at=datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC), updated_at=None, deleted_at=None,
            ),
            # op_a1 chat: text, html final, html draft, blueprint.
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000501", operation_id=OP_A1,
                message_id="m1", author_id=USER_A_SHARED, type="text", content="hello",
                version_number=None, intent=None, position=0,
                created_at=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), deleted_at=None,
            ),
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000502", operation_id=OP_A1,
                message_id="m2", author_id=None, type="html",
                content=f"cg_operation_msg_html/{OP_A1}/v1/m2/v1.html",
                version_number=1, intent="final", position=1,
                created_at=datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC), deleted_at=None,
            ),
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000503", operation_id=OP_A1,
                message_id="m3", author_id=None, type="html",
                content=f"cg_operation_msg_html/{OP_A1}/v2/m3/v2.html",
                version_number=2, intent="draft", position=2,
                created_at=datetime(2026, 1, 1, 12, 0, 3, tzinfo=UTC), deleted_at=None,
            ),
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000504", operation_id=OP_A1,
                message_id="m4", author_id=None, type="blueprint", content="{}",
                version_number=None, intent=None, position=3,
                created_at=datetime(2026, 1, 1, 12, 0, 4, tzinfo=UTC), deleted_at=None,
            ),
            # op_a2 chat: one text message.
            CgOperationMessage(
                id="00000000-0000-0000-0000-000000000510", operation_id=OP_A2,
                message_id="m10", author_id=USER_A_SHARED, type="text", content="hi from op a2",
                version_number=None, intent=None, position=0,
                created_at=datetime(2026, 1, 2, 12, 0, 1, tzinfo=UTC), deleted_at=None,
            ),
        ],
        "tenant_b": [
            Brand(id=BRAND_B1, name="Brand B1", deleted_at=None),
            BrandTeamMember(brand_id=BRAND_B1, user_id=USER_B_SHARED, user_role="ADMIN", deleted_at=None),
        ],
        "tenant_prod": [],
    }
    factories: dict[str, sessionmaker[Session]] = {}
    for slug, users in rows.items():
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as session:
            session.add_all(users)
            session.add_all(brand_rows[slug])
            session.commit()
        factories[slug] = factory

    calls: Counter[str] = Counter()

    def open_session(slug: str) -> Session:
        calls[slug] += 1
        return factories[slug]()

    settings = Settings(
        ENV="development",
        AUTH0_DOMAIN="test.auth0.local",
        MCP_RESOURCE_URL=TEST_RESOURCE,
        TENANT_CONFIG_PATH=str(tenant_file),
        SIBLING_MCP_CONFIG_PATH=str(sibling_file),
    )
    registry = TenantRegistry()
    _private, jwks = signing_material
    mcp = build_mcp_app(
        runtime_settings=settings,
        registry=registry,
        session_factory=open_session,
        cache=TenantMembershipCache(ttl_seconds=60, max_entries=8),
        jwks_cache=JWKSCache(f"{TEST_ISSUER}.well-known/jwks.json", initial=jwks),
    )
    with TestClient(mcp.streamable_http_app(), base_url="https://mcp.test.local") as client:
        yield AppHarness(client, registry, open_session, calls)
