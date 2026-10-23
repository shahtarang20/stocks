"""Batch job endpoints (customer JWT, owner-scoped) — Phase C1.

A batch groups N ordinary job specs. Every child is submitted through the SAME
``POST /v1/jobs`` handler, so validation, the FSM, the budget hold and the
scheduler behave identically; the batch itself never moves money (each child
bills itself through the normal ledger path), so a partially finished batch can
never be billed as a completed one.

All reads/writes are owner-scoped: a batch belonging to someone else is 404,
never 403.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.workloads import Batch
from app.schemas.auth import CurrentUser
from app.schemas.batch import BatchCancelOut, BatchCreate, BatchOut
from app.services import batch as batch_service
from app.services.audit import record_audit

router = APIRouter(prefix="/v1/batches", tags=["batches"])

_BATCH_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
)


async def _shape(db: AsyncSession, batch: Batch) -> BatchOut:
    jobs = await batch_service.refresh_counts(db, batch)
    items = batch_service.item_views(jobs)
    quote = await batch_service.quote_summary(db, batch, jobs)
    cancelled = sum(1 for j in jobs if j.status == "cancelled")
    pending = sum(1 for j in jobs if j.status not in batch_service.TERMINAL_STATES)
    return BatchOut(
        batch_id=batch.id,
        customer_id=batch.customer_id,
        status=batch.status,
        total_items=batch.total_items,
        submitted_items=len(jobs),
        completed_items=batch.completed_items,
        failed_items=batch.failed_items,
        cancelled_items=cancelled,
        pending_items=pending,
        discount_pct=f"{batch.discount_pct}",
        idempotency_key=batch.idempotency_key,
        quote=quote,
        items=items,
        created_at=ensure_aware(batch.created_at),
        finished_at=ensure_aware(batch.finished_at),
    )


@router.post("", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> BatchOut:
    """Submit up to ``BATCH_MAX_ITEMS`` job specs as one discounted batch."""
    batch = await batch_service.create_batch(
        db,
        request=request,
        current=current,
        items=body.items,
        idempotency_key=idempotency_key,
    )
    await record_audit(
        db,
        action="batch.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="batch",
        resource_id=str(batch.id),
    )
    return await _shape(db, batch)


@router.get("", response_model=list[BatchOut])
async def list_batches(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BatchOut]:
    result = await db.execute(
        select(Batch).where(Batch.customer_id == current.id).order_by(Batch.created_at.desc())
    )
    return [await _shape(db, b) for b in result.scalars().all()]


@router.get("/{batch_id}", response_model=BatchOut)
async def get_batch(
    batch_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchOut:
    batch = await batch_service.get_own_batch(db, current.id, batch_id)
    if batch is None:
        raise _BATCH_NOT_FOUND
    return await _shape(db, batch)


@router.post("/{batch_id}/cancel", response_model=BatchCancelOut)
async def cancel_batch(
    batch_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchCancelOut:
    """Cancel the children that are still running/queued; terminal ones stand."""
    batch = await batch_service.get_own_batch(db, current.id, batch_id)
    if batch is None:
        raise _BATCH_NOT_FOUND
    cancelled, skipped = await batch_service.cancel_remaining(
        db, request=request, current=current, batch=batch
    )
    await record_audit(
        db,
        action="batch.cancel",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="batch",
        resource_id=str(batch.id),
        reason=f"cancelled={cancelled};skipped={skipped}",
    )
    return BatchCancelOut(
        batch_id=batch.id,
        status=batch.status,
        cancelled=cancelled,
        skipped_terminal=skipped,
    )
