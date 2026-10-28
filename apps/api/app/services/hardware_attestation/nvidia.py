"""NVIDIA Confidential Computing GPU verifier (WP-F / F3): SPDM 1.1 GET_MEASUREMENTS evidence.

Built from the DMTF SPDM 1.1 message formats used by NVIDIA Hopper GPUs (as consumed by NVIDIA's
nvtrust verifier) and tested on synthetic evidence only. Recorded evidence from a real
confidential-computing GPU (checklist F5) is required before this may back a verdict.

Evidence (hex / PEM)::

    {"request": <hex GET_MEASUREMENTS request>, "response": <hex MEASUREMENTS response incl. signature>,
     "certificate_chain": [<PEM device attestation cert>, <PEM intermediates>...]}

Checks:
1. the device certificate chain reaches a PINNED NVIDIA device root;
2. the request is GET_MEASUREMENTS with a signature requested, and its 32-byte nonce is
   SHA-256(challenge nonce);
3. the response is MEASUREMENTS; its ECDSA P-384 signature (r || s, 96 bytes, big-endian) covers
   request || response-without-signature (SPDM 1.1 L1/L2), made by the device certificate's key;
4. every expected measurement (``{"<index>": hex}``, the golden values from NVIDIA's Reference
   Integrity Manifests for this driver / VBIOS) equals the DMTF measurement block with that index.
Not implemented: parsing and verifying NVIDIA's signed RIM files themselves (the golden values come
from configuration), OCSP for the device chain, and SPDM 1.2+ signing contexts.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.services.hardware_attestation import (
    HardwareAttestationError,
    HardwareAttestationResult,
    load_pem_certificate,
    verify_chain,
)

SPDM_VERSION_11 = 0x11
GET_MEASUREMENTS, MEASUREMENTS = 0xE0, 0x60
SIG_REQUESTED = 0x01
REQUEST_SIZE = 37  # version, code, param1, param2, nonce[32], slot id
SIG_SIZE = 96      # ECDSA P-384


def _err(code: str, detail: str) -> HardwareAttestationError:
    return HardwareAttestationError(f"nvidia_{code}", detail)


def parse_measurement_blocks(record: bytes) -> dict[int, bytes]:
    """DMTF measurement blocks: index, spec, size(u16 LE), then value type, value size(u16 LE), value."""
    blocks, pos = {}, 0
    while pos < len(record):
        if pos + 4 > len(record):
            raise _err("malformed_evidence", "truncated measurement block")
        index, spec, size = record[pos], record[pos + 1], struct.unpack_from("<H", record, pos + 2)[0]
        body = record[pos + 4:pos + 4 + size]
        if len(body) != size or size < 3 or not spec & 0x01:
            raise _err("malformed_evidence", "measurement block is not a DMTF block")
        value_size = struct.unpack_from("<H", body, 1)[0]
        if 3 + value_size != size or index in blocks:
            raise _err("malformed_evidence", "measurement block sizes do not add up")
        blocks[index] = body[3:3 + value_size]
        pos += 4 + size
    return blocks


class NvidiaCCVerifier:
    def verify(
        self,
        evidence: dict[str, Any],
        nonce: str,
        expected_measurements: dict[str, str] | None,
        root_certs: list,
        context: dict[str, Any] | None = None,
    ) -> HardwareAttestationResult:
        if not isinstance(evidence, dict):
            raise _err("malformed_evidence", "evidence must be a dictionary")
        try:
            request = bytes.fromhex(str(evidence.get("request", "")))
            response = bytes.fromhex(str(evidence.get("response", "")))
        except ValueError as exc:
            raise _err("malformed_evidence", "request / response are not hex") from exc

        # 1. Device chain to a pinned NVIDIA root.
        chain = [load_pem_certificate(p, "NVIDIA device chain") for p in evidence.get("certificate_chain") or []]
        if not chain:
            raise _err("bad_certificate_chain", "no device certificate")
        verify_chain(chain[0], chain[1:], root_certs, code="nvidia_bad_certificate_chain", now=(context or {}).get("now"))
        device_key = chain[0].public_key()
        if not isinstance(device_key, ec.EllipticCurvePublicKey) or not isinstance(device_key.curve, ec.SECP384R1):
            raise _err("bad_key_type", "device attestation key must be ECDSA P-384")

        # 2. The request: GET_MEASUREMENTS, signed, nonce bound to this challenge.
        if len(request) != REQUEST_SIZE or request[0] != SPDM_VERSION_11 or request[1] != GET_MEASUREMENTS:
            raise _err("malformed_evidence", "request is not an SPDM 1.1 GET_MEASUREMENTS")
        if not request[2] & SIG_REQUESTED:
            raise _err("malformed_evidence", "measurements were not requested signed")
        if request[4:36] != hashlib.sha256(nonce.encode("utf-8")).digest():
            raise _err("wrong_nonce", "the measurement request is not bound to this challenge")

        # 3. The response and its signature over request || response-without-signature.
        if len(response) < 8 + 32 + 2 + SIG_SIZE or response[0] != SPDM_VERSION_11 or response[1] != MEASUREMENTS:
            raise _err("malformed_evidence", "response is not an SPDM 1.1 MEASUREMENTS")
        record_len = int.from_bytes(response[5:8], "little")
        record = response[8:8 + record_len]
        pos = 8 + record_len + 32  # responder nonce
        if len(record) != record_len or pos + 2 > len(response):
            raise _err("malformed_evidence", "truncated MEASUREMENTS response")
        opaque_len = struct.unpack_from("<H", response, pos)[0]
        sig_start = pos + 2 + opaque_len
        if len(response) != sig_start + SIG_SIZE:
            raise _err("malformed_evidence", "MEASUREMENTS response has the wrong length")
        sig = response[sig_start:]
        try:
            der = encode_dss_signature(int.from_bytes(sig[:48], "big"), int.from_bytes(sig[48:], "big"))
            device_key.verify(der, request + response[:sig_start], ec.ECDSA(hashes.SHA384()))
        except (InvalidSignature, ValueError) as exc:
            raise _err("invalid_signature", "measurements are not signed by the device key") from exc

        # 4. Golden measurements (from NVIDIA's RIMs) against the signed blocks.
        blocks = parse_measurement_blocks(record)
        if response[4] != len(blocks):
            raise _err("malformed_evidence", "number of blocks does not match the header")
        measurements = {str(i): v.hex() for i, v in sorted(blocks.items())}
        for k, v in (expected_measurements or {}).items():
            if measurements.get(str(k)) != str(v).lower():
                raise _err("measurement_mismatch", f"measurement {k} differs from the reference value")

        return HardwareAttestationResult(
            flavor="nvidia_cc",
            verified=True,
            measurements=measurements,
            public_key=chain[0].fingerprint(hashes.SHA256()).hex(),
            security_features={"spdm_version": "1.1", "device_subject": chain[0].subject.rfc4514_string()},
        )
