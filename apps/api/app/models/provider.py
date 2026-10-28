"""Provider, machine, hardware, capability and enrollment-token models (Phase 2).

Telemetry rule (contract §19): heartbeats update the machine row's
``last_heartbeat_at`` + ``last_health`` (JSON) + ``availability`` in place. There
is intentionally NO ``provider_heartbeats`` table.

All column types are portable (Uuid, JSON, Numeric, DateTime(timezone=True),
String/Integer/Boolean) so the models run on SQLite (tests) and Postgres (prod).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, utcnow


class Provider(Base, TimestampMixin):
    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # One provider per user (idempotent registration).
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    machines: Mapped[list["ProviderMachine"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    enrollment_tokens: Mapped[list["EnrollmentToken"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ProviderMachine(Base, TimestampMixin):
    __tablename__ = "provider_machines"
    __table_args__ = (
        UniqueConstraint("provider_id", "hostname", name="uq_provider_machines_provider_hostname"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Lifecycle: registered | deregistered.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    # Operator-controlled: online | offline | maintenance.
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")

    asking_price_per_hour: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Phase B: operator-controlled market policy ---
    # Hard price floor: the marketplace must never quote this machine below it.
    floor_price_per_hour: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    # Recurring windows this machine accepts work in, e.g.
    # [{"days": [0,1,2,3,4], "start": "18:00", "end": "06:00", "tz": "UTC"}].
    availability_windows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Optional whitelist of model version tags; NULL/empty means "any approved".
    model_allowlist: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Machine credential material. ``signing_key`` is the HMAC key (a secret at
    # rest — see app.core.machine_auth for the trust decision). ``secret_hash``
    # is SHA-256 of the issued ``machine_secret`` for audit parity. The raw
    # ``machine_secret`` plaintext is never persisted.
    signing_key: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- ADR-0005: Proof-of-Privacy ---
    stake_bonded: Mapped[decimal.Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=decimal.Decimal("0")
    )
    memory_encryption_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kyc_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tpm_ek_pub: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    provider: Mapped["Provider"] = relationship(back_populates="machines")
    hardware: Mapped["HardwareInventory | None"] = relationship(
        back_populates="machine", cascade="all, delete-orphan", uselist=False
    )
    capability: Mapped["ProviderCapability | None"] = relationship(
        back_populates="machine", cascade="all, delete-orphan", uselist=False
    )

    @property
    def gpu_runtime(self) -> str:
        if self.hardware and self.hardware.gpu_runtime:
            return self.hardware.gpu_runtime
        return "cuda"

    @property
    def gpu_vendor(self) -> str:
        if self.hardware and self.hardware.gpu_vendor:
            return self.hardware.gpu_vendor
        return "nvidia"


class HardwareInventory(Base, TimestampMixin):
    __tablename__ = "hardware_inventory"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # Always "detected" from the agent. Only a future platform benchmark may raise it.
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="detected")
    cpu_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cuda_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gpu_vendor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gpu_runtime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gpu_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Full self-reported capability snapshot (JSON), kept for forensics.
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    machine: Mapped["ProviderMachine"] = relationship(back_populates="hardware")


class ProviderCapability(Base, TimestampMixin):
    __tablename__ = "provider_capabilities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    supported_workloads: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    supported_isolation_tiers: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["container"])
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="detected")

    machine: Mapped["ProviderMachine"] = relationship(back_populates="capability")


class MachineAttestation(Base, TimestampMixin):
    """Cryptographic remote attestation report from a machine (Phase 8)."""

    __tablename__ = "machine_attestations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    isolation_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="confidential")
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    pcr_measurements: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    platform_quote: Mapped[str] = mapped_column(String(1024), nullable=False)
    signature: Mapped[str] = mapped_column(String(512), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="verified")
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    machine: Mapped["ProviderMachine"] = relationship()


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True, index=True
    )

    provider: Mapped["Provider"] = relationship(back_populates="enrollment_tokens")
