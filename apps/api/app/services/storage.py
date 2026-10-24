"""Object storage + signed HMAC grants for artifact upload/download (Phase 4).

The API process NEVER executes customer code — it only stores and serves bytes.

Two backends:
* :class:`LocalFsStore` — dev/test default, writes under ``OBJECT_STORAGE_LOCAL_DIR``.
* :class:`S3Store` — boto3-backed, used when ``OBJECT_STORAGE_ENDPOINT`` is
  configured (MinIO locally, S3-compatible hosted).

Signed grants are short-lived HMAC tokens scoped to a resource id + expiry, keyed
on ``AUTH_SECRET``. Upload grants are scoped to a ``job_id``; download grants to an
``artifact_id``. A grant is a capability: holding a valid, unexpired token is the
authorization (owners are the only ones who can obtain one — see the routers).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
from abc import ABC, abstractmethod

from app.core.config import settings
from app.db.base import ensure_aware, utcnow

# ---------------------------------------------------------------------------
# Store backends
# ---------------------------------------------------------------------------
class ObjectStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        ...

    @abstractmethod
    def get(self, key: str) -> bytes:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...


class LocalFsStore(ObjectStore):
    """Filesystem-backed store (dev/test). Keys map to files under ``root``."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        try:
            os.makedirs(self.root, exist_ok=True)
            test_path = os.path.join(self.root, ".perm_test")
            with open(test_path, "wb") as f:
                f.write(b"")
            os.remove(test_path)
        except (PermissionError, OSError):
            self.root = os.path.abspath("/tmp/computecoin_artifacts")
            os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        # Prevent path traversal: the resolved path must stay under root.
        safe = os.path.normpath(os.path.join(self.root, key))
        if not safe.startswith(self.root + os.sep) and safe != self.root:
            raise ValueError("invalid_object_key")
        return safe

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        try:
            path = self._path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        except (PermissionError, OSError):
            if not self.root.startswith("/tmp"):
                self.root = os.path.abspath("/tmp/computecoin_artifacts")
                path = self._path(key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            else:
                raise

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as fh:
            return fh.read()

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class S3Store(ObjectStore):
    """S3-compatible backend (MinIO locally, Supabase Storage / S3 hosted).

    Selected when ``OBJECT_STORAGE_ENDPOINT`` is set. The bucket is created on
    first use if it does not exist, so a fresh MinIO works without a manual step.
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.endpoint = endpoint
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except ClientError as create_exc:
                # Another worker may have created it between our check and now.
                if create_exc.response.get("Error", {}).get("Code") not in (
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                ):
                    raise
        self._bucket_ready = True

    @staticmethod
    def _check_key(key: str) -> None:
        # Same traversal rule as LocalFsStore: keys are opaque, never paths.
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid_object_key")

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._check_key(key)
        self._ensure_bucket()
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        self._check_key(key)
        self._ensure_bucket()
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                # Match LocalFsStore, which callers already handle.
                raise FileNotFoundError(key) from exc
            raise

    def delete(self, key: str) -> None:
        self._check_key(key)
        self._ensure_bucket()
        self._client.delete_object(Bucket=self.bucket, Key=key)  # idempotent in S3


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    """Return the configured object store (S3 when an endpoint is set, else local FS)."""
    global _store
    if _store is None:
        if settings.OBJECT_STORAGE_ENDPOINT:
            _store = S3Store(
                settings.OBJECT_STORAGE_ENDPOINT,
                settings.OBJECT_STORAGE_BUCKET,
                settings.OBJECT_STORAGE_ACCESS_KEY,
                settings.OBJECT_STORAGE_SECRET_KEY,
                settings.OBJECT_STORAGE_REGION,
            )
        else:
            _store = LocalFsStore(settings.OBJECT_STORAGE_LOCAL_DIR)
    return _store


def reset_store() -> None:
    """Drop the cached store (used by tests that reconfigure storage)."""
    global _store
    _store = None


# ---------------------------------------------------------------------------
# Signed grants (HMAC tokens)
# ---------------------------------------------------------------------------
def _sign(payload: str) -> str:
    return hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_grant(scope: str, resource_id: str, expires_at: dt.datetime) -> str:
    exp = int(expires_at.timestamp())
    payload = f"{scope}:{resource_id}:{exp}"
    token_raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(token_raw.encode("utf-8")).decode("ascii")


def _verify_grant(scope: str, token: str, now: dt.datetime | None = None) -> str | None:
    """Return the resource id if the token is a valid, unexpired grant for ``scope``."""
    now = now or utcnow()
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = decoded.split(":")
    if len(parts) != 4:
        return None
    tok_scope, resource_id, exp_str, sig = parts
    if tok_scope != scope:
        return None
    payload = f"{tok_scope}:{resource_id}:{exp_str}"
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if exp < int(now.timestamp()):
        return None
    return resource_id


def make_upload_grant(job_id: str, expires_at: dt.datetime) -> str:
    return _make_grant("upload", str(job_id), expires_at)


def verify_upload_grant(token: str, now: dt.datetime | None = None) -> str | None:
    return _verify_grant("upload", token, now)


def make_download_grant(artifact_id: str, expires_at: dt.datetime) -> str:
    return _make_grant("download", str(artifact_id), expires_at)


def verify_download_grant(token: str, now: dt.datetime | None = None) -> str | None:
    return _verify_grant("download", token, now)


def grant_expiry(ttl_seconds: int | None = None) -> dt.datetime:
    ttl = ttl_seconds if ttl_seconds is not None else settings.ARTIFACT_GRANT_TTL_SECONDS
    return ensure_aware(utcnow()) + dt.timedelta(seconds=ttl)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
