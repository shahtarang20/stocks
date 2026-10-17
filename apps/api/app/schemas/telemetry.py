"""Pydantic schemas for Phase 9 high-volume telemetry and alerts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Ingestion Schemas
# --------------------------------------------------------------------------- #
class MachineTelemetryItem(BaseModel):
    timestamp: datetime | None = None
    gpu_index: int = Field(default=0, ge=0)
    gpu_util_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mem_util_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mem_used_mb: int | None = Field(default=None, ge=0)
    mem_total_mb: int | None = Field(default=None, ge=0)
    temp_c: float | None = Field(default=None, ge=-50.0, le=150.0)
    power_w: float | None = Field(default=None, ge=0.0)
    fan_speed_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    extra_metrics: dict[str, Any] | None = None


class MachineTelemetryBatchRequest(BaseModel):
    points: list[MachineTelemetryItem] = Field(min_length=1, max_length=1000)


class MachineTelemetryBatchResponse(BaseModel):
    accepted: int
    alerts_triggered: int


class JobTelemetryItem(BaseModel):
    job_id: uuid.UUID
    timestamp: datetime | None = None
    gpu_util_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mem_used_mb: int | None = Field(default=None, ge=0)
    tokens_per_sec: float | None = Field(default=None, ge=0.0)
    step_count: int | None = Field(default=None, ge=0)
    loss: float | None = None
    extra_metrics: dict[str, Any] | None = None


class JobTelemetryBatchRequest(BaseModel):
    points: list[JobTelemetryItem] = Field(min_length=1, max_length=1000)


class JobTelemetryBatchResponse(BaseModel):
    accepted: int


# --------------------------------------------------------------------------- #
# Query & Bucketed Aggregation Schemas
# --------------------------------------------------------------------------- #
class MetricBucketPoint(BaseModel):
    timestamp: datetime
    avg_gpu_util_pct: float | None = None
    max_gpu_util_pct: float | None = None
    avg_temp_c: float | None = None
    max_temp_c: float | None = None
    avg_mem_used_mb: float | None = None
    avg_power_w: float | None = None
    samples: int = 0


class MachineTelemetryQueryResponse(BaseModel):
    machine_id: uuid.UUID
    start_time: datetime
    end_time: datetime
    bucket_seconds: int
    points: list[MetricBucketPoint]
    current: dict[str, Any] | None = None


class JobMetricBucketPoint(BaseModel):
    timestamp: datetime
    avg_gpu_util_pct: float | None = None
    avg_tokens_per_sec: float | None = None
    max_tokens_per_sec: float | None = None
    avg_mem_used_mb: float | None = None
    samples: int = 0


class JobTelemetryQueryResponse(BaseModel):
    job_id: uuid.UUID
    points: list[JobMetricBucketPoint]
    total_samples: int = 0
    avg_tokens_per_sec: float | None = None


# --------------------------------------------------------------------------- #
# Operational Alerts Schemas
# --------------------------------------------------------------------------- #
class TelemetryAlertOut(BaseModel):
    id: uuid.UUID
    machine_id: uuid.UUID | None
    job_id: uuid.UUID | None
    alert_type: str
    severity: str
    message: str
    metric_value: float | None = None
    threshold: float | None = None
    status: str
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class AlertActionResponse(BaseModel):
    id: uuid.UUID
    status: str
    updated_at: datetime


class PlatformTelemetryStats(BaseModel):
    active_gpus: int = 0
    fleet_avg_gpu_util_pct: float = 0.0
    fleet_avg_temp_c: float = 0.0
    fleet_total_power_kw: float = 0.0
    active_alerts_count: int = 0
    critical_alerts_count: int = 0
