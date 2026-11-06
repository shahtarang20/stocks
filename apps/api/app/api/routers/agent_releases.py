"""Agent release management routes (W6/W10).

Admin: publish a signed manifest, list, pause / resume, roll back (optionally with a signed replacement), assign a machine's ring.
Host: ``GET /v1/agents/releases/manifest`` (machine HMAC) returns the signed manifest this host may take, or 204.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.db.session import get_db
from app.deps import client_ip, require_roles
from app.models.agent_releases import AgentRelease
from app.schemas.auth import CurrentUser
from app.services import agent_releases as svc
from app.services.audit import record_audit

admin_router = APIRouter(prefix="/v1/admin/agent-releases", tags=["admin-agent-releases"])
host_router = APIRouter(prefix="/v1/agents/releases", tags=["agent-releases"])


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]
    signature: str


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = None


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = None
    replacement: Optional[PublishRequest] = None


class RingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ring: str


def _summary(r: AgentRelease) -> dict:
    return {
        "id": str(r.id), "platform": r.platform, "version": r.version, "ring": r.ring, "min_supported": r.min_supported,
        "artifact_sha256": r.artifact_sha256, "status": r.status, "status_reason": r.status_reason,
        "signing_key_id": r.signing_key_id, "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _raise(exc: svc.ReleaseError):
    raise HTTPException(status_code=exc.http_status, detail=exc.code)


@admin_router.post("", status_code=201)
async def publish(
    body: PublishRequest, request: Request,
    current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db),
):
    try:
        row = await svc.publish_release(db, manifest=body.manifest, signature=body.signature, published_by=current.id)
    except svc.ReleaseError as exc:
        await record_audit(db, action="agent_release.publish", outcome="denied", actor_user_id=current.id,
                           ip=client_ip(request), resource_type="agent_release", reason=exc.code)
        await db.commit()
        _raise(exc)
    await record_audit(db, action="agent_release.publish", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="agent_release", resource_id=f"{row.platform}:{row.version}:{row.ring}")
    await db.flush()
    return _summary(row)


@admin_router.get("")
async def list_releases(
    platform: Optional[str] = None, ring: Optional[str] = None, status: Optional[str] = None,
    current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db),
):
    q = select(AgentRelease).order_by(AgentRelease.created_at.desc())
    if platform:
        q = q.where(AgentRelease.platform == platform)
    if ring:
        q = q.where(AgentRelease.ring == ring)
    if status:
        q = q.where(AgentRelease.status == status)
    return [_summary(r) for r in (await db.execute(q)).scalars().all()]


async def _change(release_id: uuid.UUID, status: str, action: str, body: StatusRequest, request: Request,
                  current: CurrentUser, db: AsyncSession) -> dict:
    try:
        row = await svc.set_status(db, release_id, status, by=current.id, reason=body.reason)
    except svc.ReleaseError as exc:
        _raise(exc)
    await record_audit(db, action=action, outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="agent_release", resource_id=str(row.id), reason=body.reason)
    await db.flush()
    return _summary(row)


@admin_router.post("/{release_id}/pause")
async def pause(release_id: uuid.UUID, request: Request, body: StatusRequest = StatusRequest(),
                current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db)):
    return await _change(release_id, "paused", "agent_release.pause", body, request, current, db)


@admin_router.post("/{release_id}/resume")
async def resume(release_id: uuid.UUID, request: Request, body: StatusRequest = StatusRequest(),
                 current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db)):
    return await _change(release_id, "published", "agent_release.resume", body, request, current, db)


@admin_router.post("/{release_id}/rollback")
async def rollback(release_id: uuid.UUID, request: Request, body: RollbackRequest = RollbackRequest(),
                   current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db)):
    try:
        row, new_row = await svc.rollback_release(
            db, release_id, by=current.id, reason=body.reason,
            replacement=body.replacement.model_dump() if body.replacement else None,
        )
    except svc.ReleaseError as exc:
        await db.rollback()
        _raise(exc)
    await record_audit(db, action="agent_release.rollback", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="agent_release", resource_id=str(row.id), reason=body.reason)
    await db.flush()
    return {"rolled_back": _summary(row), "replacement": _summary(new_row) if new_row else None}


@admin_router.put("/rings/{machine_id}")
async def put_ring(machine_id: uuid.UUID, body: RingRequest, request: Request,
                   current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db)):
    from app.models.provider import ProviderMachine

    if await db.get(ProviderMachine, machine_id) is None:
        raise HTTPException(status_code=404, detail="machine_not_found")
    try:
        row = await svc.assign_ring(db, machine_id, body.ring, by=current.id)
    except svc.ReleaseError as exc:
        _raise(exc)
    await record_audit(db, action="agent_release.ring_assign", outcome="success", actor_user_id=current.id,
                       ip=client_ip(request), resource_type="machine", resource_id=str(machine_id), reason=row.ring)
    await db.flush()
    return {"machine_id": str(machine_id), "ring": row.ring}


@host_router.get("/manifest")
async def host_manifest(request: Request, platform: str, current_version: str, ring: Optional[str] = None,
                        db: AsyncSession = Depends(get_db)):
    """The signed manifest this host may install, or 204. ``ring`` is the host's report: it can only narrow what it gets."""
    machine, _ = await authenticate_machine(request, db)
    try:
        host_ring = svc.effective_ring(await svc.assigned_ring(db, machine.id), ring)
        row = await svc.select_release(db, platform=platform, host_ring=host_ring, current_version=current_version)
    except svc.ReleaseError as exc:
        _raise(exc)
    if row is None:
        return Response(status_code=204)
    return svc.served_manifest(row)
