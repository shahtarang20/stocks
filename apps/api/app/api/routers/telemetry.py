"""Phase 9: High-volume telemetry and operational monitoring endpoints."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.db.base import ensure_aware, utcnow
from app.db.session import get_db
from app.deps import get_current_user, require_roles
from app.models.enums import Role
from app.schemas.auth import CurrentUser
from app.models.jobs import Job
from app.models.provider import Provider, ProviderMachine
from app.models.telemetry import TelemetryAlert
from app.schemas.telemetry import (
    AlertActionResponse,
    JobTelemetryBatchRequest,
    JobTelemetryBatchResponse,
    JobTelemetryQueryResponse,
    MachineTelemetryBatchRequest,
    MachineTelemetryBatchResponse,
    MachineTelemetryItem,
    MachineTelemetryQueryResponse,
    PlatformTelemetryStats,
    TelemetryAlertOut,
)
from app.services import telemetry as telemetry_service

router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])


# --------------------------------------------------------------------------- #
# Machine & Job Telemetry Ingestion (Provider Agent / Machine Auth)
# --------------------------------------------------------------------------- #
@router.post("/machine", response_model=MachineTelemetryBatchResponse)
async def ingest_machine_telemetry(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MachineTelemetryBatchResponse:
    """Ingest a batch of hardware telemetry points signed with machine HMAC."""
    machine, raw_body = await authenticate_machine(request, db)
    try:
        data = json.loads(raw_body.decode("utf-8"))
        batch = MachineTelemetryBatchRequest.model_validate(data)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_telemetry_payload",
        )

    accepted, alerts = await telemetry_service.record_machine_telemetry(
        db, machine.id, batch.points
    )
    await db.commit()
    return MachineTelemetryBatchResponse(accepted=accepted, alerts_triggered=alerts)


@router.post("/job", response_model=JobTelemetryBatchResponse)
async def ingest_job_telemetry(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobTelemetryBatchResponse:
    """Ingest a batch of job execution performance metrics."""
    machine, raw_body = await authenticate_machine(request, db)
    try:
        data = json.loads(raw_body.decode("utf-8"))
        batch = JobTelemetryBatchRequest.model_validate(data)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_telemetry_payload",
        )

    accepted = await telemetry_service.record_job_telemetry(
        db, machine.id, batch.points
    )
    await db.commit()
    return JobTelemetryBatchResponse(accepted=accepted)


# --------------------------------------------------------------------------- #
# Telemetry Query Endpoints
# --------------------------------------------------------------------------- #
@router.get("/machines/{machine_id}", response_model=MachineTelemetryQueryResponse)
async def get_machine_telemetry(
    machine_id: uuid.UUID,
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
    bucket_seconds: int = Query(default=60, ge=1, le=86400),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MachineTelemetryQueryResponse:
    """Get time-series rollups for a provider machine."""
    machine_res = await db.execute(
        select(ProviderMachine).where(ProviderMachine.id == machine_id)
    )
    machine = machine_res.scalar_one_or_none()
    if machine is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
        )

    # Authorization: machine owner or admin
    if "administrator" not in current.roles:
        prov_res = await db.execute(
            select(Provider).where(
                Provider.id == machine.provider_id, Provider.user_id == current.id
            )
        )
        if prov_res.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
            )

    return await telemetry_service.query_machine_telemetry(
        db, machine_id, start_time, end_time, bucket_seconds
    )


@router.get("/jobs/{job_id}", response_model=JobTelemetryQueryResponse)
async def get_job_telemetry(
    job_id: uuid.UUID,
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
    bucket_seconds: int = Query(default=60, ge=1, le=86400),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobTelemetryQueryResponse:
    """Get execution performance time-series for a compute job."""
    job_res = await db.execute(select(Job).where(Job.id == job_id))
    job = job_res.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )

    # Authorization: job customer owner, assigned machine provider, or admin
    if "administrator" not in current.roles and job.customer_id != current.id:
        # Check if caller is provider assigned to this job
        if job.machine_id is not None:
            mach_res = await db.execute(
                select(ProviderMachine).where(ProviderMachine.id == job.machine_id)
            )
            mach = mach_res.scalar_one_or_none()
            if mach:
                prov_res = await db.execute(
                    select(Provider).where(
                        Provider.id == mach.provider_id, Provider.user_id == current.id
                    )
                )
                if prov_res.scalar_one_or_none() is not None:
                    return await telemetry_service.query_job_telemetry(
                        db, job_id, start_time, end_time, bucket_seconds
                    )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )

    return await telemetry_service.query_job_telemetry(
        db, job_id, start_time, end_time, bucket_seconds
    )


@router.get("/stats", response_model=PlatformTelemetryStats)
async def get_platform_telemetry_stats(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlatformTelemetryStats:
    """Real-time platform-wide GPU telemetry, power metrics, and open alerts."""
    return await telemetry_service.get_platform_telemetry_stats(db)


# --------------------------------------------------------------------------- #
# Operational Alerts Management
# --------------------------------------------------------------------------- #
@router.get("/alerts", response_model=list[TelemetryAlertOut])
async def list_telemetry_alerts(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    severity: Optional[str] = Query(default=None),
    machine_id: Optional[uuid.UUID] = Query(default=None),
    job_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TelemetryAlertOut]:
    """List operational alerts with status and severity filters."""
    query = select(TelemetryAlert).order_by(TelemetryAlert.created_at.desc()).limit(limit)

    if status_filter:
        query = query.where(TelemetryAlert.status == status_filter)
    if severity:
        query = query.where(TelemetryAlert.severity == severity)
    if machine_id:
        query = query.where(TelemetryAlert.machine_id == machine_id)
    if job_id:
        query = query.where(TelemetryAlert.job_id == job_id)

    # Provider scoping: if provider, only show alerts for their machines
    if "provider" in current.roles and "administrator" not in current.roles:
        prov_res = await db.execute(
            select(Provider).where(Provider.user_id == current.id)
        )
        prov = prov_res.scalar_one_or_none()
        if prov:
            mach_ids_res = await db.execute(
                select(ProviderMachine.id).where(ProviderMachine.provider_id == prov.id)
            )
            mach_ids = mach_ids_res.scalars().all()
            query = query.where(TelemetryAlert.machine_id.in_(mach_ids))
        else:
            return []
    elif "administrator" not in current.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )

    res = await db.execute(query)
    alerts = res.scalars().all()
    return [
        TelemetryAlertOut(
            id=a.id,
            machine_id=a.machine_id,
            job_id=a.job_id,
            alert_type=a.alert_type,
            severity=a.severity,
            message=a.message,
            metric_value=a.metric_value,
            threshold=a.threshold,
            status=a.status,
            acknowledged_at=ensure_aware(a.acknowledged_at),
            resolved_at=ensure_aware(a.resolved_at),
            created_at=ensure_aware(a.created_at) or utcnow(),
        )
        for a in alerts
    ]


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertActionResponse)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertActionResponse:
    """Acknowledge an active telemetry alert."""
    alert = await telemetry_service.acknowledge_alert(db, alert_id, current.id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found"
        )
    await db.commit()
    return AlertActionResponse(
        id=alert.id,
        status=alert.status,
        updated_at=ensure_aware(alert.acknowledged_at) or utcnow(),
    )


@router.post("/alerts/{alert_id}/resolve", response_model=AlertActionResponse)
async def resolve_alert(
    alert_id: uuid.UUID,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> AlertActionResponse:
    """Resolve an operational alert (admin only)."""
    alert = await telemetry_service.resolve_alert(db, alert_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found"
        )
    await db.commit()
    return AlertActionResponse(
        id=alert.id,
        status=alert.status,
        updated_at=ensure_aware(alert.resolved_at) or utcnow(),
    )
