"""Database models for Proof-of-Privacy (ADR-0005).

Includes:
- TransparencyLogLeaf: append-only Merkle tree leaves for public auditability.
- PrivacyReceipt: cryptographic proof returned with each job.
- CanaryJob: traceable honeypot tokens to detect host snooping.
- TrustedImage: published reference OS image hashes and expected PCRs.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class TransparencyLogLeaf(Base, TimestampMixin):
    """An entry in the append-only Merkle Transparency Log.

    Records image hashes, attestation verdicts, guardian key releases, and wipe receipts.
    Root hashes are periodically anchored on-chain.
    """

    __tablename__ = "transparency_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    leaf_index: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    leaf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)
    anchored_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    anchored_block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class PrivacyReceipt(Base, TimestampMixin):
    """Cryptographic Privacy Receipt returned to customer upon job completion."""

    __tablename__ = "privacy_receipts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True, index=True
    )
    isolation_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    image_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_verdict: Mapped[dict] = mapped_column(JSON, nullable=False)
    guardian_releases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    wipe_receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)
    inclusion_proofs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CanaryJob(Base, TimestampMixin):
    """Canary tracking for honeytoken leak detection and automated host slashing."""

    __tablename__ = "canary_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canary_token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    canary_type: Mapped[str] = mapped_column(String(32), nullable=False, default="api_key")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    detected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TrustedImage(Base, TimestampMixin):
    """Catalog of approved, reproducible Sealed/Confidential OS image hashes & expected PCRs."""

    __tablename__ = "trusted_images"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    image_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    version_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False, default="sealed")
    expected_pcrs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
