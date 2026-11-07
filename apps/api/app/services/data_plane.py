"""Data plane service (U2, ADR-0006 section 9, principle P4: data is never relayed through the API).

* Resumable multipart uploads: the API issues short-lived presigned part URLs; the client sends bytes
  straight to the store. Each part carries its SHA-256 (signed into the URL, re-checked from ListParts).
* Completion proves the whole-file SHA-256 (streamed by the verifier, never by an upload request).
* Content addressing: one stored object per (scope, SHA-256), reference counted.
* Scoped expiring download links; bring-your-own-bucket access scoped to one job's prefix.
* Retention / expiry / garbage collection in :func:`sweep_data_plane` (called by the worker).

Fail closed: anything not proven is refused. Storage provider, retention and size limits are OWNER
DECISIONS, exposed only as settings (see docs/architecture/U2-data-plane-design.md).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import uuid
from urllib.parse import urlparse

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.data_plane import (
    OBJECT_DELETING,
    OBJECT_READY,
    PART_CORRUPT,
    PART_PENDING,
    PART_VERIFIED,
    UPLOAD_ABORTED,
    UPLOAD_ACTIVE,
    UPLOAD_COMPLETE,
    UPLOAD_EXPIRED,
    UPLOAD_FAILED,
    UPLOAD_OPEN,
    UPLOAD_VERIFYING,
    ByoBucket,
    DataFile,
    DataObject,
    DataUpload,
    DataUploadPart,
)
from app.models.jobs import Job
from app.services.audit import record_audit
from app.services.network_policy import is_blocked_hostname, is_forbidden_address
from app.services.data_plane_store import (
    MAX_PARTS,
    MIN_PART_BYTES,
    DataPlaneStore,
    DataPlaneStoreError,
    ScopedCredentials,
    get_byo_client,
    get_data_store,
)

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\]")


class DataPlaneError(Exception):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


# Errors raised AFTER the service recorded something the client needs to see on its next resume view (a part found
# changed in the store is marked corrupt). The route must commit before turning these into an HTTP error, because
# get_db rolls the session back on any exception.
PERSIST_ON_ERROR = frozenset({"part_changed_resend", "dedup_proof_failed"})


def require_enabled() -> DataPlaneStore:
    """Fail closed: off or unconfigured means refused."""
    if not settings.DATA_PLANE_ENABLED:
        raise DataPlaneError("data_plane_disabled", 404)
    try:
        return get_data_store()
    except DataPlaneStoreError:
        raise DataPlaneError("data_plane_not_configured", 503) from None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def scope_key(owner_id: uuid.UUID) -> str:
    return "global" if settings.DATA_PLANE_CROSS_ACCOUNT_DEDUP else str(owner_id)


PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
# Platform-built names of a job's files: out/<job id>/<output> and ckpt/<job id>/<seq>.tar
_JOB_FILE_NAME = re.compile(r"^(out|ckpt)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")


def _job_of_file_name(name: str) -> tuple[str, uuid.UUID] | None:
    m = _JOB_FILE_NAME.match(name or "")
    return (m.group(1), uuid.UUID(m.group(2))) if m else None


async def _is_private_job_file(db: AsyncSession, owner_id: uuid.UUID, name: str) -> bool:
    """True when ``name`` is an output / checkpoint of one of the caller's Sealed, Sealed Pro or Confidential jobs."""
    parsed = _job_of_file_name(name)
    if parsed is None:
        return False
    tier = (
        await db.execute(select(Job.isolation_tier).where(Job.id == parsed[1], Job.customer_id == owner_id))
    ).scalar_one_or_none()
    return (tier or "").lower() in PRIVATE_TIERS


async def _scope_for(db: AsyncSession, owner_id: uuid.UUID, name: str) -> str:
    """Dedup scope for a new object. Private-tier job files NEVER join the global (cross-account) scope."""
    if settings.DATA_PLANE_CROSS_ACCOUNT_DEDUP and await _is_private_job_file(db, owner_id, name):
        return str(owner_id)
    return scope_key(owner_id)


def object_key(scope: str, sha256: str) -> str:
    return f"cas/{scope}/{sha256[:2]}/{sha256}"


