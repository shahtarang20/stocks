"""Remote hardware attestation for confidential computing (Phase 8).

Generates and cryptographically verifies remote attestation reports.
In confidential computing (AMD SEV-SNP, Intel TDX, NVIDIA CC), remote
attestation allows an AI developer to verify that:
1. The hardware is authentic AMD/Intel/NVIDIA secure enclave silicon.
2. The hypervisor and host OS (owned by the provider) cannot read workload memory.
3. The workload sandbox runtime matches verified measurement hashes.
4. The report was freshly generated (nonce-bound, preventing replay attacks).

In dev/test environments without physical SEV-SNP/TDX silicon, cryptographic
measurements are derived deterministically from the machine identity and host
software stack, validating the full end-to-end attestation lifecycle.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class AttestationReport:
    """A cryptographic hardware attestation quote."""

    machine_id: str
    isolation_tier: str
    nonce: str
    timestamp: str
    pcr_measurements: dict[str, str] = field(default_factory=dict)
    platform_quote: str = ""
    signature: str = ""

    def canonical_bytes(self) -> bytes:
        """Deterministic bytes representation for signing and verification."""
        measurements_json = json.dumps(self.pcr_measurements, sort_keys=True)
        canonical_str = (
            f"{self.machine_id}\n"
            f"{self.isolation_tier}\n"
            f"{self.nonce}\n"
            f"{self.timestamp}\n"
            f"{measurements_json}\n"
            f"{self.platform_quote}"
        )
        return canonical_str.encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttestationReport":
        return cls(
            machine_id=str(data["machine_id"]),
            isolation_tier=str(data.get("isolation_tier", "confidential")),
            nonce=str(data["nonce"]),
            timestamp=str(data["timestamp"]),
            pcr_measurements=dict(data.get("pcr_measurements", {})),
            platform_quote=str(data.get("platform_quote", "")),
            signature=str(data.get("signature", "")),
        )


def _compute_default_measurements(machine_id: str, tier: str) -> dict[str, str]:
    """Generate baseline PCR measurements for the platform and runtime sandbox."""
    base_seed = f"computecoin:{machine_id}:{tier}".encode("utf-8")
    h0 = hashlib.sha256(base_seed + b":bios:0.1.0").hexdigest()
    h1 = hashlib.sha256(base_seed + b":bootloader:systemd-boot").hexdigest()
    h2 = hashlib.sha256(base_seed + b":kernel:6.8.0-cc").hexdigest()
    h7 = hashlib.sha256(base_seed + b":secureboot:enabled").hexdigest()
    h8 = hashlib.sha256(base_seed + b":sandbox_runtime:runsc-kata").hexdigest()
    return {
        "pcr0": h0,
        "pcr1": h1,
        "pcr2": h2,
        "pcr7": h7,
        "pcr8": h8,
    }


def generate_attestation_report(
    *,
    machine_id: str,
    signing_key: str,
    nonce: str,
    isolation_tier: str = "confidential",
    custom_measurements: Optional[dict[str, str]] = None,
    timestamp: Optional[str] = None,
) -> AttestationReport:
    """Generate a signed attestation report bound to a challenge nonce."""
    if not nonce:
        raise ValueError("nonce cannot be empty (required for replay prevention)")
    if not signing_key:
        raise ValueError("signing_key cannot be empty")

    ts = timestamp or dt.datetime.now(dt.timezone.utc).isoformat()
    measurements = custom_measurements or _compute_default_measurements(machine_id, isolation_tier)

    # Derive the platform quote from the measurements + nonce
    quote_hasher = hashlib.sha384()
    quote_hasher.update(nonce.encode("utf-8"))
    for k in sorted(measurements.keys()):
        quote_hasher.update(f"{k}:{measurements[k]}".encode("utf-8"))
    platform_quote = quote_hasher.hexdigest()

    report = AttestationReport(
        machine_id=machine_id,
        isolation_tier=isolation_tier,
        nonce=nonce,
        timestamp=ts,
        pcr_measurements=measurements,
        platform_quote=platform_quote,
    )

    # Cryptographically sign canonical bytes using the machine's hardware signing key
    sig = hmac.new(
        signing_key.encode("utf-8"),
        report.canonical_bytes(),
        hashlib.sha256,
    ).hexdigest()
    report.signature = sig
    return report


def verify_attestation_report(
    report: AttestationReport,
    *,
    signing_key: str,
    expected_nonce: str,
    max_age_seconds: int = 300,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
) -> tuple[bool, Optional[str]]:
    """Cryptographically verify an attestation report.

    Validates:
    1. Freshness / Replay prevention (nonce matches expected challenge).
    2. Timestamp within allowed age window.
    3. Platform quote matches computed hash over PCR measurements + nonce.
    4. Signature validity using the machine's authentic signing key.

    Returns (is_valid, failure_reason_if_any).
    """
    # 1. Nonce match (replay protection)
    if not hmac.compare_digest(report.nonce, expected_nonce):
        return False, "nonce_mismatch"

    # 2. Timestamp freshness
    try:
        ts_clean = report.timestamp.replace("Z", "+00:00")
        report_dt = dt.datetime.fromisoformat(ts_clean)
        if report_dt.tzinfo is None:
            report_dt = report_dt.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return False, "invalid_timestamp_format"

    now = (now_fn() if now_fn else dt.datetime.now(dt.timezone.utc))
    if abs((now - report_dt).total_seconds()) > max_age_seconds:
        return False, "attestation_expired"

    # 3. Platform quote integrity
    quote_hasher = hashlib.sha384()
    quote_hasher.update(report.nonce.encode("utf-8"))
    for k in sorted(report.pcr_measurements.keys()):
        quote_hasher.update(f"{k}:{report.pcr_measurements[k]}".encode("utf-8"))
    computed_quote = quote_hasher.hexdigest()

    if not hmac.compare_digest(report.platform_quote, computed_quote):
        return False, "platform_quote_tampered"

    # 4. Signature verification
    expected_sig = hmac.new(
        signing_key.encode("utf-8"),
        report.canonical_bytes(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(report.signature, expected_sig):
        return False, "signature_verification_failed"

    return True, None
