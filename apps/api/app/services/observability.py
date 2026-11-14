"""U19: live per-job view, progress ingestion and Prometheus rendering.

Reads/writes job_telemetry only (progress lives in extra_metrics). Never touches
ledger, pricing or billing.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.models.telemetry import JobTelemetry, TelemetryAlert
from app.schemas.observability import JobLiveOut, JobProgressItem


async def record_job_progress(
    db: AsyncSession, machine_id: uuid.UUID, points: list[JobProgressItem]
) -> tuple[int, int]:
    """Store progress points. Returns (accepted, dropped). Fails closed on ownership."""
    now = utcnow()
    capped = points[: max(settings.JOB_PROGRESS_MAX_POINTS, 0)]
    dropped = len(points) - len(capped)
    requested = {p.job_id for p in capped}
    owned: set[uuid.UUID] = set()
    if requested:
        owned = set(
            (
                await db.execute(
                    select(Job.id).where(
                        Job.id.in_(requested),
                        Job.machine_id == machine_id,
                        Job.status.in_(("running", "verifying")),
                    )
                )
            ).scalars().all()
        )
    last_written: dict[uuid.UUID, object] = {}
    accepted = 0
    min_gap = timedelta(seconds=settings.JOB_PROGRESS_MIN_INTERVAL_SECONDS)
    for pt in capped:
        if pt.job_id not in owned:
            dropped += 1
            continue
        prev = last_written.get(pt.job_id)
        if prev is None:
            prev = (
                await db.execute(
                    select(func.max(JobTelemetry.timestamp)).where(JobTelemetry.job_id == pt.job_id)
                )
            ).scalar()
        prev = ensure_aware(prev) if prev is not None else None
        ts = ensure_aware(pt.timestamp) or now
        # Cap client-supplied timestamps to the server clock to prevent future-dated rows.
        ts = min(ts, now)
        if prev is not None and (now - prev) < min_gap and pt.progress_pct < 100.0:
            dropped += 1
            continue
        db.add(
            JobTelemetry(
                job_id=pt.job_id,
                machine_id=machine_id,
                timestamp=ts,
                gpu_util_pct=pt.gpu_util_pct,
                mem_used_mb=pt.mem_used_mb,
                step_count=pt.step_count,
                extra_metrics={
                    "progress_pct": round(pt.progress_pct, 2),
                    "phase": (pt.phase or "")[:64] or None,
                    "vram_total_mb": pt.mem_total_mb,
                    "kind": "progress",
                    # U16 BYOC runtime signals, stored only when the agent sent them (abuse.scan_job reads them).
                    **{k: v for k, v in (
                        ("process_names", pt.process_names), ("blocked_egress", pt.blocked_egress),
                        ("power_w", pt.power_w), ("io_bytes", pt.io_bytes)) if v is not None},
                },
            )
        )
        last_written[pt.job_id] = now
        accepted += 1
    await db.flush()
    return accepted, dropped


async def get_job_live(db: AsyncSession, job: Job) -> JobLiveOut:
    now = utcnow()
    latest = (
        await db.execute(
            select(JobTelemetry)
            .where(JobTelemetry.job_id == job.id)
            .order_by(desc(JobTelemetry.timestamp))
            .limit(1)
        )
    ).scalar_one_or_none()
    progress = (
        await db.execute(
            select(JobTelemetry.extra_metrics)
            .where(JobTelemetry.job_id == job.id)
            .order_by(desc(JobTelemetry.timestamp))
            .limit(20)
        )
    ).scalars().all()
    pct = phase = vram_total = None
    for em in progress:
        if isinstance(em, dict) and em.get("progress_pct") is not None:
            pct, phase, vram_total = em.get("progress_pct"), em.get("phase"), em.get("vram_total_mb")
            break
    started = ensure_aware(job.started_at)
    end = ensure_aware(job.finished_at) or now
    elapsed = (end - started).total_seconds() if started else None
    cap = float(job.max_cost) if job.max_cost is not None else None
    so_far = None
    if job.final_cost is not None:
        so_far = float(job.final_cost)
    elif cap is not None and elapsed is not None and job.max_runtime_seconds:
        so_far = round(min(cap, cap * elapsed / job.max_runtime_seconds), 2)
    return JobLiveOut(
        job_id=job.id,
        status=job.status,
        sampled_at=latest.timestamp if latest else None,
        gpu_util_pct=latest.gpu_util_pct if latest else None,
        mem_used_mb=latest.mem_used_mb if latest else None,
        mem_total_mb=vram_total,
        progress_pct=pct,
        phase=phase,
        elapsed_seconds=elapsed,
        cost_cap=cap,
        cost_so_far=so_far,
        cost_so_far_is_estimate=job.final_cost is None,
    )


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# --- cross-platform plan W10: the `platform` label --------------------------------------------------------------------
# A label value is NEVER copied from a heartbeat: it is looked up in this allowlist, so a host cannot invent a series
# (cardinality) or inject text into the exposition. A host that reports nothing (every Linux agent from before W1) is
# "unknown", not "linux": the label states what was reported, not what is assumed.
PLATFORM_LABELS = ("linux", "windows")
PLATFORM_UNKNOWN = "unknown"


def machine_platform(health: object) -> str:
    """'linux' | 'windows' | 'unknown' from a heartbeat health block (``health["platform"]["os"]``, plan section 3.4)."""
    if not isinstance(health, dict):
        return PLATFORM_UNKNOWN
    plat = health.get("platform")
    os_name = plat.get("os") if isinstance(plat, dict) else None
    return os_name if isinstance(os_name, str) and os_name in PLATFORM_LABELS else PLATFORM_UNKNOWN


def _health_flag(health: object, section: str, key: str) -> bool | None:
    """``health[section][key]`` when it is a real boolean, else None (a missing or odd value is neither true nor false)."""
    block = health.get(section) if isinstance(health, dict) else None
    value = block.get(key) if isinstance(block, dict) else None
    return value if isinstance(value, bool) else None


def platform_signals(health: object) -> dict[str, bool]:
    """The per-host facts behind the Windows alerts. Contract with the supervisor (W2/W3/W6), additive and optional:

    * ``health["runtime"]["distro_ok"]`` / ``["gpu_smoke_ok"]``: the managed WSL2 distro passes its health checks / the
      last GPU smoke test passed. Only an explicit ``False`` counts as a failure; a missing value counts as nothing.
    * ``health["update"]["rolled_back"]``: the last update was rolled back.
    * ``health["availability_detail"]["state"] == "yielding"``: the owner has the machine (policy.py heartbeat_health).

    The key names were checked against what the agent really sends: ``windows/supervisor_wiring.py`` (``RuntimeHealthCache``
    and the updater's ``health()``) and ``windows/governance/policy.py`` (``Decision.heartbeat_health``). The test
    ``tests/e2e/test_platform_signals_contract.py`` feeds the agent's own output through this function, so the two
    cannot drift apart unnoticed. A host that reports nothing reads 0: nothing is ever invented as a failure."""
    detail = health.get("availability_detail") if isinstance(health, dict) else None
    return {
        "distro_unhealthy": _health_flag(health, "runtime", "distro_ok") is False,
        "gpu_smoke_failing": _health_flag(health, "runtime", "gpu_smoke_ok") is False,
        "update_rolled_back": _health_flag(health, "update", "rolled_back") is True,
        "yielding": isinstance(detail, dict) and detail.get("state") == "yielding",
    }


def render_platform_metrics(rows: list[tuple[str, object, object]], now) -> list[str]:
    """Exposition lines for machines by (availability, platform) plus the Windows-oriented gauges.

    ``rows`` are (availability, last_health, last_heartbeat_at). Health signals only count for a machine whose heartbeat
    is recent (3 online windows): a host that went away must not keep an alert firing from stale data."""
    fresh_for = timedelta(seconds=settings.ONLINE_WINDOW_SECONDS * 3)
    by_avail: dict[tuple[str, str], int] = {}
    signals = {name: {p: 0 for p in (*PLATFORM_LABELS, PLATFORM_UNKNOWN)} for name in
               ("distro_unhealthy", "gpu_smoke_failing", "update_rolled_back", "yielding")}
    for availability, health, last_seen in rows:
        plat = machine_platform(health)
        key = (str(availability), plat)
        by_avail[key] = by_avail.get(key, 0) + 1
        seen = ensure_aware(last_seen) if last_seen is not None else None
        if seen is None or now - seen > fresh_for:
            continue
        for name, on in platform_signals(health).items():
            if on:
                signals[name][plat] += 1
    out = [
        "# HELP computecoin_ops_machines_by_platform Machines by availability and reported platform (additive to computecoin_ops_machines).",
        "# TYPE computecoin_ops_machines_by_platform gauge",
    ]
    avail_names = sorted({"online", "offline", "throttled"} | {a for a, _ in by_avail})
    for a in avail_names:
        for p in (*PLATFORM_LABELS, PLATFORM_UNKNOWN):
            out.append(f'computecoin_ops_machines_by_platform{{availability="{_esc(a)}",platform="{p}"}} {by_avail.get((a, p), 0)}')
    series = (
        ("computecoin_ops_platform_hosts_yielding", "yielding", "Hosts whose owner currently has the machine (X13)."),
        ("computecoin_ops_platform_distro_unhealthy", "distro_unhealthy", "Hosts reporting an unhealthy runtime distro."),
        ("computecoin_ops_platform_gpu_smoke_failing", "gpu_smoke_failing", "Hosts whose last GPU smoke test failed."),
        ("computecoin_ops_platform_update_rollbacks", "update_rolled_back", "Hosts whose last update was rolled back."),
    )
    for metric, name, help_text in series:
        out += [f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"]
        out += [f'{metric}{{platform="{p}"}} {signals[name][p]}' for p in (*PLATFORM_LABELS, PLATFORM_UNKNOWN)]
    return out


async def render_prometheus(db: AsyncSession) -> str:
    from app.models.reconciliation import ChainReconciliationRun, StakeSyncRun

    now = utcnow()
    out: list[str] = []

    out += ["# HELP computecoin_ops_jobs Jobs by state.", "# TYPE computecoin_ops_jobs gauge"]
    for st, n in (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all():
        out.append(f'computecoin_ops_jobs{{state="{_esc(st)}"}} {int(n)}')

    queued = (
        await db.execute(select(func.count()).select_from(Job).where(Job.status == "queued"))
    ).scalar() or 0
    out += ["# TYPE computecoin_ops_queue_depth gauge", f"computecoin_ops_queue_depth {int(queued)}"]

    out += ["# HELP computecoin_ops_machines Machines by availability.", "# TYPE computecoin_ops_machines gauge"]
    seen: set[str] = {"online", "offline", "throttled"}
    rows = (
        await db.execute(
            select(ProviderMachine.availability, func.count()).group_by(ProviderMachine.availability)
        )
    ).all()
    counts = {a: int(n) for a, n in rows}
    for a in sorted(seen | set(counts)):
        out.append(f'computecoin_ops_machines{{availability="{_esc(a)}"}} {counts.get(a, 0)}')

    # W10: the same machines with a `platform` label (additive: the series above keep their labels, so existing alerts
    # such as NoOnlineMachines still mean "no machine at all", not "no machine of one platform").
    fleet = (
        await db.execute(
            select(ProviderMachine.availability, ProviderMachine.last_health, ProviderMachine.last_heartbeat_at)
        )
    ).all()
    out += render_platform_metrics([(a, h, t) for a, h, t in fleet], now)

    window = now - timedelta(seconds=settings.OBS_CLAIM_LATENCY_WINDOW_SECONDS)
    started = (
        await db.execute(
            select(Job.created_at, Job.started_at).where(Job.started_at.is_not(None), Job.started_at >= window)
        )
    ).all()
    lat = [
        (ensure_aware(s) - ensure_aware(c)).total_seconds()
        for c, s in started
        if c is not None and s is not None
    ]
    lat = [x for x in lat if x >= 0]
    out += [
        "# HELP computecoin_ops_claim_latency_seconds Creation-to-start latency over the window.",
        "# TYPE computecoin_ops_claim_latency_seconds gauge",
        f'computecoin_ops_claim_latency_seconds{{stat="avg"}} {sum(lat)/len(lat) if lat else 0:.3f}',
        f'computecoin_ops_claim_latency_seconds{{stat="max"}} {max(lat) if lat else 0:.3f}',
    ]

    failed = (
        await db.execute(
            select(func.count()).select_from(Job).where(
                Job.status.in_(("failed", "timed_out")), Job.finished_at >= window
            )
        )
    ).scalar() or 0
    out += ["# TYPE computecoin_ops_jobs_failed_window gauge", f"computecoin_ops_jobs_failed_window {int(failed)}"]

    async def _since_ok(model) -> float:
        ts = (
            await db.execute(select(func.max(model.finished_at)).where(model.ok.is_(True)))
        ).scalar()
        return -1.0 if ts is None else max((now - ensure_aware(ts)).total_seconds(), 0.0)

    out += [
        "# HELP computecoin_ops_stake_sync_seconds_since_ok Seconds since last OK stake sync (-1 = never).",
        "# TYPE computecoin_ops_stake_sync_seconds_since_ok gauge",
        f"computecoin_ops_stake_sync_seconds_since_ok {await _since_ok(StakeSyncRun):.0f}",
        "# TYPE computecoin_ops_chain_recon_seconds_since_ok gauge",
        f"computecoin_ops_chain_recon_seconds_since_ok {await _since_ok(ChainReconciliationRun):.0f}",
    ]

    alerts = (
        await db.execute(
            select(func.count()).select_from(TelemetryAlert).where(TelemetryAlert.status == "active")
        )
    ).scalar() or 0
    out += ["# TYPE computecoin_ops_telemetry_alerts_active gauge", f"computecoin_ops_telemetry_alerts_active {int(alerts)}"]

    try:  # abuse table arrives with U16 migration 0031
        from app.models.abuse import AbuseFlag

        open_flags = (
            await db.execute(
                select(func.count()).select_from(AbuseFlag).where(AbuseFlag.status == "open")
            )
        ).scalar() or 0
        out += ["# TYPE computecoin_ops_abuse_flags_open gauge", f"computecoin_ops_abuse_flags_open {int(open_flags)}"]
    except Exception:  # pragma: no cover - table absent before migration
        pass
    return "\n".join(out) + "\n"
