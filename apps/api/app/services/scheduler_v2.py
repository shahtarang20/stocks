"""Scheduler v2 (ADR-0006 section 10, U7): placement rules for Job Spec v2 (Kit) jobs.

The v1 scheduler calls into this module ONLY for jobs whose ``spec_v2`` is set; v1 jobs never reach it.
Existing gates (banned, throttled, stake / value at risk, region, runtime compatibility) stay in
``app.services.scheduler`` and still run for v2 jobs. This module adds hard resource fit and a soft
cache-affinity ranking. Design: docs/architecture/U7-scheduler-v2-design.md.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.core.config import settings

KIT_REF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}@[0-9A-Za-z][0-9A-Za-z.\-+]{0,30}$")
MAX_KIT_CACHES = 64
DEFAULT_CACHE_BONUS = 0.15


def is_v2_job(job: Any) -> bool:
    return bool(getattr(job, "spec_v2", None))


def job_kit_ref(job: Any) -> str | None:
    kid, ver = getattr(job, "kit_id", None), getattr(job, "kit_version", None)
    return f"{kid}@{ver}" if kid and ver else None


def effective_resources(job: Any) -> dict:
    spec = getattr(job, "spec_v2", None) or {}
    res = spec.get("effective_resources") if isinstance(spec, dict) else None
    return res if isinstance(res, dict) else {}


def sanitize_kit_caches(value: Any) -> list[str] | None:
    """Heartbeat input: keep valid, unique ``slug@version`` strings, at most MAX_KIT_CACHES. Never raises."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and len(item) <= 96 and KIT_REF_RE.fullmatch(item) and item not in out:
            out.append(item)
            if len(out) >= MAX_KIT_CACHES:
                break
    return out


def machine_kit_caches(machine: Any) -> set[str]:
    health = getattr(machine, "last_health", None) or {}
    caches = health.get("kit_caches") if isinstance(health, dict) else None
    return set(sanitize_kit_caches(caches) or [])


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def job_spec_dict(job: Any) -> dict:
    spec = getattr(job, "spec_v2", None) or {}
    inner = spec.get("spec") if isinstance(spec, dict) else None
    return inner if isinstance(inner, dict) else {}


def machine_advertises_vfio_gpu(machine: Any) -> bool:
    """U14: a host opts in by reporting ``vfio_gpu: true``. Self-reported, so it only widens WHERE a customer's own
    licensed VM may run; it never grants a tier or money."""
    hw = getattr(machine, "hardware", None)
    raw = (hw.raw if hw else None) or {}
    if isinstance(raw, dict) and raw.get("vfio_gpu") is True:
        return True
    health = getattr(machine, "last_health", None)  # heartbeat health block (schemas.provider.HeartbeatHealth sanitizes it)
    return isinstance(health, dict) and health.get("vfio_gpu") is True


def windows_vm_reason(job: Any, machine: Any) -> str | None:
    """U14 scheduler gate for ``os: windows`` (mode vm). None when the job is not a VM job or the host fits."""
    spec = job_spec_dict(job)
    if spec.get("mode") != "vm" and spec.get("os") != "windows":
        return None
    if not settings.WINDOWS_VM_ENABLED:
        return "windows_vm_disabled"
    if spec.get("os") != "windows" or spec.get("mode") != "vm":
        return "windows_vm_spec_invalid"
    if (spec.get("vm") or {}).get("gpu_passthrough", True) and not machine_advertises_vfio_gpu(machine):
        return "vfio_gpu_required"
    return None


class CpuOnlyKitsDisabled(Exception):
    """A Kit job with gpu_count 0 was submitted while CPU_ONLY_KITS_ENABLED is false (refused, never billed as a GPU)."""


def is_cpu_only_job(job: Any) -> bool:
    """True for a Kit job whose effective resources ask for ZERO GPUs (an explicit 0; a missing value is not CPU-only)."""
    if not is_v2_job(job):
        return False
    res = effective_resources(job)
    if "gpu_count" not in res:
        return False
    try:
        return int(res["gpu_count"]) == 0
    except (TypeError, ValueError):
        return False


