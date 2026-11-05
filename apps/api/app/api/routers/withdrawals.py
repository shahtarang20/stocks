"""Token withdrawals (B1). OFF by default (WITHDRAWALS_ENABLED) and behind an admin pause.

POST /v1/withdrawals                      request (Idempotency-Key header required)
GET  /v1/withdrawals                      the caller's withdrawals
GET  /v1/withdrawals/{id}                 one of the caller's withdrawals
GET  /v1/admin/withdrawals                admin list
POST /v1/admin/withdrawals/pause          pause or resume
POST /v1/admin/withdrawals/{id}/cancel    refund a withdrawal that was never signed
GET  /v1/admin/withdrawals/reconciliation chain transfers vs ledger payouts (a mismatch pauses)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.deps import client_ip, require_roles, require_verified_user
from app.models.withdrawal import Withdrawal
from app.schemas.auth import CurrentUser
from app.schemas.withdrawal import CancelIn, PauseIn, WithdrawalCreate, WithdrawalOut
from app.services import ledger
from app.services import withdrawals as svc
from app.services.audit import record_audit
from app.services.chain_reconciler import ChainClientError, reconcile_withdrawals
from app.services.withdrawal_pause import get_pause, set_pause

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])
admin_router = APIRouter(
    prefix="/v1/admin/withdrawals",
    tags=["withdrawals-admin"],
    dependencies=[Depends(require_roles("administrator"))],
)


def _out(w: Withdrawal) -> WithdrawalOut:
    return WithdrawalOut(
        id=w.id,
        status=w.status,
        credits=ledger.minor_to_str(w.credits_minor),
        tokens=f"{int(w.amount_raw) / 10**18:.6f}",
        destination=w.destination,
        chain_id=w.chain_id,
        tx_hash=w.tx_hash,
        refund_reason=w.refund_reason,
        created_at=w.created_at,
        updated_at=w.updated_at,
    )


@router.post("", response_model=WithdrawalOut)
async def create_withdrawal(
    body: WithdrawalCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    if not rate_limiter.allow(f"withdrawal:{current.id}", 10, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, please try again later")
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "idempotency_key_required")
    try:
        wd, created = await svc.request_withdrawal(
            db,
            user_id=current.id,
            amount=body.amount,
            destination=body.destination,
            idempotency_key=idempotency_key,
            ip=client_ip(request),
        )
    except svc.WithdrawalError as exc:
        raise HTTPException(exc.http_status, exc.code) from None
    return _out(wd)


@router.get("", response_model=list[WithdrawalOut])
async def list_my_withdrawals(
    limit: int = Query(50, ge=1, le=200),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Withdrawal).where(Withdrawal.user_id == current.id).order_by(Withdrawal.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [_out(w) for w in rows]


@router.get("/{withdrawal_id}", response_model=WithdrawalOut)
async def get_my_withdrawal(
    withdrawal_id: uuid.UUID,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    w = (
        await db.execute(select(Withdrawal).where(Withdrawal.id == withdrawal_id, Withdrawal.user_id == current.id))
    ).scalar_one_or_none()
    if w is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return _out(w)


@admin_router.get("")
async def admin_list(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(Withdrawal).order_by(Withdrawal.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Withdrawal.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "enabled": settings.withdrawals_configured,
        "pause": await get_pause(db),
        "items": [{**_out(w).model_dump(mode="json"), "user_id": str(w.user_id)} for w in rows],
    }


@admin_router.post("/pause")
async def admin_pause(
    body: PauseIn,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    value = await set_pause(db, body.paused, reason=body.reason, source="admin")
    await record_audit(
        db, action="withdrawal.pause" if body.paused else "withdrawal.resume", outcome="success",
        actor_user_id=current.id, resource_type="withdrawals", ip=client_ip(request), reason=body.reason,
    )
    return value


@admin_router.post("/{withdrawal_id}/cancel", response_model=WithdrawalOut)
async def admin_cancel(
    withdrawal_id: uuid.UUID,
    body: CancelIn,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
):
    w = (await db.execute(select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update())).scalar_one_or_none()
    if w is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    try:
        await svc.cancel_pending(db, w, current.id, body.reason)
    except svc.WithdrawalError as exc:
        raise HTTPException(exc.http_status, exc.code) from None
    return _out(w)


@admin_router.get("/reconciliation")
async def admin_reconciliation(db: AsyncSession = Depends(get_db)) -> dict:
    """Compare the chain with the ledger. A mismatch pauses withdrawals automatically."""
    if not settings.withdrawals_configured:
        raise HTTPException(status.HTTP_409_CONFLICT, "withdrawals_disabled")
    try:
        return await reconcile_withdrawals(db)
    except ChainClientError as exc:
        logger.warning("withdrawal reconciliation failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "chain_unavailable") from None
