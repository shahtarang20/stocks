"""Token withdrawals (B1, ADR-0004 stage 2): one row per request to turn credits into COMPUTE tokens."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

# pending   : credits are in withdrawal_pending; nothing sent yet
# sending   : the worker claimed it, signed it and stored tx_hash / raw_tx; broadcast may not have happened
# broadcast : sent to the chain; waiting for a receipt and the confirmations
# paid_out  : confirmed; credits moved withdrawal_pending -> paid_out
# refunded  : failed or cancelled; credits moved back to the user, with refund_reason recorded
WITHDRAWAL_STATUSES = ("pending", "sending", "broadcast", "paid_out", "refunded")
IN_FLIGHT_STATUSES = ("sending", "broadcast")
OPEN_STATUSES = ("pending", "sending", "broadcast")


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_withdrawals_user_idem"),
        Index("ix_withdrawals_status_created", "status", "created_at"),
        Index("ix_withdrawals_user_created", "user_id", "created_at"),
        # One in-flight transaction per hot wallet, enforced by the database as a backstop.
        Index(
            "uq_withdrawals_one_in_flight",
            "hot_wallet",
            unique=True,
            postgresql_where=text("status IN ('sending','broadcast')"),
            sqlite_where=text("status IN ('sending','broadcast')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False)
    destination: Mapped[str] = mapped_column(String(42), nullable=False)  # lower-case, a linked wallet
    credits_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_raw: Mapped[str] = mapped_column(String(80), nullable=False)  # token smallest units, exact
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Set only while sending / broadcast (and kept afterwards for the record).
    hot_wallet: Mapped[str | None] = mapped_column(String(42), nullable=True)
    nonce: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    raw_tx: Mapped[str | None] = mapped_column(Text, nullable=True)  # the SIGNED tx (no key material)
    receipt_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    broadcast_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refund_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_txn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    final_txn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # paid_out or refund posting
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