def stored_gpu_count(eff: dict) -> int:
    """The ``jobs.gpu_count`` for a new Kit job. Normal Kits: their GPU count (default 1).

    CPU-only (explicit 0): refused unless CPU_ONLY_KITS_ENABLED; when enabled the job carries 1 scheduler/billing SLOT
    (the existing quote and hold helpers need a count of at least 1), priced at CPU_ONLY_RATE_FRACTION of the
    machine's rate by :func:`quote_price_per_hour`. The agent still gets gpu_count 0 (:func:`runtime_gpu_count`).
    """
    try:
        zero = "gpu_count" in eff and eff["gpu_count"] is not None and int(eff["gpu_count"]) == 0
    except (TypeError, ValueError):
        zero = False
    if zero:
        if not settings.CPU_ONLY_KITS_ENABLED:
            raise CpuOnlyKitsDisabled("cpu_only_kits_disabled")
        return 1
    return int(eff.get("gpu_count") or 1)


def runtime_gpu_count(job: Any) -> int:
    """GPUs the AGENT is told to attach (the claim's limits.gpu_count): 0 for a CPU-only job, else the job's count."""
    if is_cpu_only_job(job):
        return 0
    return int(getattr(job, "gpu_count", 0) or 0)


def cpu_rate_fraction() -> Any:
    """CPU_ONLY_RATE_FRACTION as a Decimal in (0, 1]. Anything unusable falls back to 0.1, never to 0 (never free)."""
    import decimal

    try:
        f = decimal.Decimal(str(settings.CPU_ONLY_RATE_FRACTION))
    except (decimal.InvalidOperation, ValueError, TypeError):
        return decimal.Decimal("0.1")
    if not f.is_finite() or f <= 0 or f > 1:
        return decimal.Decimal("0.1")
    return f


def quote_price_per_hour(job: Any, machine_price: Any) -> Any:
    """Hourly price snapshot for the job's quote. A CPU-only job pays the fraction; everything else is unchanged.

    Settlement (billing.settle_job) meters ``quote.price_per_hour_snapshot x quote.gpu_count``, so applying the fraction
    here is the ONLY place the CPU-only rate exists; no billing function is edited.
    """
    import decimal

    price = decimal.Decimal(machine_price)
    if is_cpu_only_job(job):
        # quotes.price_per_hour_snapshot is NUMERIC(12,2): round to cents here (not in the column) and never below 0.01.
        cent = decimal.Decimal("0.01")
        return max(cent, (price * cpu_rate_fraction()).quantize(cent, rounding=decimal.ROUND_HALF_UP))
    return price


def machine_advertises_cpu(machine: Any) -> bool:
    """A host opts in to CPU-only Kits by reporting ``cpu_capable: true`` in its heartbeat health (a real boolean)."""
    health = getattr(machine, "last_health", None)
    return isinstance(health, dict) and health.get("cpu_capable") is True


def machine_fit_reason(job: Any, machine: Any) -> str | None:
    """Why ``machine`` cannot host this v2 job's resources, or None when it fits. Fail closed on unknowns.

    A job needing N GPUs needs ONE host with N GPUs; it is never split (gang on one host).
    """
    from app.services.capability_flags import capability_refusal

    cap_reason = capability_refusal(job, machine)  # W1 / X4 / X5: capability flags, and no private / BYOC / VM work on Windows
    if cap_reason:
        return cap_reason
    vm_reason = windows_vm_reason(job, machine)  # U14
    if vm_reason:
        return vm_reason
    res = effective_resources(job)
    hw = getattr(machine, "hardware", None)
    byoc = (getattr(job, "spec_v2", None) or {}).get("byoc")
    if byoc is not None:  # U8: BYOC only where the required isolation runtime is advertised (fail closed)
        health = getattr(machine, "last_health", None) or {}
        have = health.get("isolation_runtimes") if isinstance(health, dict) else None
        need = (byoc or {}).get("isolation") if isinstance(byoc, dict) else None
        if need not in ("gvisor", "microvm") or not isinstance(have, list) or need not in have:
            return "isolation_runtime"
    if is_cpu_only_job(job):  # CPU-only Kit: no GPU gates; only cpu-capable hosts, and only when the owner enabled it
        if not settings.CPU_ONLY_KITS_ENABLED:
            return "cpu_only_disabled"
        if not machine_advertises_cpu(machine):
            return "not_cpu_capable"
        want_gpus = 0
    else:
        want_gpus = int(res.get("gpu_count") or getattr(job, "gpu_count", 0) or 0)
    if want_gpus > 0:
        if hw is None or not (hw.gpu_count or 0):
            return "no_gpu"
        if (hw.gpu_count or 0) < want_gpus:
            return "gpus"
    min_vram_gb = res.get("min_vram_gb")
    if min_vram_gb:
        have_mb = (hw.vram_mb if hw else 0) or 0  # per card
        if have_mb <= 0 or have_mb < int(min_vram_gb) * 1024:
            return "vram"
    min_cc = res.get("min_compute_capability")
    if min_cc:
        raw = (hw.raw if hw else None) or {}
        have_cc = _num(raw.get("compute_capability")) if isinstance(raw, dict) else None
        want_cc = _num(min_cc)
        if have_cc is None or want_cc is None or have_cc < want_cc:
            return "compute_capability"
    if hw is not None or any(res.get(k) for k in ("cpu", "ram_gb", "disk_gb")):
        if res.get("cpu") and (hw.cpu_cores if hw else 0) < int(res["cpu"]):
            return "cpu"
        if res.get("ram_gb") and (hw.ram_mb if hw else 0) < int(res["ram_gb"]) * 1024:
            return "ram"
        if res.get("disk_gb") and (hw.disk_gb if hw else 0) < int(res["disk_gb"]):
            return "disk"
    return None


