"""A host's own usage numbers for one of their computers.

``live``  : the latest real GPU reading per card, how many jobs run now, and the last hour of utilisation.
``usage`` : how the last few days were spent: running customers' jobs, idle, or not reporting.

How "not reporting" is worked out, and its limits (so nothing here claims more than it knows):

* The platform keeps no history of a computer's availability, only its current state. What it has is the GPU readings the
  agent sends about every 30 seconds, and the job attempts. A 5-minute slice with at least one reading counts as the
  computer being there ("reporting"); a slice with none is "not reporting" (powered off, out of reach, paused, or an old agent
  that sends no readings).
* "Running" is the union of the job attempts' time on this computer, so two jobs at once are not counted twice.
* If the computer sent NO readings at all in the period, idle and not-reporting are returned as None rather than guessed.
* The reason a computer was not available (owner busy, too hot, paused) is not recorded yet, so it is not split out here.

Everything is about the host's own machine. No customer, job content, or job identity leaves this module.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware, utcnow
from app.models.jobs import JobAttempt
from app.models.provider import ProviderMachine
from app.models.telemetry import MachineTelemetry
from app.schemas.machine_usage import (
    GpuReadingOut,
    MachineLiveOut,
    MachineUsageOut,
    UsageSecondsOut,
    UtilizationPointOut,
)
from app.services.capability_flags import machine_host_kind
from app.services.provider import derive_status

REPORTING_BUCKET_SECONDS = 300  # a 5-minute slice with a reading counts as "the computer was there"
RECENT_READING_SECONDS = 600  # older than this is not "live"
LIVE_GAUGE_SECONDS = 300  # a GPU's gauge shows its newest reading only if that reading is this fresh
SERIES_BUCKET_SECONDS = 120
MAX_DAYS = 14
_MAX_TELEMETRY_ROWS = 300_000  # a hard bound on one request's work

#: Error codes that mean "this computer ended the job" rather than "the customer's code failed".
_STOPPED_HERE_CODES = {"host_interrupted", "thermal_shutdown"}


# --------------------------------------------------------------------------- #
# Pure helpers (tested directly)
# --------------------------------------------------------------------------- #
def union_seconds(intervals: Iterable[tuple[dt.datetime, dt.datetime]]) -> int:
    """Total seconds covered by the intervals, counting overlapping time once."""
    spans = sorted((s, e) for s, e in intervals if e > s)
    total = 0.0
    cur_s: Optional[dt.datetime] = None
    cur_e: Optional[dt.datetime] = None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_s is not None and cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_s is not None and cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total)


def clip(start: dt.datetime, end: dt.datetime, lo: dt.datetime, hi: dt.datetime) -> Optional[tuple[dt.datetime, dt.datetime]]:
    s, e = max(start, lo), min(end, hi)
    return (s, e) if e > s else None


def reporting_seconds(timestamps: Iterable[dt.datetime], lo: dt.datetime, hi: dt.datetime) -> int:
    """Seconds of the window in which readings arrived: each distinct 5-minute slice that has one counts in full,
    capped at the length of the window (the first and last slices may be partial)."""
    slices = {int(ts.timestamp()) // REPORTING_BUCKET_SECONDS for ts in timestamps}
    return min(len(slices) * REPORTING_BUCKET_SECONDS, int((hi - lo).total_seconds()))


def attempt_outcome(status: str, error_code: Optional[str]) -> str:
    if status in ("completed", "running", "cancelled"):
        return status
    if status == "lease_expired":
        return "lost"
    if status == "runtime_exceeded":
        return "too_long"
    if status == "failed":
        return "stopped_here" if (error_code or "") in _STOPPED_HERE_CODES else "failed"
    return "other"


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
async def machine_live(db: AsyncSession, machine: ProviderMachine, now: Optional[dt.datetime] = None) -> MachineLiveOut:
    now = now or utcnow()

    latest_ts = await db.scalar(select(func.max(MachineTelemetry.timestamp)).where(MachineTelemetry.machine_id == machine.id))
    latest = ensure_aware(latest_ts) if latest_ts is not None else None
    recent = latest is not None and (now - latest).total_seconds() <= RECENT_READING_SECONDS

    gpus: list[GpuReadingOut] = []
    if latest is not None and (now - latest).total_seconds() <= LIVE_GAUGE_SECONDS:
        rows = (
            await db.execute(
                select(MachineTelemetry)
                .where(
                    MachineTelemetry.machine_id == machine.id,
                    MachineTelemetry.timestamp >= now - dt.timedelta(seconds=LIVE_GAUGE_SECONDS),
                )
                .order_by(MachineTelemetry.timestamp.desc())
                .limit(256)
            )
        ).scalars().all()
        seen: set[int] = set()
        for r in rows:  # newest first: keep the first row of each card
            if r.gpu_index in seen:
                continue
            seen.add(r.gpu_index)
            gpus.append(
                GpuReadingOut(
                    gpu_index=r.gpu_index, gpu_util_pct=r.gpu_util_pct, mem_used_mb=r.mem_used_mb,
                    mem_total_mb=r.mem_total_mb, temp_c=r.temp_c, power_w=r.power_w,
                )
            )
        gpus.sort(key=lambda g: g.gpu_index)

    hour_rows = (
        await db.execute(
            select(MachineTelemetry.timestamp, MachineTelemetry.gpu_util_pct, MachineTelemetry.temp_c)
            .where(MachineTelemetry.machine_id == machine.id, MachineTelemetry.timestamp >= now - dt.timedelta(hours=1))
            .order_by(MachineTelemetry.timestamp.asc())
            .limit(20_000)
        )
    ).all()
    buckets: dict[int, list[tuple[Optional[float], Optional[float]]]] = defaultdict(list)
    for ts, util, temp in hour_rows:
        aware = ensure_aware(ts)
        if aware is None:
            continue
        buckets[(int(aware.timestamp()) // SERIES_BUCKET_SECONDS) * SERIES_BUCKET_SECONDS].append((util, temp))
    series = []
    for key in sorted(buckets):
        utils = [u for u, _ in buckets[key] if u is not None]
        temps = [t for _, t in buckets[key] if t is not None]
        series.append(
            UtilizationPointOut(
                timestamp=dt.datetime.fromtimestamp(key, tz=dt.timezone.utc),
                avg_gpu_util_pct=round(sum(utils) / len(utils), 1) if utils else None,
                max_temp_c=round(max(temps), 1) if temps else None,
            )
        )

    running = await db.scalar(
        select(func.count()).select_from(JobAttempt).where(JobAttempt.machine_id == machine.id, JobAttempt.status == "running")
    )
    return MachineLiveOut(
        machine_id=machine.id,
        hostname=machine.hostname,
        status=derive_status(machine, now),
        availability=machine.availability,
        host_kind=machine_host_kind(machine),
        last_heartbeat_at=ensure_aware(machine.last_heartbeat_at),
        last_reading_at=latest,
        readings_recent=recent,
        running_jobs=int(running or 0),
        gpus=gpus,
        utilization_last_hour=series,
    )


# --------------------------------------------------------------------------- #
# Usage over the last days
# --------------------------------------------------------------------------- #
async def machine_usage(
    db: AsyncSession, machine: ProviderMachine, days: int = 7, now: Optional[dt.datetime] = None
) -> MachineUsageOut:
    now = now or utcnow()
    days = max(1, min(int(days), MAX_DAYS))
    lo, hi = now - dt.timedelta(days=days), now
    window = int((hi - lo).total_seconds())

    attempts = (
        await db.execute(
            select(JobAttempt).where(
                JobAttempt.machine_id == machine.id,
                JobAttempt.started_at <= hi,
                (JobAttempt.finished_at.is_(None)) | (JobAttempt.finished_at >= lo),
            )
        )
    ).scalars().all()

    spans = []
    counts: dict[str, int] = defaultdict(int)
    for a in attempts:
        started = ensure_aware(a.started_at)
        if started is None:
            continue
        finished = ensure_aware(a.finished_at) or hi  # still running: counts up to now
        span = clip(started, finished, lo, hi)
        if span is not None:
            spans.append(span)
        counts[attempt_outcome(a.status, a.error_code)] += 1
    running = min(union_seconds(spans), window)

    ts_rows = (
        await db.execute(
            select(MachineTelemetry.timestamp)
            .where(MachineTelemetry.machine_id == machine.id, MachineTelemetry.timestamp >= lo, MachineTelemetry.timestamp <= hi)
            .limit(_MAX_TELEMETRY_ROWS)
        )
    ).scalars().all()
    stamps = [ensure_aware(t) for t in ts_rows]
    stamps = [t for t in stamps if t is not None]

    if not stamps:
        seconds = UsageSecondsOut(window=window, running=running, idle=None, not_reporting=None)
        available = False
    else:
        # A computer that ran a job was obviously there, even if a reading or two went missing.
        reporting = max(reporting_seconds(stamps, lo, hi), running)
        seconds = UsageSecondsOut(
            window=window, running=running, idle=max(0, reporting - running), not_reporting=max(0, window - reporting)
        )
        available = True

    return MachineUsageOut(
        machine_id=machine.id, days=days, window_start=lo, window_end=hi, telemetry_available=available, seconds=seconds,
        attempts=dict(counts),
    )
