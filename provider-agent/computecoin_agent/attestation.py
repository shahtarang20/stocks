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
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import httpx

from .heartbeat import canonical_bytes, sign_contract, signing_key_from_secret
from .isolation import measure_security_state
from .privacy_crypto import generate_ed25519_keypair, generate_x25519_keypair


def generate_ephemeral_keypair() -> tuple[str, str]:
    """In-RAM X25519 key pair (private_hex, public_hex) for receiving Data Key shares (ADR-0005).

    Only the PUBLIC half leaves the machine (in the attestation request). Guardians seal shares to
    it, and nothing but this private key can open them. Never write the private key to disk.
    """
    return generate_x25519_keypair()


def generate_receipt_signing_keypair() -> tuple[str, str]:
    """In-RAM Ed25519 key pair (private_hex, public_hex) for signing this session's wipe receipts.

    The public half is bound into the signed attestation verdict, so a customer can verify the
    wipe receipt offline without any secret.
    """
    return generate_ed25519_keypair()


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
    ephemeral_public_key: str = ""

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


class AttestationClientError(RuntimeError):
    """Raised when remote attestation fails."""


@dataclass
class AttestationSession:
    """An in-RAM attestation session holding ephemeral keys and the signed platform verdict.

    Locked Decision L10: One job, one set of keys. Host keys and verdicts are bound to a single
    session/job and destroyed when it ends.
    Keys exist ONLY in process memory and are NEVER written to disk (Checkpoint 2.2).
    """

    machine_id: str
    isolation_tier: str
    ephemeral_private_key: str
    ephemeral_public_key: str
    receipt_signing_private_key: str
    receipt_signing_public_key: str
    verdict: dict[str, Any]
    expires_at: dt.datetime
    log_leaf_index: Optional[int] = None

    def is_current(self, margin_seconds: float = 60.0, now: Optional[dt.datetime] = None) -> bool:
        current_time = now or dt.datetime.now(dt.timezone.utc)
        margin = dt.timedelta(seconds=margin_seconds)
        return (current_time + margin) < self.expires_at

    def ttl_remaining_seconds(self, now: Optional[dt.datetime] = None) -> float:
        current_time = now or dt.datetime.now(dt.timezone.utc)
        return max(0.0, (self.expires_at - current_time).total_seconds())

    def is_expired(self, now: Optional[dt.datetime] = None) -> bool:
        return not self.is_current(margin_seconds=0, now=now)

    def should_reattest(self, fraction: float = 2 / 3, now: Optional[dt.datetime] = None) -> bool:
        """True once ``fraction`` of the verdict's lifetime has passed (WP2 checkpoint 2.3)."""
        current_time = now or dt.datetime.now(dt.timezone.utc)
        try:
            issued = dt.datetime.fromisoformat(str(self.verdict["issued_at"]).replace("Z", "+00:00"))
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            return True  # cannot tell how old it is: refresh
        lifetime = (self.expires_at - issued).total_seconds()
        if lifetime <= 0:
            return True
        return (current_time - issued).total_seconds() >= fraction * lifetime

    def wipe(self) -> None:
        """Drop references to the private keys. Python strings are immutable, so this cannot
        overwrite the memory itself; the Sealed OS (RAM-only, process exit) provides that."""
        self.ephemeral_private_key = ""
        self.receipt_signing_private_key = ""


class AttestationManager:
    """Keeps the machine-level attestation current for the job loop: attests on first use,
    re-attests after 2/3 of the verdict's lifetime, and never writes keys to disk.

    This session only makes the machine eligible to claim private work. It never opens a job:
    each private job attests its own fresh keys with the claim's job-bound nonce (R5)."""

    def __init__(self, base_url: str, machine_id: str, machine_secret: str, *, tier: str,
                 transport: Optional[Any] = None, attest_fn: Optional[Callable[..., "AttestationSession"]] = None,
                 on_error: Optional[Callable[[Exception], None]] = None,
                 hardware: Optional[Callable[[str], tuple[str, dict[str, Any]]]] = None):
        self.base_url, self.machine_id, self.machine_secret, self.tier = base_url, machine_id, machine_secret, tier
        self.transport = transport
        self.hardware = hardware
        self._attest = attest_fn or perform_attestation
        self._on_error = on_error
        self.session: Optional[AttestationSession] = None

    def current(self) -> Optional["AttestationSession"]:
        if self.session is None or self.session.should_reattest():
            try:
                fresh = self._attest(self.base_url, self.machine_id, self.machine_secret, tier=self.tier,
                                     transport=self.transport, hardware=self.hardware)
            except Exception as exc:  # keep running open jobs; private claims fail closed without a session
                if self._on_error:
                    self._on_error(exc)
                return self.session if self.session is not None and not self.session.is_expired() else None
            if self.session is not None:
                self.session.wipe()
            self.session = fresh
        return self.session


