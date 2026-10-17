"""Declarative base + shared column helpers.

A single ``Base`` is used for all models so Alembic autogenerate and
``create_all`` see the full metadata.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_aware(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a datetime to timezone-aware UTC.

    SQLite does not preserve tzinfo on DateTime(timezone=True) columns, so
    values read back are naive. Treat naive values as UTC for safe comparison
    across both SQLite (tests) and Postgres (prod).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
