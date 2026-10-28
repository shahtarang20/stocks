"""Hardware Attestation Verifier Modules (WP-F).

Fail-closed verifiers for hardware evidence. Rules every verifier follows:

- every claim used (nonce, measurements, debug flags) is read from the SIGNED bytes, never from
  a separate JSON field next to them;
- the signing key chains, link by link, to a root certificate PINNED in configuration
  (``PRIVACY_HARDWARE_ROOT_CERTS``); no pinned roots means nothing verifies;
- the challenge nonce is bound exactly (no prefix or "one of several encodings" matching).

Status (R9): TPM 2.0 and AMD SEV-SNP parse and verify their real binary formats, but have only
been exercised with synthetic evidence; recorded evidence from real machines (checklist F5) is
still required before PRIVACY_HARDWARE_ATTESTATION may be enabled anywhere. Intel TDX and NVIDIA
CC refuse all evidence until they are built on the vendors' DCAP / SPDM formats.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from cryptography import x509

FLAVORS = ("tpm2", "sev_snp", "tdx", "nvidia_cc")
_ALIASES = {
    "tpm2": "tpm2", "tpm": "tpm2", "tpm_quote": "tpm2",
    "sev_snp": "sev_snp", "sev": "sev_snp", "amd_sev_snp": "sev_snp",
    "tdx": "tdx", "intel_tdx": "tdx",
    "nvidia": "nvidia_cc", "nvidia_cc": "nvidia_cc",
}


class HardwareAttestationError(Exception):
    """Raised when hardware attestation evidence fails verification."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class HardwareAttestationResult:
    """Result of successful hardware evidence verification. ``measurements`` hold only values that
    were covered by the hardware signature."""

    flavor: str
    verified: bool
    measurements: dict[str, str]
    public_key: str
    security_features: dict[str, Any] = field(default_factory=dict)


class HardwareVerifier(Protocol):
    def verify(
        self,
        evidence: dict[str, Any],
        nonce: str,
        expected_measurements: dict[str, str] | None,
        root_certs: list[x509.Certificate],
        context: dict[str, Any] | None = None,
    ) -> HardwareAttestationResult:
        ...


def canonical_flavor(flavor: str) -> str:
    f = _ALIASES.get((flavor or "").lower().strip())
    if f is None:
        raise HardwareAttestationError("unsupported_hardware_flavor", f"flavor {flavor!r} is not supported")
    return f


def get_hardware_verifier(flavor: str) -> HardwareVerifier:
    """The verifier for an attestation flavor."""
    f = canonical_flavor(flavor)
    if f == "tpm2":
        from app.services.hardware_attestation.tpm2 import TPM2Verifier
        return TPM2Verifier()
    if f == "sev_snp":
        from app.services.hardware_attestation.sev_snp import SEVSNPVerifier
        return SEVSNPVerifier()
    if f == "tdx":
        from app.services.hardware_attestation.tdx import TDXVerifier
        return TDXVerifier()
    from app.services.hardware_attestation.nvidia import NvidiaCCVerifier
    return NvidiaCCVerifier()


# --------------------------------------------------------------------------- pinned roots

def load_pem_certificate(pem: Any, what: str) -> x509.Certificate:
    try:
        data = pem.encode("utf-8") if isinstance(pem, str) else bytes(pem)
        return x509.load_pem_x509_certificate(data)
    except Exception as exc:
        raise HardwareAttestationError("bad_certificate", f"{what} is not a PEM X.509 certificate") from exc


def parse_root_certs(raw: str, now: dt.datetime | None = None) -> dict[str, list[x509.Certificate]]:
    """Parse ``PRIVACY_HARDWARE_ROOT_CERTS``: JSON ``{flavor: [PEM, ...]}``.

    Every root must be a currently valid, self-signed CA certificate. Raises ValueError otherwise
    (the boot guard turns that into a refusal to start)."""
    try:
        data = json.loads(raw or "")
    except ValueError as exc:
        raise ValueError("PRIVACY_HARDWARE_ROOT_CERTS must be JSON {flavor: [PEM, ...]}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("PRIVACY_HARDWARE_ROOT_CERTS must be a non-empty JSON object {flavor: [PEM, ...]}")
    ts = now or dt.datetime.now(dt.timezone.utc)
    out: dict[str, list[x509.Certificate]] = {}
    for flavor, pems in data.items():
        try:
            f = canonical_flavor(str(flavor))
        except HardwareAttestationError as exc:
            raise ValueError(f"PRIVACY_HARDWARE_ROOT_CERTS: unknown flavor {flavor!r}") from exc
        if not isinstance(pems, list) or not pems:
            raise ValueError(f"PRIVACY_HARDWARE_ROOT_CERTS[{flavor!r}] must be a non-empty list of PEM certificates")
        certs = []
        for i, pem in enumerate(pems):
            try:
                cert = load_pem_certificate(pem, f"{flavor} root {i}")
            except HardwareAttestationError as exc:
                raise ValueError(f"PRIVACY_HARDWARE_ROOT_CERTS[{flavor!r}][{i}] is not a PEM certificate") from exc
            if not _is_ca(cert) or not _valid_at(cert, ts):
                raise ValueError(f"PRIVACY_HARDWARE_ROOT_CERTS[{flavor!r}][{i}] is not a currently valid CA certificate")
            try:
                cert.verify_directly_issued_by(cert)
            except Exception as exc:
                raise ValueError(f"PRIVACY_HARDWARE_ROOT_CERTS[{flavor!r}][{i}] is not self-signed (pin roots only)") from exc
            certs.append(cert)
        out[f] = certs
    return out


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _valid_at(cert: x509.Certificate, now: dt.datetime) -> bool:
    return cert.not_valid_before_utc <= now <= cert.not_valid_after_utc


def verify_chain(
    leaf: x509.Certificate,
    intermediates: list[x509.Certificate],
    roots: list[x509.Certificate],
    *,
    code: str,
    now: dt.datetime | None = None,
    max_depth: int = 5,
) -> list[x509.Certificate]:
    """Verify ``leaf`` -> intermediates -> one PINNED root. Every link: signature (RSA PKCS#1 /
    RSA-PSS / ECDSA, per the certificate), issuer name, validity at ``now``, and CA flag on every
    issuer. Returns the path. Raises HardwareAttestationError(code) on any failure."""
    if not roots:
        raise HardwareAttestationError(code, "no pinned root certificates are configured for this flavor")
    ts = now or dt.datetime.now(dt.timezone.utc)
    path = [leaf]
    current = leaf
    pool = list(intermediates)
    for _ in range(max_depth + 1):
        if not _valid_at(current, ts):
            raise HardwareAttestationError(code, f"certificate {current.subject.rfc4514_string()} is outside its validity period")
        for root in roots:
            if _issued_by(current, root):
                if not _valid_at(root, ts):
                    raise HardwareAttestationError(code, "pinned root is outside its validity period")
                return path + [root]
        nxt = next((c for c in pool if _issued_by(current, c) and _is_ca(c)), None)
        if nxt is None:
            raise HardwareAttestationError(code, "certificate does not chain to a pinned root")
        pool.remove(nxt)
        path.append(nxt)
        current = nxt
    raise HardwareAttestationError(code, "certificate chain is too long")


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
        return True
    except Exception:
        return False