def perform_attestation(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    tier: str = "sealed",
    custom_measurements: Optional[dict[str, str]] = None,
    security_state: Optional[dict[str, bool]] = None,
    transport: Optional[Any] = None,
    timeout: float = 15.0,
    nonce: Optional[str] = None,
    hardware: Optional[Callable[[str], tuple[str, dict[str, Any]]]] = None,
) -> AttestationSession:
    """Perform a challenge-response attestation with the backend via machine HMAC authentication.

    Generates NEW ephemeral X25519 and Ed25519 keys strictly in RAM, submits evidence, and
    receives a signed Attestation Verdict. Without ``nonce`` it first asks for a machine-level
    challenge (that verdict only makes the machine eligible to claim private work). With the
    ``key_attestation_nonce`` from a private claim, the verdict is bound to that job and is the
    only kind that can fetch the job's keys (one job, one key set).

    ``hardware`` (e.g. ``tpm.hardware_provider(tpm)``) adds signed hardware evidence for the same
    nonce; the backend uses it when hardware attestation is enabled there.
    """
    if not machine_id or not machine_secret:
        raise AttestationClientError("machine is not enrolled; credentials missing")

    url = base_url.rstrip("/")
    signing_key = signing_key_from_secret(machine_secret)

    # 1. Generate keys strictly in RAM
    eph_priv, eph_pub = generate_ephemeral_keypair()
    sign_priv, sign_pub = generate_receipt_signing_keypair()

    with httpx.Client(transport=transport, timeout=timeout) as client:
        if nonce is None:
            nonce = _request_challenge(client, url, machine_id, machine_secret)
        return _submit_evidence(
            client, url, machine_id, machine_secret, signing_key, nonce, tier,
            custom_measurements, security_state, eph_priv, eph_pub, sign_priv, sign_pub,
            hardware=hardware,
        )


def _request_challenge(client: Any, url: str, machine_id: str, machine_secret: str) -> str:
    # 2. Request challenge nonce
    challenge_url = f"{url}/v1/agents/attestation/challenge"
    raw_challenge_body = b"{}"
    ts = int(time.time())
    sig = sign_contract(machine_id, ts, raw_challenge_body, machine_secret)
    headers = {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": str(ts),
        "X-ComputeCoin-Signature": sig,
    }
    res = client.post(challenge_url, content=raw_challenge_body, headers=headers)
    if res.status_code == 401:
        raise AttestationClientError("Machine authentication failed (401)")
    if res.status_code != 200:
        raise AttestationClientError(f"Challenge request failed: HTTP {res.status_code} {res.text}")

    return res.json()["nonce"]


def _submit_evidence(
    client: Any, url: str, machine_id: str, machine_secret: str, signing_key: str, nonce: str, tier: str,
    custom_measurements: Optional[dict[str, str]], security_state: Optional[dict[str, bool]],
    eph_priv: str, eph_pub: str, sign_priv: str, sign_pub: str,
    hardware: Optional[Callable[[str], tuple[str, dict[str, Any]]]] = None,
) -> "AttestationSession":
    # 3. Generate signed attestation report
    report = generate_attestation_report(
        machine_id=machine_id,
        signing_key=signing_key,
        nonce=nonce,
        isolation_tier=tier,
        custom_measurements=custom_measurements,
    )

    # 4. Submit evidence
    verify_url = f"{url}/v1/agents/attestation/verify"
    payload = {
        "nonce": nonce,
        "isolation_tier": tier,
        "timestamp": report.timestamp,
        "pcr_measurements": report.pcr_measurements,
        "platform_quote": report.platform_quote,
        "signature": report.signature,
        "ephemeral_public_key": eph_pub,
        "receipt_signing_public_key": sign_pub,
        # Report what this host actually measures; never claim a lock that was not checked.
        "security_state": security_state if security_state is not None else measure_security_state(),
    }
    if hardware is not None:
        payload["hardware_flavor"], payload["hardware_evidence"] = hardware(nonce)
    raw_verify_body = canonical_bytes(payload)
    ts_verify = int(time.time())
    sig_verify = sign_contract(machine_id, ts_verify, raw_verify_body, machine_secret)
    verify_headers = {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": str(ts_verify),
        "X-ComputeCoin-Signature": sig_verify,
    }

    verify_res = client.post(verify_url, content=raw_verify_body, headers=verify_headers)
    if verify_res.status_code == 401:
        raise AttestationClientError("Attestation verification rejected (401)")
    if verify_res.status_code == 409:
        raise AttestationClientError(f"Attestation evidence rejected: {verify_res.text}")
    if verify_res.status_code != 200:
        raise AttestationClientError(f"Attestation verify failed: HTTP {verify_res.status_code} {verify_res.text}")

    verify_data = verify_res.json()
    verdict = verify_data.get("verdict")
    if not verdict:
        raise AttestationClientError("Backend verified attestation but issued no verdict")

    expires_at_str = verdict["expires_at"].replace("Z", "+00:00")
    expires_at = dt.datetime.fromisoformat(expires_at_str)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)

    log_leaf_idx = (verify_data.get("details") or {}).get("log_leaf_index")

    return AttestationSession(
        machine_id=machine_id,
        isolation_tier=tier,
        ephemeral_private_key=eph_priv,
        ephemeral_public_key=eph_pub,
        receipt_signing_private_key=sign_priv,
        receipt_signing_public_key=sign_pub,
        verdict=verdict,
        expires_at=expires_at,
        log_leaf_index=log_leaf_idx,
    )

