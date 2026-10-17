"""On-chain settlement & smart contract escrow models (Phase 10).

Connects EVM smart-contract escrow locks/settlements with off-chain PostgreSQL
ledger balances, job execution proofs, and cryptographic platform vouchers.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, utcnow


class EscrowStatus(str, enum.Enum):
    LOCKED = "locked"
    SETTLED = "settled"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class OnChainTxType(str, enum.Enum):
    LOCK = "lock"
    SETTLE = "settle"
    REFUND = "refund"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


class OnChainTxStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    RECONCILED = "reconciled"


class OnChainEscrow(Base, TimestampMixin):
    """Tracks an on-chain escrow lock deposited into ComputeSettlement.sol."""

    __tablename__ = "on_chain_escrows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=31337)
    contract_address: Mapped[str] = mapped_column(String(42), nullable=False)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False)
    job_bytes32: Mapped[str] = mapped_column(String(66), nullable=False, index=True)

    locked_amount_units: Mapped[str] = mapped_column(String(78), nullable=False)
    deadline: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EscrowStatus.LOCKED.value, index=True
    )

    lock_tx_hash: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    settle_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    refund_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)

    final_cost_units: Mapped[str | None] = mapped_column(String(78), nullable=True)
    platform_fee_units: Mapped[str | None] = mapped_column(String(78), nullable=True)
    voucher_signature: Mapped[str | None] = mapped_column(String(132), nullable=True)

    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    job = relationship("Job", foreign_keys=[job_id])
    customer = relationship("User", foreign_keys=[customer_id])
    provider = relationship("User", foreign_keys=[provider_id])

    __table_args__ = (
        Index("ix_on_chain_escrows_status_deadline", "status", "deadline"),
        Index("ix_on_chain_escrows_contract_job", "contract_address", "job_bytes32"),
    )


class OnChainTransaction(Base, TimestampMixin):
    """Audit log of on-chain operations reconciled against Postgres ledger."""

    __tablename__ = "on_chain_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), unique=True, nullable=False, index=True)
    tx_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    sender_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    recipient_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    amount_units: Mapped[str | None] = mapped_column(String(78), nullable=True)
    token_symbol: Mapped[str] = mapped_column(String(16), default="COMPUTE", nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), default=OnChainTxStatus.CONFIRMED.value, nullable=False, index=True
    )
    reconciled_with_ledger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ledger_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationships
    ledger_txn = relationship("LedgerTransaction", foreign_keys=[ledger_txn_id])
    job = relationship("Job", foreign_keys=[job_id])

    __table_args__ = (
        Index("ix_on_chain_tx_chain_sender", "chain_id", "sender_address"),
    )
