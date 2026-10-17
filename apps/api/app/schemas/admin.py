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