def cache_bonus() -> float:
    v = _num(getattr(settings, "SCHEDULER_V2_CACHE_BONUS", DEFAULT_CACHE_BONUS))
    return DEFAULT_CACHE_BONUS if v is None or v < 0 else min(v, 1.0)


def rerank(job: Any, scored: list[tuple[Any, float, dict]]) -> list[tuple[Any, float, dict]]:
    """Add the cache-affinity bonus to v1 scored candidates and re-sort (same tie-breaks as v1)."""
    ref = job_kit_ref(job)
    bonus = cache_bonus()
    out = []
    for cand, score, factors in scored:
        warm = bool(ref) and ref in machine_kit_caches(cand.machine)
        f = dict(factors)
        f["kit_cache"] = "warm" if warm else "cold"
        f["scheduler"] = "v2"
        out.append((cand, score + (bonus if warm else 0.0), f))
    out.sort(key=lambda t: (-t[1], float(t[0].price), t[0].machine.created_at))
    return out


def estimate_minutes(manifest: dict, expected_units: int | None, gpu_count: int) -> int:
    """Whole minutes for the hold estimate from the Kit's per-unit GPU-seconds hint; at least 1."""
    hints = (manifest or {}).get("billing_hints") or {}
    per_unit = _num(hints.get("est_gpu_seconds_per_unit"))
    if not expected_units or per_unit is None or per_unit <= 0:
        return 1
    return max(1, math.ceil(expected_units * per_unit / max(1, gpu_count) / 60))


async def validate_kit_job(db: Any, job: Any) -> None:
    """VALIDATING step for a v2 job: the Kit must still exist, be published and intact. Raises JobValidationError."""
    from app.services.kits import KitError, assert_kit_intact, get_kit
    from app.services.scheduler import JobValidationError

    if (getattr(job, "spec_v2", None) or {}).get("byoc") is not None:  # U8
        from app.services.byoc import validate_byoc_job

        await validate_byoc_job(db, job)
        return

    kit = await get_kit(db, job.kit_id or "", job.kit_version or "")
    if kit is None or kit.status != "published":
        raise JobValidationError("kit_not_available")
    try:
        assert_kit_intact(kit)
    except KitError as exc:
        raise JobValidationError("kit_integrity_failed") from exc
    if kit.manifest_sha256 != (job.spec_v2 or {}).get("kit_manifest_sha256"):
        raise JobValidationError("kit_integrity_failed")
    if is_cpu_only_job(job) and not settings.CPU_ONLY_KITS_ENABLED:
        raise JobValidationError("cpu_only_kits_disabled")
    sp = job_spec_dict(job)  # U14: a VM job is refused at validation unless the owner enabled the tier
    if (sp.get("mode") == "vm" or sp.get("os")) and not settings.WINDOWS_VM_ENABLED:
        raise JobValidationError("windows_vm_disabled")


# ---- U13: multi-GPU waiting rule and multi-node group placement (docs/architecture/U13-multi-gpu-design.md) ----

def node_count_of(job: Any) -> int:
    n = effective_resources(job).get("node_count")
    try:
        return max(1, int(n or 1))
    except (TypeError, ValueError):
        return 1


