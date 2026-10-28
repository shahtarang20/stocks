"""AMD SEV-SNP Attestation Report Verifier (WP-F / F3).

Evidence (hex / PEM):

    {"report": <the 1184-byte ATTESTATION_REPORT>, "vcek_cert": <PEM>, "ask_cert": <PEM>}

Checks, all on the signed report bytes (AMD SEV-SNP ABI, ATTESTATION_REPORT):
1. VCEK -> ASK -> pinned ARK (AMD signs these with RSA-PSS; the chain check follows each cert);
2. signature_algo is ECDSA P-384 with SHA-384, and the VCEK signature (r, s little-endian at 0x2A0)
   covers bytes 0x000..0x29F;
3. REPORT_DATA (0x50, 64 bytes) == SHA-512(nonce);
4. the guest policy (0x08) does not allow debugging (bit 19);
5. MEASUREMENT (0x90, 48 bytes) is returned from the signed bytes; it must equal
   ``expected_measurements["measurement"]`` when given (the caller matches it to a trusted image).
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding

from app.services.hardware_attestation import (
    HardwareAttestationError,
    HardwareAttestationResult,
    load_pem_certificate,
    verify_chain,
)

REPORT_SIZE = 0x4A0
SIGNED_END = 0x2A0
SIG_ALGO_ECDSA_P384_SHA384 = 1
POLICY_DEBUG = 1 << 19


def _field(report: bytes, offset: int, size: int) -> bytes:
    return report[offset:offset + size]


class SEVSNPVerifier:
    """Fail-closed verifier for AMD SEV-SNP attestation reports."""

    def verify(
        self,
        evidence: dict[str, Any],
        nonce: str,
        expected_measurements: dict[str, str] | None,
        root_certs: list,
        context: dict[str, Any] | None = None,
    ) -> HardwareAttestationResult:
        if not isinstance(evidence, dict):
            raise HardwareAttestationError("sev_snp_malformed_evidence", "evidence must be a dictionary")
        try:
            report = bytes.fromhex(str(evidence.get("report", "")))
        except ValueError as exc:
            raise HardwareAttestationError("sev_snp_malformed_report", "report is not hex") from exc
        if len(report) != REPORT_SIZE:
            raise HardwareAttestationError("sev_snp_malformed_report", f"report must be {REPORT_SIZE} bytes")

        # 1. VCEK -> ASK -> pinned ARK.
        vcek = load_pem_certificate(evidence.get("vcek_cert"), "VCEK certificate")
        ask = [load_pem_certificate(evidence["ask_cert"], "ASK certificate")] if evidence.get("ask_cert") else []
        verify_chain(vcek, ask, root_certs, code="sev_snp_bad_certificate_chain")

        # 2. VCEK signature over the report body.
        if struct.unpack_from("<I", report, 0x34)[0] != SIG_ALGO_ECDSA_P384_SHA384:
            raise HardwareAttestationError("sev_snp_malformed_report", "unsupported signature algorithm")
        vcek_key = vcek.public_key()
        if not isinstance(vcek_key, ec.EllipticCurvePublicKey) or not isinstance(vcek_key.curve, ec.SECP384R1):
            raise HardwareAttestationError("sev_snp_bad_key_type", "VCEK must be an ECDSA P-384 key")
        r = int.from_bytes(_field(report, SIGNED_END, 72), "little")
        s = int.from_bytes(_field(report, SIGNED_END + 72, 72), "little")
        try:
            vcek_key.verify(encode_dss_signature(r, s), report[:SIGNED_END], ec.ECDSA(hashes.SHA384()))
        except (InvalidSignature, ValueError) as exc:
            raise HardwareAttestationError("sev_snp_invalid_signature", "report signature does not verify") from exc

        # 3-5. Claims from the signed bytes only.
        if _field(report, 0x50, 64) != hashlib.sha512(nonce.encode("utf-8")).digest():
            raise HardwareAttestationError("sev_snp_wrong_nonce", "REPORT_DATA is not bound to this challenge")
        policy = struct.unpack_from("<Q", report, 0x08)[0]
        if policy & POLICY_DEBUG:
            raise HardwareAttestationError("sev_snp_debug_mode_forbidden", "guest policy allows debugging")
        measurement = _field(report, 0x90, 48).hex()
        expected = (expected_measurements or {}).get("measurement")
        if expected is not None and measurement != str(expected).lower():
            raise HardwareAttestationError("sev_snp_measurement_mismatch", "launch measurement differs from the expected one")

        return HardwareAttestationResult(
            flavor="sev_snp",
            verified=True,
            measurements={"measurement": measurement},
            public_key=hashlib.sha256(vcek.public_bytes(Encoding.DER)).hexdigest(),
            security_features={
                "tee_type": "AMD SEV-SNP",
                "report_version": struct.unpack_from("<I", report, 0x00)[0],
                "guest_svn": struct.unpack_from("<I", report, 0x04)[0],
                "policy": policy,
                "chip_id": _field(report, 0x1A0, 64).hex(),
            },
        )

