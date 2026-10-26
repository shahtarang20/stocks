"""Schemas for Phase 8 Remote Hardware Attestation & Isolation."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field


class AttestationChallengeRequest(BaseModel):
    machine_id: uuid.UUID


class AttestationChallengeResponse(BaseModel):
    machine_id: uuid.UUID
    nonce: str
    expires_in_seconds: int = 300


class AttestationSubmitRequest(BaseModel):
    nonce: str = Field(min_length=16, max_length=128)
    isolation_tier: str = Field(
        default="confidential", pattern="^(confidential|gvisor|microvm|sealed|sealed_pro)$"
    )
    timestamp: str
    pcr_measurements: dict[str, str] = Field(default_factory=dict)
    platform_quote: str = Field(min_length=16, max_length=1024)
    signature: str = Field(min_length=32, max_length=512)
    # ADR-0005 privacy tiers: keys generated in the host's RAM (X25519 for receiving Data Key
    # shares, Ed25519 for signing its wipe receipt) and the security locks it reports.
    ephemeral_public_key: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    receipt_signing_public_key: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    security_state: dict[str, bool] | None = None


class AttestationVerifyResponse(BaseModel):
    verified: bool
    machine_id: uuid.UUID
    isolation_tier: str
    verification_status: str
    verified_at: dt.datetime
    attestation_id: uuid.UUID
    details: dict | None = None
    verdict: dict | None = None


class AttestationReportOut(BaseModel):
    attestation_id: uuid.UUID
    machine_id: uuid.UUID
    isolation_tier: str
    verification_status: str
    nonce: str
    pcr_measurements: dict[str, str]
    verified_at: dt.datetime | None
    created_at: dt.datetime
