"""What a host sees about their OWN computer: live readings and how its time was used. Never anything about customers."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from pydantic import BaseModel, Field


class GpuReadingOut(BaseModel):
    gpu_index: int
    gpu_util_pct: Optional[float] = None
    mem_used_mb: Optional[int] = None
    mem_total_mb: Optional[int] = None
    temp_c: Optional[float] = None
    power_w: Optional[float] = None


class UtilizationPointOut(BaseModel):
    timestamp: dt.datetime
    avg_gpu_util_pct: Optional[float] = None
    max_temp_c: Optional[float] = None


class MachineLiveOut(BaseModel):
    machine_id: uuid.UUID
    hostname: str
    status: str  # online | offline | maintenance (derived from the owner's setting and the heartbeat)
    availability: str
    host_kind: str = "unspecified"
    last_heartbeat_at: Optional[dt.datetime] = None
    last_reading_at: Optional[dt.datetime] = None
    # False when no real GPU reading has arrived in the last 10 minutes: the page must say so, not show old numbers as live.
    readings_recent: bool = False
    running_jobs: int = 0
    gpus: list[GpuReadingOut] = Field(default_factory=list)
    utilization_last_hour: list[UtilizationPointOut] = Field(default_factory=list)


class UsageSecondsOut(BaseModel):
    window: int  # the whole period, in seconds
    running: int  # busy with customers' jobs (concurrent jobs are not counted twice)
    # None when the computer sent no readings in the period: "idle" and "not reporting" cannot be told apart then, and
    # showing a split would be a guess.
    idle: Optional[int] = None
    not_reporting: Optional[int] = None


class MachineUsageOut(BaseModel):
    machine_id: uuid.UUID
    days: int
    window_start: dt.datetime
    window_end: dt.datetime
    telemetry_available: bool
    seconds: UsageSecondsOut
    # Attempts on this computer in the period, by outcome:
    #   completed, failed, stopped_here (the owner paused it or it overheated), lost (it went quiet mid-job),
    #   too_long, cancelled, running, other
    attempts: dict[str, int] = Field(default_factory=dict)
