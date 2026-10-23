"""Verification schemas (Phase B2): challenges + verification evidence.

Hashes are lowercase hex SHA-256 strings; similarity is serialized as a decimal
STRING (never a float) so a score is never silently re-rounded by JSON.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HEX64 = r"^[0-9a-fA-F]{64}$"


class ChallengeCreate(BaseModel):
    """Admin payload creating a known-answer challenge.

    Either ``expected_output`` (we hash it) or ``expected_output_hash`` (the
    plaintext answer never touches our DB) must be supplied — never neither.
    """

    # The field is literally named `model`; opt out of pydantic's protected ns.
    model_config = ConfigDict(protected_namespaces=())

    workload_type: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    input: dict = Field(default_factory=dict)
    expected_output: Any | None = None
    expected_output_hash: str | None = Field(default=None, pattern=_HEX64)
    config: dict = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=255)
    active: bool = True

    @field_validator("config")
    @classmethod
    def _known_mode(cls, value: dict) -> dict:
        mode = str(value.get("mode", "exact")).lower()
        if mode not in ("exact", "field"):
            raise ValueError("config.mode must be 'exact' or 'field'")
        return value

    @model_validator(mode="after")
    def _answer_required(self) -> "ChallengeCreate":
        if self.expected_output is None and self.expected_output_hash is None:
            raise ValueError("expected_output or expected_output_hash is required")
        return self


class ChallengeOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    challenge_id: uuid.UUID
    workload_type: str
    model: str
    input: dict
    expected_output_hash: str
    config: dict
    label: str | None = None
    active: bool
    created_at: dt.datetime


class VerificationResultOut(BaseModel):
    result_id: uuid.UUID
    job_id: uuid.UUID
    challenge_id: uuid.UUID | None = None
    kind: Literal["challenge", "redundant", "model_identity"]
    passed: bool
    expected_hash: str | None = None
    actual_hash: str | None = None
    detail: dict | None = None
    created_at: dt.datetime


class RedundancyCheckOut(BaseModel):
    check_id: uuid.UUID
    primary_job_id: uuid.UUID
    shadow_job_id: uuid.UUID | None = None
    status: str
    matched: bool | None = None
    # Decimal as STRING: a score is evidence, never re-rounded by a JSON float.
    similarity: str | None = None
    detail: dict | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None
