"""U16: mining / abuse detector.

DRAFT policy basis; legal sign-off required before ABUSE_ENFORCE=true. Default is
observe-only: flags are recorded, nothing is stopped. Heuristics only, not proof.
Does not change ledger, pricing or billing logic. Stopping a job does call the existing
``billing.release_hold`` (idempotent, no-op when settled) so the customer's held credits and the
machine's reservation are freed; see :func:`stop_job`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.abuse import AbuseFlag, AbuseHold
from app.models.jobs import Job
from app.models.telemetry import JobTelemetry
from app.services import job_fsm as fsm

SIG_UTIL_NO_PROGRESS = "sustained_util_no_progress"
SIG_IDENTICAL = "identical_non_progress"
SIG_MINER_EGRESS = "miner_egress"
SIG_MINER_PROCESS = "miner_process"
# U8/U16 BYOC runtime signals (only evaluated for jobs that carry a ``byoc`` block).
SIG_BYOC_STRATUM = "byoc_stratum_attempt"
SIG_BYOC_MINER_BINARY = "byoc_miner_binary"
SIG_BYOC_IDLE_IO = "byoc_full_util_zero_io"
SIG_BYOC_POWER = "byoc_periodic_identical_power"

MINER_POOL_HOST_FRAGMENTS = (
    "nanopool", "ethermine", "2miners", "f2pool", "nicehash", "hiveon", "flexpool",
    "poolin", "antpool", "viabtc", "unmineable", "herominers", "moneroocean",
    "supportxmr", "minexmr", "crazypool", "stratum",
)
MINER_PORTS = frozenset({3333, 4444, 5555, 7777, 9999, 14444, 45560, 14433, 3357, 8008})
MINER_PROCESS_NAMES = frozenset({
    "xmrig", "ethminer", "t-rex", "trex", "phoenixminer", "nbminer", "gminer", "lolminer",
    "teamredminer", "claymore", "bminer", "cpuminer", "ccminer", "nanominer", "kawpowminer",
})
_ACTIVE = ("running",)
_STRATUM_MARKERS = ("stratum", "stratum+tcp", "stratum+ssl", "stratum2")


def match_egress(destinations: Iterable[str]) -> list[str]:
    """Return destinations ('host:port' or 'host') that look like mining pools."""
    hits: list[str] = []
    for d in destinations:
        if not isinstance(d, str):
            continue
        host, _, port = d.strip().lower().rpartition(":")
        if not host:  # no colon
            host, port = d.strip().lower(), ""
        port_hit = port.isdigit() and int(port) in MINER_PORTS
        if any(f in host for f in MINER_POOL_HOST_FRAGMENTS) or port_hit:
            hits.append(d[:200])
    return hits


def match_processes(names: Iterable[str]) -> list[str]:
    return [
        n[:64] for n in names
        if isinstance(n, str) and n.strip().lower().rsplit("/", 1)[-1].removesuffix(".exe") in MINER_PROCESS_NAMES
    ]


def match_stratum(destinations: Iterable[str]) -> list[str]:
    """BYOC: blocked-egress attempts that look like a mining pool: a stratum scheme/host or a known pool port.

    Stricter than ``match_egress`` on host fragments (a blocked ``nanopool`` lookup alone is already a hit there);
    here we additionally accept the explicit stratum scheme and pool ports on any host.
    """
    hits: list[str] = []
    for d in destinations:
        if not isinstance(d, str):
            continue
        low = d.strip().lower()
        _, _, port = low.rpartition(":")
        if any(m in low for m in _STRATUM_MARKERS) or (port.isdigit() and int(port) in MINER_PORTS) or match_egress([d]):
            hits.append(d[:200])
    return hits


def evaluate_byoc_samples(samples: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Pure BYOC-only signals over ascending samples (ts, util, step, progress, io_bytes, power_w).

    * full GPU util, no progress/step change and (near) zero container I/O growth for ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS;
    * hash-rate-like power: a long run of high, near-identical power draw (spread <= tolerance) while nothing progresses.
    Heuristics only, never proof; they raise flags for review (observe-only unless ABUSE_ENFORCE).
    """
    sigs: list[tuple[str, dict[str, Any]]] = []
    if len(samples) < 2:
        return sigs

    def _tail(window: float) -> list[dict[str, Any]]:
        end = samples[-1]["ts"]
        return [x for x in samples if (end - x["ts"]).total_seconds() <= window]

    def _no_progress(rows: list[dict[str, Any]]) -> bool:
        prog = [x["progress"] for x in rows if x.get("progress") is not None]
        steps = [x["step"] for x in rows if x.get("step") is not None]
        return (not prog or max(prog) <= min(prog)) and (not steps or max(steps) <= min(steps))

    w = float(settings.ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS)
    rows = _tail(w)
    if len(rows) >= settings.ABUSE_MIN_SAMPLES and (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() >= w * 0.9:
        utils = [x["util"] for x in rows if x.get("util") is not None]
        io = [x["io_bytes"] for x in rows if x.get("io_bytes") is not None]
        if (
            len(utils) == len(rows)
            and min(utils) >= settings.ABUSE_UTIL_THRESHOLD_PCT
            and len(io) >= settings.ABUSE_MIN_SAMPLES  # no I/O telemetry -> no verdict (never guess)
            and max(io) - min(io) <= settings.ABUSE_BYOC_IO_DELTA_MAX_BYTES
            and _no_progress(rows)
        ):
            sigs.append((SIG_BYOC_IDLE_IO, {"span_seconds": (rows[-1]["ts"] - rows[0]["ts"]).total_seconds(),
                                            "min_util": min(utils), "io_delta_bytes": max(io) - min(io)}))

    pw = float(settings.ABUSE_BYOC_POWER_WINDOW_SECONDS)
    rows = _tail(pw)
    watts = [x["power_w"] for x in rows if x.get("power_w") is not None]
    if (
        len(watts) >= settings.ABUSE_BYOC_POWER_MIN_SAMPLES
        and (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() >= pw * 0.9
        and min(watts) >= settings.ABUSE_BYOC_POWER_MIN_W
        and max(watts) - min(watts) <= settings.ABUSE_BYOC_POWER_TOLERANCE_W
        and _no_progress(rows)
    ):
        sigs.append((SIG_BYOC_POWER, {"samples": len(watts), "watts_min": min(watts), "watts_max": max(watts)}))
    return sigs


def evaluate_samples(samples: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Pure signal evaluation. samples: ascending dicts with ts (aware dt), util, mem, step, progress."""
    sigs: list[tuple[str, dict[str, Any]]] = []
    if len(samples) < settings.ABUSE_MIN_SAMPLES:
        return sigs
    span = (samples[-1]["ts"] - samples[0]["ts"]).total_seconds()
    utils = [s["util"] for s in samples if s["util"] is not None]
    progress = [s["progress"] for s in samples if s["progress"] is not None]
    steps = [s["step"] for s in samples if s["step"] is not None]
    no_progress = (not progress or max(progress) <= min(progress)) and (
        not steps or max(steps) <= min(steps)
    )
    if (
        span >= settings.ABUSE_UTIL_WINDOW_SECONDS
        and len(utils) >= settings.ABUSE_MIN_SAMPLES
        and min(utils) >= settings.ABUSE_UTIL_THRESHOLD_PCT
        and no_progress
    ):
        sigs.append((SIG_UTIL_NO_PROGRESS, {"span_seconds": span, "min_util": min(utils), "samples": len(utils)}))
    tuples = {(s["util"], s["mem"], s["step"], s["progress"]) for s in samples}
    if (
        len(tuples) == 1
        and span >= settings.ABUSE_UTIL_WINDOW_SECONDS
        and (utils and utils[0] >= 50.0)
    ):
        sigs.append((SIG_IDENTICAL, {"span_seconds": span, "samples": len(samples)}))
    return sigs


async def _open_flag_exists(db: AsyncSession, job_id: uuid.UUID, signal: str) -> bool:
    return (
        await db.execute(
            select(AbuseFlag.id).where(
                AbuseFlag.job_id == job_id, AbuseFlag.signal == signal, AbuseFlag.status == "open"
            )
        )
    ).first() is not None


async def scan_job(db: AsyncSession, job: Job) -> list[AbuseFlag]:
    """Evaluate one running job; create AbuseFlags (deduplicated); enforce if configured."""
    now = utcnow()
    from app.services.byoc import job_byoc

    is_byoc = job_byoc(job) is not None
    span = settings.ABUSE_UTIL_WINDOW_SECONDS
    if is_byoc:  # BYOC windows may be longer than the generic one
        span = max(span, settings.ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS, settings.ABUSE_BYOC_POWER_WINDOW_SECONDS)
    window = now - timedelta(seconds=span)
    rows = (
        await db.execute(
            select(JobTelemetry)
            .where(JobTelemetry.job_id == job.id, JobTelemetry.timestamp >= window)
            .order_by(JobTelemetry.timestamp)
            .limit(2000)
        )
    ).scalars().all()
    samples = []
    hits: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        em = r.extra_metrics if isinstance(r.extra_metrics, dict) else {}
        samples.append({
            "ts": ensure_aware(r.timestamp), "util": r.gpu_util_pct, "mem": r.mem_used_mb,
            "step": r.step_count, "progress": em.get("progress_pct"),
            "io_bytes": em.get("io_bytes"), "power_w": em.get("power_w"),
        })
        blocked = em.get("blocked_egress")
        if isinstance(blocked, list) and (m := match_egress(blocked[:50])):
            hits.append((SIG_MINER_EGRESS, {"destinations": m[:10], "source": "agent_blocked_egress"}))
        procs = em.get("process_names")
        if isinstance(procs, list) and (m := match_processes(procs[:100])):
            hits.append((SIG_MINER_PROCESS, {"processes": m[:10]}))
        if is_byoc:
            if isinstance(blocked, list) and (m := match_stratum(blocked[:50])):
                hits.append((SIG_BYOC_STRATUM, {"destinations": m[:10], "source": "agent_blocked_egress"}))
            if isinstance(procs, list) and (m := match_processes(procs[:100])):
                hits.append((SIG_BYOC_MINER_BINARY, {"processes": m[:10], "source": "docker_top"}))
    # The generic util/identical signals keep their own (shorter) window; BYOC samples may span longer.
    gen_cut = now - timedelta(seconds=settings.ABUSE_UTIL_WINDOW_SECONDS)
    hits += evaluate_samples([x for x in samples if x["ts"] >= gen_cut])
    if is_byoc:
        hits += evaluate_byoc_samples(samples)
    policy = job.network_policy if isinstance(job.network_policy, dict) else {}
    if m := match_egress([e for e in (policy.get("egress") or [])[:100]]):
        hits.append((SIG_MINER_EGRESS, {"destinations": m[:10], "source": "network_policy"}))

    created: list[AbuseFlag] = []
    seen: set[str] = set()
    for sig, detail in hits:
        if sig in seen or await _open_flag_exists(db, job.id, sig):
            continue
        seen.add(sig)
        flag = AbuseFlag(job_id=job.id, user_id=job.customer_id, signal=sig, detail=detail, status="open")
        db.add(flag)
        created.append(flag)
    if created:
        await db.flush()
        if settings.ABUSE_ENFORCE:
            await enforce(db, job, created[0])
    return created


async def stop_job(
    db: AsyncSession, job: Job, *, reason: str, actor: str, error_code: str, error_message: str
) -> bool:
    """End a job for abuse and clean up after it. Returns False when it was already terminal.

    Moving a job to a terminal state is not enough on its own: nothing else will ever release its hold or
    reservation once it is terminal (the agent's late result is answered idempotently without settling), so the
    customer's credits would stay in ``holding`` forever and the machine slot would stay taken. This mirrors the
    lease-expiry cancel path: transition, close the attempt, free the reservation, release the hold. A job that
    cannot go to FAILED from its current state (reserved, matched, retry_pending...) is CANCELLED instead.
    """
    from app.services import billing
    from app.services.leases import close_running_attempt, release_job_reservation

    if job.status in fsm.TERMINAL_STATES:
        return False
    was_running = job.status == fsm.RUNNING
    job.cancel_requested = True
    job.error_code = error_code
    job.error_message = error_message
    target = fsm.FAILED if fsm.is_allowed(job.status, fsm.FAILED) else fsm.CANCELLED
    await fsm.transition(db, job, target, reason=reason, actor=actor)
    now = utcnow()
    if was_running:
        await close_running_attempt(db, job, now, outcome="failed", error_code=error_code)
    await release_job_reservation(db, job, now)
    await billing.release_hold(db, job)  # idempotent per job; a no-op when there is no hold or it was settled
    return True


async def enforce(db: AsyncSession, job: Job, flag: AbuseFlag) -> None:
    """Stop the job and hold new submissions pending admin review. Only when ABUSE_ENFORCE=true."""
    if not settings.ABUSE_ENFORCE:
        return
    job.cancel_requested = True
    await stop_job(
        db, job, reason=f"abuse:{flag.signal}", actor="abuse_detector", error_code="abuse_suspected",
        error_message="stopped pending review under the acceptable use policy",
    )
    flag.enforced = True
    if not await has_submission_hold(db, job.customer_id):
        db.add(AbuseHold(user_id=job.customer_id, flag_id=flag.id))
    await db.flush()


async def has_submission_hold(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(AbuseHold.id).where(AbuseHold.user_id == user_id, AbuseHold.released_at.is_(None)).limit(1)
        )
    ).first() is not None


async def assert_can_submit(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Raise 403 if the user has an unreleased abuse hold. Only active when ABUSE_ENFORCE=true."""
    from fastapi import HTTPException

    if settings.ABUSE_ENFORCE and await has_submission_hold(db, user_id):
        raise HTTPException(status_code=403, detail="submissions_suspended_pending_review")


async def sweep(db: AsyncSession, limit: int = 500) -> int:
    """Scan running jobs; returns the number of new flags. Call from the worker loop."""
    jobs = (
        await db.execute(select(Job).where(Job.status.in_(_ACTIVE)).limit(limit))
    ).scalars().all()
    n = 0
    for j in jobs:
        n += len(await scan_job(db, j))
    return n
