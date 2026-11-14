"""Admin schemas for Phase 7 operations (users, audit logs, platform stats)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    status: str
    roles: list[str]
    email_verified: bool
    created_at: dt.datetime


class AdminUserActionResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    status: str
    message: str


class AdminAuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_user_id: Optional[uuid.UUID] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    ip: Optional[str] = None
    reason: Optional[str] = None
    outcome: str
    created_at: dt.datetime


class AdminPlatformStatsOut(BaseModel):
    total_users: int
    active_providers: int
    total_jobs: int
    completed_jobs: int
    success_rate: float
    total_volume: str
    currency: str = "USDC_CREDITS"


class AdminJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID
    customer_id: uuid.UUID
    workload_type: str
    model: str
    status: str
    execution_mode: str
    created_at: dt.datetime
    final_cost: Optional[str] = None
    error_code: Optional[str] = None


class AdminJobEventOut(BaseModel):
    seq: int
    from_state: Optional[str] = None
    to_state: str
    reason: Optional[str] = None
    actor: str
    created_at: dt.datetime


class AdminJobAttemptOut(BaseModel):
    attempt_number: int
    machine_id: Optional[uuid.UUID] = None
    status: str
    error_code: Optional[str] = None
    started_at: dt.datetime
    finished_at: Optional[dt.datetime] = None


class AdminJobDetailOut(BaseModel):
    """One job as support and operations need to see it: what happened, where, when, what it cost.

    Deliberately NOT included: the customer's input, output, encrypted payloads and data policy. Diagnosing a job does
    not need the content of a customer's prompt or result, and some jobs are private; an administrator who needs more
    must ask the customer. Every view is written to the audit log."""

    job_id: uuid.UUID
    customer_id: uuid.UUID
    customer_email: Optional[str] = None
    project_id: Optional[uuid.UUID] = None
    workload_type: str
    model: str
    kit_id: Optional[str] = None
    kit_version: Optional[str] = None
    status: str
    execution_mode: str
    isolation_tier: str = "container"
    required_gpu_runtime: str = "cuda"
    gpu_count: int
    estimated_minutes: int
    max_cost: Optional[str] = None
    final_cost: Optional[str] = None
    max_runtime_seconds: int
    retry_policy: dict
    attempts_made: int
    priority: int = 0
    cancel_requested: bool = False
    machine_id: Optional[uuid.UUID] = None
    machine_hostname: Optional[str] = None
    host_kind: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    verification_valid: Optional[bool] = None
    verification_reason: Optional[str] = None
    # Measured by the host's agent: another program used the same GPU while the job ran (None = not seen).
    gpu_shared: Optional[bool] = None
    gpu_other_processes: Optional[int] = None
    created_at: dt.datetime
    updated_at: dt.datetime
    started_at: Optional[dt.datetime] = None
    finished_at: Optional[dt.datetime] = None
    lease_expires_at: Optional[dt.datetime] = None
    events: list[AdminJobEventOut] = []
    attempts: list[AdminJobAttemptOut] = []

