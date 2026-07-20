"""Tenant S3 read access for HTML document bodies.

Read-only: presign a GET URL (default, cheap, no body transfer) or download the
object inline (only when the caller explicitly asks). The MCP server never
writes or deletes; only ``s3:GetObject`` permission is required on the tenant
buckets.

The ``S3Reader`` protocol is the seam used for dependency injection —
``build_mcp_app`` accepts any object implementing ``presign`` / ``download`` so
tests can substitute a fake without touching boto3.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class S3Error(Exception):
    """Base for S3 read failures surfaced to the tool layer."""


class S3ObjectMissing(S3Error):
    """The object does not exist (NoSuchKey / 404)."""


class S3ObjectTooLarge(S3Error):
    """The object exceeds the inline-download size cap."""


class S3Reader(Protocol):
    """Read-only S3 surface used by the operation-html tool."""

    def presign(self, bucket: str, key: str, expires_in: int) -> str: ...

    def download(self, bucket: str, key: str, max_bytes: int) -> bytes: ...


class TenantS3:
    """boto3-backed ``S3Reader``. Presigns GET URLs and downloads objects.

    A single client is shared across tenants; the bucket is selected per call
    from the tenant config (``TenantConfig.s3_bucket``). Mirrors the
    Backend-Server ``S3StorageAdapter`` (s3v4 signatures, same region source).
    """

    def __init__(self, *, region_name: str = "us-east-1") -> None:
        import boto3
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            config=Config(
                s3={"signature_version": "s3v4"},
                retries={"max_attempts": 3},
                connect_timeout=5,
                read_timeout=10,
            ),
            region_name=region_name,
        )

    def presign(self, bucket: str, key: str, expires_in: int) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def download(self, bucket: str, key: str, max_bytes: int) -> bytes:
        from botocore.exceptions import ClientError

        try:
            head = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404"}:
                raise S3ObjectMissing(key) from exc
            raise S3Error(f"head_object failed for {key!r}: {exc}") from exc

        size = int(head.get("ContentLength") or 0)
        if size > max_bytes:
            raise S3ObjectTooLarge(f"object {key!r} is {size} bytes (cap {max_bytes})")

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404"}:
                raise S3ObjectMissing(key) from exc
            raise S3Error(f"get_object failed for {key!r}: {exc}") from exc


__all__ = ["S3Error", "S3ObjectMissing", "S3ObjectTooLarge", "S3Reader", "TenantS3"]
