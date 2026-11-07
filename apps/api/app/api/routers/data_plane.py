"""Data plane (U2). OFF by default (DATA_PLANE_ENABLED). File bytes never pass through these endpoints.

POST   /v1/data/uploads                              start (Idempotency-Key required); may dedupe instantly
GET    /v1/data/uploads/{id}                         resume state (which parts are proven)
POST   /v1/data/uploads/{id}/parts/{n}/url           presigned PUT for one part (body: sha256, size)
POST   /v1/data/uploads/{id}/parts/{n}/confirm       check the stored part; 409 {"action":"resend"} if corrupt
POST   /v1/data/dedup-proof/start                    (cross-account dedup ON only) challenge + 4 byte ranges for a hash
POST   /v1/data/dedup-proof/verify                   (same) check the client's digest; returns a receipt for POST /uploads
POST   /v1/data/uploads/{id}/complete                assemble + whole-file proof
DELETE /v1/data/uploads/{id}                         abort
GET    /v1/data/files                                the caller's files
POST   /v1/data/files/{id}/download-link             scoped, expiring presigned GET
DELETE /v1/data/files/{id}                           delete (drops a reference)
GET    /v1/data/usage                                quota and usage
POST   /v1/data/byo-buckets                          register a customer bucket (credentials write-only)
GET    /v1/data/byo-buckets                          list (never returns credentials)
DELETE /v1/data/byo-buckets/{id}                     revoke and wipe credentials
POST   /v1/data/byo-buckets/{id}/jobs/{job_id}/url   presigned URL for one key under that job's prefix
POST   /v1/data/byo-buckets/{id}/jobs/{job_id}/credentials  STS credentials limited to that job's prefix
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.deps import client_ip, require_verified_user
from app.models.data_plane import PART_CORRUPT, ByoBucket, DataFile, DataUpload
from app.schemas.auth import CurrentUser
from app.services import data_plane as svc

router = APIRouter(prefix="/v1/data", tags=["data-plane"])


class UploadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)
    retention_days: int | None = Field(default=None, ge=0, le=36500)
    dedup_proof: str | None = Field(default=None, max_length=2048)  # receipt from /dedup-proof/verify, else ignored


class DedupProofStartIn(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)


class DedupProofVerifyIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    proof_sha256: str = Field(min_length=64, max_length=64)


class PartUrlIn(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)


class DownloadIn(BaseModel):
    ttl_seconds: int | None = Field(default=None, ge=1)


class FileOut(BaseModel):
    id: uuid.UUID
    name: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: dt.datetime
    expires_at: dt.datetime | None


class UploadOut(BaseModel):
    id: uuid.UUID
    status: str
    size_bytes: int
    sha256: str
    part_size: int
    part_count: int
    expires_at: dt.datetime
    error_code: str | None = None
    deduplicated: bool = False
    file: FileOut | None = None


class ByoCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(default="", max_length=512)
    bucket: str = Field(min_length=1, max_length=128)
    region: str = Field(default="us-east-1", max_length=64)
    prefix: str = Field(min_length=1, max_length=256)
    mode: str = Field(default="presign")
    role_arn: str | None = Field(default=None, max_length=256)
    access_key_id: str = Field(min_length=1, max_length=256, repr=False)
    secret_access_key: str = Field(min_length=1, max_length=512, repr=False)


class ByoOut(BaseModel):
    id: uuid.UUID
    name: str
    endpoint: str
    bucket: str
    region: str
    prefix: str
    mode: str
    created_at: dt.datetime


class ByoUrlIn(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    method: str = Field(default="GET")
    ttl_seconds: int | None = Field(default=None, ge=1)


class ByoCredsIn(BaseModel):
    read_only: bool = True
    ttl_seconds: int | None = Field(default=None, ge=1)


def _file_out(f: DataFile) -> FileOut:
    return FileOut(id=f.id, name=f.name, content_type=f.content_type, size_bytes=f.size_bytes, sha256=f.sha256,
                   created_at=f.created_at, expires_at=f.expires_at)


def _upload_out(up: DataUpload, file: DataFile | None, dedup: bool = False) -> UploadOut:
    return UploadOut(id=up.id, status=up.status, size_bytes=up.size_bytes, sha256=up.sha256, part_size=up.part_size,
                     part_count=up.part_count, expires_at=up.expires_at, error_code=up.error_code, deduplicated=dedup,
                     file=_file_out(file) if file else None)


def _raise(exc: svc.DataPlaneError):
    raise HTTPException(exc.http_status, exc.code) from None


def _limit(user: CurrentUser, bucket: str, n: int = 600, window: int = 3600) -> None:
    if not rate_limiter.allow(f"data:{bucket}:{user.id}", n, window):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, please try again later")


@router.post("/uploads", response_model=UploadOut)
async def create_upload(
    body: UploadCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    _limit(current, "create", 120)
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "idempotency_key_required")
    try:
        up, file = await svc.create_upload(
            db, owner_id=current.id, idempotency_key=idempotency_key, name=body.name, content_type=body.content_type,
            sha256=body.sha256, size_bytes=body.size_bytes, retention_days=body.retention_days, ip=client_ip(request),
            dedup_proof=body.dedup_proof,
        )
    except svc.DataPlaneError as exc:
        _raise(exc)
    return _upload_out(up, file, dedup=up.store_upload_id == "dedup")


@router.post("/dedup-proof/start")
async def dedup_proof_start(body: DedupProofStartIn, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    """A challenge and four server-chosen byte ranges. The answer looks the same whether or not the hash is stored."""
    _limit(current, "dedup_proof", 120)
    try:
        return await svc.start_dedup_proof(db, owner_id=current.id, sha256=body.sha256, size_bytes=body.size_bytes)
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.post("/dedup-proof/verify")
async def dedup_proof_verify(
    body: DedupProofVerifyIn, request: Request, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    """Verify sha256(challenge || the four ranges). Every failure is the same 403, so it never confirms a hash exists."""
    _limit(current, "dedup_proof", 120)
    try:
        return await svc.verify_dedup_proof(
            db, owner_id=current.id, token=body.token, proof_sha256=body.proof_sha256, ip=client_ip(request)
        )
    except svc.DataPlaneError as exc:
        if exc.code in svc.PERSIST_ON_ERROR:
            await db.commit()  # keep the failure's audit entry: the error response would otherwise roll it back
        _raise(exc)


@router.get("/uploads/{upload_id}")
async def get_upload(upload_id: uuid.UUID, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        svc.require_enabled()
        up = await svc.get_owned_upload(db, current.id, upload_id)
    except svc.DataPlaneError as exc:
        _raise(exc)
    st = await svc.upload_state(db, up)
    file = await db.get(DataFile, up.file_id) if up.file_id else None
    return {
        "upload": _upload_out(up, file, dedup=up.store_upload_id == "dedup"),
        "verified_parts": st["verified_parts"],
        "corrupt_parts": st["corrupt_parts"],
        "pending_parts": st["pending_parts"],
        "missing_parts": st["missing_parts"],
    }


@router.post("/uploads/{upload_id}/parts/{part_number}/url")
async def part_url(
    upload_id: uuid.UUID, part_number: int, body: PartUrlIn,
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    _limit(current, "part", 20000)
    try:
        up = await svc.get_owned_upload(db, current.id, upload_id)
        return await svc.issue_part_url(db, up, part_number=part_number, sha256=body.sha256, size_bytes=body.size_bytes)
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.post("/uploads/{upload_id}/parts/{part_number}/confirm")
async def confirm_part(
    upload_id: uuid.UUID, part_number: int,
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    _limit(current, "confirm", 20000)
    try:
        up = await svc.get_owned_upload(db, current.id, upload_id)
        part = await svc.confirm_part(db, up, part_number=part_number)
    except svc.DataPlaneError as exc:
        _raise(exc)
    if part.status == PART_CORRUPT:
        await db.commit()  # keep the corrupt marker; the error response would otherwise roll it back
        return JSONResponse(status_code=409, content={"detail": "part_corrupt", "action": "resend", "part_number": part_number})
    return {"part_number": part_number, "status": part.status}


@router.post("/uploads/{upload_id}/complete", response_model=UploadOut)
async def complete(upload_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    _limit(current, "complete", 600)
    try:
        up = await svc.get_owned_upload(db, current.id, upload_id)
        up, file = await svc.complete_upload(db, up, ip=client_ip(request))
    except svc.DataPlaneError as exc:
        if exc.code in svc.PERSIST_ON_ERROR:
            await db.commit()  # keep the corrupt-part marker: the error response would otherwise roll it back
        _raise(exc)
    return _upload_out(up, file, dedup=up.store_upload_id == "dedup")


@router.delete("/uploads/{upload_id}", status_code=204)
async def abort(upload_id: uuid.UUID, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        svc.require_enabled()
        up = await svc.get_owned_upload(db, current.id, upload_id)
        await svc.abort_upload(db, up)
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.get("/files", response_model=list[FileOut])
async def list_files(
    limit: int = Query(50, ge=1, le=200),
    name_prefix: str | None = Query(None, max_length=255, description="only files whose name starts with this, e.g. out/<job_id>/"),
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    if not settings.DATA_PLANE_ENABLED:
        raise HTTPException(404, "data_plane_disabled")
    now = dt.datetime.now(dt.timezone.utc)
    stmt = select(DataFile).where(DataFile.owner_id == current.id, DataFile.deleted_at.is_(None))
    if name_prefix:
        # A job's outputs are stored as out/<job_id>/<name>; the web console asks for exactly those. Owner-scoped above.
        stmt = stmt.where(DataFile.name.startswith(name_prefix, autoescape=True))
    rows = (await db.execute(stmt.order_by(DataFile.created_at.desc()).limit(limit))).scalars().all()
    return [_file_out(f) for f in rows if f.expires_at is None or svc._aware(f.expires_at) > now]


@router.post("/files/{file_id}/download-link")
async def download_link(
    file_id: uuid.UUID, request: Request, body: DownloadIn | None = None,
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    _limit(current, "download", 600)
    try:
        return await svc.create_download_link(
            db, owner_id=current.id, file_id=file_id, ttl_seconds=body.ttl_seconds if body else None, ip=client_ip(request)
        )
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(file_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        await svc.delete_file(db, owner_id=current.id, file_id=file_id, ip=client_ip(request))
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.get("/usage")
async def usage(current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    if not settings.DATA_PLANE_ENABLED:
        raise HTTPException(404, "data_plane_disabled")
    return {
        "used_bytes": await svc.account_usage_bytes(db, current.id),
        "quota_bytes": int(settings.DATA_PLANE_ACCOUNT_QUOTA_BYTES),
        "max_file_bytes": int(settings.DATA_PLANE_MAX_FILE_BYTES),
    }


# ---------------------------------------------------------------------------
# Bring your own bucket
# ---------------------------------------------------------------------------
def _byo_out(b: ByoBucket) -> ByoOut:
    return ByoOut(id=b.id, name=b.name, endpoint=b.endpoint, bucket=b.bucket, region=b.region, prefix=b.prefix,
                  mode=b.mode, created_at=b.created_at)


@router.post("/byo-buckets", response_model=ByoOut)
async def register_bucket(body: ByoCreate, request: Request, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    _limit(current, "byo", 60)
    try:
        b = await svc.register_byo_bucket(
            db, owner_id=current.id, name=body.name, endpoint=body.endpoint, bucket=body.bucket, region=body.region,
            prefix=body.prefix, mode=body.mode, role_arn=body.role_arn, access_key_id=body.access_key_id,
            secret_access_key=body.secret_access_key, ip=client_ip(request),
        )
    except svc.DataPlaneError as exc:
        _raise(exc)
    return _byo_out(b)


@router.get("/byo-buckets", response_model=list[ByoOut])
async def list_buckets(current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    if not (settings.DATA_PLANE_ENABLED and settings.DATA_PLANE_BYO_ENABLED):
        raise HTTPException(404, "byo_disabled")
    rows = (
        await db.execute(select(ByoBucket).where(ByoBucket.owner_id == current.id, ByoBucket.revoked.is_(False)).order_by(ByoBucket.created_at))
    ).scalars().all()
    return [_byo_out(b) for b in rows]


@router.delete("/byo-buckets/{bucket_id}", status_code=204)
async def revoke_bucket(bucket_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        await svc.revoke_byo_bucket(db, owner_id=current.id, bucket_id=bucket_id, ip=client_ip(request))
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.post("/byo-buckets/{bucket_id}/jobs/{job_id}/url")
async def byo_url(
    bucket_id: uuid.UUID, job_id: uuid.UUID, body: ByoUrlIn, request: Request,
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    _limit(current, "byo_url", 2000)
    try:
        return await svc.issue_byo_presigned_url(
            db, owner_id=current.id, bucket_id=bucket_id, job_id=job_id, sub_key=body.key, method=body.method.upper(),
            ttl_seconds=body.ttl_seconds, ip=client_ip(request),
        )
    except svc.DataPlaneError as exc:
        _raise(exc)


@router.post("/byo-buckets/{bucket_id}/jobs/{job_id}/credentials")
async def byo_credentials(
    bucket_id: uuid.UUID, job_id: uuid.UUID, body: ByoCredsIn, request: Request,
    current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    _limit(current, "byo_creds", 120)
    try:
        c = await svc.issue_byo_scoped_credentials(
            db, owner_id=current.id, bucket_id=bucket_id, job_id=job_id, read_only=body.read_only,
            ttl_seconds=body.ttl_seconds, ip=client_ip(request),
        )
    except svc.DataPlaneError as exc:
        _raise(exc)
    return {
        "access_key_id": c.access_key_id, "secret_access_key": c.secret_access_key, "session_token": c.session_token,
        "expires_at": c.expires_at, "bucket": c.bucket, "prefix": c.prefix, "read_only": c.read_only,
    }
