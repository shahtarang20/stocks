"""Kit registry routes (U1): public reads, admin publish/yank, v2 spec validation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles
from app.models.kits import Kit
from app.schemas.auth import CurrentUser
from app.services import kits as kit_svc
from app.services.audit import record_audit

router = APIRouter(prefix="/v1/kits", tags=["kits"])
admin_router = APIRouter(prefix="/v1/admin/kits", tags=["admin-kits"])


class PublishKitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]
    signature: str


class KitSummary(BaseModel):
    slug: str
    version: str
    name: str
    category: str
    status: str
    image_digest: str
    # batch (ordinary job) | session (interactive terminal, started from Sessions) | service (always-on endpoint)
    mode: str = "batch"


class KitDetail(KitSummary):
    manifest: dict[str, Any]
    manifest_sha256: str
    signature: str
    signing_key_id: str


def _summary(k: Kit) -> dict:
    return {"slug": k.slug, "version": k.version, "name": k.name, "category": k.category,
            "status": k.status, "image_digest": k.image_digest, "mode": (k.manifest or {}).get("mode", "batch")}


def _detail(k: Kit) -> dict:
    return {**_summary(k), "manifest": k.manifest, "manifest_sha256": k.manifest_sha256,
            "signature": k.signature, "signing_key_id": k.signing_key_id}


def _raise(exc: kit_svc.KitError):
    raise HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})


@router.get("", response_model=list[KitSummary])
async def list_kits(category: str | None = None, db: AsyncSession = Depends(get_db)):
    q = select(Kit).where(Kit.status == "published").order_by(Kit.slug, Kit.created_at.desc())
    if category:
        q = q.where(Kit.category == category)
    latest: dict[str, Kit] = {}
    for k in (await db.execute(q)).scalars():
        cur = latest.get(k.slug)
        if cur is None or ensure_aware(k.created_at) > ensure_aware(cur.created_at):
            latest[k.slug] = k
    return [_summary(k) for k in latest.values()]


@router.get("/{slug}", response_model=list[KitSummary])
async def list_versions(slug: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Kit).where(Kit.slug == slug).order_by(Kit.created_at.desc()))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="kit_not_found")
    return [_summary(k) for k in rows]


@router.get("/{slug}/{version}", response_model=KitDetail)
async def read_kit(slug: str, version: str, db: AsyncSession = Depends(get_db)):
    k = await kit_svc.get_kit(db, slug, version)
    if k is None:
        raise HTTPException(status_code=404, detail="kit_not_found")
    return _detail(k)


@router.post("/validate-spec")
async def validate_spec(
    body: dict[str, Any],
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Validate a Job Spec v2 without creating a job. 422 carries per-field errors."""
    try:
        v = await kit_svc.validate_job_spec_v2(db, body)
    except kit_svc.KitError as exc:
        _raise(exc)
    return {"valid": True, "kit": f"{v.kit.slug}@{v.kit.version}", "effective_resources": v.effective_resources}


@admin_router.post("", response_model=KitDetail, status_code=201)
async def publish(
    body: PublishKitRequest,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
):
    try:
        k = await kit_svc.publish_kit(db, manifest=body.manifest, signature=body.signature, published_by=current.id)
    except kit_svc.KitError as exc:
        await record_audit(db, action="kit.publish", outcome="denied", actor_user_id=current.id,
                           ip=client_ip(request), resource_type="kit", reason=exc.code)
        await db.commit()
        _raise(exc)
    await record_audit(db, action="kit.publish", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="kit", resource_id=f"{k.slug}@{k.version}", reason=k.image_digest)
    await db.flush()
    return _detail(k)


@admin_router.post("/{slug}/{version}/yank", response_model=KitSummary)
async def yank(
    slug: str,
    version: str,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
):
    try:
        k = await kit_svc.yank_kit(db, slug, version)
    except kit_svc.KitError as exc:
        _raise(exc)
    await record_audit(db, action="kit.yank", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="kit", resource_id=f"{slug}@{version}")
    await db.flush()
    return _summary(k)
