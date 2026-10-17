"""Phase 10: On-chain settlement & smart contract router."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import get_current_user, require_roles
from app.models.settlement import OnChainEscrow, OnChainTransaction
from app.schemas.auth import CurrentUser
from app.schemas.settlement import (
    ConfirmRefundTxRequest,
    ConfirmSettlementTxRequest,
    ContractDeploymentInfo,
    CreateEscrowLockRequest,
    EscrowResponse,
    IssueSettlementVoucherRequest,
    OnChainReconciliationSummary,
    OnChainTransactionResponse,
    RecordOnChainTxRequest,
    SettlementVoucherResponse,
)
from app.services import settlement as settlement_service

router = APIRouter(prefix="/v1/settlement", tags=["settlement"])


def _is_admin(user: CurrentUser) -> bool:
    return "administrator" in user.roles


@router.get("/contracts", response_model=ContractDeploymentInfo)
async def get_contract_info():
    """Retrieve verified smart contract addresses, chain parameters, and signer identity.

    Public, non-sensitive deployment metadata (addresses + chain id only)."""
    return settlement_service.get_contract_deployment_info()


@router.post("/escrows", response_model=EscrowResponse, status_code=status.HTTP_201_CREATED)
async def create_escrow(
    payload: CreateEscrowLockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Register an on-chain budget lock deposited by a customer.

    Authorization: the escrow is bound to ``current_user`` as the customer, and
    the service enforces that the caller owns the job (or is an administrator)."""
    escrow = await settlement_service.create_escrow_lock(db, payload, current_user)
    return escrow


@router.get("/escrows", response_model=List[EscrowResponse])
async def list_escrows(
    job_id: Optional[uuid.UUID] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List on-chain escrows. Non-admins only see escrows where they are the
    customer or the provider."""
    stmt = select(OnChainEscrow)
    if job_id:
        stmt = stmt.where(OnChainEscrow.job_id == job_id)
    if status_filter:
        stmt = stmt.where(OnChainEscrow.status == status_filter)

    if not _is_admin(current_user):
        stmt = stmt.where(
            (OnChainEscrow.customer_id == current_user.id)
            | (OnChainEscrow.provider_id == current_user.id)
        )

    stmt = stmt.order_by(desc(OnChainEscrow.created_at)).offset(offset).limit(limit)
    rows = (await db.scalars(stmt)).all()
    return rows


@router.get("/escrows/{job_id}", response_model=EscrowResponse)
async def get_escrow_by_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Retrieve escrow lock state for a specific job (owner or admin only)."""
    escrow = await db.scalar(
        select(OnChainEscrow).where(OnChainEscrow.job_id == job_id)
    )
    # Return 404 (not 403) to non-owners so existence is not disclosed.
    if not escrow or (
        not _is_admin(current_user)
        and current_user.id not in (escrow.customer_id, escrow.provider_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Escrow for job {job_id} not found",
        )
    return escrow


@router.post("/vouchers", response_model=SettlementVoucherResponse)
async def issue_voucher(
    payload: IssueSettlementVoucherRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """Generate a cryptographic platform voucher signing off on job completion.

    Admin-only: issuing a voucher signs a payout authorization with the platform
    signer key, so it is a privileged platform action."""
    voucher = await settlement_service.issue_settlement_voucher(db, payload, current_user)
    return voucher


@router.post("/confirm", response_model=EscrowResponse)
async def confirm_settlement(
    payload: ConfirmSettlementTxRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """Confirm on-chain execution of settleJob transaction and mark reconciled (admin-only)."""
    escrow = await settlement_service.confirm_settlement_tx(db, payload, current_user)
    return escrow


@router.post("/refund", response_model=EscrowResponse)
async def confirm_refund(
    payload: ConfirmRefundTxRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """Confirm on-chain execution of refundJob transaction and mark reconciled (admin-only)."""
    escrow = await settlement_service.confirm_refund_tx(db, payload, current_user)
    return escrow


@router.post("/transactions", response_model=OnChainTransactionResponse, status_code=status.HTTP_201_CREATED)
async def record_transaction(
    payload: RecordOnChainTxRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """Record raw on-chain transaction receipt for audit (admin-only ingestion)."""
    tx = await settlement_service.record_on_chain_tx(db, payload)
    return tx


@router.get("/transactions", response_model=List[OnChainTransactionResponse])
async def list_transactions(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """List on-chain transaction audit trail (admin-only; platform-wide)."""
    stmt = (
        select(OnChainTransaction)
        .order_by(desc(OnChainTransaction.created_at))
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.scalars(stmt)).all()
    return rows


@router.get("/reconciliation", response_model=OnChainReconciliationSummary)
async def get_reconciliation(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
):
    """Retrieve global off-chain vs on-chain reconciliation health (admin-only)."""
    return await settlement_service.get_reconciliation_summary(db)
