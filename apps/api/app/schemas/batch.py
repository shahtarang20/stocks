"""Batch, dataset and training-run schemas (Phase C1).

Money is serialized as a 2dp decimal STRING (never a float) exactly like the
job/quote schemas, and every timestamp is an aware UTC datetime.

Dataset and training schemas live here (rather than in a `datasets` schema
module) so Phase C1 owns a single schema surface for the workload models in
``app/models/workloads.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.jobs import JobCreate


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------
class BatchCreate(BaseModel):
    """A batch submission: N ordinary job specs priced with a bulk discount.

    Items are the SAME shape as a single ``POST /v1/jobs`` body — a batch is a
    grouping on top of the existing pipeline, never a second pipeline.
    """

    items: list[JobCreate] = Field(min_length=1)


class BatchItemOut(BaseModel):
    """Per-item state inside a batch (index is the submission order)."""

    index: int
    job_id: uuid.UUID | None = None
    status: str
    error_code: str | None = None
    final_cost: str | None = None


class BatchQuoteOut(BaseModel):
    """Disclosed batch pricing: what the items would cost, and the discount."""

    currency: str
    priced_items: int
    discount_pct: str
    undiscounted_total: str
    discount_amount: str
    discounted_total: str


class BatchOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    batch_id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    total_items: int
    submitted_items: int
    completed_items: int
    failed_items: int
    cancelled_items: int
    pending_items: int
    discount_pct: str
    idempotency_key: str | None = None
    quote: BatchQuoteOut
    items: list[BatchItemOut] = Field(default_factory=list)
    created_at: dt.datetime
    finished_at: dt.datetime | None = None


class BatchCancelOut(BaseModel):
    batch_id: uuid.UUID
    status: str
    cancelled: int
    skipped_terminal: int


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
class DatasetCreate(BaseModel):
    """Declare a dataset upload; the server answers with a short-lived grant."""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    # Declared size, checked against DATASET_MAX_BYTES *before* any bytes move.
    size_bytes: int = Field(ge=0)
    metadata_json: dict | None = None


class DatasetUploadGrantOut(BaseModel):
    dataset_id: uuid.UUID
    url: str
    token: str
    expires_at: dt.datetime
    max_bytes: int


class DatasetOut(BaseModel):
    dataset_id: uuid.UUID
    customer_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    metadata_json: dict | None = None
    created_at: dt.datetime


class DatasetDownloadOut(BaseModel):
    dataset_id: uuid.UUID
    url: str
    expires_at: dt.datetime


# ---------------------------------------------------------------------------
# Training runs / checkpoints
# ---------------------------------------------------------------------------
class TrainingRunCreate(BaseModel):
    """Attach a dataset (and optionally a checkpoint to resume from) to a job."""

    job_id: uuid.UUID
    dataset_id: uuid.UUID
    resume_from_checkpoint_id: uuid.UUID | None = None
    hyperparameters: dict | None = None


class CheckpointOut(BaseModel):
    checkpoint_id: uuid.UUID
    job_id: uuid.UUID
    step: int
    sha256: str
    size_bytes: int
    metrics: dict | None = None
    created_at: dt.datetime


class CheckpointGrantOut(BaseModel):
    job_id: uuid.UUID
    url: str
    token: str
    expires_at: dt.datetime
    max_bytes: int


class CheckpointDownloadOut(BaseModel):
    checkpoint_id: uuid.UUID
    url: str
    expires_at: dt.datetime


class TrainingRunOut(BaseModel):
    job_id: uuid.UUID
    dataset_id: uuid.UUID
    status: str
    # Honesty: training is NOT executed by the API; the runner stays simulated
    # unless a hardware model version was selected. Echoed from the job.
    execution_mode: str
    resume_from: dict | None = None
    hyperparameters: dict | None = None
    checkpoints: list[CheckpointOut] = Field(default_factory=list)
