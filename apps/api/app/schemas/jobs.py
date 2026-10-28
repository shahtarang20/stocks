"""Job request & response schemas (Phase 4).

Money is serialized as a 2dp decimal STRING (never a float). Timestamps are
RFC 3339 UTC (FastAPI serializes aware datetimes).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

PRIVACY_TIERS = ("sealed", "sealed_pro", "confidential")


# ---------------------------------------------------------------------------
# Customer: submit
# ---------------------------------------------------------------------------
class RetryPolicy(BaseModel):
    max_retries: int = Field(default=0, ge=0, le=10)


class DataPolicy(BaseModel):
    """Customer rules for where a private job may run (ADR-0005 section 4). Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    classification: str | None = Field(default=None, max_length=64)
    allowed_regions: list[str] | None = Field(default=None, max_length=32)
    min_stake: decimal.Decimal | None = Field(default=None, ge=0, le=decimal.Decimal("1000000000"))
    # X25519 public key (hex) the sealed host encrypts the result to. Required for privacy tiers.
    result_public_key: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class JobCreate(BaseModel):
    # Avoid pydantic's protected "model_" namespace warning for the `model` field.
    model_config = ConfigDict(protected_namespaces=())

    workload_type: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    input: dict = Field(default_factory=dict)
    machine_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    gpu_count: int = Field(default=1, ge=1, le=64)
    estimated_minutes: int = Field(default=1, gt=0)
    # A spending cap must be positive — a negative/zero budget is rejected at
    # validation (defense-in-depth; the hold/budget math never sees a negative).
    max_cost: decimal.Decimal | None = Field(default=None, gt=0)
    max_runtime_seconds: int | None = Field(default=None, gt=0)
    retry_policy: RetryPolicy | None = None
    priority: int = Field(default=0, ge=0)
    isolation_tier: str = Field(
        default="container", pattern="^(container|open|gvisor|microvm|sealed|sealed_pro|confidential)$"
    )
    data_policy: DataPolicy | None = None
    # ccpp-v1 data envelope (see app/services/privacy_crypto.py); the platform never sees plaintext.
    encrypted_input: dict | None = None
    # {guardian_name: {"x": int, "envelope": sealed ccpp-v1 envelope}}; shape checked at submit.
    dk_shares: dict | None = None
    value_at_risk: decimal.Decimal | None = Field(
        default=None, ge=0, le=decimal.Decimal("1000000000"), max_digits=12, decimal_places=2
    )

    @model_validator(mode="after")
    def _privacy_fields_match_tier(self) -> "JobCreate":
        if self.isolation_tier in PRIVACY_TIERS:
            if self.input:
                raise ValueError("privacy tiers take encrypted_input only; send an empty input")
            if not self.encrypted_input or not self.dk_shares:
                raise ValueError("privacy tiers need encrypted_input and dk_shares")
            if not self.data_policy or not self.data_policy.result_public_key:
                raise ValueError("privacy tiers need data_policy.result_public_key")
        elif self.encrypted_input is not None or self.dk_shares is not None:
            raise ValueError("encrypted_input and dk_shares are only for sealed, sealed_pro or confidential jobs")
        return self


# ---------------------------------------------------------------------------
# Customer: read
# ---------------------------------------------------------------------------
class JobOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    job_id: uuid.UUID
    customer_id: uuid.UUID
    project_id: uuid.UUID | None = None
    workload_type: str
    model: str
    input: dict
    status: str
    execution_mode: str
    isolation_tier: str = "container"
    machine_id: uuid.UUID | None = None
    reservation_id: uuid.UUID | None = None
    gpu_count: int
    estimated_minutes: int
    max_cost: str | None = None
    max_runtime_seconds: int
    retry_policy: dict
    attempts_made: int
    priority: int
    idempotency_key: str | None = None
    lease_expires_at: dt.datetime | None = None
    match_explanation: dict | None = None
    verification: dict | None = None
    output: dict | None = None
    encrypted_output: dict | None = None
    data_policy: dict | None = None
    value_at_risk: str | None = None
    privacy_receipt_id: uuid.UUID | None = None
    metrics: dict | None = None
    final_cost: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None


class JobEventOut(BaseModel):
    seq: int
    from_state: str | None = None
    to_state: str
    reason: str | None = None
    actor: str
    created_at: dt.datetime


class ArtifactOut(BaseModel):
    artifact_id: uuid.UUID
    kind: str
    size: int
    sha256: str
    content_type: str
    created_at: dt.datetime


class ArtifactDownloadOut(BaseModel):
    artifact_id: uuid.UUID
    url: str
    expires_at: dt.datetime


class JobLogsOut(BaseModel):
    job_id: uuid.UUID
    lines: list[str]


class JobCancelOut(BaseModel):
    job_id: uuid.UUID
    status: str
    cancel_requested: bool


class ArtifactUploadResult(BaseModel):
    artifact_id: uuid.UUID
    sha256: str
    size: int
    kind: str
    content_type: str


# ---------------------------------------------------------------------------
# Agent protocol
# ---------------------------------------------------------------------------
class ClaimRequest(BaseModel):
    machine_id: uuid.UUID


class ArtifactUploadGrant(BaseModel):
    url: str
    token: str
    expires_at: dt.datetime


class ClaimLimits(BaseModel):
    max_runtime_seconds: int
    memory_mb: int
    cpus: str
    gpu_count: int


class ClaimResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    job_id: uuid.UUID
    workload_type: str
    model: str
    input: dict
    execution_mode: str
    isolation_tier: str = "container"
    limits: ClaimLimits
    image_digest: str | None = None
    artifact_upload: ArtifactUploadGrant
    lease_expires_at: dt.datetime
    encrypted_input: dict | None = None
    result_public_key: str | None = None
    # Kept for older agents (R10); always null since WP-B: keys come from /jobs/{id}/keys.
    wrapped_shares: list[dict] | None = None
    verdict_id: str | None = None
    # WP-B: a private claim carries no keys. The agent attests with this nonce (fresh keys for
    # this job), then calls POST /v1/agents/jobs/{job_id}/keys with the job-bound verdict.
    key_attestation_required: bool = False
    key_attestation_nonce: str | None = None


class JobHeartbeatRequest(BaseModel):
    progress: float | None = None
    metrics: dict | None = None


class JobHeartbeatResponse(BaseModel):
    ok: bool = True
    lease_expires_at: dt.datetime


class JobLogsAppendRequest(BaseModel):
    lines: list[str]


class JobLogsAppendResponse(BaseModel):
    ok: bool = True
    stored_lines: int


class ResultArtifactRef(BaseModel):
    artifact_id: uuid.UUID
    kind: str = "output"
    sha256: str | None = None
    size: int | None = None
    content_type: str | None = None


class JobResultRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    status: str  # "success" | "failed"
    execution_mode: str = "simulated"
    output: dict | None = None
    artifacts: list[ResultArtifactRef] = Field(default_factory=list)
    metrics: dict | None = None
    evidence: dict | None = None
    error: dict | None = None
    # Optional provider attestation of the model artifact it actually loaded.
    # Compared against the registry's pinned digest (model identity audit); a
    # provider that reports nothing is simply not audited on this dimension.
    model_weight_hash: str | None = Field(default=None, max_length=128)
    encrypted_output: dict | None = None
    output_commitment: str | None = None
    wipe_receipt: dict | None = None


class JobResultResponse(BaseModel):
    job_id: uuid.UUID
    status: str
