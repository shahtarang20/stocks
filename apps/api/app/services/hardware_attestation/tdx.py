"""Intel TDX quote verifier (WP-F / F3), DCAP quote format v4.

Built from Intel's published formats (Intel TDX DCAP Quoting Library API, quote v4; Intel PCS TCB
Info v3 and Enclave Identity v2) and tested on synthetic quotes only. Recorded quotes from real TDX
hardware (checklist F5) are required before this may back a verdict.

Evidence (hex / PEM / raw JSON text as Intel serves it)::

    {"quote": <hex quote v4>,
     "collateral": {"tcb_info": "<raw JSON {tcbInfo, signature}>",
                    "qe_identity": "<raw JSON {enclaveIdentity, signature}>",
                    "tcb_signing_chain": [<PEM TCB Signing cert>, ...]}}

Collateral is signed by Intel, so the host may fetch and send it. Checks:
1. PCK certificate chain (in the quote, certification data type 5) to a PINNED Intel SGX root;
2. QE report signed by the PCK key; QE REPORTDATA[0:32] == SHA-256(attestation key || QE auth data)
   (this binds the attestation key to a genuine Quoting Enclave);
3. the attestation key's signature over header || TD report;
4. QE Identity collateral: Intel-signed, current, MRSIGNER / ISVPRODID match, QE ISVSVN is UpToDate;
5. TCB Info collateral: Intel-signed, current, FMSPC equals the PCK's, and the first TCB level
   matched by the PCK's SGX TCB and the quote's TEE_TCB_SVN has status UpToDate;
6. from the signed TD report: debug off, REPORTDATA == SHA-512(nonce), MRTD and RTMR0-3.
Not implemented: CRL / OCSP revocation of the PCK chain, and the TDX-module identity check.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import struct
from typing import Any

from cryptography import x509
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

HEADER_SIZE, BODY_SIZE, QE_REPORT_SIZE = 48, 584, 384
TEE_TYPE_TDX = 0x81
ATT_KEY_ECDSA_P256 = 2
CERT_TYPE_QE_REPORT = 6
CERT_TYPE_PCK_CHAIN = 5
SGX_EXT_OID = "1.2.840.113741.1.13.1"
TD_ATTRIBUTES_DEBUG = 1  # TDATTRIBUTES bit 0

# Offsets in the 584-byte TD report body (quote v4).
_BODY = {"tee_tcb_svn": (0, 16), "mrseam": (16, 48), "td_attributes": (120, 8), "mrtd": (136, 48),
         "rtmr0": (328, 48), "rtmr1": (376, 48), "rtmr2": (424, 48), "rtmr3": (472, 48), "report_data": (520, 64)}


def _err(code: str, detail: str) -> HardwareAttestationError:
    return HardwareAttestationError(f"tdx_{code}", detail)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise _err("malformed_quote", "truncated quote")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]


def _ecdsa_ok(key: Any, sig: bytes, data: bytes) -> bool:
    """DCAP signatures are r || s, 32 bytes each, big-endian."""
    if len(sig) != 64:
        return False
    try:
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _p256_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + raw)
    except ValueError as exc:
        raise _err("malformed_quote", "attestation key is not a P-256 point") from exc


# --------------------------------------------------------------------------- minimal DER (PCK extension)

def _der_items(data: bytes) -> list[tuple[int, bytes]]:
    items, pos = [], 0
    while pos < len(data):
        if pos + 2 > len(data):
            raise ValueError("truncated DER")
        tag, length = data[pos], data[pos + 1]
        pos += 2
        if length & 0x80:
            n = length & 0x7F
            length = int.from_bytes(data[pos:pos + n], "big")
            pos += n
        if pos + length > len(data):
            raise ValueError("truncated DER")
        items.append((tag, data[pos:pos + length]))
        pos += length
    return items


def _der_oid(body: bytes) -> str:
    parts, value = [body[0] // 40, body[0] % 40], 0
    for b in body[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def pck_sgx_extension(pck: x509.Certificate) -> dict[str, Any]:
    """FMSPC, PCESVN and the 16 SGX TCB component SVNs from the PCK certificate's SGX extension."""
    try:
        ext = pck.extensions.get_extension_for_oid(x509.ObjectIdentifier(SGX_EXT_OID)).value
        (_, top), = _der_items(ext.value)
        out: dict[str, Any] = {"comps": [None] * 16}
        for _, entry in _der_items(top):
            (_, oid_b), (_, value) = _der_items(entry)
            oid = _der_oid(oid_b)
            if oid == SGX_EXT_OID + ".4":
                out["fmspc"] = value.hex()
            elif oid == SGX_EXT_OID + ".2":
                for _, tcb_entry in _der_items(value):
                    (_, toid_b), (_, tval) = _der_items(tcb_entry)
                    n = int(_der_oid(toid_b).rsplit(".", 1)[1])
                    if 1 <= n <= 16:
                        out["comps"][n - 1] = int.from_bytes(tval, "big")
                    elif n == 17:
                        out["pcesvn"] = int.from_bytes(tval, "big")
        if "fmspc" not in out or "pcesvn" not in out or None in out["comps"]:
            raise ValueError("incomplete SGX extension")
        return out
    except (x509.ExtensionNotFound, ValueError, IndexError) as exc:
        raise _err("bad_certificate_chain", "PCK certificate has no valid SGX extension") from exc


