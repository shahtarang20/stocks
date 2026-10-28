"""TPM 2.0 quote verifier (WP-F / F2) on synthetic evidence built in the real TPM wire formats.

Each negative test pins one way the earlier verifier could be fooled: it read the nonce and PCR
digest from an UNSIGNED JSON field next to the signed bytes (so an old genuine quote could be
replayed with a fresh nonce), skipped the EK chain when no roots were configured, accepted PCRs
the quote never covered, and "bound" the AK by an EK signature a real (decrypt-only) EK never makes.
Real-machine evidence is still required (checklist F5).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from app.services.hardware_attestation import HardwareAttestationError
from app.services.hardware_attestation.tpm2 import (
    FIXED_PARENT, FIXED_TPM, RESTRICTED, SENSITIVE_DATA_ORIGIN, SIGN, TPM2Verifier, tpm_name,
)

NONCE = "challenge-nonce-0123456789"
NOW = dt.datetime.now(dt.timezone.utc)


def _cert(subject, key, issuer=None, issuer_key=None, ca=False, days=(-1, 365)):
    issuer = issuer or subject
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + dt.timedelta(days=days[0]))
        .not_valid_after(NOW + dt.timedelta(days=days[1]))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key or key, hashes.SHA256())
    )


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _tpm2b(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


def _ak_public(key: ec.EllipticCurvePrivateKey, attrs=FIXED_TPM | FIXED_PARENT | SENSITIVE_DATA_ORIGIN | (1 << 6) | RESTRICTED | SIGN) -> bytes:
    n = key.public_key().public_numbers()
    return (
        struct.pack(">HHI", 0x0023, 0x000B, attrs) + _tpm2b(b"")
        + struct.pack(">H", 0x0010)              # symmetric: NULL
        + struct.pack(">HH", 0x0018, 0x000B)     # scheme: ECDSA / SHA-256
        + struct.pack(">HH", 0x0003, 0x0010)     # curve P-256, kdf NULL
        + _tpm2b(n.x.to_bytes(32, "big")) + _tpm2b(n.y.to_bytes(32, "big"))
    )


def _attest(extra: bytes, pcrs: dict[int, bytes], selected=(0, 4, 7), magic=0xFF544347, digest=None) -> bytes:
    bitmap = bytearray(3)
    for i in selected:
        bitmap[i // 8] |= 1 << (i % 8)
    pcr_digest = digest if digest is not None else hashlib.sha256(b"".join(pcrs[i] for i in sorted(selected))).digest()
    return (
        struct.pack(">IH", magic, 0x8018) + _tpm2b(b"\x00\x0b" + b"q" * 32) + _tpm2b(extra)
        + struct.pack(">QIIB", 1000, 1, 0, 1) + struct.pack(">Q", 0x2E)
        + struct.pack(">I", 1) + struct.pack(">HB", 0x000B, 3) + bytes(bitmap)
        + _tpm2b(pcr_digest)
    )


class Machine:
    """A TPM with a vendor-issued EK (root -> intermediate -> EK) and an enrolled AK."""

    def __init__(self):
        root_key, inter_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        self.root = _cert("TPM Vendor Root", root_key, ca=True)
        self.inter = _cert("TPM Vendor EK CA", inter_key, "TPM Vendor Root", root_key, ca=True)
        self.ek_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ek = _cert("EK", self.ek_key, "TPM Vendor EK CA", inter_key)
        self.ak = ec.generate_private_key(ec.SECP256R1())
        self.ak_public = _ak_public(self.ak)
        self.pcrs = {i: hashlib.sha256(f"pcr{i}".encode()).digest() for i in range(24)}

    def evidence(self, nonce=NONCE, attest=None, ak=None, ak_public=None, pcrs=None):
        attest = attest if attest is not None else _attest(hashlib.sha256(nonce.encode()).digest(), self.pcrs)
        sig = (ak or self.ak).sign(attest, ec.ECDSA(hashes.SHA256()))
        return {
            "quote": {"attest": attest.hex(), "signature": sig.hex(), "ak_public": (ak_public or self.ak_public).hex()},
            "pcrs": {str(i): v.hex() for i, v in (pcrs or self.pcrs).items()},
            "ek_cert": _pem(self.ek), "ek_intermediates": [_pem(self.inter)],
        }

    def verify(self, evidence, nonce=NONCE, expected=None, roots="default", bound="default"):
        roots = [self.root] if roots == "default" else roots
        bound = tpm_name(self.ak_public).hex() if bound == "default" else bound
        return TPM2Verifier().verify(evidence, nonce, expected, roots, {"bound_ak_name": bound})


@pytest.fixture
def m():
    return Machine()


def _code(fn):
    with pytest.raises(HardwareAttestationError) as exc:
        fn()
    return exc.value.code


def test_golden_path_returns_only_signed_pcrs(m):
    res = m.verify(m.evidence(), expected={"pcr0": m.pcrs[0].hex(), "pcr7": m.pcrs[7].hex()})
    assert res.verified is True and res.flavor == "tpm2"
    assert set(res.measurements) == {"pcr0", "pcr4", "pcr7"}  # only the quoted selection
    assert res.public_key == tpm_name(m.ak_public).hex()


def test_replayed_quote_with_a_fresh_nonce_is_refused(m):
    """The old bypass: a genuine quote for an OLD nonce presented for a NEW challenge."""
    old = m.evidence(nonce="an-old-challenge")
    assert _code(lambda: m.verify(old, nonce=NONCE)) == "tpm2_wrong_nonce"


def test_editing_the_signed_bytes_breaks_the_signature(m):
    ev = m.evidence(nonce="an-old-challenge")
    attest = bytes.fromhex(ev["quote"]["attest"])
    fresh = hashlib.sha256(NONCE.encode()).digest()
    old = hashlib.sha256(b"an-old-challenge").digest()
    ev["quote"]["attest"] = attest.replace(old, fresh).hex()
    assert _code(lambda: m.verify(ev)) == "tpm2_invalid_signature"


@pytest.mark.parametrize("roots", [[], None])
def test_no_pinned_roots_means_nothing_verifies(m, roots):
    assert _code(lambda: m.verify(m.evidence(), roots=roots or [])) == "tpm2_bad_certificate_chain"


def test_untrusted_root_is_refused(m):
    other = _cert("Other Root", ec.generate_private_key(ec.SECP256R1()), ca=True)
    assert _code(lambda: m.verify(m.evidence(), roots=[other])) == "tpm2_bad_certificate_chain"


def test_missing_intermediate_or_expired_ek_is_refused(m):
    ev = m.evidence()
    ev["ek_intermediates"] = []
    assert _code(lambda: m.verify(ev)) == "tpm2_bad_certificate_chain"
    inter_key = ec.generate_private_key(ec.SECP256R1())
    root_key = ec.generate_private_key(ec.SECP256R1())
    m.root = _cert("TPM Vendor Root", root_key, ca=True)
    m.inter = _cert("TPM Vendor EK CA", inter_key, "TPM Vendor Root", root_key, ca=True)
    m.ek = _cert("EK", m.ek_key, "TPM Vendor EK CA", inter_key, days=(-30, -1))
    assert _code(lambda: m.verify(m.evidence())) == "tpm2_bad_certificate_chain"


def test_an_intermediate_that_is_not_a_ca_cannot_issue(m):
    root_key, inter_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    m.root = _cert("TPM Vendor Root", root_key, ca=True)
    m.inter = _cert("TPM Vendor EK CA", inter_key, "TPM Vendor Root", root_key, ca=False)
    m.ek = _cert("EK", m.ek_key, "TPM Vendor EK CA", inter_key)
    assert _code(lambda: m.verify(m.evidence())) == "tpm2_bad_certificate_chain"


def test_ak_must_be_enrolled_and_must_be_the_enrolled_one(m):
    assert _code(lambda: m.verify(m.evidence(), bound=None)) == "tpm2_ak_not_enrolled"
    stranger = ec.generate_private_key(ec.SECP256R1())
    ev = m.evidence(ak=stranger, ak_public=_ak_public(stranger))
    assert _code(lambda: m.verify(ev)) == "tpm2_unbound_ak"


def test_ak_must_be_a_restricted_signing_key(m):
    loose = _ak_public(m.ak, attrs=FIXED_TPM | FIXED_PARENT | SENSITIVE_DATA_ORIGIN | SIGN)  # not restricted
    ev = m.evidence(ak_public=loose)
    assert _code(lambda: m.verify(ev, bound=tpm_name(loose).hex())) == "tpm2_bad_ak"


def test_reported_pcrs_must_match_the_signed_digest(m):
    ev = m.evidence()
    ev["pcrs"]["4"] = "00" * 32
    assert _code(lambda: m.verify(ev)) == "tpm2_pcr_mismatch"


def test_an_expected_pcr_outside_the_quote_is_refused(m):
    """PCR 11 is reported (unsigned) with the 'right' value but the quote never covered it."""
    ev = m.evidence()
    assert _code(lambda: m.verify(ev, expected={"pcr11": m.pcrs[11].hex()})) == "tpm2_pcr_not_quoted"


def test_expected_value_mismatch_is_refused(m):
    assert _code(lambda: m.verify(m.evidence(), expected={"pcr7": "11" * 32})) == "tpm2_pcr_mismatch"


@pytest.mark.parametrize("attest", [
    lambda m: _attest(hashlib.sha256(NONCE.encode()).digest(), m.pcrs, magic=0x12345678),
    lambda m: _attest(hashlib.sha256(NONCE.encode()).digest(), m.pcrs)[:-5],
    lambda m: _attest(hashlib.sha256(NONCE.encode()).digest(), m.pcrs) + b"\x00",
])
def test_malformed_signed_structures_are_refused(m, attest):
    assert _code(lambda: m.verify(m.evidence(attest=attest(m)))) == "tpm2_malformed_quote"
