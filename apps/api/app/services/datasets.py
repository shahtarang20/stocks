"""Dataset upload/download + training checkpoint plumbing (Phase C1).

The API process NEVER executes customer code or customer data — it only stores
and serves bytes, exactly like :mod:`app.services.storage` does for artifacts.

Capability grants
-----------------
Datasets and checkpoints are moved with short-lived HMAC capability tokens that
follow the artifact-grant pattern (``scope:resource_id:expiry:signature``,
base64url). They use their OWN scope namespace (``ds.*``) so an artifact grant
can never be replayed against a dataset endpoint and vice versa — the signature
covers the scope string.

A grant is only ever handed to the owner (the issuing endpoints are
owner-scoped and return 404 to everyone else), so holding a valid token is the
authorization for the subsequent byte transfer.

Honesty
-------
Recording a :class:`TrainingCheckpoint` does NOT mean the platform trained a
model: checkpoints are bytes + metrics *reported by the runner*. Real training
needs a GPU; simulated jobs stay stamped ``execution_mode="simulated"``.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.jobs import Job
from app.models.workloads import Dataset, TrainingCheckpoint
from app.services.storage import get_store, sha256_hex

# Content types a dataset may be uploaded with. Deliberately narrow: tabular /
# text corpora and opaque archives. Anything renderable by a browser (html, svg)
# is excluded so stored bytes can never become a stored-XSS vector.
DATASET_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/x-ndjson",
        "application/jsonl",
        "text/csv",
        "text/plain",
        "application/parquet",
        "application/zip",
        "application/x-tar",
        "application/gzip",
        "application/octet-stream",
    }
)

SCOPE_DATASET_UPLOAD = "ds.dataset_upload"
SCOPE_DATASET_DOWNLOAD = "ds.dataset_download"
SCOPE_CHECKPOINT_UPLOAD = "ds.checkpoint_upload"
SCOPE_CHECKPOINT_DOWNLOAD = "ds.checkpoint_download"


class DatasetError(Exception):
    """A dataset/checkpoint rule violation, carrying the HTTP status to surface."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Signed grants (same construction as app.services.storage, separate namespace)
# ---------------------------------------------------------------------------
def _sign(payload: str) -> str:
    return hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_grant(scope: str, resource_id: str, expires_at: dt.datetime) -> str:
    """Mint a capability token for ``scope`` + ``resource_id`` until ``expires_at``."""
    exp = int(expires_at.timestamp())
    payload = f"{scope}:{resource_id}:{exp}"
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode("utf-8")).decode("ascii")


def verify_grant(scope: str, token: str, now: dt.datetime | None = None) -> str | None:
    """Return the resource id iff ``token`` is a valid, unexpired grant for ``scope``."""
    now = now or utcnow()
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = decoded.split(":")
    if len(parts) != 4:
        return None
    tok_scope, resource_id, exp_str, sig = parts
    if tok_scope != scope:
        return None
    if not hmac.compare_digest(_sign(f"{tok_scope}:{resource_id}:{exp_str}"), sig):
        return None
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if exp < int(now.timestamp()):
        return None
    return resource_id


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
def normalize_content_type(raw: str) -> str:
    """Strip any charset parameter and lowercase (``text/csv; charset=utf-8``)."""
    return raw.split(";")[0].strip().lower()


def check_content_type(raw: str) -> str:
    base = normalize_content_type(raw)
    if base not in DATASET_CONTENT_TYPES:
        raise DatasetError("content_type_not_allowed", 415)
    return base


def check_size(size_bytes: int) -> None:
    if size_bytes > settings.DATASET_MAX_BYTES:
        raise DatasetError("dataset_too_large", 413)


async def create_dataset(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    filename: str,
    content_type: str,
    size_bytes: int,
    metadata_json: dict | None = None,
) -> Dataset:
    """Create the PENDING dataset row the upload grant will be scoped to.

    The declared size is rejected up front so an oversized upload never streams
    through the API at all; the real byte count is re-checked on upload.
    """
    base_ct = check_content_type(content_type)
    check_size(size_bytes)
    dataset_id = uuid.uuid4()
    dataset = Dataset(
        id=dataset_id,
        customer_id=customer_id,
        # Keyed by owner so a leaked key cannot be guessed across customers.
        object_key=f"datasets/{customer_id}/{dataset_id}",
        filename=filename,
        size_bytes=0,
        sha256="",
        content_type=base_ct,
        status="pending",
        metadata_json=metadata_json,
    )
    db.add(dataset)
    await db.flush()
    return dataset


async def store_dataset_bytes(
    db: AsyncSession, dataset: Dataset, data: bytes, content_type: str
) -> Dataset:
    """Persist the uploaded bytes, recording size + sha256.

    Idempotent: re-uploading identical bytes is a no-op; uploading DIFFERENT
    bytes over an already-uploaded dataset is refused (409) so a dataset
    reference inside a job can never silently change meaning.
    """
    base_ct = check_content_type(content_type)
    check_size(len(data))
    digest = sha256_hex(data)
    if dataset.status == "uploaded":
        if dataset.sha256 == digest:
            return dataset
        raise DatasetError("dataset_immutable", 409)
    get_store().put(dataset.object_key, data, base_ct)
    dataset.size_bytes = len(data)
    dataset.sha256 = digest
    dataset.content_type = base_ct
    dataset.status = "uploaded"
    await db.flush()
    return dataset


