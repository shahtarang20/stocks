"""Intel TDX (DCAP quote v4 + Intel collateral) and NVIDIA CC (SPDM 1.1) verifiers (WP-F / F3).

Evidence is built here in the published binary layouts, signed by test keys whose roots are pinned.
This proves the verifiers enforce every check; it does not prove they accept real hardware evidence
(that needs recorded quotes, checklist F5).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from app.services.hardware_attestation import HardwareAttestationError
from app.services.hardware_attestation.nvidia import NvidiaCCVerifier
from app.services.hardware_attestation.tdx import SGX_EXT_OID, TDXVerifier

NONCE = "tee-nonce-2468"
NOW = dt.datetime.now(dt.timezone.utc)


def _cert(subject, key, issuer=None, issuer_key=None, ca=False, extensions=(), hash_alg=None):
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer or subject)]))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    for ext in extensions:
        b = b.add_extension(ext, critical=False)
    return b.sign(issuer_key or key, hash_alg or hashes.SHA256())


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _code(fn):
    with pytest.raises(HardwareAttestationError) as exc:
        fn()
    return exc.value.code


# ----------------------------------------------------------------------------- tiny DER encoder

def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    length = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + length + content


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _der(0x06, out)


def _int(v: int) -> bytes:
    return _der(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big"))


def _sgx_extension(fmspc: bytes, comps: list[int], pcesvn: int) -> x509.UnrecognizedExtension:
    tcb = b"".join(_der(0x30, _oid(f"{SGX_EXT_OID}.2.{i + 1}") + _int(c)) for i, c in enumerate(comps))
    tcb += _der(0x30, _oid(f"{SGX_EXT_OID}.2.17") + _int(pcesvn)) + _der(0x30, _oid(f"{SGX_EXT_OID}.2.18") + _der(0x04, bytes(comps)))
    body = (_der(0x30, _oid(f"{SGX_EXT_OID}.1") + _der(0x04, b"\x01" * 16))
            + _der(0x30, _oid(f"{SGX_EXT_OID}.2") + _der(0x30, tcb))
            + _der(0x30, _oid(f"{SGX_EXT_OID}.4") + _der(0x04, fmspc)))
    return x509.UnrecognizedExtension(x509.ObjectIdentifier(SGX_EXT_OID), _der(0x30, body))


def _rs(key, data: bytes, size: int, alg) -> bytes:
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(alg)))
    return r.to_bytes(size, "big") + s.to_bytes(size, "big")


# ----------------------------------------------------------------------------- TDX

class Tdx:
    def __init__(self, comps=None, pcesvn=10):
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _cert("Intel SGX Root CA", self.root_key, ca=True)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        self.pck_ca = _cert("Intel SGX PCK Platform CA", ca_key, "Intel SGX Root CA", self.root_key, ca=True)
        self.pck_key = ec.generate_private_key(ec.SECP256R1())
        self.fmspc = bytes.fromhex("00806f050000")
        self.pck = _cert("Intel SGX PCK Certificate", self.pck_key, "Intel SGX PCK Platform CA", ca_key,
                         extensions=[_sgx_extension(self.fmspc, comps or [2] * 16, pcesvn)])
        self.tcb_key = ec.generate_private_key(ec.SECP256R1())
        self.tcb_cert = _cert("Intel SGX TCB Signing", self.tcb_key, "Intel SGX Root CA", self.root_key)
        self.att_key = ec.generate_private_key(ec.SECP256R1())
        self.mrsigner = hashlib.sha256(b"intel qe signer").digest()
        self.mrtd = hashlib.sha384(b"td image").digest()

    def quote(self, nonce=NONCE, td_attributes=0, tee_tcb_svn=3, qe_signer=None, bind_key=True, mrtd=None):
        header = struct.pack("<HHI", 4, 2, 0x81) + bytes(40)
        body = bytearray(584)
        body[0:16] = bytes([tee_tcb_svn]) * 16
        body[120:128] = struct.pack("<Q", td_attributes)
        body[136:184] = mrtd or self.mrtd
        for i in range(4):
            body[328 + 48 * i:376 + 48 * i] = hashlib.sha384(f"rtmr{i}".encode()).digest()
        body[520:584] = hashlib.sha512(nonce.encode()).digest()
        nums = self.att_key.public_key().public_numbers()
        att_raw = nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
        quote_sig = _rs(self.att_key, header + bytes(body), 32, hashes.SHA256())
        qe_auth = b"auth-data"
        qe_report = bytearray(384)
        qe_report[128:160] = self.mrsigner
        qe_report[256:260] = struct.pack("<HH", 2, 8)
        qe_report[320:352] = hashlib.sha256((att_raw if bind_key else bytes(64)) + qe_auth).digest()
        qe_sig = _rs(qe_signer or self.pck_key, bytes(qe_report), 32, hashes.SHA256())
        chain = (_pem(self.pck) + _pem(self.pck_ca) + _pem(self.root)).encode()
        qe_data = bytes(qe_report) + qe_sig + struct.pack("<H", len(qe_auth)) + qe_auth + struct.pack("<HI", 5, len(chain)) + chain
        sig_data = quote_sig + att_raw + struct.pack("<HI", 6, len(qe_data)) + qe_data
        return (header + bytes(body) + struct.pack("<I", len(sig_data)) + sig_data).hex()

    def signed(self, member, body, key=None, next_update=None):
        body = dict(body, nextUpdate=(next_update or NOW + dt.timedelta(days=30)).isoformat())
        text = json.dumps(body, separators=(",", ":"))
        sig = _rs(key or self.tcb_key, text.encode(), 32, hashes.SHA256()).hex()
        return '{"' + member + '":' + text + ',"signature":"' + sig + '"}'

    def collateral(self, tcb_levels=None, fmspc=None, qe_mrsigner=None, **kw):
        levels = tcb_levels or [
            {"tcb": {"sgxtcbcomponents": [{"svn": 2}] * 16, "pcesvn": 10, "tdxtcbcomponents": [{"svn": 3}] * 16}, "tcbStatus": "UpToDate"},
            {"tcb": {"sgxtcbcomponents": [{"svn": 1}] * 16, "pcesvn": 5, "tdxtcbcomponents": [{"svn": 1}] * 16}, "tcbStatus": "OutOfDate"},
        ]
        return {
            "tcb_info": self.signed("tcbInfo", {"id": "TDX", "version": 3, "fmspc": (fmspc or self.fmspc).hex(), "tcbLevels": levels}, **kw),
            "qe_identity": self.signed("enclaveIdentity", {"id": "TD_QE", "mrsigner": (qe_mrsigner or self.mrsigner).hex(),
                                                           "isvprodid": 2, "tcbLevels": [{"tcb": {"isvsvn": 8}, "tcbStatus": "UpToDate"}]}),
            "tcb_signing_chain": [_pem(self.tcb_cert)],
        }

    def verify(self, quote=None, collateral=None, nonce=NONCE, expected=None, roots=None):
        ev = {"quote": quote or self.quote(), "collateral": collateral or self.collateral()}
        return TDXVerifier().verify(ev, nonce, expected, [self.root] if roots is None else roots)


@pytest.fixture
def tdx():
    return Tdx()


def test_tdx_golden_path(tdx):
    res = tdx.verify(expected={"mrtd": tdx.mrtd.hex()})
    assert res.flavor == "tdx" and res.measurements["mrtd"] == tdx.mrtd.hex()
    assert set(res.measurements) == {"mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3"}


def test_tdx_quote_for_another_challenge(tdx):
    assert _code(lambda: tdx.verify(quote=tdx.quote(nonce="old"))) == "tdx_wrong_nonce"


def test_tdx_edited_td_report_breaks_the_quote_signature(tdx):
    q = bytearray(bytes.fromhex(tdx.quote(nonce="old")))
    q[48 + 520:48 + 584] = hashlib.sha512(NONCE.encode()).digest()
    assert _code(lambda: tdx.verify(quote=q.hex())) == "tdx_invalid_signature"


def test_tdx_qe_report_must_be_signed_by_the_pck_and_bind_the_key(tdx):
    assert _code(lambda: tdx.verify(quote=tdx.quote(qe_signer=ec.generate_private_key(ec.SECP256R1())))) == "tdx_invalid_signature"
    assert _code(lambda: tdx.verify(quote=tdx.quote(bind_key=False))) == "tdx_invalid_signature"


def test_tdx_debug_and_measurements(tdx):
    assert _code(lambda: tdx.verify(quote=tdx.quote(td_attributes=1))) == "tdx_debug_mode_forbidden"
    assert _code(lambda: tdx.verify(expected={"mrtd": "00" * 48})) == "tdx_measurement_mismatch"


def test_tdx_pck_chain_must_reach_a_pinned_intel_root(tdx):
    other = _cert("Other Root", ec.generate_private_key(ec.SECP256R1()), ca=True)
    assert _code(lambda: tdx.verify(roots=[other])) == "tdx_bad_certificate_chain"
    assert _code(lambda: tdx.verify(roots=[])) == "tdx_bad_certificate_chain"


def test_tdx_collateral_must_be_intel_signed_current_and_for_this_platform(tdx):
    forged = tdx.collateral(key=ec.generate_private_key(ec.SECP256R1()))
    assert _code(lambda: tdx.verify(collateral=forged)) == "tdx_bad_collateral"
    stale = tdx.collateral(next_update=NOW - dt.timedelta(days=1))
    assert _code(lambda: tdx.verify(collateral=stale)) == "tdx_stale_collateral"
    assert _code(lambda: tdx.verify(collateral=tdx.collateral(fmspc=b"\x01" * 6))) == "tdx_bad_collateral"
    assert _code(lambda: tdx.verify(collateral=tdx.collateral(qe_mrsigner=b"\x02" * 32))) == "tdx_bad_quoting_enclave"
    tampered = tdx.collateral()
    tampered["tcb_info"] = tampered["tcb_info"].replace('"OutOfDate"', '"UpToDate"')  # editing the signed text
    assert _code(lambda: tdx.verify(collateral=tampered)) == "tdx_bad_collateral"


def test_tdx_out_of_date_tcb_is_refused():
    low = Tdx(comps=[1] * 16, pcesvn=5)  # meets only the OutOfDate level
    assert _code(lambda: low.verify()) == "tdx_tcb_not_up_to_date"
    tdx = Tdx()
    assert _code(lambda: tdx.verify(quote=tdx.quote(tee_tcb_svn=2))) == "tdx_tcb_not_up_to_date"  # TDX module below the level


@pytest.mark.parametrize("cut", [10, 600, 800])
def test_tdx_truncated_quotes_are_refused(tdx, cut):
    assert _code(lambda: tdx.verify(quote=tdx.quote()[: cut * 2])) == "tdx_malformed_quote"


# ----------------------------------------------------------------------------- NVIDIA CC

class Gpu:
    def __init__(self):
        root_key = ec.generate_private_key(ec.SECP384R1())
        self.root = _cert("NVIDIA Device Identity CA", root_key, ca=True, hash_alg=hashes.SHA384())
        self.key = ec.generate_private_key(ec.SECP384R1())
        self.cert = _cert("GH100 A01 FSP BROM", self.key, "NVIDIA Device Identity CA", root_key, hash_alg=hashes.SHA384())
        self.values = {i: hashlib.sha384(f"measurement {i}".encode()).digest() for i in (1, 2, 3)}

    def evidence(self, nonce=NONCE, attrs=0x01, values=None, key=None):
        request = bytes([0x11, 0xE0, attrs, 0xFF]) + hashlib.sha256(nonce.encode()).digest() + b"\x00"
        record = b"".join(bytes([i, 0x01]) + struct.pack("<H", 3 + len(v)) + bytes([0x01]) + struct.pack("<H", len(v)) + v
                          for i, v in (values or self.values).items())
        unsigned = (bytes([0x11, 0x60, 0, 0, len(values or self.values)]) + len(record).to_bytes(3, "little") + record
                    + b"\x07" * 32 + struct.pack("<H", 0))
        sig = _rs(key or self.key, request + unsigned, 48, hashes.SHA384())
        return {"request": request.hex(), "response": (unsigned + sig).hex(), "certificate_chain": [_pem(self.cert)]}

    def verify(self, ev=None, nonce=NONCE, expected=None, roots=None):
        return NvidiaCCVerifier().verify(ev or self.evidence(), nonce, expected, [self.root] if roots is None else roots)


@pytest.fixture
def gpu():
    return Gpu()


def test_nvidia_golden_path(gpu):
    res = gpu.verify(expected={"2": gpu.values[2].hex()})
    assert res.flavor == "nvidia_cc" and res.measurements == {str(i): v.hex() for i, v in gpu.values.items()}


def test_nvidia_refusals(gpu):
    assert _code(lambda: gpu.verify(gpu.evidence(nonce="old"))) == "nvidia_wrong_nonce"
    assert _code(lambda: gpu.verify(gpu.evidence(attrs=0))) == "nvidia_malformed_evidence"  # unsigned measurements
    assert _code(lambda: gpu.verify(gpu.evidence(key=ec.generate_private_key(ec.SECP384R1())))) == "nvidia_invalid_signature"
    assert _code(lambda: gpu.verify(expected={"1": "00" * 48})) == "nvidia_measurement_mismatch"
    assert _code(lambda: gpu.verify(expected={"9": "00" * 48})) == "nvidia_measurement_mismatch"  # missing block
    other = _cert("Other", ec.generate_private_key(ec.SECP384R1()), ca=True, hash_alg=hashes.SHA384())
    assert _code(lambda: gpu.verify(roots=[other])) == "nvidia_bad_certificate_chain"


def test_nvidia_edited_measurement_breaks_the_signature(gpu):
    ev = gpu.evidence()
    resp = bytearray(bytes.fromhex(ev["response"]))
    resp[20] ^= 0xFF  # inside the first measurement value
    ev["response"] = resp.hex()
    assert _code(lambda: gpu.verify(ev)) == "nvidia_invalid_signature"


def test_nvidia_device_key_must_be_p384(gpu):
    weak = ec.generate_private_key(ec.SECP256R1())
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _cert("R", root_key, ca=True)
    gpu.cert = _cert("D", weak, "R", root_key)
    assert _code(lambda: gpu.verify(roots=[root])) == "nvidia_bad_key_type"


@pytest.mark.parametrize("field,cut", [("request", 20), ("response", 30)])
def test_nvidia_truncated_messages_are_refused(gpu, field, cut):
    ev = gpu.evidence()
    ev[field] = ev[field][: cut * 2]
    assert _code(lambda: gpu.verify(ev)) == "nvidia_malformed_evidence"
