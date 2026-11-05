"""U19 schemas: live job view and agent progress reports (additive)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field


class JobProgressItem(BaseModel):
    job_id: uuid.UUID
    timestamp: datetime | None = None
    progress_pct: float = Field(ge=0.0, le=100.0)
    phase: str | None = Field(default=None, max_length=64)
    gpu_util_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mem_used_mb: int | None = Field(default=None, ge=0, le=10_000_000)
    mem_total_mb: int | None = Field(default=None, ge=0, le=10_000_000)
    step_count: int | None = Field(default=None, ge=0, le=2**31 - 1)  # Integer column
    # U8/U16 additive BYOC runtime signals. Names only, never contents; size-capped. Older agents omit them.
    process_names: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=64)
    blocked_egress: list[Annotated[str, Field(max_length=200)]] | None = Field(default=None, max_length=50)
    power_w: float | None = Field(default=None, ge=0.0, le=10_000.0)
    io_bytes: int | None = Field(default=None, ge=0)  # cumulative container net + block I/O bytes


class JobProgressBatchRequest(BaseModel):
    # Hard schema cap; the settings cap may be lower.
    points: list[JobProgressItem] = Field(min_length=1, max_length=1000)


class JobProgressBatchResponse(BaseModel):
    accepted: int
    dropped: int


class JobLiveOut(BaseModel):
    job_id: uuid.UUID
    status: str
    sampled_at: datetime | None = None
    gpu_util_pct: float | None = None
    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    progress_pct: float | None = None
    phase: str | None = None
    elapsed_seconds: float | None = None
    cost_cap: float | None = None
    cost_so_far: float | None = None
    # Estimate from the cost cap and elapsed time. Never the billed amount.
    cost_so_far_is_estimate: bool = True
