"""Provider-neutral S3-compatible client for the data plane (U2), behind an injectable interface.

File bytes never pass through the API: this interface only creates uploads, signs URLs and asks the
store about what it holds. The one place bytes are read is :meth:`DataPlaneStore.sha256_of`, used by
the verifier (whole-file proof), which streams in bounded chunks and never writes.

L4: the boto3 calls below follow the documented S3 API (CreateMultipartUpload with ChecksumAlgorithm,
UploadPart with ChecksumSHA256, ListParts, CompleteMultipartUpload, AbortMultipartUpload, CopyObject
via the managed ``copy``, presigned GET/PUT, STS AssumeRole with an inline session ``Policy``).
Docs to re-verify before first real use (not fetched when written, so UNVERIFIED):
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html
  https://docs.aws.amazon.com/AmazonS3/latest/API/API_UploadPart.html
  https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html
  https://developers.cloudflare.com/r2/api/s3/api/   (R2 supports a subset; no STS)
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any

from app.core.config import settings

MIN_PART_BYTES = 5 * 1024 * 1024  # S3 minimum for every part but the last
MAX_PARTS = 10_000  # S3 maximum
VERIFY_CHUNK_BYTES = 8 * 1024 * 1024
MAX_RANGE_READ_BYTES = 1024 * 1024  # ceiling for read_range (dedup proof of possession)


class DataPlaneStoreError(Exception):
    """The store refused or failed; carries a short stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class StoredPart:
    part_number: int
    size_bytes: int
    etag: str
    sha256_hex: str | None  # None when the store did not record a checksum


@dataclasses.dataclass(frozen=True)
class ScopedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: dt.datetime
    bucket: str
    prefix: str  # the only key prefix the credentials allow
    read_only: bool


def sha256_hex_to_b64(hex_digest: str) -> str:
    return base64.b64encode(binascii.unhexlify(hex_digest)).decode("ascii")


def sha256_b64_to_hex(b64: str) -> str:
    return binascii.hexlify(base64.b64decode(b64)).decode("ascii")


def check_key(key: str) -> None:
    """Keys are opaque, never paths."""
    if not key or key.startswith("/") or "\x00" in key or ".." in key.split("/"):
        raise DataPlaneStoreError("invalid_object_key")


class DataPlaneStore(ABC):
    @abstractmethod
    def create_multipart(self, key: str, content_type: str) -> str:
        """Start a multipart upload (SHA-256 part checksums required); return the store's upload id."""

    @abstractmethod
    def presign_upload_part(
        self, key: str, upload_id: str, part_number: int, sha256_hex: str, size_bytes: int, ttl_seconds: int
    ) -> tuple[str, dict[str, str]]:
        """Return (url, headers the client MUST send). The checksum header is signed in, so the store
        rejects a body whose SHA-256 differs from ``sha256_hex``."""

    @abstractmethod
    def list_parts(self, key: str, upload_id: str) -> list[StoredPart]:
        ...

    @abstractmethod
    def complete_multipart(self, key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
        """``parts`` is [(part_number, etag)] in ascending order."""

    @abstractmethod
    def abort_multipart(self, key: str, upload_id: str) -> None:
        ...

    @abstractmethod
    def head_size(self, key: str) -> int | None:
        """Object size, or None when it does not exist."""

    @abstractmethod
    def sha256_of(self, key: str) -> str:
        """Stream the object in bounded chunks and return its SHA-256 hex (whole-file proof)."""

    def read_range(self, key: str, start: int, length: int) -> bytes:
        """``length`` bytes starting at ``start`` (a ranged read; never the whole object). Used only by the
        cross-account dedup proof of possession. Not abstract so existing stores keep working; a store that does not
        implement it makes the proof fail closed (the caller then simply uploads the file)."""
        raise DataPlaneStoreError("range_read_not_supported")

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy (multipart for large objects); no bytes reach this process."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Idempotent."""

    @abstractmethod
    def presign_download(self, key: str, ttl_seconds: int, filename: str, content_type: str) -> str:
        ...


class ByoClient(ABC):
    """Operations against a CUSTOMER bucket, using that customer's stored credentials."""

    @abstractmethod
    def presign(
        self, *, bucket: Any, credentials: dict[str, str], key: str, method: str, ttl_seconds: int
    ) -> str:
        ...

    @abstractmethod
    def scoped_credentials(
        self, *, bucket: Any, credentials: dict[str, str], prefix: str, read_only: bool, ttl_seconds: int, session_name: str
    ) -> ScopedCredentials:
        ...


# ---------------------------------------------------------------------------
# boto3 implementation
# ---------------------------------------------------------------------------
def _boto_client(service: str, *, endpoint: str, access_key: str, secret_key: str, region: str, style: str, token: str | None = None):
    import boto3
    from botocore.config import Config

    return boto3.client(
        service,
        endpoint_url=endpoint or None,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        aws_session_token=token,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": style},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    )


class S3DataPlaneStore(DataPlaneStore):
    def __init__(self, *, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str, style: str = "path") -> None:
        self.bucket = bucket
        self._c = _boto_client("s3", endpoint=endpoint, access_key=access_key, secret_key=secret_key, region=region, style=style)

    def create_multipart(self, key: str, content_type: str) -> str:
        check_key(key)
        r = self._c.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type, ChecksumAlgorithm="SHA256"
        )
        return r["UploadId"]

    def presign_upload_part(self, key, upload_id, part_number, sha256_hex, size_bytes, ttl_seconds):
        check_key(key)
        b64 = sha256_hex_to_b64(sha256_hex)
        url = self._c.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
                "ContentLength": size_bytes,
                "ChecksumSHA256": b64,
            },
            ExpiresIn=ttl_seconds,
            HttpMethod="PUT",
        )
        return url, {"x-amz-checksum-sha256": b64, "Content-Length": str(size_bytes)}

    def list_parts(self, key, upload_id):
        check_key(key)
        out: list[StoredPart] = []
        marker = 0
        while True:
            r = self._c.list_parts(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000)
            for p in r.get("Parts", []):
                b64 = p.get("ChecksumSHA256")
                out.append(
                    StoredPart(p["PartNumber"], int(p["Size"]), str(p["ETag"]).strip('"'), sha256_b64_to_hex(b64) if b64 else None)
                )
            if not r.get("IsTruncated"):
                return out
            marker = r["NextPartNumberMarker"]

    def complete_multipart(self, key, upload_id, parts):
        check_key(key)
        self._c.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": n, "ETag": e} for n, e in parts]},
        )

    def abort_multipart(self, key, upload_id):
        check_key(key)
        self._c.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    def head_size(self, key):
        check_key(key)
        from botocore.exceptions import ClientError

        try:
            return int(self._c.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def sha256_of(self, key):
        check_key(key)
        body = self._c.get_object(Bucket=self.bucket, Key=key)["Body"]
        h = hashlib.sha256()
        for chunk in body.iter_chunks(VERIFY_CHUNK_BYTES):
            h.update(chunk)
        return h.hexdigest()

    def read_range(self, key, start, length):
        check_key(key)
        if start < 0 or length <= 0 or length > MAX_RANGE_READ_BYTES:
            raise DataPlaneStoreError("invalid_range")
        r = self._c.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{start + length - 1}")
        data = r["Body"].read(length + 1)  # bounded: never more than one byte past what was asked for
        if len(data) > length:
            raise DataPlaneStoreError("range_too_large")
        return data

    def copy(self, src_key, dst_key):
        check_key(src_key)
        check_key(dst_key)
        self._c.copy({"Bucket": self.bucket, "Key": src_key}, self.bucket, dst_key)

    def delete(self, key):
        check_key(key)
        self._c.delete_object(Bucket=self.bucket, Key=key)

    def presign_download(self, key, ttl_seconds, filename, content_type):
        check_key(key)
        safe = "".join(ch for ch in filename if ch.isprintable() and ch not in '"\\/;\r\n')[:200] or "download"
        return self._c.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{safe}"',
                "ResponseContentType": "application/octet-stream",  # never let a browser render stored bytes
            },
            ExpiresIn=ttl_seconds,
            HttpMethod="GET",
        )


