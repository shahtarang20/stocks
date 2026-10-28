"""AMD GPU support: hardware inventory vendor/runtime, required_gpu_runtime, model_version_images, and challenge hashes by runtime (WP4).

Revision ID: 0022_amd_gpu_support
Revises: 0021_tpm_enrolment
"""

from typing import Sequence, Union
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0022_amd_gpu_support"
down_revision: Union[str, None] = "0021_tpm_enrolment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. hardware_inventory: add gpu_vendor, gpu_runtime, gpu_runtime_version
    with op.batch_alter_table("hardware_inventory") as batch_op:
        batch_op.add_column(sa.Column("gpu_vendor", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("gpu_runtime", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("gpu_runtime_version", sa.String(length=64), nullable=True))

    # 2. jobs: add required_gpu_runtime (NOT NULL, server default 'cuda')
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("required_gpu_runtime", sa.String(length=32), nullable=False, server_default="cuda")
        )

    # 3. model_version_images child table
    op.create_table(
        "model_version_images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "model_version_id",
            sa.Uuid(),
            sa.ForeignKey("model_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gpu_runtime", sa.String(length=32), nullable=False),
        sa.Column("image_digest", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("model_version_id", "gpu_runtime", name="uq_model_version_images_runtime"),
    )
    op.create_index("ix_model_version_images_model_version_id", "model_version_images", ["model_version_id"])

    # 4. verification_challenges: per-runtime expected hashes
    # Stored as a JSON column {"cuda": "...", "rocm": "..."} because it is compact,
    # read atomically with the challenge without join overhead, and easily nullable.
    with op.batch_alter_table("verification_challenges") as batch_op:
        batch_op.add_column(sa.Column("expected_hashes_by_runtime", sa.JSON(), nullable=True))

    # 5. Backfill existing data (D4 & D8)
    conn = op.get_bind()

    # Backfill hardware_inventory for machines with GPUs
    conn.execute(
        sa.text(
            "UPDATE hardware_inventory "
            "SET gpu_vendor = 'nvidia', gpu_runtime = 'cuda' "
            "WHERE gpu_count > 0 AND (gpu_vendor IS NULL OR gpu_vendor = '')"
        )
    )

    # Backfill jobs
    conn.execute(
        sa.text(
            "UPDATE jobs "
            "SET required_gpu_runtime = 'cuda' "
            "WHERE required_gpu_runtime IS NULL OR required_gpu_runtime = ''"
        )
    )

    # Backfill model_version_images from model_versions.image_digest
    mv_rows = conn.execute(
        sa.text("SELECT id, image_digest FROM model_versions WHERE image_digest IS NOT NULL")
    ).fetchall()

    if mv_rows:
        mvi_table = sa.table(
            "model_version_images",
            sa.column("id", sa.Uuid),
            sa.column("model_version_id", sa.Uuid),
            sa.column("gpu_runtime", sa.String),
            sa.column("image_digest", sa.String),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        )
        for row in mv_rows:
            mv_id, digest = row[0], row[1]
            if digest:
                mv_uuid = mv_id if isinstance(mv_id, uuid.UUID) else uuid.UUID(str(mv_id))
                conn.execute(
                    sa.insert(mvi_table).values(
                        id=uuid.uuid4(),
                        model_version_id=mv_uuid,
                        gpu_runtime="cuda",
                        image_digest=digest,
                        created_at=sa.func.now(),
                        updated_at=sa.func.now(),
                    )
                )


def downgrade() -> None:
    # 4. Drop expected_hashes_by_runtime from verification_challenges
    with op.batch_alter_table("verification_challenges") as batch_op:
        batch_op.drop_column("expected_hashes_by_runtime")

    # 3. Drop model_version_images table
    op.drop_index("ix_model_version_images_model_version_id", table_name="model_version_images")
    op.drop_table("model_version_images")

    # 2. Drop required_gpu_runtime from jobs
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("required_gpu_runtime")

    # 1. Drop gpu columns from hardware_inventory
    with op.batch_alter_table("hardware_inventory") as batch_op:
        batch_op.drop_column("gpu_runtime_version")
        batch_op.drop_column("gpu_runtime")
        batch_op.drop_column("gpu_vendor")
