"""Multi-node placement (U13, docs/architecture/U13-multi-gpu-design.md).

* ``NetworkBenchmark``: the latest measured latency and bandwidth for one directed host pair, reported by the
  prober's agent and proven by a signature from the peer (the peer's machine signing key over the API nonce).
* ``NodeGroup`` / ``NodeGroupMember``: one multi-node job. Rank 0 is the job the customer submitted; every other rank
  is an ordinary v2 job pinned to one host of the same ``datacenter_group`` (so each host is billed by the existing
  per-job pipeline and no money code changes).

Written, never run. Migration: 0037_multi_gpu_node.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

GROUP_PLACED = "placed"
GROUP_RUNNING = "running"
GROUP_DONE = "done"
GROUP_FAILED = "failed"


class NetworkBenchmark(Base):
    __tablename__ = "network_benchmarks"
    __table_args__ = (
        UniqueConstraint("from_machine_id", "to_machine_id", name="uq_network_benchmarks_pair"),
        Index("ix_network_benchmarks_to", "to_machine_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    from_machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False
    )
    to_machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False
    )
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    bandwidth_mbps: Mapped[float] = mapped_column(Float, nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class NodeGroup(Base):
    __tablename__ = "node_groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    master_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    datacenter_group: Mapped[str] = mapped_column(String(64), nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    master_port: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=GROUP_PLACED)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class NodeGroupMember(Base):
    __tablename__ = "node_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "rank", name="uq_node_group_members_rank"),
        UniqueConstraint("job_id", name="uq_node_group_members_job"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("node_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
