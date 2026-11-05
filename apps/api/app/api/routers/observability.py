"""U19 observability endpoints: agent progress, live job view (poll + SSE), ops metrics."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.core.config import settings
from app.db.session import get_db, get_sessionmaker
from app.deps import get_current_user
from app.models.jobs import Job
from app.schemas.auth import CurrentUser
from app.schemas.observability import (
    JobLiveOut,
    JobProgressBatchRequest,
    JobProgressBatchResponse,
)
from app.services import observability as obs

router = APIRouter(tags=["observability"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")


@router.post("/v1/telemetry/job/progress", response_model=JobProgressBatchResponse)
async def ingest_job_progress(
    request: Request, db: AsyncSession = Depends(get_db)
) -> JobProgressBatchResponse:
    """Machine-signed progress reports; only for jobs running on the calling machine."""
    machine, raw = await authenticate_machine(request, db)
    try:
        batch = JobProgressBatchRequest.model_validate(json.loads(raw.decode("utf-8")))
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_progress_payload")
    accepted, dropped = await obs.record_job_progress(db, machine.id, batch.points)
    await db.commit()
    return JobProgressBatchResponse(accepted=accepted, dropped=dropped)


async def _own_job(db: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = (
        await db.execute(select(Job).where(Job.id == job_id, Job.customer_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        raise _NOT_FOUND  # others' jobs are indistinguishable from missing
    return job


@router.get("/v1/jobs/{job_id}/live", response_model=JobLiveOut)
async def job_live(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobLiveOut:
    job = await _own_job(db, current.id, job_id)
    return await obs.get_job_live(db, job)


@router.get("/v1/jobs/{job_id}/live/stream")
async def job_live_stream(
    job_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    await _own_job(db, current.id, job_id)  # authorize once up front
    user_id = current.id
    maker = get_sessionmaker()

    async def gen():
        deadline = time.monotonic() + settings.JOB_LIVE_STREAM_MAX_SECONDS
        terminal = {"completed", "failed", "timed_out", "cancelled", "disputed"}
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            async with maker() as s:
                job = await _own_job(s, user_id, job_id)
                live = await obs.get_job_live(s, job)
            yield f"data: {live.model_dump_json()}\n\n"
            if live.status in terminal:
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(settings.JOB_LIVE_STREAM_INTERVAL_SECONDS)
        yield "event: reconnect\ndata: {}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/v1/ops/metrics")
async def ops_metrics(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    token = settings.OBSERVABILITY_METRICS_TOKEN
    if not token:
        raise HTTPException(status_code=404, detail="not_found")  # default off
    if not hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="metrics_unauthorized")
    return Response(await obs.render_prometheus(db), media_type="text/plain; version=0.0.4")