# --------------------------------------------------------------------------- Intel-signed collateral

def _raw_member(text: str, member: str) -> str:
    """The exact text of a top-level JSON object member: Intel signs those bytes, not a re-serialisation."""
    key = text.find(f'"{member}"')
    start = text.find("{", key) if key >= 0 else -1
    if start < 0:
        raise ValueError(f"no {member}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError(f"unterminated {member}")


def verify_signed_collateral(raw: str, member: str, signing_cert: x509.Certificate, now: dt.datetime) -> dict[str, Any]:
    try:
        body_text = _raw_member(raw, member)
        signature = bytes.fromhex(json.loads(raw)["signature"])
        body = json.loads(body_text)
    except (ValueError, KeyError, TypeError) as exc:
        raise _err("bad_collateral", f"{member} collateral is malformed") from exc
    if not _ecdsa_ok(signing_cert.public_key(), signature, body_text.encode("utf-8")):
        raise _err("bad_collateral", f"{member} collateral is not signed by the Intel TCB signing key")
    try:
        next_update = dt.datetime.fromisoformat(str(body["nextUpdate"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise _err("bad_collateral", f"{member} has no nextUpdate") from exc
    if now > next_update:
        raise _err("stale_collateral", f"{member} collateral expired at {next_update.isoformat()}")
    return body


def _svns(level: dict[str, Any], key: str) -> list[int]:
    return [int(c["svn"]) for c in level["tcb"][key]]


def tcb_status(tcb_info: dict[str, Any], pck: dict[str, Any], tee_tcb_svn: bytes) -> str:
    """Status of the first TCB level that the PCK's SGX TCB and the quote's TEE_TCB_SVN both meet."""
    for level in tcb_info.get("tcbLevels", []):
        try:
            sgx, tdx, pcesvn = _svns(level, "sgxtcbcomponents"), _svns(level, "tdxtcbcomponents"), int(level["tcb"]["pcesvn"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(sgx) != 16 or len(tdx) != 16:
            continue
        if (all(p >= s for p, s in zip(pck["comps"], sgx)) and pck["pcesvn"] >= pcesvn
                and all(t >= s for t, s in zip(tee_tcb_svn, tdx))):
            return str(level.get("tcbStatus"))
    return "NoMatchingLevel"


def qe_identity_status(identity: dict[str, Any], isvsvn: int) -> str:
    for level in identity.get("tcbLevels", []):
        try:
            if isvsvn >= int(level["tcb"]["isvsvn"]):
                return str(level.get("tcbStatus"))
        except (KeyError, TypeError, ValueError):
            continue
    return "NoMatchingLevel"


# --------------------------------------------------------------------------- verifier

class TDXVerifier:
    def verify(
        self,
        evidence: dict[str, Any],
        nonce: str,
        expected_measurements: dict[str, str] | None,
        root_certs: list,
        context: dict[str, Any] | None = None,
    ) -> HardwareAttestationResult:
        now = (context or {}).get("now") or dt.datetime.now(dt.timezone.utc)
        if not isinstance(evidence, dict):
            raise _err("malformed_evidence", "evidence must be a dictionary")
        try:
            quote = bytes.fromhex(str(evidence.get("quote", "")))
        except ValueError as exc:
            raise _err("malformed_quote", "quote is not hex") from exc

        r = _Reader(quote)
        version, att_key_type, tee_type = r.u16(), r.u16(), r.u32()
        if version != 4 or att_key_type != ATT_KEY_ECDSA_P256 or tee_type != TEE_TYPE_TDX:
            raise _err("malformed_quote", "not a v4 ECDSA-P256 TDX quote")
        r.take(HEADER_SIZE - 8)
        body = r.take(BODY_SIZE)
        signed = quote[:HEADER_SIZE + BODY_SIZE]
        sig_data = _Reader(r.take(r.u32()))
        quote_sig, att_key_raw = sig_data.take(64), sig_data.take(64)
        if sig_data.u16() != CERT_TYPE_QE_REPORT:
            raise _err("malformed_quote", "expected QE report certification data")
        qe = _Reader(sig_data.take(sig_data.u32()))
        qe_report, qe_report_sig = qe.take(QE_REPORT_SIZE), qe.take(64)
        qe_auth = qe.take(qe.u16())
        if qe.u16() != CERT_TYPE_PCK_CHAIN:
            raise _err("malformed_quote", "expected the PCK certificate chain")
        chain_pem = qe.take(qe.u32()).decode("ascii", "replace")

        # 1. PCK chain to a pinned Intel root.
        pems = [p + "-----END CERTIFICATE-----\n" for p in chain_pem.split("-----END CERTIFICATE-----") if "BEGIN" in p]
        if not pems:
            raise _err("bad_certificate_chain", "no PCK certificate in the quote")
        certs = [load_pem_certificate(p, "PCK chain") for p in pems]
        verify_chain(certs[0], certs[1:], root_certs, code="tdx_bad_certificate_chain", now=now)
        pck = certs[0]
        pck_ext = pck_sgx_extension(pck)

        # 2. QE report by the PCK key, binding the attestation key.
        if not _ecdsa_ok(pck.public_key(), qe_report_sig, qe_report):
            raise _err("invalid_signature", "QE report is not signed by the PCK key")
        if qe_report[320:352] != hashlib.sha256(att_key_raw + qe_auth).digest() or any(qe_report[352:384]):
            raise _err("invalid_signature", "attestation key is not bound by the QE report")

        # 3. Attestation key over header || TD report.
        if not _ecdsa_ok(_p256_key(att_key_raw), quote_sig, signed):
            raise _err("invalid_signature", "quote signature does not verify")

        # 4-5. Intel-signed collateral.
        coll = evidence.get("collateral") or {}
        tcb_chain = [load_pem_certificate(p, "TCB signing chain") for p in coll.get("tcb_signing_chain") or []]
        if not tcb_chain:
            raise _err("bad_collateral", "no TCB signing certificate")
        verify_chain(tcb_chain[0], tcb_chain[1:], root_certs, code="tdx_bad_collateral", now=now)
        identity = verify_signed_collateral(str(coll.get("qe_identity", "")), "enclaveIdentity", tcb_chain[0], now)
        mrsigner = qe_report[128:160].hex()
        isvprodid, isvsvn = struct.unpack_from("<HH", qe_report, 256)
        if str(identity.get("mrsigner", "")).lower() != mrsigner or int(identity.get("isvprodid", -1)) != isvprodid:
            raise _err("bad_quoting_enclave", "the QE is not Intel's TDX Quoting Enclave")
        status = qe_identity_status(identity, isvsvn)
        if status != "UpToDate":
            raise _err("tcb_not_up_to_date", f"Quoting Enclave TCB status is {status}")
        tcb_info = verify_signed_collateral(str(coll.get("tcb_info", "")), "tcbInfo", tcb_chain[0], now)
        if str(tcb_info.get("id", "")) != "TDX" or str(tcb_info.get("fmspc", "")).lower() != pck_ext["fmspc"]:
            raise _err("bad_collateral", "TCB Info is not the TDX TCB Info for this platform (FMSPC)")
        status = tcb_status(tcb_info, pck_ext, body[0:16])
        if status != "UpToDate":
            raise _err("tcb_not_up_to_date", f"platform TCB status is {status}")

        # 6. Claims from the signed TD report.
        f = {k: body[o:o + n] for k, (o, n) in _BODY.items()}
        if struct.unpack("<Q", f["td_attributes"])[0] & TD_ATTRIBUTES_DEBUG:
            raise _err("debug_mode_forbidden", "TD has debug enabled")
        if f["report_data"] != hashlib.sha512(nonce.encode("utf-8")).digest():
            raise _err("wrong_nonce", "REPORTDATA is not bound to this challenge")
        measurements = {k: f[k].hex() for k in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3")}
        for k, v in (expected_measurements or {}).items():
            if measurements.get(k.lower()) != str(v).lower():
                raise _err("measurement_mismatch", f"{k} differs from the expected value")

        return HardwareAttestationResult(
            flavor="tdx",
            verified=True,
            measurements=measurements,
            public_key=hashlib.sha256(att_key_raw).hexdigest(),
            security_features={"tee_type": "Intel TDX", "mrseam": f["mrseam"].hex(), "fmspc": pck_ext["fmspc"],
                               "tcb_status": "UpToDate", "qe_isvsvn": isvsvn},
        )
