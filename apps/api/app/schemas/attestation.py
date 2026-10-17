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
    isolation_tier: str = Field(default="confidential", pattern="^(confidential|gvisor|microvm)$")
    timestamp: str
    pcr_measurements: dict[str, str] = Field(default_factory=dict)
    platform_quote: str = Field(min_length=16, max_length=1024)
    signature: str = Field(min_length=32, max_length=512)


class AttestationVerifyResponse(BaseModel):
    verified: bool
    machine_id: uuid.UUID
    isolation_tier: str
    verification_status: str
    verified_at: dt.datetime
    attestation_id: uuid.UUID
    details: dict | None = None


class AttestationReportOut(BaseModel):
    attestation_id: uuid.UUID
    machine_id: uuid.UUID
    isolation_tier: str
    verification_status: str
    nonce: str
    pcr_measurements: dict[str, str]
    verified_at: dt.datetime | None
    created_at: dt.datetime
