"""COMPUTE tokens received at the platform deposit address, one row per on-chain transfer."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

DEPOSIT_STATUSES = ("credited", "unclaimed", "rejected")


class TokenDeposit(Base):
    """A Transfer(token -> deposit address) event, recorded exactly once.

    ``(chain_id, tx_hash, log_index)`` is unique, so scanning the same block twice, or two scanners
    running at once, can never credit the same transfer twice.

    credited  : the sender is a wallet linked to an account; credits were posted to the ledger
    unclaimed : tokens arrived from an address nobody has linked; nothing is credited (an admin can
                resolve it by hand); the tokens are still counted in the reconciliation
    rejected  : outside the allowed size range, or too small to be worth a whole cent of credit
    """

    __tablename__ = "token_deposits"
    __table_args__ = (
        UniqueConstraint("chain_id", "tx_hash", "log_index", name="uq_token_deposits_event"),
        Index("ix_token_deposits_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False)
    from_address: Mapped[str] = mapped_column(String(42), nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), nullable=False)
    # Smallest-unit amount as a decimal string: token amounts have 18 decimals, and SQLite would turn a
    # numeric column into a float and quietly lose digits. Python ints keep it exact everywhere.
    amount_raw: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    credits_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ledger_txn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
