"""Seed real pinned hardware model for Workstream B.

Revision ID: 0009_hardware_model_phase4
Revises: 0008_settlement_phase10
Create Date: 2026-10-17
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_hardware_model_phase4"
down_revision: Union[str, None] = "0008_settlement_phase10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HW_MODEL_ID = uuid.UUID("7a2e4c19-1234-4567-89ab-cdef01234567")
_HW_MODEL_VERSION_ID = uuid.UUID("8b3f5d2a-2345-6789-abcd-ef0123456789")

PINNED_IMAGE_DIGEST = (
    "ghcr.io/computecoin/model-runners/tinyllama-v1@sha256:"
    "4a382c2b3e83b429d2f2d93e2b243888365671158d3493864a66a1523497c654"
)


def upgrade() -> None:
    models = sa.Table(
        "models",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=128)),
        sa.Column("display_name", sa.String(length=255)),
        sa.Column("trust_status", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    model_versions = sa.Table(
        "model_versions",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("model_id", sa.Uuid()),
        sa.Column("version_tag", sa.String(length=128)),
        sa.Column("runtime", sa.String(length=32)),
        sa.Column("image_digest", sa.String(length=255)),
        sa.Column("supported_workloads", sa.JSON()),
        sa.Column("trust_status", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    now = dt.datetime.now(dt.timezone.utc)
    op.bulk_insert(
        models,
        [
            {
                "id": _HW_MODEL_ID,
                "name": "tinyllama-1.1b-chat",
                "display_name": "TinyLlama 1.1B Chat (Hardware)",
                "trust_status": "approved",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        model_versions,
        [
            {
                "id": _HW_MODEL_VERSION_ID,
                "model_id": _HW_MODEL_ID,
                "version_tag": "tinyllama-1.1b-chat-v1",
                "runtime": "hardware",
                "image_digest": PINNED_IMAGE_DIGEST,
                "supported_workloads": ["text-generation"],
                "trust_status": "approved",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM model_versions WHERE id = :id").bindparams(
            id=_HW_MODEL_VERSION_ID
        )
    )
    op.execute(
        sa.text("DELETE FROM models WHERE id = :id").bindparams(id=_HW_MODEL_ID)
    )
