"""Phase 9: High-volume time-series telemetry and operational alerting service.

Handles batch ingestion, threshold violation checks, time-window rollups,
and platform-wide health monitoring.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.jobs import Job
from app.models.telemetry import JobTelemetry, MachineTelemetry, TelemetryAlert

# Hard cap on rows materialized by a single telemetry query (DoS guard).
_MAX_QUERY_ROWS = 50_000
from app.schemas.telemetry import (
    JobMetricBucketPoint,
    JobTelemetryItem,
    JobTelemetryQueryResponse,
    MachineTelemetryItem,
    MachineTelemetryQueryResponse,
    MetricBucketPoint,
    PlatformTelemetryStats,
)


# --------------------------------------------------------------------------- #
# Ingestion & Alert Detection
# --------------------------------------------------------------------------- #
async def record_machine_telemetry(
    db: AsyncSession,
    machine_id: uuid.UUID,
    points: list[MachineTelemetryItem],
) -> tuple[int, int]:
    """Ingest a batch of machine telemetry points and trigger threshold alerts."""
    now = utcnow()
    telemetry_objects: list[MachineTelemetry] = []
    alerts_to_create: list[TelemetryAlert] = []

    for pt in points:
        ts = ensure_aware(pt.timestamp) or now
        obj = MachineTelemetry(
            machine_id=machine_id,
            timestamp=ts,
            gpu_index=pt.gpu_index,
            gpu_util_pct=pt.gpu_util_pct,
            mem_util_pct=pt.mem_util_pct,
            mem_used_mb=pt.mem_used_mb,
            mem_total_mb=pt.mem_total_mb,
            temp_c=pt.temp_c,
            power_w=pt.power_w,
            fan_speed_pct=pt.fan_speed_pct,
            extra_metrics=pt.extra_metrics,
        )
        telemetry_objects.append(obj)

        # 1. Overheating check
        if pt.temp_c is not None and pt.temp_c >= settings.ALERT_TEMP_THRESHOLD_C:
            sev = "critical" if pt.temp_c >= 90.0 else "warning"
            alerts_to_create.append(
                TelemetryAlert(
                    machine_id=machine_id,
                    alert_type="high_temperature",
                    severity=sev,
                    message=f"GPU {pt.gpu_index} temperature high: {pt.temp_c:.1f}°C (threshold: {settings.ALERT_TEMP_THRESHOLD_C}°C)",
                    metric_value=pt.temp_c,
                    threshold=settings.ALERT_TEMP_THRESHOLD_C,
                    status="active",
                    created_at=ts,
                )
            )

        # 2. Memory exhaustion check
        if pt.mem_util_pct is not None and pt.mem_util_pct >= settings.ALERT_MEM_UTIL_THRESHOLD_PCT:
            alerts_to_create.append(
                TelemetryAlert(
                    machine_id=machine_id,
                    alert_type="memory_exhaustion",
                    severity="warning",
                    message=f"GPU {pt.gpu_index} VRAM utilization critical: {pt.mem_util_pct:.1f}% (threshold: {settings.ALERT_MEM_UTIL_THRESHOLD_PCT}%)",
                    metric_value=pt.mem_util_pct,
                    threshold=settings.ALERT_MEM_UTIL_THRESHOLD_PCT,
                    status="active",
                    created_at=ts,
                )
            )

    db.add_all(telemetry_objects)

    created_alerts_count = 0
    if alerts_to_create:
        # Deduplicate alerts if an active alert for the machine & type was fired within the last 5m
        recent_threshold = now - timedelta(minutes=5)
        for alert in alerts_to_create:
            recent_alert = await db.execute(
                select(TelemetryAlert.id).where(
                    TelemetryAlert.machine_id == alert.machine_id,
                    TelemetryAlert.alert_type == alert.alert_type,
                    TelemetryAlert.status == "active",
                    TelemetryAlert.created_at >= recent_threshold,
                )
            )
            if recent_alert.scalar_one_or_none() is None:
                db.add(alert)
                created_alerts_count += 1

    await db.flush()
    return len(telemetry_objects), created_alerts_count


async def record_job_telemetry(
    db: AsyncSession,
    machine_id: uuid.UUID | None,
    points: list[JobTelemetryItem],
) -> int:
    """Ingest a batch of execution telemetry points for jobs.

    Telemetry is only accepted for jobs currently assigned to the authenticated
    machine; points referencing another machine's (or an unknown) job are dropped
    so a machine cannot inject telemetry into jobs it does not run.
    """
    now = utcnow()
    telemetry_objects: list[JobTelemetry] = []

    owned_job_ids: set[uuid.UUID] = set()
    if machine_id is not None:
        requested = {pt.job_id for pt in points if pt.job_id is not None}
        if requested:
            owned_job_ids = set(
                (
                    await db.execute(
                        select(Job.id).where(
                            Job.id.in_(requested), Job.machine_id == machine_id
                        )
                    )
                ).scalars().all()
            )

    for pt in points:
        if pt.job_id not in owned_job_ids:
            continue  # cross-tenant / unknown job -> drop
        ts = ensure_aware(pt.timestamp) or now
        telemetry_objects.append(
            JobTelemetry(
                job_id=pt.job_id,
                machine_id=machine_id,
                timestamp=ts,
                gpu_util_pct=pt.gpu_util_pct,
                mem_used_mb=pt.mem_used_mb,
                tokens_per_sec=pt.tokens_per_sec,
                step_count=pt.step_count,
                loss=pt.loss,
                extra_metrics=pt.extra_metrics,
            )
        )

    db.add_all(telemetry_objects)
    await db.flush()
    return len(telemetry_objects)


# --------------------------------------------------------------------------- #
# Query & Bucketing Rollups
# --------------------------------------------------------------------------- #
async def query_machine_telemetry(
    db: AsyncSession,
    machine_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    bucket_seconds: int = 60,
) -> MachineTelemetryQueryResponse:
    """Aggregate machine telemetry into bucketed rollups over a time window."""
    start_utc = ensure_aware(start_time) or (utcnow() - timedelta(hours=1))
    end_utc = ensure_aware(end_time) or utcnow()
    if bucket_seconds < 1:
        bucket_seconds = 60

    result = await db.execute(
        select(MachineTelemetry)
        .where(
            MachineTelemetry.machine_id == machine_id,
            MachineTelemetry.timestamp >= start_utc,
            MachineTelemetry.timestamp <= end_utc,
        )
        .order_by(MachineTelemetry.timestamp.asc())
        .limit(_MAX_QUERY_ROWS)
    )
    rows = result.scalars().all()

    buckets: dict[int, list[MachineTelemetry]] = defaultdict(list)
    for r in rows:
        ts_aware = ensure_aware(r.timestamp)
        epoch = int(ts_aware.timestamp()) if ts_aware else 0
        bucket_key = (epoch // bucket_seconds) * bucket_seconds
        buckets[bucket_key].append(r)

    points: list[MetricBucketPoint] = []
    for bucket_key in sorted(buckets.keys()):
        pts = buckets[bucket_key]
        utils = [p.gpu_util_pct for p in pts if p.gpu_util_pct is not None]
        temps = [p.temp_c for p in pts if p.temp_c is not None]
        mems = [p.mem_used_mb for p in pts if p.mem_used_mb is not None]
        powers = [p.power_w for p in pts if p.power_w is not None]

        points.append(
            MetricBucketPoint(
                timestamp=datetime.fromtimestamp(bucket_key, tz=timezone.utc),
                avg_gpu_util_pct=round(sum(utils) / len(utils), 2) if utils else None,
                max_gpu_util_pct=round(max(utils), 2) if utils else None,
                avg_temp_c=round(sum(temps) / len(temps), 2) if temps else None,
                max_temp_c=round(max(temps), 2) if temps else None,
                avg_mem_used_mb=int(sum(mems) / len(mems)) if mems else None,
                avg_power_w=round(sum(powers) / len(powers), 2) if powers else None,
                samples=len(pts),
            )
        )

    # Latest current snapshot
    latest_pt = rows[-1] if rows else None
    current_data = None
    if latest_pt is not None:
        current_data = {
            "timestamp": ensure_aware(latest_pt.timestamp).isoformat() if latest_pt.timestamp else None,
            "gpu_index": latest_pt.gpu_index,
            "gpu_util_pct": latest_pt.gpu_util_pct,
            "mem_util_pct": latest_pt.mem_util_pct,
            "mem_used_mb": latest_pt.mem_used_mb,
            "mem_total_mb": latest_pt.mem_total_mb,
            "temp_c": latest_pt.temp_c,
            "power_w": latest_pt.power_w,
        }

    return MachineTelemetryQueryResponse(
        machine_id=machine_id,
        start_time=start_utc,
        end_time=end_utc,
        bucket_seconds=bucket_seconds,
        points=points,
        current=current_data,
    )


async def query_job_telemetry(
    db: AsyncSession,
    job_id: uuid.UUID,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    bucket_seconds: int = 60,
) -> JobTelemetryQueryResponse:
    """Aggregate job execution telemetry into time-bucketed points."""
    query = select(JobTelemetry).where(JobTelemetry.job_id == job_id)
    if start_time is not None:
        query = query.where(JobTelemetry.timestamp >= ensure_aware(start_time))
    if end_time is not None:
        query = query.where(JobTelemetry.timestamp <= ensure_aware(end_time))
    query = query.order_by(JobTelemetry.timestamp.asc()).limit(_MAX_QUERY_ROWS)

    result = await db.execute(query)
    rows = result.scalars().all()

    if not rows:
        return JobTelemetryQueryResponse(
            job_id=job_id,
            points=[],
            total_samples=0,
            avg_tokens_per_sec=None,
        )

    buckets: dict[int, list[JobTelemetry]] = defaultdict(list)
    all_tok_rates: list[float] = []

    for r in rows:
        ts_aware = ensure_aware(r.timestamp)
        epoch = int(ts_aware.timestamp()) if ts_aware else 0
        bucket_key = (epoch // bucket_seconds) * bucket_seconds
        buckets[bucket_key].append(r)
        if r.tokens_per_sec is not None:
            all_tok_rates.append(r.tokens_per_sec)

    points: list[JobMetricBucketPoint] = []
    for bucket_key in sorted(buckets.keys()):
        pts = buckets[bucket_key]
        utils = [p.gpu_util_pct for p in pts if p.gpu_util_pct is not None]
        rates = [p.tokens_per_sec for p in pts if p.tokens_per_sec is not None]
        mems = [p.mem_used_mb for p in pts if p.mem_used_mb is not None]

        points.append(
            JobMetricBucketPoint(
                timestamp=datetime.fromtimestamp(bucket_key, tz=timezone.utc),
                avg_gpu_util_pct=round(sum(utils) / len(utils), 2) if utils else None,
                avg_tokens_per_sec=round(sum(rates) / len(rates), 2) if rates else None,
                max_tokens_per_sec=round(max(rates), 2) if rates else None,
                avg_mem_used_mb=int(sum(mems) / len(mems)) if mems else None,
                samples=len(pts),
            )
        )

    overall_avg_toks = (
        round(sum(all_tok_rates) / len(all_tok_rates), 2) if all_tok_rates else None
    )

    return JobTelemetryQueryResponse(
        job_id=job_id,
        points=points,
        total_samples=len(rows),
        avg_tokens_per_sec=overall_avg_toks,
    )


# --------------------------------------------------------------------------- #
# Platform Stats & Alerts Management
# --------------------------------------------------------------------------- #
async def get_platform_telemetry_stats(db: AsyncSession) -> PlatformTelemetryStats:
    """Calculate platform-wide live telemetry KPIs and alert counts."""
    now = utcnow()
    recent = now - timedelta(minutes=5)

    # 1. Telemetry points within the last 5 minutes
    res = await db.execute(
        select(MachineTelemetry).where(MachineTelemetry.timestamp >= recent)
    )
    points = res.scalars().all()

    active_gpus = len({(p.machine_id, p.gpu_index) for p in points})
    utils = [p.gpu_util_pct for p in points if p.gpu_util_pct is not None]
    temps = [p.temp_c for p in points if p.temp_c is not None]
    powers = [p.power_w for p in points if p.power_w is not None]

    avg_util = round(sum(utils) / len(utils), 2) if utils else 0.0
    avg_temp = round(sum(temps) / len(temps), 2) if temps else 0.0
    total_power_kw = round(sum(powers) / 1000.0, 2) if powers else 0.0

    # 2. Active alerts count
    active_alerts_res = await db.execute(
        select(func.count(TelemetryAlert.id)).where(TelemetryAlert.status == "active")
    )
    active_count = active_alerts_res.scalar_one() or 0

    critical_alerts_res = await db.execute(
        select(func.count(TelemetryAlert.id)).where(
            TelemetryAlert.status == "active",
            TelemetryAlert.severity == "critical",
        )
    )
    critical_count = critical_alerts_res.scalar_one() or 0

    return PlatformTelemetryStats(
        active_gpus=active_gpus,
        fleet_avg_gpu_util_pct=avg_util,
        fleet_avg_temp_c=avg_temp,
        fleet_total_power_kw=total_power_kw,
        active_alerts_count=active_count,
        critical_alerts_count=critical_count,
    )


async def acknowledge_alert(
    db: AsyncSession, alert_id: uuid.UUID, user_id: uuid.UUID
) -> TelemetryAlert | None:
    """Acknowledge an operational alert."""
    result = await db.execute(
        select(TelemetryAlert).where(TelemetryAlert.id == alert_id)
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        return None
    alert.status = "acknowledged"
    alert.acknowledged_by = user_id
    alert.acknowledged_at = utcnow()
    await db.flush()
    return alert


async def resolve_alert(
    db: AsyncSession, alert_id: uuid.UUID
) -> TelemetryAlert | None:
    """Resolve an operational alert."""
    result = await db.execute(
        select(TelemetryAlert).where(TelemetryAlert.id == alert_id)
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        return None
    alert.status = "resolved"
    alert.resolved_at = utcnow()
    await db.flush()
    return alert
