"""Agent TPM support (WP-F): enrolment client, hardware evidence in attestation, quote safety.

The real-TPM path (swtpm + tpm2-tools) is exercised in tests/e2e/test_tpm_swtpm_e2e.py.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from computecoin_agent.attestation import perform_attestation
from computecoin_agent.config import AgentConfig
from computecoin_agent.jobrunner import JobRunnerError
from computecoin_agent.tpm import Tpm2, TpmError, _trim_der, enrol, hardware_provider


class FakeTpm:
    def __init__(self):
        self.calls = []

    def ensure_keys(self):
        self.calls.append("ensure_keys")

    def ek_certificate(self):
        return "EK-PEM", ["CA-PEM"]

    def ak_public(self):
        return b"\x00\x23ak"

    def activate(self, blob, secret):
        self.calls.append(("activate", blob, secret))
        return b"\x42" * 32


def _backend(seen, start_status=200, activate_status=200):
    def handle(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers.get("X-ComputeCoin-Signature")))
        if request.url.path.endswith("/tpm/enrolments"):
            if start_status != 200:
                return httpx.Response(start_status, json={"detail": "tpm_ek_not_trusted"})
            return httpx.Response(200, json={"enrolment_id": "e-1", "ak_name": "000b" + "aa" * 32,
                                             "credential_blob": "0102", "encrypted_secret": "0304", "expires_at": "x"})
        if activate_status != 200:
            return httpx.Response(activate_status, json={"detail": "tpm_activation_failed"})
        return httpx.Response(200, json={"enrolment_id": "e-1", "ak_name": "000b" + "aa" * 32, "status": "active"})

    return httpx.MockTransport(handle)


def test_enrol_sends_ek_and_ak_then_the_recovered_secret():
    seen, tpm = [], FakeTpm()
    name = enrol("http://api", "m-1", "msk_secret", tpm, transport=_backend(seen))
    assert name == "000b" + "aa" * 32
    assert tpm.calls == ["ensure_keys", ("activate", b"\x01\x02", b"\x03\x04")]
    (p1, b1, s1), (p2, b2, s2) = seen
    assert p1 == "/v1/agents/tpm/enrolments" and b1 == {"ek_cert": "EK-PEM", "ek_intermediates": ["CA-PEM"], "ak_public": "0023616b"}
    assert p2 == "/v1/agents/tpm/enrolments/e-1/activate" and b2 == {"secret": "42" * 32}
    assert s1 and s2  # machine-signed requests


@pytest.mark.parametrize("start,activate,detail", [(409, 200, "tpm_ek_not_trusted"), (200, 409, "tpm_activation_failed")])
def test_enrol_refusals_surface_the_reason(start, activate, detail):
    with pytest.raises(JobRunnerError, match=detail):
        enrol("http://api", "m-1", "msk_secret", FakeTpm(), transport=_backend([], start, activate))


def test_attestation_carries_hardware_evidence_for_the_same_nonce():
    seen = {}

    def handle(request):
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json={"machine_id": "m-1", "nonce": "nonce-from-backend-01"})
        seen["body"] = json.loads(request.content)
        now = dt.datetime.now(dt.timezone.utc)
        return httpx.Response(200, json={"verdict": {"verdict_id": "v", "expires_at": (now + dt.timedelta(minutes=30)).isoformat()}})

    quoted = []

    def hardware(nonce):
        quoted.append(nonce)
        return "tpm2", {"quote": {"attest": "aa"}}

    perform_attestation("http://api", "m-1", "msk_secret", tier="sealed", transport=httpx.MockTransport(handle),
                        security_state={"bare_metal": True}, hardware=hardware)
    assert quoted == ["nonce-from-backend-01"]
    assert seen["body"]["hardware_flavor"] == "tpm2" and seen["body"]["hardware_evidence"] == {"quote": {"attest": "aa"}}


def test_without_a_tpm_no_hardware_fields_are_sent():
    assert hardware_provider(None) is None
    assert AgentConfig(tpm_tcti=None).tpm() is None
    t = AgentConfig(tpm_tcti="swtpm:host=127.0.0.1,port=1", tpm_pcrs=[7, 0, 7]).tpm()
    assert isinstance(t, Tpm2) and t.pcrs == [0, 7] and t.env["TPM2TOOLS_TCTI"] == "swtpm:host=127.0.0.1,port=1"


def test_a_pcr_that_changes_during_the_quote_is_refused(tmp_path):
    """PCRs are read before and after the quote; if they differ the evidence would not verify."""
    reads = {"n": 0}

    def runner(args, cwd=None, **kw):
        if args[0] == "tpm2_pcrread":
            reads["n"] += 1
            open(f"{cwd}/{args[-1]}", "wb").write(bytes([reads["n"]]) * 32 * 2)
        elif args[0] == "tpm2_quote":
            open(f"{cwd}/q.msg", "wb").write(b"m")
            open(f"{cwd}/q.sig", "wb").write(b"s")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    tpm = Tpm2("swtpm:host=x,port=1", pcrs=[0, 7], runner=runner)
    with pytest.raises(TpmError, match="changed while quoting"):
        tpm.quote("n")


def test_ek_certificate_from_nv_is_trimmed_to_its_der_length():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EK")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    assert _trim_der(der + b"\xff" * 100) == der
    with pytest.raises(TpmError):
        _trim_der(b"\x04\x00")


def test_tpm_requires_a_tcti():
    with pytest.raises(TpmError):
        Tpm2("")