async def get_own_dataset(
    db: AsyncSession, customer_id: uuid.UUID, dataset_id: uuid.UUID
) -> Dataset | None:
    """Owner-scoped fetch — a non-owner gets ``None`` (the router answers 404)."""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.customer_id == customer_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Training checkpoints
# ---------------------------------------------------------------------------
async def record_checkpoint(
    db: AsyncSession, *, job: Job, step: int, data: bytes, metrics: dict | None
) -> TrainingCheckpoint:
    """Record (or re-record) the checkpoint a runner reported for ``step``.

    Idempotent on ``(job_id, step)`` — the table's unique constraint — so a
    runner that retries a checkpoint upload never creates a duplicate row.
    """
    if len(data) > settings.ARTIFACT_MAX_BYTES:
        raise DatasetError("checkpoint_too_large", 413)
    digest = sha256_hex(data)
    existing = (
        await db.execute(
            select(TrainingCheckpoint).where(
                TrainingCheckpoint.job_id == job.id, TrainingCheckpoint.step == step
            )
        )
    ).scalar_one_or_none()
    object_key = f"checkpoints/{job.id}/{step}"
    get_store().put(object_key, data, "application/octet-stream")
    if existing is not None:
        existing.object_key = object_key
        existing.sha256 = digest
        existing.size_bytes = len(data)
        existing.metrics = _stamp_metrics(job, metrics)
        await db.flush()
        return existing
    checkpoint = TrainingCheckpoint(
        job_id=job.id,
        step=step,
        object_key=object_key,
        sha256=digest,
        size_bytes=len(data),
        metrics=_stamp_metrics(job, metrics),
    )
    db.add(checkpoint)
    await db.flush()
    return checkpoint


def _stamp_metrics(job: Job, metrics: dict | None) -> dict:
    """Keep reported metrics honest: they are claims by the runner, not ours.

    The execution mode of the owning job travels with them so a consumer can
    never mistake a simulated run's numbers for measured training results.
    """
    return {
        "reported": dict(metrics or {}),
        "source": "runner_reported",
        "execution_mode": job.execution_mode,
    }


async def get_own_checkpoint(
    db: AsyncSession, customer_id: uuid.UUID, checkpoint_id: uuid.UUID
) -> TrainingCheckpoint | None:
    """Owner-scoped checkpoint fetch (joined through the checkpoint's job)."""
    result = await db.execute(
        select(TrainingCheckpoint)
        .join(Job, Job.id == TrainingCheckpoint.job_id)
        .where(TrainingCheckpoint.id == checkpoint_id, Job.customer_id == customer_id)
    )
    return result.scalar_one_or_none()


async def list_checkpoints(db: AsyncSession, job_id: uuid.UUID) -> list[TrainingCheckpoint]:
    result = await db.execute(
        select(TrainingCheckpoint)
        .where(TrainingCheckpoint.job_id == job_id)
        .order_by(TrainingCheckpoint.step)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Training runs
# ---------------------------------------------------------------------------
# A training run is an ordinary job carrying a training spec in ``job.input``.
# Phase C1 deliberately does NOT invent a "training" workload type: the
# scheduler's workload allowlist is owned elsewhere, and forking the job FSM to
# sneak one in is exactly what the conventions forbid. The spec must therefore
# be attached before the runner claims the job.
ATTACHABLE_STATES: frozenset[str] = frozenset({"created", "validating", "queued", "matched", "reserved"})


def build_training_spec(
    *,
    dataset: Dataset,
    resume_from: TrainingCheckpoint | None,
    hyperparameters: dict | None,
) -> dict:
    """Build the ``input["training"]`` block a runner reads at claim time."""
    spec: dict = {
        "dataset_id": str(dataset.id),
        "dataset_object_key": dataset.object_key,
        "dataset_sha256": dataset.sha256,
        "dataset_size_bytes": dataset.size_bytes,
        "hyperparameters": dict(hyperparameters or {}),
    }
    if resume_from is not None:
        spec["resume_from"] = {
            "checkpoint_id": str(resume_from.id),
            "step": resume_from.step,
            "object_key": resume_from.object_key,
            "sha256": resume_from.sha256,
        }
    return spec


async def attach_training_spec(db: AsyncSession, job: Job, spec: dict) -> Job:
    """Attach the training spec to a not-yet-running job.

    Refuses once the job has been claimed: changing the dataset under a running
    runner would make the recorded inputs a lie about what actually executed.
    """
    if job.status not in ATTACHABLE_STATES:
        raise DatasetError("job_not_attachable", 409)
    if (job.input or {}).get("training") is not None and job.input["training"] != spec:
        # Re-attaching the SAME spec is idempotent; changing it is not allowed.
        raise DatasetError("training_spec_already_set", 409)
    # Reassign (never mutate in place) so SQLAlchemy sees the JSON column change.
    job.input = {**(job.input or {}), "training": spec}
    await db.flush()
    return job
