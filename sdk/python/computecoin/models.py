"""Typed request/response models (Pydantic)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, Field, model_validator


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: Optional[int] = None


class Project(BaseModel):
    id: str
    name: str
    created_at: Optional[str] = None


class ApiKey(BaseModel):
    id: str
    # Only shown once at creation time; afterwards only the prefix is returned. The API names this field
    # ``api_key``; ``key`` is accepted too so older servers and fixtures keep working.
    key: Optional[str] = Field(default=None, validation_alias=AliasChoices("api_key", "key"))
    prefix: str
    name: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletion(BaseModel):
    """OpenAI-compatible chat completion shape (subset)."""

    id: str
    object: str = "chat.completion"
    model: str
    choices: list[dict[str, Any]]
    usage: Optional[dict[str, Any]] = None


class EmbeddingResponse(BaseModel):
    object: str = "list"
    model: str
    data: list[dict[str, Any]]
    usage: Optional[dict[str, Any]] = None


# -- Phase 3: marketplace / quotes / reservations ------------------------- #
# NOTE: every money field is a decimal STRING with 2 fraction digits (e.g.
# "1.50"), computed server-side with Decimal. The SDK keeps them as ``str`` and
# NEVER coerces to float, so no precision is lost. Credits ("USDC_CREDITS") are
# simulated test credits in Phase 3 -- there is no real payment rail.


class MachineReliability(BaseModel):
    completed_jobs: int = 0
    # ``None`` until completed-job history exists -- never fabricate a rating.
    success_rate: Optional[str] = None


class MarketplaceMachine(BaseModel):
    machine_id: str
    gpu_model: Optional[str] = None
    gpu_count: int = 0
    vram_mb: int = 0
    cpu_model: Optional[str] = None
    cpu_cores: int = 0
    ram_mb: int = 0
    region: Optional[str] = None
    verification_status: Optional[str] = None
    price_per_hour: str  # money -> string
    currency: str = "USDC_CREDITS"
    supported_workloads: list[str] = []
    available_slots: int = 0
    max_concurrent_jobs: int = 0
    reliability: Optional[MachineReliability] = None
    last_heartbeat_at: Optional[str] = None
    gpu_vendor: Optional[str] = None
    gpu_runtime: Optional[str] = None
    gpu_runtime_version: Optional[str] = None


class MachinePage(BaseModel):
    items: list[MarketplaceMachine]
    page: int
    page_size: int
    total: int


class QuoteBreakdown(BaseModel):
    # All money / rate fields are decimal strings.
    price_per_hour: str
    estimated_hours: str
    gpu_cost: str
    platform_fee_rate: str
    platform_fee: str
    total: str


class Quote(BaseModel):
    quote_id: str
    machine_id: str
    customer_id: str
    workload_type: str
    gpu_count: int
    estimated_minutes: int
    currency: str = "USDC_CREDITS"
    breakdown: QuoteBreakdown
    max_authorized_spend: Optional[str] = None  # money -> string
    status: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None


class Reservation(BaseModel):
    reservation_id: str
    quote_id: str
    machine_id: Optional[str] = None
    slots: int = 1
    status: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    released_at: Optional[str] = None


# -- Phase 4: jobs / events / artifacts ---------------------------------- #
# ``execution_mode`` is "simulated" or "hardware". The SimulatedExecutor
# (CPU dev, no GPU/Docker) produces a DETERMINISTIC transform and stamps every
# job it creates ``execution_mode: "simulated"`` -- it is NEVER real model
# inference, and the SDK surfaces that stamp rather than hiding it. Money fields
# (max_cost, final_cost) stay decimal STRINGS, never coerced to float.


class Job(BaseModel):
    # The API names it "job_id" (JobOut); "id" is kept for callers/tests that use it. Without the alias every
    # submit_job / get_job / submit_private_job against the real API raised a ValidationError.
    id: str = Field(validation_alias=AliasChoices("id", "job_id"))
    customer_id: Optional[str] = None
    project_id: Optional[str] = None
    workload_type: str
    model: str
    input: dict[str, Any] = {}
    status: str
    # "simulated" | "hardware" -- honesty stamp from the executor.
    execution_mode: Optional[str] = None
    machine_id: Optional[str] = None
    reservation_id: Optional[str] = None
    gpu_count: int = 1
    estimated_minutes: Optional[int] = None
    max_cost: Optional[str] = None  # money -> string
    max_runtime_seconds: Optional[int] = None
    retry_policy: Optional[dict[str, Any]] = None
    attempts_made: int = 0
    priority: Optional[int] = None
    idempotency_key: Optional[str] = None
    # Explainable scheduler match (chosen machine + score factors); None until MATCHED.
    match_explanation: Optional[dict[str, Any]] = None
    verification: Optional[dict[str, Any]] = None
    final_cost: Optional[str] = None  # money -> string; null in Phase 4
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    isolation_tier: Optional[str] = "container"
    required_gpu_runtime: Optional[str] = "cuda"
    gpu_runtime: Optional[str] = None

    @model_validator(mode="after")
    def _sync_gpu_runtime(self) -> "Job":
        if self.gpu_runtime is None and self.required_gpu_runtime is not None:
            self.gpu_runtime = self.required_gpu_runtime
        return self
    # The job's result and run metrics (JobOut.output / .metrics): without these the SDK dropped the result.
    output: Optional[dict[str, Any]] = None
    metrics: Optional[dict[str, Any]] = None
    cancel_requested: Optional[bool] = None
    encrypted_input: Optional[dict[str, Any]] = None
    encrypted_output: Optional[dict[str, Any]] = None
    dk_shares: Optional[dict[str, Any]] = None
    data_policy: Optional[dict[str, Any]] = None
    output_commitment: Optional[str] = None
    privacy_receipt_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobEvent(BaseModel):
    """One immutable row of the append-only job FSM history."""

    # The API's JobEventOut has no job_id (the job is in the URL); a required field made every
    # get_job_events call against the real API raise a ValidationError.
    job_id: Optional[str] = None
    seq: int
    from_state: Optional[str] = None
    to_state: str
    reason: Optional[str] = None
    actor: Optional[str] = None  # customer | scheduler | agent | system
    created_at: Optional[str] = None


class JobArtifact(BaseModel):
    artifact_id: str
    kind: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    content_type: Optional[str] = None
    created_at: Optional[str] = None


class ArtifactDownload(BaseModel):
    """A short-lived signed download URL for an artifact (owner only)."""

    url: str
    expires_at: Optional[str] = None


class JobLogs(BaseModel):
    lines: list[str] = []


class CancelAck(BaseModel):
    """Acknowledgement that cancellation was requested (honored at next safe point)."""

    job_id: Optional[str] = None
    status: Optional[str] = None
    # The API (JobCancelOut) names it ``cancel_requested``; the old name is still accepted.
    cancellation_requested: Optional[bool] = Field(
        default=None, validation_alias=AliasChoices("cancel_requested", "cancellation_requested")
    )


# -- Phase 5: billing / ledger / invoices / disputes / payouts ------------ #
# This is the MONEY layer. EVERY money field below is a decimal STRING (e.g.
# "12.34"), computed server-side from integer minor units / Decimal. The SDK
# keeps them as ``str`` and NEVER coerces to float, so no precision is lost.
# Currency "USDC_CREDITS" is a SIMULATED test credit -- there is no real payment
# rail (PAYMENT_PROVIDER_CONFIGURATION=simulated). An estimated amount is never
# reported as a received/actual amount; the two are kept separate on invoices.


class BillingAccount(BaseModel):
    """A customer billing account snapshot.

    ``available`` is the spendable balance (customer_credits), ``reserved`` is
    the total of current holds attributable to the customer, and ``balance`` is
    the total owned (available + reserved). All three are decimal strings.
    """

    currency: str = "USDC_CREDITS"
    available: str  # money -> string (spendable)
    reserved: str  # money -> string (current holds)
    balance: str  # money -> string (available + reserved)


class LedgerEntry(BaseModel):
    """One immutable (append-only) row of the double-entry ledger."""

    entry_id: str
    transaction_id: str
    kind: str  # credit_purchase | hold | capture | release | payout_request | ...
    direction: str  # "debit" | "credit"
    amount: str  # money -> string, always > 0
    currency: str = "USDC_CREDITS"
    reference: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    created_at: Optional[str] = None


class UsageRecord(BaseModel):
    """Per-job usage: duration + estimated vs actual cost (kept separate)."""

    job_id: str
    duration_seconds: Optional[int] = None
    estimated_cost: Optional[str] = None  # money -> string
    actual_cost: Optional[str] = None  # money -> string
    currency: str = "USDC_CREDITS"
    created_at: Optional[str] = None


class Usage(BaseModel):
    """Spend summary plus the per-job usage records."""

    currency: str = "USDC_CREDITS"
    total_estimated: Optional[str] = None  # money -> string
    total_actual: Optional[str] = None  # money -> string
    records: list[UsageRecord] = []


class InvoiceLineItem(BaseModel):
    label: str
    amount: str  # money -> string


class Invoice(BaseModel):
    """Itemized invoice per settled job. Estimated vs actual are kept separate
    -- an estimated total is NEVER reported as the actual charged amount."""

    invoice_id: str
    job_id: str
    line_items: list[InvoiceLineItem] = []
    estimated_total: Optional[str] = None  # money -> string
    actual_total: Optional[str] = None  # money -> string
    platform_fee: Optional[str] = None  # money -> string
    currency: str = "USDC_CREDITS"
    status: str
    created_at: Optional[str] = None


class Dispute(BaseModel):
    """A dispute opened on a settled job."""

    dispute_id: str
    job_id: str
    reason: Optional[str] = None
    status: str  # open | resolved | ...
    resolution: Optional[str] = None
    refund_amount: Optional[str] = None  # money -> string
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


class ProviderEarnings(BaseModel):
    """LIVE accrued provider earnings derived from the ledger (never the demo)."""

    currency: str = "USDC_CREDITS"
    available: str  # money -> string (withdrawable now)
    lifetime: str  # money -> string (total ever accrued)
    recent: list[LedgerEntry] = []


class Payout(BaseModel):
    """A provider payout request against accrued earnings."""

    payout_id: str
    amount: str  # money -> string
    currency: str = "USDC_CREDITS"
    status: str  # requested | authorized | settled | ...
    created_at: Optional[str] = None
    settled_at: Optional[str] = None
