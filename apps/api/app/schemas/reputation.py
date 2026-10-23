"""Reputation + slashing request/response schemas (Phase B3).

Money is serialized as a 2dp decimal STRING (never float), matching
``app/schemas/billing.py``. Scores are 4dp decimal strings for the same reason:
a float round-trip could make an audited score disagree with the stored value.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, Field, field_validator


class ReputationOut(BaseModel):
    """A provider's current reputation plus the evidence behind it."""

    provider_id: uuid.UUID
    score: str
    completed_count: int
    failed_count: int
    verification_failures: int
    disputes_lost: int
    # False when no aggregate has been persisted yet: the score shown is a
    # read-only derivation from history, not a stored row.
    persisted: bool
    factors: dict
    updated_at: dt.datetime | None = None


class ReputationEventOut(BaseModel):
    event_id: uuid.UUID
    kind: str
    delta: str
    reason: str | None = None
    job_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    created_at: dt.datetime


class SlashCreate(BaseModel):
    """Admin slash request. ``idempotency_key`` is mandatory: a slash moves real
    money, so a retried request must be provably the same request."""

    amount: decimal.Decimal = Field(gt=0)
    reason: str = Field(min_length=8, max_length=1024)
    idempotency_key: str = Field(min_length=8, max_length=128)
    job_id: uuid.UUID | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v) -> decimal.Decimal:
        return decimal.Decimal(str(v))


class SlashOut(BaseModel):
    slash_id: uuid.UUID
    provider_id: uuid.UUID
    job_id: uuid.UUID | None = None
    amount: str
    currency: str
    reason: str
    ledger_txn_id: uuid.UUID | None = None
    created_at: dt.datetime
