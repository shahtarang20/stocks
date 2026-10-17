"""Model registry (Phase 4): approved models + their versions.

A ``job.model`` string references a ``model_versions.version_tag``. Validation
checks the version is ``trust_status == 'approved'`` and that it supports the
requested workload. ``runtime`` is ``"simulated"`` (in-process deterministic
transform, ``image_digest`` NULL) or ``"hardware"`` (pinned ``image_digest``).

Portable column types only (Uuid, JSON, String, DateTime(timezone=True)) so the
models run on SQLite (tests) and Postgres (prod).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin

# Trust vocabulary (checked varchar — portable Enum).
MODEL_TRUST_STATUSES = ("pending", "approved", "rejected", "suspended")
MODEL_RUNTIMES = ("simulated", "hardware")


class Model(Base, TimestampMixin):
    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trust_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    versions: Mapped[list["ModelVersion"]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )


class ModelVersion(Base, TimestampMixin):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("version_tag", name="uq_model_versions_version_tag"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The public reference used in job submissions (e.g. "echo-sim-v1").
    version_tag: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="simulated")
    # NULL => simulated executor; a pinned digest => DockerExecutor.
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supported_workloads: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    trust_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    model: Mapped["Model"] = relationship(back_populates="versions")
