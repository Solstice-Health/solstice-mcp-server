"""Read-only brand context: rules, design assets, and clinical claims.

Mirrors Backend-Server tables without importing the Backend application:

- ``guidelines_and_rules`` — brand guidelines (name/description/steps)
- ``brands.design_bible`` / ``isi`` / ``drug_info`` — admin brand fields
- ``design_library`` — logos, heroes, and other design assets (S3 keys)
- ``clinical_claims`` — clinical claim library rows for a brand

Authorization: every function routes through ``require_brand_role`` (MEMBER).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text, Uuid, func, select
from sqlalchemy.orm import Mapped, mapped_column

from solstice_mcp.brands import Brand, UserRole, require_brand_role, reset_brand_role
from solstice_mcp.storage import S3Reader
from solstice_mcp.tenants import Base, SessionFactory, TenantRegistry, tenant_session

_DEFAULT_CLAIMS_LIMIT = 100
_MAX_CLAIMS_LIMIT = 500


class GuidelineAndRule(Base):
    """Read-only mapping of ``guidelines_and_rules``."""

    __tablename__ = "guidelines_and_rules"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    implementation_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DesignLibrary(Base):
    """Read-only mapping of ``design_library``."""

    __tablename__ = "design_library"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    image_type: Mapped[str | None] = mapped_column(String, nullable=True)
    image_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    image_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    thumbnail_s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    public_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_placeholder: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClinicalClaim(Base):
    """Read-only mapping of ``clinical_claims``."""

    __tablename__ = "clinical_claims"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    brand_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    claim_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_type: Mapped[str | None] = mapped_column(String, nullable=True)
    is_extracted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_author: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_name: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_year: Mapped[int | None] = mapped_column(nullable=True)
    counted_page_number: Mapped[int | None] = mapped_column(nullable=True)
    group_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def get_brand_rules(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
) -> dict[str, Any]:
    """Return guidelines_and_rules plus brand admin fields. Gated at MEMBER."""
    try:
        require_brand_role(
            subject,
            tenant_slug,
            brand_id,
            min_role=UserRole.MEMBER,
            registry=registry,
            session_factory=session_factory,
        )
        with tenant_session(tenant_slug, session_factory) as session:
            brand = session.scalar(
                select(Brand).where(Brand.id == brand_id, Brand.deleted_at.is_(None))
            )
            rules = session.scalars(
                select(GuidelineAndRule)
                .where(
                    GuidelineAndRule.brand_id == brand_id,
                    GuidelineAndRule.deleted_at.is_(None),
                )
                .order_by(GuidelineAndRule.name)
            ).all()
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "design_bible": brand.design_bible if brand is not None else None,
            "isi": brand.isi if brand is not None else None,
            "drug_info": brand.drug_info if brand is not None else None,
            "rules": [
                {
                    "id": rule.id,
                    "name": rule.name,
                    "description": rule.description,
                    "implementation_steps": rule.implementation_steps,
                    "created_at": _iso(rule.created_at),
                }
                for rule in rules
            ],
            "count": len(rules),
        }
    finally:
        reset_brand_role()


def get_brand_design_assets(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    s3: S3Reader,
    presign_expiry: int = 600,
) -> dict[str, Any]:
    """Return design_library rows with presigned GET URLs. Gated at MEMBER."""
    try:
        require_brand_role(
            subject,
            tenant_slug,
            brand_id,
            min_role=UserRole.MEMBER,
            registry=registry,
            session_factory=session_factory,
        )
        tenant_config = registry.get(tenant_slug)
        bucket = tenant_config.s3_bucket if tenant_config is not None else ""
        with tenant_session(tenant_slug, session_factory) as session:
            rows = session.scalars(
                select(DesignLibrary)
                .where(DesignLibrary.brand_id == brand_id)
                .order_by(DesignLibrary.image_file_name)
            ).all()
        assets: list[dict[str, Any]] = []
        for row in rows:
            url = None
            if row.s3_key and bucket:
                url = s3.presign(bucket, row.s3_key, presign_expiry)
            elif row.public_url:
                url = row.public_url
            assets.append(
                {
                    "id": row.id,
                    "image_type": row.image_type,
                    "image_file_name": row.image_file_name,
                    "image_description": row.image_description,
                    "s3_key": row.s3_key,
                    "url": url,
                    "source_url": row.source_url,
                    "public_url": row.public_url,
                    "is_placeholder": bool(row.is_placeholder) if row.is_placeholder is not None else False,
                    "created_at": _iso(row.created_at),
                }
            )
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "assets": assets,
            "count": len(assets),
        }
    finally:
        reset_brand_role()


def get_brand_claims(
    subject: str,
    tenant_slug: str,
    brand_id: str,
    *,
    registry: TenantRegistry,
    session_factory: SessionFactory,
    limit: int = _DEFAULT_CLAIMS_LIMIT,
    extracted_only: bool = True,
) -> dict[str, Any]:
    """Return clinical claims for a brand. Gated at MEMBER.

    Defaults to extracted claims (the set useful for content conversion) with a
    hard cap so a brand with tens of thousands of rows cannot blow the tool
    response.
    """
    try:
        require_brand_role(
            subject,
            tenant_slug,
            brand_id,
            min_role=UserRole.MEMBER,
            registry=registry,
            session_factory=session_factory,
        )
        capped = max(1, min(limit, _MAX_CLAIMS_LIMIT))
        with tenant_session(tenant_slug, session_factory) as session:
            stmt = select(ClinicalClaim).where(
                ClinicalClaim.brand_id == brand_id,
                ClinicalClaim.deleted_at.is_(None),
                ClinicalClaim.claim_text.is_not(None),
            )
            if extracted_only:
                stmt = stmt.where(ClinicalClaim.is_extracted.is_(True))
            total: int = session.scalar(
                select(func.count()).select_from(stmt.subquery())
            ) or 0
            rows = session.scalars(
                stmt.order_by(ClinicalClaim.id).limit(capped)
            ).all()
        claims = [
            {
                "id": claim.id,
                "claim_text": claim.claim_text,
                "claim_type": claim.claim_type,
                "is_extracted": bool(claim.is_extracted) if claim.is_extracted is not None else False,
                "first_author": claim.first_author,
                "publication_name": claim.publication_name,
                "publication_year": claim.publication_year,
                "counted_page_number": claim.counted_page_number,
                "group_id": claim.group_id,
            }
            for claim in rows
        ]
        return {
            "tenant_slug": tenant_slug,
            "brand_id": brand_id,
            "extracted_only": extracted_only,
            "limit": capped,
            "claims": claims,
            "count": len(claims),
            "total": total,
            "has_more": total > capped,
        }
    finally:
        reset_brand_role()


__all__ = [
    "ClinicalClaim",
    "DesignLibrary",
    "GuidelineAndRule",
    "get_brand_claims",
    "get_brand_design_assets",
    "get_brand_rules",
]
