"""Multi-node: network benchmarks, group placement and rendezvous (U13, docs/architecture/U13-multi-gpu-design.md).

Nothing here touches the ledger. Rank 0 is the customer's job; other ranks are ordinary v2 jobs pinned to one host of
the same approved ``datacenter_group`` and billed by the existing per-job pipeline. Written, never run.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import ipaddress
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import constant_time_compare
from app.db.base import utcnow
from app.models.jobs import Job
from app.models.node_group import GROUP_PLACED, NetworkBenchmark, NodeGroup, NodeGroupMember
from app.models.provider import ProviderMachine
from app.services import job_fsm as fsm
from app.services import scheduler_v2

LABEL_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_LATENCY_REPORT_MS = 10_000.0
MAX_BANDWIDTH_REPORT_MBPS = 1_000_000.0


class MultiNodeError(Exception):
    def __init__(self, code: str, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def valid_label(label: Any) -> bool:
    return isinstance(label, str) and bool(LABEL_RE.match(label))


def valid_cluster_addr(addr: Any) -> bool:
    """A literal private (RFC 1918 / ULA) address. Never a hostname, never a public or loopback address."""
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    return ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def make_nonce(prober_id: uuid.UUID, peer_id: uuid.UUID, expires_unix: int) -> str:
    """Stateless nonce the prober hands to the peer. ``expiry.mac``; the mac uses a key derived from the server secret."""
    msg = f"bench\n{prober_id}\n{peer_id}\n{expires_unix}".encode()
    mac = hmac.new(settings.AUTH_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{expires_unix}.{mac}"


def nonce_ok(nonce: str, prober_id: uuid.UUID, peer_id: uuid.UUID, now_unix: int) -> bool:
    try:
        exp_s, mac = nonce.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < now_unix or exp > now_unix + int(settings.MULTI_NODE_BENCHMARK_NONCE_TTL_SECONDS) + 5:
        return False
    return constant_time_compare(make_nonce(prober_id, peer_id, exp), nonce)


def peer_proof(peer_signing_key: str, nonce: str, prober_id: uuid.UUID) -> str:
    """What the peer agent returns from its benchmark endpoint: HMAC(peer machine key, nonce | prober)."""
    return hmac.new(peer_signing_key.encode(), f"{nonce}\n{prober_id}".encode(), hashlib.sha256).hexdigest()


async def benchmark_plan(db: AsyncSession, prober: ProviderMachine, now: dt.datetime) -> list[dict]:
    """Peers this host should probe: same approved datacenter_group, known cluster_addr. Empty when the host has no group."""
    if not prober.datacenter_group:
        return []
    peers = (await db.execute(select(ProviderMachine).where(
        ProviderMachine.datacenter_group == prober.datacenter_group, ProviderMachine.id != prober.id,
        ProviderMachine.status != "deregistered", ProviderMachine.cluster_addr.is_not(None)))).scalars().all()
    exp = int(now.timestamp()) + int(settings.MULTI_NODE_BENCHMARK_NONCE_TTL_SECONDS)
    return [{"peer_id": str(p.id), "addr": p.cluster_addr, "nonce": make_nonce(prober.id, p.id, exp)} for p in peers]


async def store_benchmark(db: AsyncSession, prober: ProviderMachine, body: dict, now: dt.datetime) -> int:
    """Validate and upsert a report. Each result needs the peer's proof, so a host cannot invent a fast link to a
    peer that never answered. Returns the number of pairs stored; raises MultiNodeError on a malformed body."""
    if not prober.datacenter_group:
        raise MultiNodeError("no_datacenter_group", 409)
    addr = body.get("own_addr")
    if addr is not None:
        if not valid_cluster_addr(addr):
            raise MultiNodeError("invalid_cluster_addr")
        prober.cluster_addr = str(ipaddress.ip_address(addr))
    results = body.get("results") or []
    if not isinstance(results, list) or len(results) > 64:
        raise MultiNodeError("invalid_results")
    stored = 0
    for r in results:
        if not isinstance(r, dict):
            raise MultiNodeError("invalid_results")
        try:
            peer_id = uuid.UUID(str(r.get("peer_id")))
            lat, bw = float(r["latency_ms"]), float(r["bandwidth_mbps"])
            samples = int(r.get("samples", 1))
        except (KeyError, ValueError, TypeError):
            raise MultiNodeError("invalid_results") from None
        if not (0 < lat <= MAX_LATENCY_REPORT_MS and 0 < bw <= MAX_BANDWIDTH_REPORT_MBPS and 1 <= samples <= 10_000):
            raise MultiNodeError("invalid_results")
        peer = (await db.execute(select(ProviderMachine).where(ProviderMachine.id == peer_id))).scalar_one_or_none()
        if peer is None or peer.datacenter_group != prober.datacenter_group or peer.id == prober.id:
            continue  # unknown peer or a different group: ignored, never stored
        nonce, proof = str(r.get("nonce") or ""), str(r.get("peer_proof") or "")
        if not nonce_ok(nonce, prober.id, peer.id, int(now.timestamp())):
            continue
        if not constant_time_compare(peer_proof(peer.signing_key, nonce, prober.id), proof):
            continue
        row = (await db.execute(select(NetworkBenchmark).where(
            NetworkBenchmark.from_machine_id == prober.id, NetworkBenchmark.to_machine_id == peer.id))).scalar_one_or_none()
        if row is None:
            row = NetworkBenchmark(from_machine_id=prober.id, to_machine_id=peer.id)
            db.add(row)
        row.latency_ms, row.bandwidth_mbps, row.samples, row.measured_at = lat, bw, samples, now
        stored += 1
    await db.flush()
    return stored


async def load_benchmarks(db: AsyncSession, machine_ids: list[uuid.UUID]) -> dict[tuple[uuid.UUID, uuid.UUID], NetworkBenchmark]:
    if not machine_ids:
        return {}
    rows = (await db.execute(select(NetworkBenchmark).where(
        NetworkBenchmark.from_machine_id.in_(machine_ids), NetworkBenchmark.to_machine_id.in_(machine_ids)))).scalars().all()
    return {(r.from_machine_id, r.to_machine_id): r for r in rows}


async def select_group(db: AsyncSession, job: Job, ranked_machines: list[ProviderMachine], now: dt.datetime):
    """(label, machines) for a multi-node job, or None. Fails closed when the feature is off."""
    if not settings.MULTI_NODE_ENABLED:
        return None
    ids = [m.id for m in ranked_machines if m.datacenter_group]
    bench = await load_benchmarks(db, ids)
    return scheduler_v2.pick_node_group(ranked_machines, bench, scheduler_v2.node_count_of(job), now)


async def create_group(db: AsyncSession, job: Job, label: str, machines: list[ProviderMachine], schedule) -> NodeGroup:
    """Record the group (rank 0 = ``job`` on machines[0]) and create one pinned sibling job per other rank.
    ``schedule`` is ``scheduler.schedule_job`` (passed in to avoid an import cycle). Each sibling is billed on its own."""
    group = NodeGroup(master_job_id=job.id, datacenter_group=label, node_count=len(machines),
                      master_port=int(settings.MULTI_NODE_MASTER_PORT), status=GROUP_PLACED)
    db.add(group)
    await db.flush()
    db.add(NodeGroupMember(group_id=group.id, rank=0, job_id=job.id, machine_id=machines[0].id))
    # Per-rank cap (design note section 2, billing): max_cost is the cap of the WHOLE run, split equally across ranks.
    # jobs_v2 already stored the per-rank share on rank 0; recompute from the run cap when present so both agree.
    he = (job.spec_v2 or {}).get("hold_estimate") or {}
    if he.get("rank_max_cost") is not None:
        rank_cap = __import__("decimal").Decimal(str(he["rank_max_cost"]))
    else:
        rank_cap = scheduler_v2.per_rank_max_cost(he.get("run_max_cost", job.max_cost), len(machines))
    for rank, m in enumerate(machines[1:], start=1):
        spec = copy.deepcopy(job.spec_v2 or {})
        eff = dict(spec.get("effective_resources") or {})
        eff["node_count"] = 1  # a sibling is a single-node job: no recursive grouping
        spec["effective_resources"] = eff
        sib = Job(
            customer_id=job.customer_id, project_id=job.project_id, workload_type=job.workload_type, model=job.model,
            input=job.input, status=fsm.CREATED, gpu_count=job.gpu_count, estimated_minutes=job.estimated_minutes,
            max_cost=rank_cap, max_runtime_seconds=job.max_runtime_seconds, retry_policy={"max_retries": 0},
            priority=job.priority, execution_mode=job.execution_mode, isolation_tier=job.isolation_tier,
            required_gpu_runtime=job.required_gpu_runtime, network_policy=job.network_policy,
            data_policy=copy.deepcopy(job.data_policy), machine_id=m.id, spec_v2=spec, kit_id=job.kit_id,
            kit_version=job.kit_version,
        )
        db.add(sib)
        await db.flush()
        await fsm.transition(db, sib, fsm.VALIDATING, reason="node_group_member", actor="system")
        await fsm.transition(db, sib, fsm.QUEUED, reason="node_group_member", actor="system")
        db.add(NodeGroupMember(group_id=group.id, rank=rank, job_id=sib.id, machine_id=m.id))
        await db.flush()
        try:
            await schedule(db, sib)  # may stay QUEUED; the sweeper retries it under the gang waiting rule
        except Exception:  # InsufficientFunds etc.: the sibling stays QUEUED and is failed by the wait rule
            pass
    return group


async def rendezvous_for_job(db: AsyncSession, job: Job) -> tuple[bool, dict | None]:
    """(is_member, rendezvous). is_member with None means rendezvous is not available yet: the claim must refuse."""
    mem = (await db.execute(select(NodeGroupMember).where(NodeGroupMember.job_id == job.id))).scalar_one_or_none()
    if mem is None:
        return False, None
    group = (await db.execute(select(NodeGroup).where(NodeGroup.id == mem.group_id))).scalar_one()
    master = (await db.execute(select(NodeGroupMember).where(
        NodeGroupMember.group_id == group.id, NodeGroupMember.rank == 0))).scalar_one()
    m = (await db.execute(select(ProviderMachine).where(ProviderMachine.id == master.machine_id))).scalar_one_or_none()
    if m is None or not m.cluster_addr or not valid_cluster_addr(m.cluster_addr):
        return True, None
    return True, scheduler_v2.rendezvous_for(m.cluster_addr, group.master_port, mem.rank, group.node_count,
                                             int(job.gpu_count or 1))


# ---- rank failure propagation (docs/architecture/U13-multi-gpu-design.md, section 2) ----

_RANK_BAD = frozenset({fsm.FAILED, fsm.CANCELLED, fsm.TIMED_OUT})


async def _cancel_rank_job(db: AsyncSession, job: Job) -> None:
    """The v1 customer-cancel sequence (routers/jobs.py::cancel_job), read-only reuse of its building blocks: flag, free the
    machine reservation, FSM -> CANCELLED, release the hold in full (a cancelled job is never charged). The agent sees the
    job leave RUNNING at its next job heartbeat (409) and stops its container."""
    from app.services import billing
    from app.services.leases import release_job_reservation

    if job.status not in fsm.CANCELLABLE_STATES:
        return  # already cancelled/terminal: no second FSM transition and no second release
    job.cancel_requested = True
    await release_job_reservation(db, job, utcnow())
    await fsm.transition(db, job, fsm.CANCELLED, reason="node_group_rank_failed", actor="system")
    await billing.release_hold(db, job)  # idempotent per job (ledger key release_hold:<job_id>)


async def propagate_group_failure(db: AsyncSession, group: NodeGroup) -> int:
    """If any rank job of ``group`` failed, was cancelled or timed out, cancel the still-live sibling ranks. Returns the
    number cancelled. Marks the group failed (or done when every rank completed)."""
    from app.models.node_group import GROUP_DONE, GROUP_FAILED

    if group.status in (GROUP_DONE, GROUP_FAILED):
        return 0  # exactly-once: a finished group is never swept again (siblings were cancelled and their holds released)
    members = (await db.execute(select(NodeGroupMember).where(NodeGroupMember.group_id == group.id))).scalars().all()
    jobs = {}
    for m in members:
        j = (await db.execute(select(Job).where(Job.id == m.job_id).with_for_update())).scalar_one_or_none()
        if j is not None:
            jobs[m.job_id] = j
    if not jobs:
        return 0
    if not any(j.status in _RANK_BAD for j in jobs.values()):
        if len(jobs) == len(members) and all(j.status == fsm.COMPLETED for j in jobs.values()):
            group.status = GROUP_DONE
        return 0
    n = 0
    for j in jobs.values():
        if j.status in fsm.CANCELLABLE_STATES:
            await _cancel_rank_job(db, j)
            n += 1
    group.status = GROUP_FAILED
    await db.flush()
    return n


async def sweep_rank_failures(sm: Any) -> dict:
    """Worker step. One transaction and one try/except per group: a broken group never blocks the others."""
    from app.models.node_group import GROUP_RUNNING

    import logging

    log = logging.getLogger(__name__)
    totals = {"groups": 0, "cancelled": 0, "errors": 0}
    async with sm() as db:
        ids = list((await db.execute(select(NodeGroup.id).where(
            NodeGroup.status.in_((GROUP_PLACED, GROUP_RUNNING))).order_by(NodeGroup.created_at).limit(200))).scalars().all())
    for gid in ids:
        try:
            async with sm() as db:
                group = (await db.execute(select(NodeGroup).where(NodeGroup.id == gid).with_for_update(skip_locked=True))
                         ).scalar_one_or_none()
                if group is None or group.status not in (GROUP_PLACED, GROUP_RUNNING):
                    continue
                totals["cancelled"] += await propagate_group_failure(db, group)
                await db.commit()
            totals["groups"] += 1
        except Exception:
            log.exception("multi-node rank failure sweep failed for group %s (will retry next sweep)", gid)
            totals["errors"] += 1
    return totals
