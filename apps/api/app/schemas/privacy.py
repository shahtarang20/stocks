"""Pydantic schemas for Proof-of-Privacy (ADR-0005)."""

from __future__ import annotations

import datetime as dt
import re
import decimal
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransparencyLogLeafOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leaf_index: int
    event_type: str
    leaf_hash: str
    payload: dict[str, Any]
    merkle_root: str
    anchored_tx_hash: str | None = None
    anchored_block_number: int | None = None
    created_at: dt.datetime


class InclusionProofOut(BaseModel):
    """RFC 6962 inclusion proof. ``audit_path`` is the list of sibling hashes, bottom-up."""

    leaf_index: int
    tree_size: int
    leaf_hash: str
    root_hash: str
    audit_path: list[str]
    event_type: str
    payload: dict[str, Any]


class VerifierKeysOut(BaseModel):
    verifier_public_key: str
    guardians: dict[str, str]
    threshold: int


class PrivacyReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    machine_id: uuid.UUID | None = None
    isolation_tier: str
    image_hash: str
    attestation_verdict: dict[str, Any]
    guardian_releases: list[dict[str, Any]]
    wipe_receipt: dict[str, Any] | None = None
    merkle_root: str
    inclusion_proofs: dict[str, Any]
    verified: bool
    created_at: dt.datetime


class TrustedImageCreate(BaseModel):
    image_hash: str = Field(pattern="^[0-9a-f]{64}$")
    version_tag: str = Field(min_length=3, max_length=64)
    tier: str = Field(default="sealed", pattern="^(sealed|sealed_pro|confidential)$")
    # Reference measurements a host must report exactly. At least one, or every host would match.
    expected_pcrs: dict[str, str] = Field(min_length=1, max_length=24)

    @field_validator("expected_pcrs")
    @classmethod
    def _pcrs_are_digests(cls, v: dict[str, str]) -> dict[str, str]:
        for name, digest in v.items():
            if not re.fullmatch(r"pcr([0-9]|1[0-9]|2[0-3])", name) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{96}", digest):
                raise ValueError(f"{name}: expected 'pcrN' -> lowercase SHA-1/256/384 hex digest")
        return v


class TrustedImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    image_hash: str
    version_tag: str
    tier: str
    expected_pcrs: dict[str, str]
    status: str
    created_at: dt.datetime


class CanaryLeakReportRequest(BaseModel):
    canary_token: str = Field(min_length=16, max_length=128)
    proof_description: str = Field(min_length=5, max_length=1024)
    evidence: dict[str, Any] = Field(default_factory=dict)


class CanaryLeakReportResponse(BaseModel):
    status: str
    machine_id: uuid.UUID | None = None
    slashed_amount: str | None = None
    customer_compensation: str | None = None
    detected_at: dt.datetime | None = None


class FreivaldsVerifyRequest(BaseModel):
    matrix_a: list[list[float]]
    matrix_b: list[list[float]]
    matrix_c: list[list[float]]
    rounds: int = Field(default=10, ge=1, le=50)


class FreivaldsVerifyResponse(BaseModel):
    verified: bool
    rounds: int
    error_bound: float


class StakeBondRequest(BaseModel):
    machine_id: uuid.UUID
    amount: decimal.Decimal = Field(gt=0, le=decimal.Decimal("1000000000"), max_digits=18, decimal_places=2)
    # The ComputeStakeVault deposit transaction this records.
    vault_tx_hash: str = Field(pattern="^0x[0-9a-fA-F]{64}$")


class StakeBondResponse(BaseModel):
    machine_id: uuid.UUID
    bonded_stake: decimal.Decimal
    eligible_tiers: list[str]
