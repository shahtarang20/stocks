"""Workload Kit registry (U1, ADR-0006 section 8). A published Kit version is immutable.

Only ``status`` and ``yanked_at`` may change after insert (yank = stop new jobs from using it).
Everything else, including the signed manifest, is guarded by a ``before_update`` hook and has no
update or delete route. A fix is a new version.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

KIT_STATUSES = ("published", "yanked")
_MUTABLE = {"status", "yanked_at"}


class Kit(Base):
    __tablename__ = "kits"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_kits_slug_version"),
        Index("ix_kits_category", "category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    yanked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KitImmutableError(Exception):
    """Raised when code tries to change a published Kit beyond yanking it."""


@event.listens_for(Kit, "before_update")
def _guard_kit_immutable(_mapper, _connection, target: Kit) -> None:
    state = inspect(target)
    for attr in state.attrs:
        if attr.key in _MUTABLE:
            continue
        if attr.history.has_changes():
            raise KitImmutableError(f"kit_immutable:{attr.key}")
