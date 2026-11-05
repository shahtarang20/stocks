"""Schemas for token withdrawals (B1)."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, Field, field_validator

from app.schemas.billing import parse_money


class WithdrawalCreate(BaseModel):
    """Amount is in credits (strict decimal, max 2 places). Destination must be the caller's linked wallet."""

    amount: decimal.Decimal = Field(gt=0)
    destination: str = Field(min_length=42, max_length=42, pattern=r"^0x[0-9a-fA-F]{40}$")

    @field_validator("amount", mode="before")
    @classmethod
    def _strict(cls, v):
        return parse_money(v)


class WithdrawalOut(BaseModel):
    id: uuid.UUID
    status: str
    credits: str
    tokens: str
    destination: str
    chain_id: int
    tx_hash: str | None = None
    refund_reason: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class PauseIn(BaseModel):
    paused: bool
    reason: str = Field(min_length=1, max_length=500)


class CancelIn(BaseModel):
    reason: str = Field(default="cancelled_by_admin", min_length=1, max_length=64)
