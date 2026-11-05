"""Data plane (U2, ADR-0006 section 9): content-addressed objects, resumable uploads, files, BYO buckets.

File bytes never pass through the API. These tables only hold metadata and the state of each upload.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

# DataObject.status
OBJECT_READY = "ready"
OBJECT_DELETING = "deleting"

# DataUpload.status
UPLOAD_ACTIVE = "active"  # parts are being uploaded
UPLOAD_VERIFYING = "verifying"  # all parts in, whole-file hash not yet proven
UPLOAD_COMPLETE = "complete"
UPLOAD_FAILED = "failed"  # whole-file hash did not match, or the store refused
UPLOAD_ABORTED = "aborted"  # the owner cancelled
UPLOAD_EXPIRED = "expired"  # the sweeper aborted it
UPLOAD_OPEN = (UPLOAD_ACTIVE, UPLOAD_VERIFYING)

# DataUploadPart.status
PART_PENDING = "pending"  # a URL was issued, nothing proven yet
PART_VERIFIED = "verified"  # the store holds the part and its SHA-256 matches the declared one
PART_CORRUPT = "corrupt"  # the store's part does not match; the client must send it again


class DataObject(Base):
    """One stored blob per (scope, SHA-256). ``refcount`` counts the DataFile rows pointing at it."""

    __tablename__ = "data_objects"
    __table_args__ = (
        UniqueConstraint("scope_key", "sha256", name="uq_data_objects_scope_sha"),
        CheckConstraint("refcount >= 0", name="ck_data_objects_refcount_nonneg"),
        Index("ix_data_objects_gc", "status", "refcount", "last_referenced_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # "global" when cross-account dedup is on, otherwise the owner's user id (no hash oracle).
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # lowercase hex of the whole file
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    refcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OBJECT_READY)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_referenced_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DataFile(Base):
    """A named reference to a DataObject owned by one account, with an optional expiry."""

    __tablename__ = "data_files"
    __table_args__ = (
        Index("ix_data_files_owner_created", "owner_id", "created_at"),
        Index("ix_data_files_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # NULL once the file was deleted and the object garbage-collected.
    object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("data_objects.id", ondelete="RESTRICT"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # owner | retention


class DataUpload(Base):
    __tablename__ = "data_uploads"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_data_uploads_owner_idem"),
        Index("ix_data_uploads_status_expiry", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # the customer's claim, proven at completion
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UPLOAD_ACTIVE)
    store_upload_id: Mapped[str] = mapped_column(String(512), nullable=False)
    staging_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # set when complete
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataUploadPart(Base):
    __tablename__ = "data_upload_parts"
    __table_args__ = (UniqueConstraint("upload_id", "part_number", name="uq_data_upload_parts"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("data_uploads.id", ondelete="CASCADE"), nullable=False)
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # declared by the client, enforced by the store
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PART_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ByoBucket(Base):
    """A customer-owned bucket. Credentials are encrypted at rest and never returned."""

    __tablename__ = "byo_buckets"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_byo_buckets_owner_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)  # "" = AWS default
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="us-east-1")
    prefix: Mapped[str] = mapped_column(String(256), nullable=False, default="")  # ComputeCoin may only touch this
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="presign")  # presign | sts
    role_arn: Mapped[str | None] = mapped_column(String(256), nullable=True)  # sts mode
    credentials_enc: Mapped[str] = mapped_column(Text, nullable=False)  # AES-GCM, base64
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