def plan_parts(size: int) -> tuple[int, int]:
    """(part_size, part_count). Part size is raised so the count stays under the 10 000 part limit."""
    part_size = max(MIN_PART_BYTES, int(settings.DATA_PLANE_PART_SIZE_BYTES))
    need = -(-size // MAX_PARTS)
    part_size = max(part_size, need)
    return part_size, max(1, -(-size // part_size))


def expected_part_size(upload: DataUpload, part_number: int) -> int:
    if part_number < upload.part_count:
        return upload.part_size
    return upload.size_bytes - upload.part_size * (upload.part_count - 1)


def effective_retention_days(requested: int | None) -> int:
    """0 = keep until deleted. The owner-set maximum always wins."""
    days = requested if requested is not None else int(settings.DATA_PLANE_RETENTION_DEFAULT_DAYS)
    cap = int(settings.DATA_PLANE_RETENTION_MAX_DAYS)
    if cap > 0 and (days <= 0 or days > cap):
        days = cap
    return max(0, days)


_NAME_BAD_INTERNAL = re.compile(r"[\x00-\x1f\x7f\\]")


def clean_name(name: str, *, internal: bool = False) -> str:
    """A customer-visible file name may not contain a path separator. ``internal`` (platform-generated names such as
    ``out/<job>/<output>`` and ``ckpt/<job>/<seq>.tar``, which the agent routes build themselves and which are looked up by
    that prefix) may contain "/" as a namespace separator, but never "..", control characters or a backslash."""
    name = (name or "").strip()
    bad = _NAME_BAD_INTERNAL if internal else _NAME_BAD
    if not name or len(name) > 255 or bad.search(name) or (internal and ".." in name.split("/")):
        raise DataPlaneError("invalid_name", 422)
    return name


def _aware(v: dt.datetime) -> dt.datetime:
    return ensure_aware(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------
async def account_usage_bytes(db: AsyncSession, owner_id: uuid.UUID) -> int:
    files = (
        await db.execute(
            select(func.coalesce(func.sum(DataFile.size_bytes), 0)).where(
                DataFile.owner_id == owner_id, DataFile.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    open_uploads = (
        await db.execute(
            select(func.coalesce(func.sum(DataUpload.size_bytes), 0)).where(
                DataUpload.owner_id == owner_id, DataUpload.status.in_(UPLOAD_OPEN)
            )
        )
    ).scalar_one()
    return int(files) + int(open_uploads)


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
async def _ref_object(db: AsyncSession, obj_id: uuid.UUID) -> bool:
    """Atomically take a reference. False when the object is being deleted."""
    res = await db.execute(
        update(DataObject)
        .where(DataObject.id == obj_id, DataObject.status == OBJECT_READY)
        .values(refcount=DataObject.refcount + 1, last_referenced_at=utcnow())
    )
    return (res.rowcount or 0) == 1


async def _unref_object(db: AsyncSession, obj_id: uuid.UUID) -> None:
    await db.execute(
        update(DataObject)
        .where(DataObject.id == obj_id, DataObject.refcount > 0)
        .values(refcount=DataObject.refcount - 1, last_referenced_at=utcnow())
    )


def _new_file(upload_or_args, owner_id, obj: DataObject, name, content_type, retention_days) -> DataFile:
    days = effective_retention_days(retention_days)
    return DataFile(
        id=uuid.uuid4(),
        owner_id=owner_id,
        object_id=obj.id,
        name=name,
        content_type=content_type,
        size_bytes=obj.size_bytes,
        sha256=obj.sha256,
        expires_at=(utcnow() + dt.timedelta(days=days)) if days > 0 else None,
    )


async def create_upload(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    idempotency_key: str,
    name: str,
    content_type: str,
    sha256: str,
    size_bytes: int,
    retention_days: int | None = None,
    ip: str | None = None,
    internal_name: bool = False,
    dedup_proof: str | None = None,
) -> tuple[DataUpload, DataFile | None]:
    """Start (or dedupe, or idempotently replay) an upload. Returns (upload, file); file is set on a dedup hit.

    With cross-account dedup ON, a hit on an object the caller never uploaded is only taken with a valid
    ``dedup_proof`` (see :func:`start_dedup_proof`); without one the call behaves exactly as if the hash were unknown
    (a normal upload), so it never confirms that a hash exists."""
    store = require_enabled()
    name = clean_name(name, internal=internal_name)
    sha256 = (sha256 or "").lower()
    if not _SHA_RE.match(sha256):
        raise DataPlaneError("invalid_sha256", 422)
    if size_bytes <= 0 or size_bytes > int(settings.DATA_PLANE_MAX_FILE_BYTES):
        raise DataPlaneError("file_size_not_allowed", 413)
    content_type = (content_type or "application/octet-stream").split(";")[0].strip().lower()[:128] or "application/octet-stream"
    if retention_days is not None and retention_days < 0:
        raise DataPlaneError("invalid_retention", 422)

    prior = (
        await db.execute(
            select(DataUpload).where(DataUpload.owner_id == owner_id, DataUpload.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior.sha256 != sha256 or prior.size_bytes != size_bytes or prior.name != name:
            raise DataPlaneError("idempotency_key_reused", 409)
        file = await db.get(DataFile, prior.file_id) if prior.file_id else None
        return prior, file

    if await account_usage_bytes(db, owner_id) + size_bytes > int(settings.DATA_PLANE_ACCOUNT_QUOTA_BYTES):
        raise DataPlaneError("quota_exceeded", 413)

    scope = await _scope_for(db, owner_id, name)
    existing = (
        await db.execute(
            select(DataObject).where(
                DataObject.scope_key == scope,
                DataObject.sha256 == sha256,
                DataObject.size_bytes == size_bytes,
                DataObject.status == OBJECT_READY,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and scope == "global" and not await _may_dedup_into(db, owner_id, existing, dedup_proof):
        existing = None  # indistinguishable from "not stored": the caller uploads the bytes (which proves possession)
    upload_id = uuid.uuid4()
    now = utcnow()
    part_size, part_count = plan_parts(size_bytes)
    if existing is not None and await _ref_object(db, existing.id):
        file = _new_file(None, owner_id, existing, name, content_type, retention_days)
        db.add(file)
        up = DataUpload(
            id=upload_id, owner_id=owner_id, idempotency_key=idempotency_key, name=name, content_type=content_type,
            sha256=sha256, size_bytes=size_bytes, part_size=part_size, part_count=part_count, status=UPLOAD_COMPLETE,
            store_upload_id="dedup", staging_key=f"dedup/{upload_id}", retention_days=retention_days,
            file_id=file.id, expires_at=now,
        )
        db.add(up)
        await db.flush()
        await record_audit(db, action="data.upload.dedup", outcome="success", actor_user_id=owner_id,
                           resource_type="data_file", resource_id=str(file.id), ip=ip)
        return up, file

    staging = f"staging/{owner_id}/{upload_id}"
    try:
        store_upload_id = store.create_multipart(staging, content_type)
    except Exception:
        logger.exception("create_multipart failed")
        raise DataPlaneError("store_unavailable", 503) from None
    up = DataUpload(
        id=upload_id, owner_id=owner_id, idempotency_key=idempotency_key, name=name, content_type=content_type,
        sha256=sha256, size_bytes=size_bytes, part_size=part_size, part_count=part_count, status=UPLOAD_ACTIVE,
        store_upload_id=store_upload_id, staging_key=staging, retention_days=retention_days,
        expires_at=now + dt.timedelta(seconds=int(settings.DATA_PLANE_UPLOAD_TTL_SECONDS)),
    )
    db.add(up)
    await db.flush()
    await record_audit(db, action="data.upload.create", outcome="success", actor_user_id=owner_id,
                       resource_type="data_upload", resource_id=str(up.id), ip=ip)
    return up, None


async def get_owned_upload(db: AsyncSession, owner_id: uuid.UUID, upload_id: uuid.UUID) -> DataUpload:
    up = (
        await db.execute(select(DataUpload).where(DataUpload.id == upload_id, DataUpload.owner_id == owner_id))
    ).scalar_one_or_none()
    if up is None:
        raise DataPlaneError("not_found", 404)
    return up


def _require_active(up: DataUpload) -> None:
    if up.status != UPLOAD_ACTIVE:
        raise DataPlaneError(f"upload_{up.status}", 409)
    if _aware(up.expires_at) <= utcnow():
        raise DataPlaneError("upload_expired", 410)


async def _get_part(db: AsyncSession, upload_id: uuid.UUID, n: int) -> DataUploadPart | None:
    return (
        await db.execute(
            select(DataUploadPart).where(DataUploadPart.upload_id == upload_id, DataUploadPart.part_number == n)
        )
    ).scalar_one_or_none()


async def issue_part_url(
    db: AsyncSession, up: DataUpload, *, part_number: int, sha256: str, size_bytes: int
) -> dict:
    """Short-lived presigned PUT for one part. The client states the part's SHA-256 and size up front."""
    store = require_enabled()
    _require_active(up)
    sha256 = (sha256 or "").lower()
    if not _SHA_RE.match(sha256):
        raise DataPlaneError("invalid_sha256", 422)
    if part_number < 1 or part_number > up.part_count:
        raise DataPlaneError("invalid_part_number", 422)
    if size_bytes != expected_part_size(up, part_number):
        raise DataPlaneError("invalid_part_size", 422)
    part = await _get_part(db, up.id, part_number)
    if part is not None and part.status == PART_VERIFIED and part.sha256 == sha256:
        return {"part_number": part_number, "already_uploaded": True}
    if part is None:
        part = DataUploadPart(upload_id=up.id, part_number=part_number, size_bytes=size_bytes, sha256=sha256)
        db.add(part)
    else:  # re-send (corrupt, or the client changed its mind): the old proof no longer applies
        part.sha256, part.size_bytes, part.status, part.etag = sha256, size_bytes, PART_PENDING, None
    ttl = max(1, min(int(settings.DATA_PLANE_PART_URL_TTL_SECONDS), int((_aware(up.expires_at) - utcnow()).total_seconds())))
    try:
        url, headers = store.presign_upload_part(up.staging_key, up.store_upload_id, part_number, sha256, size_bytes, ttl)
    except Exception:
        logger.exception("presign part failed")
        raise DataPlaneError("store_unavailable", 503) from None
    await db.flush()
    return {
        "part_number": part_number, "already_uploaded": False, "method": "PUT", "url": url, "headers": headers,
        "expires_at": utcnow() + dt.timedelta(seconds=ttl),
    }


async def confirm_part(db: AsyncSession, up: DataUpload, *, part_number: int) -> DataUploadPart:
    """Check what the store holds for this part against the declared SHA-256 and size.

    A mismatch (or a missing checksum) marks the part corrupt; the client asks for a new URL and re-sends it.
    """
    store = require_enabled()
    _require_active(up)
    part = await _get_part(db, up.id, part_number)
    if part is None:
        raise DataPlaneError("part_url_not_requested", 409)
    try:
        stored = {p.part_number: p for p in store.list_parts(up.staging_key, up.store_upload_id)}
    except Exception:
        logger.exception("list_parts failed")
        raise DataPlaneError("store_unavailable", 503) from None
    sp = stored.get(part_number)
    part.attempts += 1
    if sp is None:
        part.status = PART_PENDING
        await db.flush()
        raise DataPlaneError("part_not_found_in_store", 409)
    if sp.size_bytes != part.size_bytes or sp.sha256_hex is None or sp.sha256_hex != part.sha256:
        part.status, part.etag = PART_CORRUPT, None
    else:
        part.status, part.etag = PART_VERIFIED, sp.etag
    await db.flush()
    return part


async def upload_state(db: AsyncSession, up: DataUpload) -> dict:
    """Resume view: which parts are proven. Reconciled with the store while the upload is open."""
    parts = (
        await db.execute(select(DataUploadPart).where(DataUploadPart.upload_id == up.id).order_by(DataUploadPart.part_number))
    ).scalars().all()
    return {
        "upload": up,
        "verified_parts": [p.part_number for p in parts if p.status == PART_VERIFIED],
        "corrupt_parts": [p.part_number for p in parts if p.status == PART_CORRUPT],
        "pending_parts": [p.part_number for p in parts if p.status == PART_PENDING],
        "missing_parts": [n for n in range(1, up.part_count + 1) if n not in {p.part_number for p in parts}],
    }


async def complete_upload(db: AsyncSession, up: DataUpload, *, ip: str | None = None) -> tuple[DataUpload, DataFile | None]:
    """Assemble the parts, then prove the whole-file SHA-256 before the file becomes addressable."""
    store = require_enabled()
    if up.status == UPLOAD_COMPLETE:
        return up, (await db.get(DataFile, up.file_id) if up.file_id else None)
    if up.status == UPLOAD_ACTIVE:
        _require_active(up)
        parts = (
            await db.execute(select(DataUploadPart).where(DataUploadPart.upload_id == up.id).order_by(DataUploadPart.part_number))
        ).scalars().all()
        if [p.part_number for p in parts] != list(range(1, up.part_count + 1)):
            raise DataPlaneError("parts_missing", 409)
        if any(p.status != PART_VERIFIED or not p.etag for p in parts):
            raise DataPlaneError("parts_not_verified", 409)
        if sum(p.size_bytes for p in parts) != up.size_bytes:
            raise DataPlaneError("size_mismatch", 409)
        try:
            stored = {p.part_number: p for p in store.list_parts(up.staging_key, up.store_upload_id)}
            # A part overwritten after it was confirmed must not slip in: re-check every one.
            for p in parts:
                sp = stored.get(p.part_number)
                if sp is None or sp.etag != p.etag or sp.sha256_hex != p.sha256 or sp.size_bytes != p.size_bytes:
                    p.status = PART_CORRUPT
                    await db.flush()
                    raise DataPlaneError("part_changed_resend", 409)
            store.complete_multipart(up.staging_key, up.store_upload_id, [(p.part_number, p.etag) for p in parts])
        except DataPlaneError:
            raise
        except Exception:
            logger.exception("complete_multipart failed")
            raise DataPlaneError("store_unavailable", 503) from None
        up.status = UPLOAD_VERIFYING
        await db.flush()
    if up.status == UPLOAD_VERIFYING and up.size_bytes <= int(settings.DATA_PLANE_INLINE_VERIFY_MAX_BYTES):
        await finalize_upload(db, up, ip=ip)
    file = await db.get(DataFile, up.file_id) if up.file_id else None
    return up, file


async def finalize_upload(db: AsyncSession, up: DataUpload, *, ip: str | None = None) -> None:
    """Whole-file proof, then promote to the content-addressed key (dedup) and create the DataFile.

    Called inline for small files and by the worker for large ones. Store outages leave the upload in
    ``verifying`` so a later sweep retries; a proven mismatch is final.
    """
    if up.status != UPLOAD_VERIFYING:
        return
    store = require_enabled()
    try:
        size = store.head_size(up.staging_key)
        actual = store.sha256_of(up.staging_key) if size == up.size_bytes else None
    except Exception:
        logger.exception("verification read failed for upload %s", up.id)
        return  # retried by the sweeper
    if size != up.size_bytes or actual != up.sha256:
        up.status, up.error_code = UPLOAD_FAILED, "hash_mismatch" if actual else "size_mismatch"
        _best_effort_delete(store, up.staging_key)
        await db.flush()
        await record_audit(db, action="data.upload.verify", outcome="failure", actor_user_id=up.owner_id,
                           resource_type="data_upload", resource_id=str(up.id), reason=up.error_code, ip=ip)
        return

    scope = await _scope_for(db, up.owner_id, up.name)
    obj = (
        await db.execute(select(DataObject).where(DataObject.scope_key == scope, DataObject.sha256 == up.sha256))
    ).scalar_one_or_none()
    if obj is not None and obj.status != OBJECT_READY:
        return  # being garbage-collected right now; retry on the next sweep
    if obj is not None:
        if not await _ref_object(db, obj.id):
            return
    else:
        key = object_key(scope, up.sha256)
        try:
            store.copy(up.staging_key, key)
        except Exception:
            logger.exception("promote failed for upload %s", up.id)
            return
        obj = DataObject(
            id=uuid.uuid4(), scope_key=scope, sha256=up.sha256, size_bytes=up.size_bytes, storage_key=key, refcount=1
        )
        try:
            async with db.begin_nested():
                db.add(obj)
                await db.flush()
        except IntegrityError:  # another upload of the same bytes won the race: share its object
            obj = (
                await db.execute(select(DataObject).where(DataObject.scope_key == scope, DataObject.sha256 == up.sha256))
            ).scalar_one()
            if not await _ref_object(db, obj.id):
                return
    file = _new_file(None, up.owner_id, obj, up.name, up.content_type, up.retention_days)
    db.add(file)
    up.status, up.file_id = UPLOAD_COMPLETE, file.id
    await db.flush()
    _best_effort_delete(store, up.staging_key)
    await record_audit(db, action="data.upload.verify", outcome="success", actor_user_id=up.owner_id,
                       resource_type="data_file", resource_id=str(file.id), ip=ip)


def _best_effort_delete(store: DataPlaneStore, key: str) -> None:
    try:
        store.delete(key)
    except Exception:
        logger.warning("could not delete %s (the lifecycle sweep will retry)", key)


async def abort_upload(db: AsyncSession, up: DataUpload, *, reason: str = UPLOAD_ABORTED) -> None:
    if up.status not in UPLOAD_OPEN:
        return
    store = require_enabled()
    try:
        if up.status == UPLOAD_ACTIVE:
            store.abort_multipart(up.staging_key, up.store_upload_id)
        else:
            store.delete(up.staging_key)
    except Exception:
        logger.warning("abort of upload %s failed at the store; marked anyway", up.id)
    up.status = reason
    await db.flush()


# ---------------------------------------------------------------------------
# Cross-account dedup: proof of possession (only when DATA_PLANE_CROSS_ACCOUNT_DEDUP is on)
# ---------------------------------------------------------------------------
# Why: with a global dedup scope, "this hash is already stored" is an oracle for anyone who knows (or guesses) a hash.
# So a dedup hit on an object the caller never uploaded needs proof that the caller holds the bytes:
#   1. start_dedup_proof   -> random 32-byte challenge + 4 server-chosen byte ranges, in a signed stateless token.
#                             The answer has the same shape whether or not the hash is stored.
#   2. client computes     sha256(challenge || range0 || range1 || range2 || range3) over ITS copy of the file.
#   3. verify_dedup_proof  -> the service does 4 RANGED reads from the store (never the whole object) and compares.
#                             Success returns a short-lived signed receipt bound to (caller, sha256, size) that
#                             create_upload accepts as ``dedup_proof``. Every failure is the same 403.
# Never applies to private tiers (their files are always per-account, see _scope_for) or when cross-account dedup is off.
_PROOF_RANGES = 4
_MAX_PROOF_RANGE_BYTES = 1024 * 1024


def _proof_key() -> bytes:
    return hashlib.sha256(b"computecoin.data_plane.dedup_proof.v1|" + settings.AUTH_SECRET.encode()).digest()


def _sign_token(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{body}.{hmac.new(_proof_key(), body.encode(), hashlib.sha256).hexdigest()}"


def _read_token(token: str | None, kind: str) -> dict | None:
    """The payload of a token we signed, of this kind, not expired; otherwise None (no reason is ever given)."""
    try:
        body, mac = str(token).rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(_proof_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or payload.get("k") != kind or int(payload["x"]) <= int(utcnow().timestamp()):
            return None
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def _require_dedup_proof_applicable() -> None:
    if not settings.DATA_PLANE_CROSS_ACCOUNT_DEDUP:
        raise DataPlaneError("dedup_proof_not_applicable", 404)


async def start_dedup_proof(db: AsyncSession, *, owner_id: uuid.UUID, sha256: str, size_bytes: int) -> dict:
    """Issue a challenge for ``sha256`` / ``size_bytes``. Never touches the store and never says whether the hash exists."""
    require_enabled()
    _require_dedup_proof_applicable()
    sha256 = (sha256 or "").lower()
    if not _SHA_RE.match(sha256):
        raise DataPlaneError("invalid_sha256", 422)
    if size_bytes <= 0 or size_bytes > int(settings.DATA_PLANE_MAX_FILE_BYTES):
        raise DataPlaneError("file_size_not_allowed", 413)
    length = max(1, min(int(settings.DATA_PLANE_DEDUP_PROOF_RANGE_BYTES), _MAX_PROOF_RANGE_BYTES, size_bytes))
    ranges = [{"offset": secrets.randbelow(size_bytes - length + 1), "length": length} for _ in range(_PROOF_RANGES)]
    challenge = secrets.token_bytes(32).hex()
    expires = utcnow() + dt.timedelta(seconds=max(30, int(settings.DATA_PLANE_DEDUP_PROOF_TTL_SECONDS)))
    token = _sign_token({
        "k": "challenge", "o": str(owner_id), "h": sha256, "s": int(size_bytes), "c": challenge,
        "r": [[r["offset"], r["length"]] for r in ranges], "x": int(expires.timestamp()),
    })
    return {
        "token": token, "challenge": challenge, "ranges": ranges, "expires_at": expires,
        "digest": "sha256(challenge_bytes || range0 || range1 || range2 || range3), challenge_bytes = hex-decoded challenge",
    }


async def verify_dedup_proof(
    db: AsyncSession, *, owner_id: uuid.UUID, token: str, proof_sha256: str, ip: str | None = None
) -> dict:
    """Check the client's digest against four ranged reads of the stored object. Returns ``{"dedup_proof", "expires_at"}``.

    Every failure (bad / expired / foreign token, unknown hash, wrong digest, store problem) raises the same
    ``dedup_proof_failed`` 403 and is audit-logged, so it cannot be used to learn whether a hash is stored."""
    store = require_enabled()
    _require_dedup_proof_applicable()
    ok = False
    payload = _read_token(token, "challenge")
    proof = (proof_sha256 or "").lower()
    if payload is not None and payload.get("o") == str(owner_id) and _SHA_RE.match(proof):
        obj = (
            await db.execute(
                select(DataObject).where(
                    DataObject.scope_key == "global", DataObject.sha256 == payload["h"],
                    DataObject.size_bytes == int(payload["s"]), DataObject.status == OBJECT_READY,
                )
            )
        ).scalar_one_or_none()
        if obj is not None:
            try:
                digest = hashlib.sha256(bytes.fromhex(payload["c"]))
                for offset, length in payload["r"]:
                    if offset < 0 or length <= 0 or offset + length > obj.size_bytes or length > _MAX_PROOF_RANGE_BYTES:
                        raise ValueError("range outside the object")
                    chunk = store.read_range(obj.storage_key, int(offset), int(length))
                    if len(chunk) != length:
                        raise ValueError("short range read")
                    digest.update(chunk)
                ok = hmac.compare_digest(digest.hexdigest(), proof)
            except Exception:
                logger.warning("dedup proof could not be checked against the store")
                ok = False
    await record_audit(db, action="data.dedup_proof.verify", outcome="success" if ok else "failure",
                       actor_user_id=owner_id, resource_type="data_object", resource_id=None, ip=ip)
    if not ok:
        raise DataPlaneError("dedup_proof_failed", 403)
    expires = utcnow() + dt.timedelta(seconds=max(30, int(settings.DATA_PLANE_DEDUP_PROOF_TTL_SECONDS)))
    receipt = _sign_token({"k": "receipt", "o": str(owner_id), "h": payload["h"], "s": int(payload["s"]), "x": int(expires.timestamp())})
    return {"dedup_proof": receipt, "expires_at": expires}


async def _may_dedup_into(db: AsyncSession, owner_id: uuid.UUID, obj: DataObject, dedup_proof: str | None) -> bool:
    """May this caller take a dedup hit on ``obj`` in the global scope? Yes if it uploaded these bytes before
    (any DataFile of its own, even a deleted one, points at this object) or holds a valid proof receipt."""
    mine = (
        await db.execute(
            select(DataFile.id).where(DataFile.owner_id == owner_id, DataFile.object_id == obj.id).limit(1)
        )
    ).first()
    if mine is not None:
        return True
    receipt = _read_token(dedup_proof, "receipt") if dedup_proof else None
    return bool(
        receipt is not None and receipt.get("o") == str(owner_id) and receipt.get("h") == obj.sha256
        and int(receipt.get("s", -1)) == obj.size_bytes
    )


# ---------------------------------------------------------------------------
# Files, downloads, deletion
# ---------------------------------------------------------------------------
async def get_owned_file(db: AsyncSession, owner_id: uuid.UUID, file_id: uuid.UUID) -> DataFile:
    f = (
        await db.execute(select(DataFile).where(DataFile.id == file_id, DataFile.owner_id == owner_id))
    ).scalar_one_or_none()
    if f is None or f.deleted_at is not None:
        raise DataPlaneError("not_found", 404)
    if f.expires_at is not None and _aware(f.expires_at) <= utcnow():
        raise DataPlaneError("not_found", 404)  # expired files are gone, even before the sweeper runs
    return f


async def create_download_link(
    db: AsyncSession, *, owner_id: uuid.UUID, file_id: uuid.UUID, ttl_seconds: int | None = None, ip: str | None = None
) -> dict:
    """A presigned GET for exactly this file's object, expiring in at most DATA_PLANE_DOWNLOAD_MAX_TTL_SECONDS."""
    store = require_enabled()
    f = await get_owned_file(db, owner_id, file_id)
    obj = await db.get(DataObject, f.object_id) if f.object_id else None
    if obj is None or obj.status != OBJECT_READY:
        raise DataPlaneError("not_found", 404)
    ttl = ttl_seconds if ttl_seconds is not None else int(settings.DATA_PLANE_DOWNLOAD_TTL_SECONDS)
    ttl = max(1, min(ttl, int(settings.DATA_PLANE_DOWNLOAD_MAX_TTL_SECONDS)))
    try:
        url = store.presign_download(obj.storage_key, ttl, f.name, f.content_type)
    except Exception:
        logger.exception("presign download failed")
        raise DataPlaneError("store_unavailable", 503) from None
    await record_audit(db, action="data.download.link", outcome="success", actor_user_id=owner_id,
                       resource_type="data_file", resource_id=str(f.id), ip=ip)
    return {"url": url, "expires_at": utcnow() + dt.timedelta(seconds=ttl), "sha256": f.sha256, "size_bytes": f.size_bytes}


async def delete_file(db: AsyncSession, *, owner_id: uuid.UUID, file_id: uuid.UUID, reason: str = "owner", ip: str | None = None) -> None:
    require_enabled()
    f = await get_owned_file(db, owner_id, file_id)
    await _drop_file(db, f, reason)
    await record_audit(db, action="data.file.delete", outcome="success", actor_user_id=owner_id,
                       resource_type="data_file", resource_id=str(f.id), reason=reason, ip=ip)


async def _drop_file(db: AsyncSession, f: DataFile, reason: str) -> None:
    res = await db.execute(
        update(DataFile).where(DataFile.id == f.id, DataFile.deleted_at.is_(None)).values(deleted_at=utcnow(), deleted_reason=reason)
    )
    if (res.rowcount or 0) == 1 and f.object_id is not None:  # decrement exactly once even if two sweeps race
        await _unref_object(db, f.object_id)
    if (res.rowcount or 0) == 1 and reason != "shred":
        # The owner (or retention) removed a job OUTPUT: for a private-tier job that is the end of its data, so the
        # keys go too. Checkpoints (ckpt/) never trigger it. ``reason == "shred"`` is the shredder's own call.
        parsed = _job_of_file_name(f.name)
        if parsed is not None and parsed[0] == "out":
            job = (await db.execute(select(Job).where(Job.id == parsed[1], Job.customer_id == f.owner_id))).scalar_one_or_none()
            if job is not None and (job.isolation_tier or "").lower() in PRIVATE_TIERS:
                await shred_private_data(db, job, reason=reason)


async def _purge_object_now(db: AsyncSession, store: DataPlaneStore | None, object_id: uuid.UUID) -> bool:
    """Delete an unreferenced object from the store immediately (the sweep would do it after the grace period).
    False when it is still referenced or the store delete failed; a failed delete leaves it ``deleting`` so the sweep's
    stuck-object pass retries it."""
    obj = await db.get(DataObject, object_id)
    if obj is None:
        return True
    if store is None:
        return False
    await db.refresh(obj)
    claimed = await db.execute(
        update(DataObject).where(DataObject.id == object_id, DataObject.status == OBJECT_READY, DataObject.refcount == 0)
        .values(status=OBJECT_DELETING)
    )
    if (claimed.rowcount or 0) != 1:
        return False
    try:
        store.delete(obj.storage_key)
    except Exception:
        logger.warning("shred: object %s delete failed; the sweep will retry", object_id)
        return False
    await _detach_deleted_files(db, object_id)
    await db.delete(obj)
    await db.flush()
    return True


async def shred_private_data(db: AsyncSession, job: Job, *, reason: str = "retention", ip: str | None = None) -> dict:
    """Crypto-shred a PRIVATE-tier job's data (sealed, sealed_pro, confidential only; any other tier is a no-op).

    1. every open upload of the job is aborted and every output / checkpoint file (``out/<job>/``, ``ckpt/<job>/``) is
       dropped and its stored object deleted now, unless another file still references it (then the reference goes and
       the sweep deletes the object when the last one is gone);
    2. the key material on the job row is wiped: ``dk_shares`` (the sealed Data Key shares), the ``wrapped_share`` of every
       guardian release (the log pointers stay as audit evidence, and the entry list stays non-empty so keys can never
       be released again), ``data_policy.result_public_key``, and the ciphertext columns ``encrypted_input`` /
       ``encrypted_output`` (the platform keeps no copy it could be made to hand over; the commitment hash stays).

    Idempotent (a repeated call finds nothing to do and writes no second audit entry). Fails closed in the sense that
    a store failure leaves the object marked ``deleting`` for the sweep to finish, while the keys are wiped regardless.
    Only terminal jobs are shredded. The caller commits. Never raises for a missing data plane.
    """
    if (job.isolation_tier or "").lower() not in PRIVATE_TIERS:
        return {"shredded": False, "reason": "not_private_tier"}
    from app.services import job_fsm as fsm

    if job.status not in fsm.TERMINAL_STATES or job.status == fsm.DISPUTED:
        # never pull the keys from under a running job, nor destroy the evidence of a disputed one
        return {"shredded": False, "reason": "job_not_terminal_or_disputed"}

    did = 0
    store: DataPlaneStore | None = None
    if settings.data_plane_configured:
        try:
            store = get_data_store()
        except DataPlaneStoreError:
            store = None
    like_out, like_ckpt = f"out/{job.id}/%", f"ckpt/{job.id}/%"
    files = (
        await db.execute(
            select(DataFile).where(
                DataFile.owner_id == job.customer_id, DataFile.deleted_at.is_(None),
                (DataFile.name.like(like_out)) | (DataFile.name.like(like_ckpt)),
            )
        )
    ).scalars().all()
    for f in files:
        oid = f.object_id
        await _drop_file(db, f, "shred")
        if oid is not None:
            await _purge_object_now(db, store, oid)
        did += 1
    open_uploads = (
        await db.execute(
            select(DataUpload).where(
                DataUpload.owner_id == job.customer_id, DataUpload.status.in_(UPLOAD_OPEN),
                DataUpload.idempotency_key.like(f"job:{job.id}:%"),
            )
        )
    ).scalars().all()
    for up in open_uploads:
        if store is not None:
            await abort_upload(db, up)
        did += 1

    policy = dict(job.data_policy or {})
    if "result_public_key" in policy:
        policy.pop("result_public_key")
        did += 1
    if job.dk_shares is not None or job.encrypted_input is not None or job.encrypted_output is not None:
        did += 1
    if isinstance(job.guardian_releases, list) and any(isinstance(r, dict) and "wrapped_share" in r for r in job.guardian_releases):
        job.guardian_releases = [
            {k: v for k, v in r.items() if k != "wrapped_share"} if isinstance(r, dict) else r for r in job.guardian_releases
        ]
        did += 1
    job.dk_shares = None
    job.encrypted_input = None
    job.encrypted_output = None
    if did == 0 and policy.get("shredded_at"):
        return {"shredded": True, "already": True}
    policy["shredded_at"] = policy.get("shredded_at") or utcnow().isoformat()
    policy["shredded_reason"] = policy.get("shredded_reason") or str(reason)[:32]
    job.data_policy = policy
    await db.flush()
    await record_audit(db, action="data.private.shred", outcome="success", actor_user_id=None, resource_type="job",
                       resource_id=str(job.id), reason=str(reason)[:128], ip=ip)
    return {"shredded": True, "already": False, "items": did}


# ---------------------------------------------------------------------------
# Bring your own bucket
# ---------------------------------------------------------------------------
def _credential_key() -> bytes:
    raw = settings.DATA_PLANE_CREDENTIAL_KEY
    if len(raw) < 32:
        raise DataPlaneError("byo_not_configured", 503)
    return hashlib.sha256(b"computecoin.data_plane.byo.v1|" + raw.encode()).digest()


def encrypt_credentials(creds: dict[str, str]) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(_credential_key()).encrypt(nonce, json.dumps(creds).encode(), b"byo-credentials")
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_credentials(blob: str) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(blob)
    return json.loads(AESGCM(_credential_key()).decrypt(raw[:12], raw[12:], b"byo-credentials"))


_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]*|\d+)(\.(0x[0-9a-f]*|\d+))*")


def _check_endpoint(endpoint: str) -> None:
    """HTTPS only, and never a loopback / private / link-local literal (blocks metadata-service SSRF).

    DNS names that resolve to private addresses are NOT caught here (documented residual risk: the
    API never connects to a customer endpoint itself, only signs URLs; STS mode calls AWS only).
    """
    if not endpoint:
        return
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    if (
        u.scheme != "https" or not host or host in {"localhost", "metadata.google.internal"}
        or host.endswith((".internal", ".localhost", ".local"))
        # "2130706433", "0x7f.1", "127.1": ip_address() rejects these forms but the resolver (inet_aton) turns them into
        # loopback / private addresses. No real host name has an all-numeric last label.
        or _NUMERIC_HOST.fullmatch(host)
    ):
        raise DataPlaneError("invalid_endpoint", 422)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise DataPlaneError("invalid_endpoint", 422)


def _resolve_addresses(host: str, port: int) -> list[str]:
    """Every address the system resolver returns for ``host`` (blocking; run it in a thread). Tests replace this."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]).split("%", 1)[0] for i in infos})


async def check_endpoint_resolves_public(endpoint: str) -> list[str]:
    """Resolve a BYO endpoint's host name NOW and refuse it if ANY address is private, loopback, link-local, shared,
    reserved, multicast or a cloud-metadata address (``network_policy.is_forbidden_address``). Returns the addresses.

    Called at registration AND again right before each presigned URL / credential grant is issued, because DNS can
    change after validation (rebinding). Residual risk, stated plainly: the API never connects to the endpoint itself
    (it signs URLs), and the signed host name cannot be pinned to an address, so the machine that finally uses the URL
    must also refuse private addresses after its own resolution (the agent's egress proxy does). An empty endpoint
    (the provider's default endpoint) is not checked. An unresolvable name is refused (fail closed).
    """
    if not endpoint:
        return []
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    if not host or is_blocked_hostname(host):
        raise DataPlaneError("invalid_endpoint", 422)
    try:
        port = u.port or 443
    except ValueError:
        raise DataPlaneError("invalid_endpoint", 422) from None
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(_resolve_addresses, host, port), timeout=5.0)
        except (OSError, asyncio.TimeoutError):
            raise DataPlaneError("endpoint_unresolvable", 422) from None
    if not addresses or any(is_forbidden_address(a) for a in addresses):
        raise DataPlaneError("invalid_endpoint", 422)
    return addresses


def normalize_prefix(prefix: str) -> str:
    p = (prefix or "").strip().lstrip("/")
    if not p or ".." in p.split("/") or "\x00" in p or len(p) > 256:
        raise DataPlaneError("invalid_prefix", 422)  # a bucket-wide grant is never allowed
    return p if p.endswith("/") else p + "/"


def job_prefix(bucket: ByoBucket, job_id: uuid.UUID) -> str:
    return f"{bucket.prefix}jobs/{job_id}/"


def _require_byo() -> None:
    if not (settings.DATA_PLANE_ENABLED and settings.DATA_PLANE_BYO_ENABLED):
        raise DataPlaneError("byo_disabled", 404)


async def register_byo_bucket(
    db: AsyncSession, *, owner_id: uuid.UUID, name: str, endpoint: str, bucket: str, region: str, prefix: str,
    mode: str, role_arn: str | None, access_key_id: str, secret_access_key: str, ip: str | None = None,
) -> ByoBucket:
    _require_byo()
    name = clean_name(name)[:64]
    if mode not in ("presign", "sts"):
        raise DataPlaneError("invalid_mode", 422)
    if mode == "sts" and not role_arn:
        raise DataPlaneError("role_arn_required", 422)
    if not bucket or not access_key_id or not secret_access_key:
        raise DataPlaneError("invalid_bucket", 422)
    _check_endpoint(endpoint)
    await check_endpoint_resolves_public(endpoint)
    row = ByoBucket(
        owner_id=owner_id, name=name, endpoint=endpoint, bucket=bucket, region=region or "us-east-1",
        prefix=normalize_prefix(prefix), mode=mode, role_arn=role_arn,
        credentials_enc=encrypt_credentials({"access_key_id": access_key_id, "secret_access_key": secret_access_key}),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        raise DataPlaneError("name_in_use", 409) from None
    await record_audit(db, action="data.byo.register", outcome="success", actor_user_id=owner_id,
                       resource_type="byo_bucket", resource_id=str(row.id), ip=ip)
    return row


async def get_owned_bucket(db: AsyncSession, owner_id: uuid.UUID, bucket_id: uuid.UUID) -> ByoBucket:
    b = (
        await db.execute(select(ByoBucket).where(ByoBucket.id == bucket_id, ByoBucket.owner_id == owner_id))
    ).scalar_one_or_none()
    if b is None or b.revoked:
        raise DataPlaneError("not_found", 404)
    return b


async def revoke_byo_bucket(db: AsyncSession, *, owner_id: uuid.UUID, bucket_id: uuid.UUID, ip: str | None = None) -> None:
    _require_byo()
    b = await get_owned_bucket(db, owner_id, bucket_id)
    b.revoked = True
    b.credentials_enc = encrypt_credentials({"access_key_id": "", "secret_access_key": ""})  # wipe the secret
    await db.flush()
    await record_audit(db, action="data.byo.revoke", outcome="success", actor_user_id=owner_id,
                       resource_type="byo_bucket", resource_id=str(b.id), ip=ip)


def _safe_subkey(sub: str) -> str:
    sub = sub or ""
    # Absolute sub-keys are refused rather than silently re-rooted: a caller that sends one is confused or probing.
    if not sub or sub.startswith("/") or ".." in sub.split("/") or "\x00" in sub:
        raise DataPlaneError("invalid_key", 422)
    return sub


async def _owned_job(db: AsyncSession, owner_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.customer_id == owner_id))).scalar_one_or_none()
    if job is None:
        raise DataPlaneError("not_found", 404)
    return job


def _ttl(ttl_seconds: int | None) -> int:
    cap = int(settings.DATA_PLANE_BYO_MAX_TTL_SECONDS)
    return max(1, min(ttl_seconds if ttl_seconds is not None else cap, cap))


async def issue_byo_presigned_url(
    db: AsyncSession, *, owner_id: uuid.UUID, bucket_id: uuid.UUID, job_id: uuid.UUID, sub_key: str,
    method: str, ttl_seconds: int | None = None, ip: str | None = None,
) -> dict:
    """A presigned URL for ONE key under <prefix>jobs/<job_id>/ in the customer's bucket. Expires fast."""
    _require_byo()
    if method not in ("GET", "PUT"):
        raise DataPlaneError("invalid_method", 422)
    await _owned_job(db, owner_id, job_id)
    b = await get_owned_bucket(db, owner_id, bucket_id)
    key = job_prefix(b, job_id) + _safe_subkey(sub_key)
    ttl = _ttl(ttl_seconds)
    await check_endpoint_resolves_public(b.endpoint)  # re-check at use: DNS may have changed since registration
    try:
        url = get_byo_client().presign(
            bucket=b, credentials=decrypt_credentials(b.credentials_enc), key=key, method=method, ttl_seconds=ttl
        )
    except DataPlaneError:
        raise
    except Exception:
        logger.exception("byo presign failed")
        raise DataPlaneError("store_unavailable", 503) from None
    await record_audit(db, action="data.byo.presign", outcome="success", actor_user_id=owner_id,
                       resource_type="byo_bucket", resource_id=str(b.id), reason=f"job={job_id} method={method}", ip=ip)
    return {"url": url, "key": key, "expires_at": utcnow() + dt.timedelta(seconds=ttl)}


async def issue_byo_scoped_credentials(
    db: AsyncSession, *, owner_id: uuid.UUID, bucket_id: uuid.UUID, job_id: uuid.UUID, read_only: bool,
    ttl_seconds: int | None = None, ip: str | None = None,
) -> ScopedCredentials:
    """Temporary credentials (STS session policy) that only reach <prefix>jobs/<job_id>/ and expire."""
    _require_byo()
    await _owned_job(db, owner_id, job_id)
    b = await get_owned_bucket(db, owner_id, bucket_id)
    if b.mode != "sts":
        raise DataPlaneError("byo_sts_not_configured", 409)
    ttl = _ttl(ttl_seconds)
    await check_endpoint_resolves_public(b.endpoint)  # re-check at use: DNS may have changed since registration
    try:
        creds = get_byo_client().scoped_credentials(
            bucket=b, credentials=decrypt_credentials(b.credentials_enc), prefix=job_prefix(b, job_id),
            read_only=read_only, ttl_seconds=ttl, session_name=f"cc-job-{job_id}",
        )
    except DataPlaneStoreError as exc:
        raise DataPlaneError(exc.code, 409) from None
    except DataPlaneError:
        raise
    except Exception:
        logger.exception("byo scoped credentials failed")
        raise DataPlaneError("store_unavailable", 503) from None
    await record_audit(db, action="data.byo.credentials", outcome="success", actor_user_id=owner_id,
                       resource_type="byo_bucket", resource_id=str(b.id), reason=f"job={job_id} ro={read_only}", ip=ip)
    return creds


# ---------------------------------------------------------------------------
# Worker sweep: expiry, retention, garbage collection, pending verification
# ---------------------------------------------------------------------------
async def _detach_deleted_files(db: AsyncSession, object_id: uuid.UUID) -> None:
    """Soft-deleted file rows keep their audit trail but stop pointing at the vanished object."""
    await db.execute(
        update(DataFile).where(DataFile.object_id == object_id, DataFile.deleted_at.is_not(None)).values(object_id=None)
    )


async def sweep_data_plane(db: AsyncSession) -> dict[str, int]:
    """One bounded pass. The caller commits. Each store call is best effort and retried next pass."""
    out = {"uploads_expired": 0, "verified": 0, "files_expired": 0, "objects_deleted": 0}
    if not settings.data_plane_configured:
        return out
    store = get_data_store()
    now = utcnow()
    batch = max(1, int(settings.DATA_PLANE_SWEEP_BATCH))

    stale = (
        await db.execute(
            select(DataUpload)
            .where(DataUpload.status.in_(UPLOAD_OPEN), DataUpload.expires_at <= now)
            .order_by(DataUpload.expires_at).limit(batch)
        )
    ).scalars().all()
    for up in stale:
        if up.status == UPLOAD_VERIFYING:
            continue  # handled below: finish the proof rather than discard bytes that were fully sent
        await abort_upload(db, up, reason=UPLOAD_EXPIRED)
        out["uploads_expired"] += 1

    pending = (
        await db.execute(select(DataUpload).where(DataUpload.status == UPLOAD_VERIFYING).order_by(DataUpload.created_at).limit(batch))
    ).scalars().all()
    for up in pending:
        await finalize_upload(db, up)
        if up.status == UPLOAD_COMPLETE:
            out["verified"] += 1

    expired = (
        await db.execute(
            select(DataFile).where(DataFile.deleted_at.is_(None), DataFile.expires_at.is_not(None), DataFile.expires_at <= now).limit(batch)
        )
    ).scalars().all()
    for f in expired:
        await _drop_file(db, f, "retention")
        out["files_expired"] += 1

    cutoff = now - dt.timedelta(seconds=int(settings.DATA_PLANE_GC_GRACE_SECONDS))
    unreferenced = (
        await db.execute(
            select(DataObject.id).where(
                DataObject.status == OBJECT_READY, DataObject.refcount == 0, DataObject.last_referenced_at <= cutoff
            ).limit(batch)
        )
    ).scalars().all()
    for oid in unreferenced:
        # Claim first (atomic): a concurrent dedup hit sees status != ready and uploads normally.
        claimed = await db.execute(
            update(DataObject).where(DataObject.id == oid, DataObject.status == OBJECT_READY, DataObject.refcount == 0)
            .values(status=OBJECT_DELETING)
        )
        if (claimed.rowcount or 0) != 1:
            continue
        obj = await db.get(DataObject, oid)
        try:
            store.delete(obj.storage_key)
        except Exception:
            logger.warning("object %s delete failed; will retry", oid)
            continue
        await _detach_deleted_files(db, oid)
        await db.delete(obj)
        await db.flush()  # sessions run with autoflush off: without this the "stuck" query below re-finds and recounts it
        out["objects_deleted"] += 1

    # Objects stuck in "deleting" (a previous pass crashed after the claim): retry the store delete.
    stuck = (
        await db.execute(select(DataObject).where(DataObject.status == OBJECT_DELETING, DataObject.refcount == 0).limit(batch))
    ).scalars().all()
    for obj in stuck:
        try:
            store.delete(obj.storage_key)
            await _detach_deleted_files(db, obj.id)
            await db.delete(obj)
            out["objects_deleted"] += 1
        except Exception:
            logger.warning("stuck object %s delete failed; will retry", obj.id)
    await db.flush()
    return out
