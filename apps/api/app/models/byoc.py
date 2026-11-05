"""U8 BYOC: known-bad image denylist and cached scan results (docs/architecture/U8-byoc-design.md)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class ByocImageDenylist(Base):
    """An image content digest (``sha256:<64 hex>``) that must never run. Checked at submit, validate and claim."""

    __tablename__ = "byoc_image_denylist"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    digest: Mapped[str] = mapped_column(String(71), nullable=False, unique=True, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ByocImageScan(Base):
    """Latest scan result for a digest; only ``status == "clean"`` and fresh lets a job in."""

    __tablename__ = "byoc_image_scans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    digest: Mapped[str] = mapped_column(String(71), nullable=False, unique=True, index=True)
    scanner: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # clean | denied
    findings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    scanned_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
