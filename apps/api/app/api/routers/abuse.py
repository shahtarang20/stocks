"""U16: report-abuse and admin review/takedown routes. LEGAL SIGN-OFF required on process before launch."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles
from app.models.abuse import AbuseFlag, AbuseHold, AbuseReport
from app.models.jobs import Job
from app.schemas.auth import CurrentUser
from app.services import abuse as abuse_svc
from app.services.audit import record_audit

router = APIRouter(tags=["abuse"])

Category = Literal["mining", "illegal_content", "malware", "harassment", "copyright", "other"]


class ReportIn(BaseModel):
    job_id: uuid.UUID | None = None
    category: Category
    description: str = Field(min_length=10, max_length=settings.ABUSE_REPORT_MAX_DESCRIPTION)


class ResolveIn(BaseModel):
    outcome: Literal["confirmed", "dismissed"]
    note: str = Field(default="", max_length=2000)
    release_hold: bool = False


class TakedownIn(BaseModel):
    action: Literal["takedown", "reject"]
    note: str = Field(default="", max_length=2000)


def _flag_out(f: AbuseFlag) -> dict:
    return {
        "id": str(f.id), "job_id": str(f.job_id) if f.job_id else None,
        "user_id": str(f.user_id) if f.user_id else None, "signal": f.signal,
        "detail": f.detail, "status": f.status, "enforced": f.enforced,
        "created_at": f.created_at, "reviewed_at": f.reviewed_at, "review_note": f.review_note,
    }


@router.post("/v1/abuse/report", status_code=status.HTTP_201_CREATED)
async def report_abuse(
    body: ReportIn,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not rate_limiter.allow(f"abuse_report:{current.id}", 20, 3600):  # an open form must not be a free write amplifier
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests, please try again later")
    # Any authenticated user may report; the job reference is not validated for ownership on
    # purpose (a reporter is usually a third party) and is never echoed back with job details.
    rep = AbuseReport(
        reporter_id=current.id, job_id=body.job_id, category=body.category, description=body.description
    )
    db.add(rep)
    await record_audit(
        db, action="abuse.report", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="job" if body.job_id else None, resource_id=str(body.job_id) if body.job_id else None,
    )
    await db.flush()
    return {"report_id": str(rep.id), "status": rep.status}


@router.get("/v1/admin/abuse/flags")
async def list_flags(
    status_filter: str | None = None,
    limit: int = 100,
    _: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    q = select(AbuseFlag).order_by(AbuseFlag.created_at.desc()).limit(max(1, min(limit, 500)))
    if status_filter:
        q = q.where(AbuseFlag.status == status_filter)
    rows = (await db.execute(q)).scalars().all()
    return {"flags": [_flag_out(f) for f in rows], "enforce_mode": settings.ABUSE_ENFORCE}


@router.get("/v1/admin/abuse/flags/{flag_id}")
async def get_flag(
    flag_id: uuid.UUID,
    _: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    f = await db.get(AbuseFlag, flag_id)
    if f is None:
        raise HTTPException(status_code=404, detail="flag_not_found")
    return _flag_out(f)


@router.post("/v1/admin/abuse/flags/{flag_id}/resolve")
async def resolve_flag(
    flag_id: uuid.UUID,
    body: ResolveIn,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    f = await db.get(AbuseFlag, flag_id)
    if f is None:
        raise HTTPException(status_code=404, detail="flag_not_found")
    if f.status != "open":
        raise HTTPException(status_code=409, detail="flag_already_resolved")
    f.status, f.reviewed_by, f.reviewed_at, f.review_note = body.outcome, current.id, utcnow(), body.note
    if body.release_hold or body.outcome == "dismissed":
        holds = (
            await db.execute(
                select(AbuseHold).where(AbuseHold.flag_id == f.id, AbuseHold.released_at.is_(None))
            )
        ).scalars().all()
        for h in holds:
            h.released_at, h.released_by = utcnow(), current.id
    await record_audit(
        db, action="abuse.flag.resolve", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="abuse_flag", resource_id=str(f.id), reason=body.outcome,
    )
    await db.flush()
    return _flag_out(f)


@router.post("/v1/admin/abuse/users/{user_id}/release-hold")
async def release_hold(
    user_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    holds = (
        await db.execute(
            select(AbuseHold).where(AbuseHold.user_id == user_id, AbuseHold.released_at.is_(None))
        )
    ).scalars().all()
    for h in holds:
        h.released_at, h.released_by = utcnow(), current.id
    await record_audit(
        db, action="abuse.hold.release", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="user", resource_id=str(user_id),
    )
    await db.flush()
    return {"released": len(holds)}


@router.get("/v1/admin/abuse/reports")
async def list_reports(
    status_filter: str | None = None,
    limit: int = 100,
    _: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    q = select(AbuseReport).order_by(AbuseReport.created_at.desc()).limit(max(1, min(limit, 500)))
    if status_filter:
        q = q.where(AbuseReport.status == status_filter)
    rows = (await db.execute(q)).scalars().all()
    return {"reports": [
        {"id": str(r.id), "job_id": str(r.job_id) if r.job_id else None, "category": r.category,
         "description": r.description, "status": r.status, "created_at": r.created_at}
        for r in rows
    ]}


@router.post("/v1/admin/abuse/reports/{report_id}/takedown")
async def action_report(
    report_id: uuid.UUID,
    body: TakedownIn,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rep = await db.get(AbuseReport, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report_not_found")
    if rep.status != "open":
        raise HTTPException(status_code=409, detail="report_already_actioned")
    if body.action == "takedown" and rep.job_id is not None:
        job = (await db.execute(select(Job).where(Job.id == rep.job_id).with_for_update())).scalar_one_or_none()
        if job is not None:
            await abuse_svc.stop_job(
                db, job, reason="takedown", actor="admin", error_code="takedown",
                error_message="stopped following an abuse report",
            )
    rep.status = "actioned" if body.action == "takedown" else "rejected"
    rep.actioned_by, rep.actioned_at, rep.action_note = current.id, utcnow(), body.note
    await record_audit(
        db, action=f"abuse.report.{body.action}", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="abuse_report", resource_id=str(rep.id),
    )
    await db.flush()
    return {"report_id": str(rep.id), "status": rep.status}
