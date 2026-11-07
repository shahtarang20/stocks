"""Provider / machine / agent request & response schemas (Phase 2 contract)."""

from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    gpu_vendor: str | None = None
    gpu_runtime: str | None = None
    gpu_runtime_version: str | None = None


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
    supported_isolation_tiers: list[str] = Field(default_factory=lambda: ["container"])
    created_at: dt.datetime
    # W1 / W10 fleet view: the host's reported OS / runtime backend and capability flags (sanitised; None = a legacy
    # agent that has not reported them).
    platform: dict[str, str] | None = None
    capabilities: list[str] | None = None


class MachineDetailOut(MachineOut):
    last_health: dict | None = None


class MachineUpdate(BaseModel):
    availability: str | None = None
    asking_price_per_hour: decimal.Decimal | None = None
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=1024)  # Integer column
    region: str | None = Field(default=None, max_length=64)
    supported_workloads: list[str] | None = None
    supported_isolation_tiers: list[str] | None = None


# ---------------------------------------------------------------------------
# Agent: enrollment
# ---------------------------------------------------------------------------
_INT32_MAX = 2**31 - 1  # Integer DB columns: an unbounded agent-supplied value would overflow on Postgres (HTTP 500)


class GpuReport(BaseModel):
    model: str | None = Field(default=None, max_length=255)
    vram_mb: int = Field(default=0, ge=0, le=_INT32_MAX)
    # Column widths of hardware_inventory: a longer value would be a Postgres error (HTTP 500) at enrollment.
    cuda_version: str | None = Field(default=None, max_length=64)
    gpu_vendor: str | None = Field(default=None, max_length=32)
    gpu_runtime: str | None = Field(default=None, max_length=32)
    gpu_runtime_version: str | None = Field(default=None, max_length=64)


class CapabilityReport(BaseModel):
    verification_status: str = "detected"
    cpu_model: str | None = Field(default=None, max_length=255)
    cpu_cores: int = Field(default=0, ge=0, le=_INT32_MAX)
    ram_mb: int = Field(default=0, ge=0, le=_INT32_MAX)
    disk_gb: int = Field(default=0, ge=0, le=_INT32_MAX)
    gpus: list[GpuReport] = Field(default_factory=list)
    supported_workloads: list[str] = Field(default_factory=list)
    isolation_tiers: list[str] = Field(default_factory=lambda: ["container"])


class AgentEnrollRequest(BaseModel):
    enrollment_token: str
    agent_version: str | None = Field(default=None, max_length=64)
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
    gpu_count: int = Field(default=0, ge=0, le=1024)
    vram_mb: int = Field(default=0, ge=0, le=_INT32_MAX)


# Windows supervisor health keys (see HeartbeatHealth.runtime / update / availability_detail): short ASCII only.
_HEALTH_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
_HEALTH_CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_AVAILABILITY_DETAIL_STATES = frozenset({"online", "throttled", "yielding", "maintenance", "draining"})


class HeartbeatHealth(BaseModel):
    model_config = ConfigDict(extra="allow")

    gpu_temp_c: float | None = None
    gpu_util_pct: float | None = None
    mem_used_mb: int | None = None
    uptime_s: int = 0
    # U7: Kits whose images this machine has cached ("slug@version"). Self-reported; ranking hint only.
    kit_caches: list[str] | None = None
    # U8: isolation runtimes the host can run ("gvisor", "microvm"). Self-reported; BYOC is placed only where listed.
    isolation_runtimes: list[str] | None = None
    # U14: the host can run a Windows VM with GPU passthrough (vm_runner.advertise()). Self-reported; only widens WHERE a
    # customer's own VM may run. Anything but a real boolean is dropped (never truthy-coerced).
    vfio_gpu: bool | None = None
    # CPU-only Kits: the host opted in to run them (agent setting cpu_only_kits_enabled). Self-reported; it only widens
    # WHERE a CPU-only Kit may run and is honoured only while CPU_ONLY_KITS_ENABLED is on.
    cpu_capable: bool | None = None
    # W1 / X4 (cross-platform plan): the host's OS and the capability flags it can prove. Self-reported and sanitised here
    # (known keys / known flags only, short ASCII, size-capped); placement uses them in app.services.capability_flags.
    platform: dict | None = None
    capabilities: list[str] | None = None
    # Windows supervisor (W2/W3/W6) additive health keys. Self-reported and sanitised here; app.services.observability
    # reads ``runtime.distro_ok``, ``runtime.gpu_smoke_ok``, ``update.rolled_back`` and ``availability_detail.state``.
    runtime: dict | None = None
    update: dict | None = None
    availability_detail: dict | None = None

    @field_validator("runtime", mode="before")
    @classmethod
    def _sanitize_runtime(cls, v):
        # Booleans only: a missing or odd value is "not reported", never truthy-coerced. Unknown keys are dropped.
        if v is None or not isinstance(v, dict):
            return None
        return {k: v[k] for k in ("distro_ok", "gpu_smoke_ok") if isinstance(v.get(k), bool)}

    @field_validator("update", mode="before")
    @classmethod
    def _sanitize_update(cls, v):
        if v is None or not isinstance(v, dict):
            return None
        out: dict = {}
        if isinstance(v.get("rolled_back"), bool):
            out["rolled_back"] = v["rolled_back"]
        version = v.get("version")
        if isinstance(version, str) and _HEALTH_VERSION_RE.fullmatch(version):
            out["version"] = version
        return out

    @field_validator("availability_detail", mode="before")
    @classmethod
    def _sanitize_availability_detail(cls, v):
        if v is None or not isinstance(v, dict):
            return None
        out: dict = {}
        state = v.get("state")
        if isinstance(state, str) and state in _AVAILABILITY_DETAIL_STATES:
            out["state"] = state
        for key in ("code", "reason"):
            raw = v.get(key)
            if isinstance(raw, str) and _HEALTH_CODE_RE.fullmatch(raw):
                out[key] = raw
        return out

    @field_validator("platform", mode="before")
    @classmethod
    def _sanitize_platform(cls, v):
        from app.services.capability_flags import sanitize_platform

        return sanitize_platform(v)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _sanitize_capabilities(cls, v):
        from app.services.capability_flags import sanitize_capabilities

        return sanitize_capabilities(v)

    @field_validator("vfio_gpu", "cpu_capable", mode="before")
    @classmethod
    def _sanitize_vfio_gpu(cls, v):
        return v if isinstance(v, bool) else None

    @field_validator("isolation_runtimes", mode="before")
    @classmethod
    def _sanitize_isolation_runtimes(cls, v):
        if not isinstance(v, (list, tuple)):
            return None if v is None else []
        return sorted({x for x in v if x in ("gvisor", "microvm")})

    @field_validator("kit_caches", mode="before")
    @classmethod
    def _sanitize_kit_caches(cls, v):
        from app.services.scheduler_v2 import sanitize_kit_caches

        return sanitize_kit_caches(v)


class HeartbeatRequest(BaseModel):
    availability: str
    capability_summary: HeartbeatCapabilitySummary = Field(
        default_factory=HeartbeatCapabilitySummary
    )
    health: HeartbeatHealth = Field(default_factory=HeartbeatHealth)


class HeartbeatResponse(BaseModel):
    ok: bool = True
    directives: dict = Field(default_factory=dict)
