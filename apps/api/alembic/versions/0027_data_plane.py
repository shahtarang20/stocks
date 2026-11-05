"""Data plane (U2): content-addressed objects, resumable uploads, files, BYO buckets. OFF by default.

Revision ID: 0027_data_plane
Revises: 0026_kits
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_data_plane"
down_revision: Union[str, None] = "0026_kits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "data_objects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(256), nullable=False, unique=True),
        sa.Column("refcount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("created_at", _now(), nullable=False),
        sa.Column("last_referenced_at", _now(), nullable=False),
        sa.UniqueConstraint("scope_key", "sha256", name="uq_data_objects_scope_sha"),
        sa.CheckConstraint("refcount >= 0", name="ck_data_objects_refcount_nonneg"),
    )
    op.create_index("ix_data_objects_gc", "data_objects", ["status", "refcount", "last_referenced_at"])

    op.create_table(
        "data_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("object_id", sa.Uuid(), sa.ForeignKey("data_objects.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", _now(), nullable=False),
        sa.Column("expires_at", _now(), nullable=True),
        sa.Column("deleted_at", _now(), nullable=True),
        sa.Column("deleted_reason", sa.String(32), nullable=True),
    )
    op.create_index("ix_data_files_owner_created", "data_files", ["owner_id", "created_at"])
    op.create_index("ix_data_files_expiry", "data_files", ["expires_at"])

    op.create_table(
        "data_uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("store_upload_id", sa.String(512), nullable=False),
        sa.Column("staging_key", sa.String(256), nullable=False, unique=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", _now(), nullable=False),
        sa.Column("expires_at", _now(), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_data_uploads_owner_idem"),
    )
    op.create_index("ix_data_uploads_status_expiry", "data_uploads", ["status", "expires_at"])

    op.create_table(
        "data_upload_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("upload_id", sa.Uuid(), sa.ForeignKey("data_uploads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("etag", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", _now(), nullable=False),
        sa.UniqueConstraint("upload_id", "part_number", name="uq_data_upload_parts"),
    )

    op.create_table(
        "byo_buckets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("bucket", sa.String(128), nullable=False),
        sa.Column("region", sa.String(64), nullable=False, server_default="us-east-1"),
        sa.Column("prefix", sa.String(256), nullable=False, server_default=""),
        sa.Column("mode", sa.String(16), nullable=False, server_default="presign"),
        sa.Column("role_arn", sa.String(256), nullable=True),
        sa.Column("credentials_enc", sa.Text(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", _now(), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_byo_buckets_owner_name"),
    )


def downgrade() -> None:
    op.drop_table("byo_buckets")
    op.drop_table("data_upload_parts")
    op.drop_index("ix_data_uploads_status_expiry", table_name="data_uploads")
    op.drop_table("data_uploads")
    op.drop_index("ix_data_files_expiry", table_name="data_files")
    op.drop_index("ix_data_files_owner_created", table_name="data_files")
    op.drop_table("data_files")
    op.drop_index("ix_data_objects_gc", table_name="data_objects")
    op.drop_table("data_objects")
