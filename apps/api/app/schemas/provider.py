"""Provider / machine / agent request & response schemas (Phase 2 contract)."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------
class ProviderRegisterRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    contact_email: str | None = Field(default=None, max_length=320)


class ProviderOut(BaseModel):
    provider_id: uuid.UUID
    display_name: str | None
    contact_email: str | None = None
    status: str
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Enrollment tokens
# ---------------------------------------------------------------------------
class EnrollmentTokenCreate(BaseModel):
    label: str | None = Field(default=None, max_length=255)


class EnrollmentTokenCreatedOut(BaseModel):
    enrollment_token_id: uuid.UUID
    enrollment_token: str  # shown once, never again
    expires_at: dt.datetime
    label: str | None


class EnrollmentTokenOut(BaseModel):
    """List view — never exposes the raw secret."""

    id: uuid.UUID
    label: str | None
    expires_at: dt.datetime
    used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    machine_id: uuid.UUID | None
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Machines (management views)
# ---------------------------------------------------------------------------
class HardwareOut(BaseModel):
    gpu_model: str | None = None
    gpu_count: int = 0
    vram_mb: int = 0
    cpu_model: str | None = None
    cpu_cores: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    cuda_version: str | None = None


class MachineOut(BaseModel):
    machine_id: uuid.UUID
    hostname: str
    agent_version: str | None
    status: str  # derived: online | offline | maintenance
    availability: str
    verification_status: str
    asking_price_per_hour: str | None
    max_concurrent_jobs: int
    region: str | None
    last_heartbeat_at: dt.datetime | None
    hardware: HardwareOut
    supported_workloads: list[str]
    created_at: dt.datetime


class MachineDetailOut(MachineOut):
    last_health: dict | None = None


class MachineUpdate(BaseModel):
    availability: str | None = None
    asking_price_per_hour: decimal.Decimal | None = None
    max_concurrent_jobs: int | None = Field(default=None, ge=1)
    region: str | None = Field(default=None, max_length=64)
    supported_workloads: list[str] | None = None


# ---------------------------------------------------------------------------
# Agent: enrollment
# ---------------------------------------------------------------------------
class GpuReport(BaseModel):
    model: str | None = None
    vram_mb: int = 0
    cuda_version: str | None = None


class CapabilityReport(BaseModel):
    verification_status: str = "detected"
    cpu_model: str | None = None
    cpu_cores: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    gpus: list[GpuReport] = Field(default_factory=list)
    supported_workloads: list[str] = Field(default_factory=list)


class AgentEnrollRequest(BaseModel):
    enrollment_token: str
    agent_version: str | None = None
    hostname: str = Field(min_length=1, max_length=255)
    capability_report: CapabilityReport


class AgentEnrollResponse(BaseModel):
    machine_id: uuid.UUID
    machine_secret: str  # shown once
    heartbeat_interval_seconds: int
    online_window_seconds: int


# ---------------------------------------------------------------------------
# Agent: heartbeat
# ---------------------------------------------------------------------------
class HeartbeatCapabilitySummary(BaseModel):
    gpu_count: int = 0
    vram_mb: int = 0


class HeartbeatHealth(BaseModel):
    model_config = ConfigDict(extra="allow")

    gpu_temp_c: float | None = None
    gpu_util_pct: float | None = None
    mem_used_mb: int | None = None
    uptime_s: int = 0


class HeartbeatRequest(BaseModel):
    availability: str
    capability_summary: HeartbeatCapabilitySummary = Field(
        default_factory=HeartbeatCapabilitySummary
    )
    health: HeartbeatHealth = Field(default_factory=HeartbeatHealth)


class HeartbeatResponse(BaseModel):
    ok: bool = True
    directives: dict = Field(default_factory=dict)
