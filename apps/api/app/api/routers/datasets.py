"""Dataset and training-checkpoint endpoints (Phase C1).

Two routers live here:

* ``router``          — ``/v1/datasets``: declare a dataset, upload its bytes
  with a short-lived signed grant, list/read/download (owner-scoped).
* ``training_router`` — ``/v1/training``: attach a dataset (and optionally a
  checkpoint to resume from) to a job, record the checkpoints a runner reports,
  and download them.

Byte transfers use capability tokens (see :mod:`app.services.datasets`) rather
than a JWT, mirroring the artifact upload/download pattern: a grant is only ever
issued by an owner-scoped endpoint, so non-owners get 404 and never a token.

HONESTY: the API does not train anything. A job's ``execution_mode`` is decided
by the model registry (``simulated`` for simulated runtimes) and checkpoints are
stored as *runner-reported* claims — see ``_stamp_metrics``.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.jobs import Job
from app.models.workloads import Dataset, TrainingCheckpoint
from app.schemas.auth import CurrentUser
from app.schemas.batch import (
    CheckpointDownloadOut,
    CheckpointGrantOut,
    CheckpointOut,
    DatasetCreate,
    DatasetDownloadOut,
    DatasetOut,
    DatasetUploadGrantOut,
    TrainingRunCreate,
    TrainingRunOut,
)
from app.services import datasets as ds
from app.services.audit import record_audit
from app.services.storage import get_store, grant_expiry

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])
training_router = APIRouter(prefix="/v1/training", tags=["training"])

_DATASET_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found"
)
_JOB_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
_CHECKPOINT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Checkpoint not found"
)
_INVALID_GRANT = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_or_expired_grant"
)


def _http(exc: ds.DatasetError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.code)


def _shape_dataset(dataset: Dataset) -> DatasetOut:
    return DatasetOut(
        dataset_id=dataset.id,
        customer_id=dataset.customer_id,
        filename=dataset.filename,
        content_type=dataset.content_type,
        size_bytes=dataset.size_bytes,
        sha256=dataset.sha256,
        status=dataset.status,
        metadata_json=dataset.metadata_json,
        created_at=ensure_aware(dataset.created_at),
    )


def _shape_checkpoint(cp: TrainingCheckpoint) -> CheckpointOut:
    return CheckpointOut(
        checkpoint_id=cp.id,
        job_id=cp.job_id,
        step=cp.step,
        sha256=cp.sha256,
        size_bytes=cp.size_bytes,
        metrics=cp.metrics,
        created_at=ensure_aware(cp.created_at),
    )


async def _own_job(db: AsyncSession, customer_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.customer_id == customer_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
@router.post("", response_model=DatasetUploadGrantOut, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: DatasetCreate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> DatasetUploadGrantOut:
    """Declare a dataset and return a short-lived signed upload URL."""
    try:
        dataset = await ds.create_dataset(
            db,
            customer_id=current.id,
            filename=body.filename,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            metadata_json=body.metadata_json,
        )
    except ds.DatasetError as exc:
        raise _http(exc)
    expires_at = grant_expiry()
    token = ds.make_grant(ds.SCOPE_DATASET_UPLOAD, str(dataset.id), expires_at)
    await record_audit(
        db,
        action="dataset.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="dataset",
        resource_id=str(dataset.id),
    )
    return DatasetUploadGrantOut(
        dataset_id=dataset.id,
        url=f"/v1/datasets/upload/{token}",
        token=token,
        expires_at=expires_at,
        max_bytes=settings.DATASET_MAX_BYTES,
    )


@router.put("/upload/{token}", response_model=DatasetOut)
async def upload_dataset(
    token: str,
    request: Request,
    content_type: str = Header(default="application/octet-stream", alias="Content-Type"),
    db: AsyncSession = Depends(get_db),
) -> DatasetOut:
    """Store the dataset bytes behind a valid upload grant (never executes them)."""
    dataset_id_str = ds.verify_grant(ds.SCOPE_DATASET_UPLOAD, token)
    if dataset_id_str is None:
        raise _INVALID_GRANT
    try:
        dataset_id = uuid.UUID(dataset_id_str)
    except ValueError:
        raise _INVALID_GRANT
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        raise _DATASET_NOT_FOUND
    data = await request.body()
    try:
        dataset = await ds.store_dataset_bytes(db, dataset, data, content_type)
    except ds.DatasetError as exc:
        raise _http(exc)
    return _shape_dataset(dataset)


@router.get("", response_model=list[DatasetOut])
async def list_datasets(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DatasetOut]:
    result = await db.execute(
        select(Dataset)
        .where(Dataset.customer_id == current.id)
        .order_by(Dataset.created_at.desc())
    )
    return [_shape_dataset(d) for d in result.scalars().all()]


@router.get("/download/{token}")
async def download_dataset_bytes(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve dataset bytes to the holder of a valid download grant."""
    dataset_id_str = ds.verify_grant(ds.SCOPE_DATASET_DOWNLOAD, token)
    if dataset_id_str is None:
        raise _INVALID_GRANT
    try:
        dataset_id = uuid.UUID(dataset_id_str)
    except ValueError:
        raise _INVALID_GRANT
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None or dataset.status != "uploaded":
        raise _DATASET_NOT_FOUND
    try:
        data = get_store().get(dataset.object_key)
    except (FileNotFoundError, OSError, ValueError):
        raise _DATASET_NOT_FOUND
    return Response(
        content=data,
        media_type=dataset.content_type,
        headers={
            "X-Dataset-SHA256": dataset.sha256,
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{dataset.id}"',
        },
    )


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(
    dataset_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DatasetOut:
    dataset = await ds.get_own_dataset(db, current.id, dataset_id)
    if dataset is None:
        raise _DATASET_NOT_FOUND
    return _shape_dataset(dataset)


@router.get("/{dataset_id}/download", response_model=DatasetDownloadOut)
async def get_dataset_download_url(
    dataset_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DatasetDownloadOut:
    """Issue a short-lived download grant — owner only (others get 404)."""
    dataset = await ds.get_own_dataset(db, current.id, dataset_id)
    if dataset is None or dataset.status != "uploaded":
        raise _DATASET_NOT_FOUND
    expires_at = grant_expiry()
    token = ds.make_grant(ds.SCOPE_DATASET_DOWNLOAD, str(dataset.id), expires_at)
    return DatasetDownloadOut(
        dataset_id=dataset.id,
        url=f"/v1/datasets/download/{token}",
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Training runs
# ---------------------------------------------------------------------------
@training_router.post(
    "/runs", response_model=TrainingRunOut, status_code=status.HTTP_201_CREATED
)
async def create_training_run(
    body: TrainingRunCreate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingRunOut:
    """Attach a dataset (+ optional resume checkpoint) to a not-yet-running job.

    The job itself is submitted through the ordinary ``POST /v1/jobs`` pipeline;
    this only records WHAT the runner should train on, so the FSM is untouched.
    """
    job = await _own_job(db, current.id, body.job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    dataset = await ds.get_own_dataset(db, current.id, body.dataset_id)
    if dataset is None or dataset.status != "uploaded":
        raise _DATASET_NOT_FOUND
    resume = None
    if body.resume_from_checkpoint_id is not None:
        resume = await ds.get_own_checkpoint(db, current.id, body.resume_from_checkpoint_id)
        if resume is None:
            raise _CHECKPOINT_NOT_FOUND
    spec = ds.build_training_spec(
        dataset=dataset, resume_from=resume, hyperparameters=body.hyperparameters
    )
    try:
        job = await ds.attach_training_spec(db, job, spec)
    except ds.DatasetError as exc:
        raise _http(exc)
    await record_audit(
        db,
        action="training.attach_dataset",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="job",
        resource_id=str(job.id),
        reason=f"dataset={dataset.id}",
    )
    return await _shape_run(db, job)


async def _shape_run(db: AsyncSession, job: Job) -> TrainingRunOut:
    spec = (job.input or {}).get("training") or {}
    checkpoints = await ds.list_checkpoints(db, job.id)
    return TrainingRunOut(
        job_id=job.id,
        dataset_id=uuid.UUID(spec["dataset_id"]),
        status=job.status,
        execution_mode=job.execution_mode,
        resume_from=spec.get("resume_from"),
        hyperparameters=spec.get("hyperparameters"),
        checkpoints=[_shape_checkpoint(c) for c in checkpoints],
    )


@training_router.get("/runs/{job_id}", response_model=TrainingRunOut)
async def get_training_run(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingRunOut:
    job = await _own_job(db, current.id, job_id)
    if job is None or (job.input or {}).get("training") is None:
        raise _JOB_NOT_FOUND
    return await _shape_run(db, job)


@training_router.post(
    "/runs/{job_id}/checkpoint-grants",
    response_model=CheckpointGrantOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_checkpoint_grant(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CheckpointGrantOut:
    """Mint the capability the runner uses to report checkpoints for this job."""
    job = await _own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    expires_at = grant_expiry()
    token = ds.make_grant(ds.SCOPE_CHECKPOINT_UPLOAD, str(job.id), expires_at)
    return CheckpointGrantOut(
        job_id=job.id,
        url=f"/v1/training/checkpoints/upload/{token}",
        token=token,
        expires_at=expires_at,
        max_bytes=settings.ARTIFACT_MAX_BYTES,
    )


@training_router.put(
    "/checkpoints/upload/{token}",
    response_model=CheckpointOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_checkpoint(
    token: str,
    request: Request,
    x_checkpoint_step: int = Header(default=0, alias="X-Checkpoint-Step"),
    x_checkpoint_metrics: str | None = Header(default=None, alias="X-Checkpoint-Metrics"),
    db: AsyncSession = Depends(get_db),
) -> CheckpointOut:
    """Record a checkpoint reported by the runner (idempotent per job+step)."""
    job_id_str = ds.verify_grant(ds.SCOPE_CHECKPOINT_UPLOAD, token)
    if job_id_str is None:
        raise _INVALID_GRANT
    try:
        job_id = uuid.UUID(job_id_str)
    except ValueError:
        raise _INVALID_GRANT
    job = await db.get(Job, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    if x_checkpoint_step < 0:
        raise HTTPException(status_code=422, detail="invalid_step")
    metrics: dict | None = None
    if x_checkpoint_metrics:
        # A malformed metrics header is rejected rather than silently dropped —
        # recorded metrics must reflect exactly what the runner claimed.
        try:
            parsed = json.loads(x_checkpoint_metrics)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_metrics")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="invalid_metrics")
        metrics = parsed
    data = await request.body()
    try:
        checkpoint = await ds.record_checkpoint(
            db, job=job, step=x_checkpoint_step, data=data, metrics=metrics
        )
    except ds.DatasetError as exc:
        raise _http(exc)
    return _shape_checkpoint(checkpoint)


@training_router.get("/runs/{job_id}/checkpoints", response_model=list[CheckpointOut])
async def list_job_checkpoints(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CheckpointOut]:
    job = await _own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    return [_shape_checkpoint(c) for c in await ds.list_checkpoints(db, job.id)]


@training_router.get("/checkpoints/download/{token}")
async def download_checkpoint_bytes(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    checkpoint_id_str = ds.verify_grant(ds.SCOPE_CHECKPOINT_DOWNLOAD, token)
    if checkpoint_id_str is None:
        raise _INVALID_GRANT
    try:
        checkpoint_id = uuid.UUID(checkpoint_id_str)
    except ValueError:
        raise _INVALID_GRANT
    checkpoint = await db.get(TrainingCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise _CHECKPOINT_NOT_FOUND
    try:
        data = get_store().get(checkpoint.object_key)
    except (FileNotFoundError, OSError, ValueError):
        raise _CHECKPOINT_NOT_FOUND
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Checkpoint-SHA256": checkpoint.sha256,
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{checkpoint.id}"',
        },
    )


@training_router.get(
    "/checkpoints/{checkpoint_id}/download", response_model=CheckpointDownloadOut
)
async def get_checkpoint_download_url(
    checkpoint_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckpointDownloadOut:
    """Issue a short-lived checkpoint download grant — owner only."""
    checkpoint = await ds.get_own_checkpoint(db, current.id, checkpoint_id)
    if checkpoint is None:
        raise _CHECKPOINT_NOT_FOUND
    expires_at = grant_expiry()
    token = ds.make_grant(ds.SCOPE_CHECKPOINT_DOWNLOAD, str(checkpoint.id), expires_at)
    return CheckpointDownloadOut(
        checkpoint_id=checkpoint.id,
        url=f"/v1/training/checkpoints/download/{token}",
        expires_at=expires_at,
    )
