"""Phase 10: On-chain settlement & smart contract escrow service."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from eth_abi.packed import encode_packed
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.jobs import Job
from app.models.settlement import (
    EscrowStatus,
    OnChainEscrow,
    OnChainTransaction,
    OnChainTxStatus,
    OnChainTxType,
)
from app.models.user import User
from app.schemas.settlement import (
    ConfirmRefundTxRequest,
    ConfirmSettlementTxRequest,
    ContractDeploymentInfo,
    CreateEscrowLockRequest,
    IssueSettlementVoucherRequest,
    OnChainReconciliationSummary,
    RecordOnChainTxRequest,
    SettlementVoucherResponse,
)


def get_signer_address() -> str:
    """Return the derived Ethereum address of the platform signer key."""
    acct = Account.from_key(settings.PLATFORM_SIGNER_PRIVATE_KEY)
    return acct.address


def job_id_to_bytes32(job_id: uuid.UUID) -> str:
    """Deterministically convert a job UUID to a bytes32 hex string."""
    h = keccak(text=str(job_id))
    return "0x" + h.hex()


def get_contract_deployment_info() -> ContractDeploymentInfo:
    """Return on-chain contract registry details for frontend web3 clients."""
    signer_addr = get_signer_address()
    return ContractDeploymentInfo(
        chain_id=settings.SETTLEMENT_CHAIN_ID,
        network_name="Local Hardhat / Dev EVM (Cancun)"
        if settings.SETTLEMENT_CHAIN_ID == 31337
        else "Sepolia Testnet",
        token_address=settings.COMPUTE_TOKEN_ADDRESS,
        token_symbol="COMPUTE",
        token_name="ComputeCoin",
        token_total_supply="1000000000000000000000000000",
        settlement_contract_address=settings.SETTLEMENT_CONTRACT_ADDRESS,
        platform_signer_address=signer_addr,
        treasury_address=settings.PLATFORM_TREASURY_ADDRESS,
        is_paused=False,
    )


async def create_escrow_lock(
    db: AsyncSession,
    request: CreateEscrowLockRequest,
    current_user: User,
) -> OnChainEscrow:
    """Record an on-chain budget lock transaction."""
    # 1. Verify job exists
    job = await db.get(Job, request.job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {request.job_id} not found",
        )

    # 1a. Only the job's owning customer (or an administrator) may lock an escrow
    # for it — prevents a third party from squatting/creating escrows on jobs they
    # do not own.
    if "administrator" not in current_user.roles and job.customer_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to create an escrow for this job",
        )

    # 2. Verify provider exists
    provider = await db.get(User, request.provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider user {request.provider_id} not found",
        )

    # 3. Check for existing active escrow
    existing = await db.scalar(
        select(OnChainEscrow).where(
            OnChainEscrow.job_id == request.job_id,
            OnChainEscrow.status.in_([EscrowStatus.LOCKED.value, EscrowStatus.SETTLED.value]),
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Active escrow already exists for job {request.job_id}",
        )

    job_b32 = job_id_to_bytes32(request.job_id)
    deadline_dt = utcnow() + dt.timedelta(seconds=request.deadline_seconds)

    escrow = OnChainEscrow(
        job_id=request.job_id,
        customer_id=current_user.id,
        provider_id=request.provider_id,
        chain_id=settings.SETTLEMENT_CHAIN_ID,
        contract_address=settings.SETTLEMENT_CONTRACT_ADDRESS,
        token_address=settings.COMPUTE_TOKEN_ADDRESS,
        job_bytes32=job_b32,
        locked_amount_units=request.locked_amount_units,
        deadline=deadline_dt,
        status=EscrowStatus.LOCKED.value,
        lock_tx_hash=request.lock_tx_hash,
        reconciled=False,
    )
    db.add(escrow)

    # Record on-chain transaction log
    tx = OnChainTransaction(
        chain_id=settings.SETTLEMENT_CHAIN_ID,
        tx_hash=request.lock_tx_hash,
        tx_type=OnChainTxType.LOCK.value,
        sender_address=request.customer_address,
        recipient_address=settings.SETTLEMENT_CONTRACT_ADDRESS,
        amount_units=request.locked_amount_units,
        token_symbol="COMPUTE",
        status=OnChainTxStatus.CONFIRMED.value,
        reconciled_with_ledger=False,
        job_id=request.job_id,
        metadata_json={
            "customer_id": str(current_user.id),
            "provider_id": str(request.provider_id),
            "deadline": deadline_dt.isoformat(),
        },
    )
    db.add(tx)

    await db.commit()
    await db.refresh(escrow)
    return escrow


async def issue_settlement_voucher(
    db: AsyncSession,
    request: IssueSettlementVoucherRequest,
    current_user: User,
) -> SettlementVoucherResponse:
    """Sign an off-chain cryptographic voucher authorizing provider payout."""
    escrow = await db.scalar(
        select(OnChainEscrow).where(
            OnChainEscrow.job_id == request.job_id,
            OnChainEscrow.status == EscrowStatus.LOCKED.value,
        )
    )
    if not escrow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active locked escrow for job {request.job_id} not found",
        )

    # Refuse to mint a second (differently-valued) voucher for the same escrow —
    # one escrow must have at most one valid payout authorization (money F6).
    if escrow.voucher_signature:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A settlement voucher has already been issued for this escrow",
        )

    final_cost_int = int(request.final_cost_units)
    platform_fee_int = int(request.platform_fee_units)
    locked_amt_int = int(escrow.locked_amount_units)

    if final_cost_int > locked_amt_int:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Final cost {request.final_cost_units} exceeds locked budget {escrow.locked_amount_units}",
        )

    if platform_fee_int > final_cost_int:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Platform fee {request.platform_fee_units} exceeds final cost {request.final_cost_units}",
        )

    # Create EVM-compatible packed hash
    job_bytes = bytes.fromhex(escrow.job_bytes32[2:])
    contract_addr = to_checksum_address(escrow.contract_address)

    packed_data = encode_packed(
        ["bytes32", "uint256", "uint256", "uint256", "address"],
        [
            job_bytes,
            final_cost_int,
            platform_fee_int,
            escrow.chain_id,
            contract_addr,
        ],
    )
    message_hash = keccak(packed_data)
    signable = encode_defunct(primitive=message_hash)

    acct = Account.from_key(settings.PLATFORM_SIGNER_PRIVATE_KEY)
    signed = acct.sign_message(signable)
    signature_hex = "0x" + signed.signature.hex()

    # Update escrow record
    escrow.final_cost_units = request.final_cost_units
    escrow.platform_fee_units = request.platform_fee_units
    escrow.voucher_signature = signature_hex

    await db.commit()
    await db.refresh(escrow)

    return SettlementVoucherResponse(
        job_id=escrow.job_id,
        job_bytes32=escrow.job_bytes32,
        final_cost_units=request.final_cost_units,
        platform_fee_units=request.platform_fee_units,
        chain_id=escrow.chain_id,
        contract_address=escrow.contract_address,
        signer_address=acct.address,
        signature=signature_hex,
    )


async def confirm_settlement_tx(
    db: AsyncSession,
    request: ConfirmSettlementTxRequest,
    current_user: User,
) -> OnChainEscrow:
    """Mark an escrow settled once on-chain transaction succeeds."""
    escrow = await db.scalar(
        select(OnChainEscrow).where(OnChainEscrow.job_id == request.job_id)
    )
    if not escrow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Escrow for job {request.job_id} not found",
        )

    if escrow.status == EscrowStatus.SETTLED.value:
        return escrow

    escrow.status = EscrowStatus.SETTLED.value
    escrow.settle_tx_hash = request.settle_tx_hash
    escrow.settled_at = utcnow()
    escrow.reconciled = True

    # Record OnChainTransaction
    tx = OnChainTransaction(
        chain_id=escrow.chain_id,
        tx_hash=request.settle_tx_hash,
        tx_type=OnChainTxType.SETTLE.value,
        sender_address=get_signer_address(),
        recipient_address=escrow.contract_address,
        amount_units=escrow.final_cost_units,
        token_symbol="COMPUTE",
        block_number=request.block_number,
        status=OnChainTxStatus.RECONCILED.value,
        reconciled_with_ledger=True,
        job_id=escrow.job_id,
        metadata_json={
            "final_cost_units": escrow.final_cost_units,
            "platform_fee_units": escrow.platform_fee_units,
            "settled_at": escrow.settled_at.isoformat() if escrow.settled_at else None,
        },
    )
    db.add(tx)

    await db.commit()
    await db.refresh(escrow)
    return escrow


async def confirm_refund_tx(
    db: AsyncSession,
    request: ConfirmRefundTxRequest,
    current_user: User,
) -> OnChainEscrow:
    """Mark an escrow refunded upon contract refund event."""
    escrow = await db.scalar(
        select(OnChainEscrow).where(OnChainEscrow.job_id == request.job_id)
    )
    if not escrow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Escrow for job {request.job_id} not found",
        )

    if escrow.status == EscrowStatus.REFUNDED.value:
        return escrow

    # A SETTLED escrow has already paid the provider — it must never also be
    # refunded to the customer (escrow double-spend, H3).
    if escrow.status == EscrowStatus.SETTLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot refund an already-settled escrow",
        )

    escrow.status = EscrowStatus.REFUNDED.value
    escrow.refund_tx_hash = request.refund_tx_hash
    escrow.reconciled = True

    tx = OnChainTransaction(
        chain_id=escrow.chain_id,
        tx_hash=request.refund_tx_hash,
        tx_type=OnChainTxType.REFUND.value,
        sender_address=escrow.contract_address,
        recipient_address=None,
        amount_units=escrow.locked_amount_units,
        token_symbol="COMPUTE",
        block_number=request.block_number,
        status=OnChainTxStatus.RECONCILED.value,
        reconciled_with_ledger=True,
        job_id=escrow.job_id,
        metadata_json={
            "locked_amount_units": escrow.locked_amount_units,
            "refunded_at": utcnow().isoformat(),
        },
    )
    db.add(tx)

    await db.commit()
    await db.refresh(escrow)
    return escrow


async def record_on_chain_tx(
    db: AsyncSession,
    request: RecordOnChainTxRequest,
) -> OnChainTransaction:
    """Manually or worker-ingested on-chain transaction receipt."""
    existing = await db.scalar(
        select(OnChainTransaction).where(OnChainTransaction.tx_hash == request.tx_hash)
    )
    if existing:
        return existing

    tx = OnChainTransaction(
        chain_id=request.chain_id,
        tx_hash=request.tx_hash,
        tx_type=request.tx_type,
        sender_address=request.sender_address,
        recipient_address=request.recipient_address,
        amount_units=request.amount_units,
        token_symbol=request.token_symbol,
        block_number=request.block_number,
        status=OnChainTxStatus.CONFIRMED.value,
        reconciled_with_ledger=False,
        job_id=request.job_id,
        metadata_json=request.metadata_json,
    )
    db.add(tx)
    await db.commit()
    await db.refresh(tx)
    return tx


async def get_reconciliation_summary(db: AsyncSession) -> OnChainReconciliationSummary:
    """Compute aggregate state across escrows, transactions and ledger integrity."""
    escrows = (await db.scalars(select(OnChainEscrow))).all()

    total_escrows = len(escrows)
    active_locked = sum(
        int(e.locked_amount_units) for e in escrows if e.status == EscrowStatus.LOCKED.value
    )
    total_settled = sum(
        int(e.final_cost_units)
        for e in escrows
        if e.status == EscrowStatus.SETTLED.value and e.final_cost_units
    )
    total_fees = sum(
        int(e.platform_fee_units)
        for e in escrows
        if e.status == EscrowStatus.SETTLED.value and e.platform_fee_units
    )
    total_refunded = sum(
        int(e.locked_amount_units) for e in escrows if e.status == EscrowStatus.REFUNDED.value
    )

    unreconciled_txs = await db.scalar(
        select(func.count(OnChainTransaction.id)).where(
            OnChainTransaction.reconciled_with_ledger.is_(False)
        )
    ) or 0

    # Invariant: all escrows in settled or refunded state should have reconciled=True
    unreconciled_escrows = sum(
        1 for e in escrows if e.status in (EscrowStatus.SETTLED.value, EscrowStatus.REFUNDED.value) and not e.reconciled
    )

    ledger_parity = (unreconciled_escrows == 0)

    return OnChainReconciliationSummary(
        total_escrows=total_escrows,
        active_locked_amount_units=str(active_locked),
        total_settled_amount_units=str(total_settled),
        total_fees_collected_units=str(total_fees),
        total_refunded_amount_units=str(total_refunded),
        unreconciled_transactions_count=unreconciled_txs,
        ledger_parity_verified=ledger_parity,
    )