def per_rank_max_cost(run_max_cost: Any, node_count: int) -> Any:
    """U13: the run-wide cap divided by node_count, rounded DOWN to cents, never below MULTI_NODE_MIN_RANK_COST.
    ``run_max_cost`` None = the platform default cap. Total held across ranks never exceeds the run cap (except when the
    minimum per-rank amount itself exceeds cap/n, in which case the caller's budget check refuses the job)."""
    import decimal

    n = max(1, int(node_count))
    total = decimal.Decimal(str(run_max_cost if run_max_cost is not None else settings.MULTI_NODE_DEFAULT_MAX_COST))
    per = (total / n).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_DOWN)
    return max(per, decimal.Decimal(settings.MULTI_NODE_MIN_RANK_COST))


def is_gang_job(job: Any) -> bool:
    """A job that needs several GPUs on one host, or several hosts. These WAIT for capacity instead of failing fast."""
    if not is_v2_job(job):
        return False
    res = effective_resources(job)
    try:
        gpus = int(res.get("gpu_count") or getattr(job, "gpu_count", 0) or 0)
    except (TypeError, ValueError):
        gpus = 0
    return gpus > 1 or node_count_of(job) > 1


def max_wait_seconds() -> int:
    v = _num(getattr(settings, "MULTI_GPU_MAX_WAIT_SECONDS", 1800))
    return 1800 if v is None or v < 0 else int(v)


def wait_expired(job: Any, now: Any) -> bool:
    """True once a queued gang job has waited longer than the configured maximum (measured from job creation).
    Fails closed: an unknown creation time counts as expired, so a job can never wait forever."""
    created = getattr(job, "created_at", None)
    if created is None or now is None:
        return True
    try:
        if getattr(created, "tzinfo", None) is None and getattr(now, "tzinfo", None) is not None:
            created = created.replace(tzinfo=now.tzinfo)
        return (now - created).total_seconds() > max_wait_seconds()
    except TypeError:
        return True


def pair_ok(bench: Any, now: Any) -> bool:
    """One measured direction passes the thresholds and is fresh. Unknown or stale fails closed."""
    if bench is None:
        return False
    lat, bw = _num(getattr(bench, "latency_ms", None)), _num(getattr(bench, "bandwidth_mbps", None))
    if lat is None or bw is None:
        return False
    if lat > float(settings.MULTI_NODE_MAX_LATENCY_MS) or bw < float(settings.MULTI_NODE_MIN_BANDWIDTH_MBPS):
        return False
    at = getattr(bench, "measured_at", None)
    if at is None or now is None:
        return False
    try:
        if getattr(at, "tzinfo", None) is None and getattr(now, "tzinfo", None) is not None:
            at = at.replace(tzinfo=now.tzinfo)
        return 0 <= (now - at).total_seconds() <= float(settings.MULTI_NODE_BENCHMARK_MAX_AGE_SECONDS)
    except TypeError:
        return False


def pick_node_group(machines: list[Any], bench: dict[tuple[Any, Any], Any], want: int, now: Any) -> tuple[str, list[Any]] | None:
    """Choose ``want`` machines from ONE approved datacenter_group where every ordered pair passes pair_ok.

    ``machines`` are already-eligible hosts (the first is preferred as master: input order is the scheduler's ranking).
    ``bench`` maps (from_machine_id, to_machine_id) -> benchmark row. Returns (group label, ordered machines) or None.
    Greedy by ranking order; deterministic. Hosts without a datacenter_group are never used.
    """
    if want < 2 or want > int(settings.MULTI_NODE_MAX_NODES):
        return None
    by_group: dict[str, list[Any]] = {}
    for m in machines:
        label = getattr(m, "datacenter_group", None)
        if label:
            by_group.setdefault(str(label), []).append(m)
    for label in sorted(by_group, key=lambda g: min(machines.index(m) for m in by_group[g])):
        chosen: list[Any] = []
        for m in by_group[label]:
            if all(pair_ok(bench.get((m.id, c.id)), now) and pair_ok(bench.get((c.id, m.id)), now) for c in chosen):
                chosen.append(m)
                if len(chosen) == want:
                    return label, chosen
    return None


def rendezvous_for(master_addr: str, port: int, node_rank: int, nnodes: int, gpus_per_node: int) -> dict:
    """The block delivered in the claim. Values are plain data; the agent turns them into container env."""
    return {"master_addr": master_addr, "master_port": int(port), "node_rank": int(node_rank),
            "nnodes": int(nnodes), "gpus_per_node": int(gpus_per_node),
            "world_size": int(nnodes) * int(gpus_per_node)}
