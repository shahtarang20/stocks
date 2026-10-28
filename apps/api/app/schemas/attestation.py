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
    # Hardware evidence (WP-F): used only when PRIVACY_HARDWARE_ATTESTATION is on. "tpm2" for Sealed /
    # Sealed Pro; "confidential" = {cpu_flavor: tdx|sev_snp, cpu, gpu} for Confidential. The signed
    # measurements replace pcr_measurements for the trusted-image match.
    hardware_flavor: str | None = Field(default=None, pattern="^(tpm2|confidential)$")
    hardware_evidence: dict | None = None


class TpmEnrolmentStartRequest(BaseModel):
    """The host's EK certificate (+ intermediates up to a pinned TPM vendor root) and its AK."""

    ek_cert: str = Field(min_length=64, max_length=16384)
    ek_intermediates: list[str] = Field(default_factory=list, max_length=5)
    ak_public: str = Field(pattern="^[0-9a-f]{2,2048}$")  # TPMT_PUBLIC, hex


class TpmEnrolmentStartResponse(BaseModel):
    enrolment_id: uuid.UUID
    ak_name: str
    credential_blob: str  # TPM2B_ID_OBJECT contents, hex
    encrypted_secret: str  # TPM2B_ENCRYPTED_SECRET contents, hex
    expires_at: dt.datetime


class TpmEnrolmentActivateRequest(BaseModel):
    secret: str = Field(pattern="^[0-9a-f]{2,64}$")


class TpmEnrolmentActivateResponse(BaseModel):
    enrolment_id: uuid.UUID
    ak_name: str
    status: str


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
