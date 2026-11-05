"""U16: abuse flags, submission holds and abuse reports. LEGAL SIGN-OFF required before enforcement."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class AbuseFlag(Base):
    __tablename__ = "abuse_flags"
    __table_args__ = (Index("ix_abuse_flags_job_signal", "job_id", "signal"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    signal: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # open | confirmed | dismissed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    enforced: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AbuseHold(Base):
    """Blocks new job submissions for a user until an admin releases it."""

    __tablename__ = "abuse_holds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flag_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("abuse_flags.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AbuseReport(Base):
    __tablename__ = "abuse_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # open | actioned | rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    actioned_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actioned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action_note: Mapped[str | None] = mapped_column(Text, nullable=True)