class S3ByoClient(ByoClient):
    def presign(self, *, bucket, credentials, key, method, ttl_seconds):
        c = _boto_client(
            "s3",
            endpoint=bucket.endpoint,
            access_key=credentials["access_key_id"],
            secret_key=credentials["secret_access_key"],
            region=bucket.region,
            style="path" if bucket.endpoint else "virtual",
        )
        op = {"GET": "get_object", "PUT": "put_object"}[method]
        return c.generate_presigned_url(op, Params={"Bucket": bucket.bucket, "Key": key}, ExpiresIn=ttl_seconds, HttpMethod=method)

    def scoped_credentials(self, *, bucket, credentials, prefix, read_only, ttl_seconds, session_name):
        if not bucket.role_arn:
            raise DataPlaneStoreError("byo_sts_not_configured")
        sts = _boto_client(
            "sts",
            endpoint="",
            access_key=credentials["access_key_id"],
            secret_key=credentials["secret_access_key"],
            region=bucket.region,
            style="path",
        )
        actions = ["s3:GetObject"] if read_only else ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": actions, "Resource": f"arn:aws:s3:::{bucket.bucket}/{prefix}*"},
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": f"arn:aws:s3:::{bucket.bucket}",
                    "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}},
                },
            ],
        }
        r = sts.assume_role(
            RoleArn=bucket.role_arn,
            RoleSessionName=session_name[:64],
            Policy=json.dumps(policy),
            DurationSeconds=max(900, ttl_seconds),  # STS minimum is 900 s; the grant's own expiry is the shorter one
        )
        c = r["Credentials"]
        exp = min(c["Expiration"], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_seconds))
        return ScopedCredentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"], exp, bucket.bucket, prefix, read_only)


# ---------------------------------------------------------------------------
# Injection point
# ---------------------------------------------------------------------------
_store: DataPlaneStore | None = None
_byo: ByoClient | None = None


def get_data_store() -> DataPlaneStore:
    """The configured store. Fails closed (no local-disk fallback: presigned multipart needs a real store)."""
    global _store
    if _store is None:
        if not settings.data_plane_configured:
            raise DataPlaneStoreError("data_plane_not_configured")
        _store = S3DataPlaneStore(
            endpoint=settings.DATA_PLANE_ENDPOINT,
            bucket=settings.DATA_PLANE_BUCKET,
            access_key=settings.DATA_PLANE_ACCESS_KEY,
            secret_key=settings.DATA_PLANE_SECRET_KEY_ID,
            region=settings.DATA_PLANE_REGION,
            style=settings.DATA_PLANE_ADDRESSING_STYLE,
        )
    return _store


def get_byo_client() -> ByoClient:
    global _byo
    if _byo is None:
        _byo = S3ByoClient()
    return _byo


def set_data_store(store: DataPlaneStore | None) -> None:
    """Tests inject a fake here (and pass None to reset)."""
    global _store
    _store = store


def set_byo_client(client: ByoClient | None) -> None:
    global _byo
    _byo = client
