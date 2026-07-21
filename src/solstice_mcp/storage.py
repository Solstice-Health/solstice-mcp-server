"""Tenant S3 access for HTML/PDF document bodies.

Read side: presign a GET URL (default, cheap, no body transfer) or download the
object inline (only when the caller explicitly asks).

Write side: presign a PUT URL so the client can upload a new document version
directly to tenant S3, then ``head`` to confirm the upload before the commit
insert. The MCP server itself never receives the body; it only signs the URL
and records the resulting key. Presigning a PUT requires the signing role to
hold ``s3:PutObject`` on the bucket/prefix; the actual PUT is performed by the
client holding the presigned URL.

The ``S3Reader`` protocol is the seam used for dependency injection —
``build_mcp_app`` accepts any object implementing the surface so tests can
substitute a fake without touching boto3.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class S3Error(Exception):
    """Base for S3 failures surfaced to the tool layer."""


class S3ObjectMissing(S3Error):
    """The object does not exist (NoSuchKey / 404)."""


class S3ObjectTooLarge(S3Error):
    """The object exceeds the inline-download size cap."""


class S3Reader(Protocol):
    """S3 surface used by the operation tools (read + version-write)."""

    def presign(self, bucket: str, key: str, expires_in: int) -> str: ...

    def download(self, bucket: str, key: str, max_bytes: int) -> bytes: ...

    def presign_put(
        self, bucket: str, key: str, expires_in: int, content_type: str
    ) -> str: ...

    def head(self, bucket: str, key: str) -> int | None:
        """Return object size in bytes, or None if the object is absent."""


class TenantS3:
    """boto3-backed ``S3Reader``. Presigns GET/PUT URLs, downloads, and heads objects.

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

    def presign_put(
        self, bucket: str, key: str, expires_in: int, content_type: str
    ) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )

    def head(self, bucket: str, key: str) -> int | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404"}:
                return None
            raise S3Error(f"head_object failed for {key!r}: {exc}") from exc
        return int(response.get("ContentLength") or 0)

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
