"""Object storage + signed HMAC grants for artifact upload/download (Phase 4).

The API process NEVER executes customer code — it only stores and serves bytes.

Two backends:
* :class:`LocalFsStore` — dev/test default, writes under ``OBJECT_STORAGE_LOCAL_DIR``.
* :class:`S3Store` — a clearly-marked stub used when ``OBJECT_STORAGE_ENDPOINT``
  is configured (prod S3/MinIO). Wiring the real client is out of Phase-4 scope.

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

    def _path(self, key: str) -> str:
        # Prevent path traversal: the resolved path must stay under root.
        safe = os.path.normpath(os.path.join(self.root, key))
        if not safe.startswith(self.root + os.sep) and safe != self.root:
            raise ValueError("invalid_object_key")
        return safe

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as fh:
            return fh.read()

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class S3Store(ObjectStore):
    """STUB: prod S3/MinIO backend.

    Intentionally not implemented in Phase 4 — the real boto3/minio client is
    wired in a later phase. Selected only when ``OBJECT_STORAGE_ENDPOINT`` is set,
    so dev/test never reach it.
    """

    def __init__(self, endpoint: str, bucket: str) -> None:
        self.endpoint = endpoint
        self.bucket = bucket

    def _unimplemented(self) -> RuntimeError:
        return RuntimeError(
            "S3Store is a Phase-4 stub; configure OBJECT_STORAGE_LOCAL_DIR for dev/test"
        )

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        raise self._unimplemented()

    def get(self, key: str) -> bytes:
        raise self._unimplemented()

    def delete(self, key: str) -> None:
        raise self._unimplemented()


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    """Return the configured object store (S3 when an endpoint is set, else local FS)."""
    global _store
    if _store is None:
        if settings.OBJECT_STORAGE_ENDPOINT:
            _store = S3Store(settings.OBJECT_STORAGE_ENDPOINT, settings.OBJECT_STORAGE_BUCKET)
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
