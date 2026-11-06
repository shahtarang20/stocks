"""Agent release management (cross-platform plan W6/W10): signed update manifests per platform and ring.

A row holds one SIGNED manifest, exactly as the release engineer signed it offline (the API holds only the public key,
L7). Only the distribution state may change after insert (``status`` and its bookkeeping); the manifest and its signature
are immutable, guarded by a ``before_update`` hook. A change of ring or content is a new row (a new signed manifest).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

RELEASE_STATUSES = ("published", "paused", "rolled_back")
_MUTABLE = {"status", "status_reason", "status_changed_at", "status_changed_by"}


class AgentRelease(Base):
    __tablename__ = "agent_releases"
    __table_args__ = (
        UniqueConstraint("platform", "version", "ring", name="uq_agent_releases_platform_version_ring"),
        Index("ix_agent_releases_platform_status", "platform", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    ring: Mapped[str] = mapped_column(String(16), nullable=False)  # widest ring this release is offered to
    min_supported: Mapped[str] = mapped_column(String(40), nullable=False)  # the forced-update floor it carries
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)  # the signed payload, without the signature
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    status_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_changed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentRingAssignment(Base):
    """Which update ring a machine belongs to. No row = ``general`` (the safest, last ring)."""

    __tablename__ = "agent_ring_assignments"

    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), primary_key=True
    )
    ring: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentReleaseImmutableError(Exception):
    """Raised when code tries to change a published release beyond its distribution state."""


@event.listens_for(AgentRelease, "before_update")
def _guard_release_immutable(_mapper, _connection, target: AgentRelease) -> None:
    state = inspect(target)
    for attr in state.attrs:
        if attr.key in _MUTABLE:
            continue
        if attr.history.has_changes():
            raise AgentReleaseImmutableError(f"agent_release_immutable:{attr.key}")
