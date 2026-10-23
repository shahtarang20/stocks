"""Chain reconciliation audit runs (Phase D).

WHY persist each run: "the off-chain ledger agrees with on-chain escrow" is a
claim, and an unrecorded claim is not evidence. Every sweep writes one row with
how many escrows it checked and exactly which drifted, so a later auditor can
see both the passes and the failures — never only the happy ones (HONESTY rule).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow


class ChainReconciliationRun(Base):
    """One pass of the escrow/ledger reconciliation sweeper."""

    __tablename__ = "chain_reconciliation_runs"
    __table_args__ = (
        Index("ix_chain_reconciliation_runs_chain_started", "chain_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escrows_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drift_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-escrow findings; empty list means "checked, nothing drifted".
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # False while running and for any run that found drift or raised.
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
