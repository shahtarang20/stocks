"""Hardware attestation verifiers and configuration (WP-F / F1, F3, F4).

- F1: dispatcher; the software path stays the default and hardware is off unless configured.
- F3: AMD SEV-SNP on a synthetic report in the real ABI layout, signed by a VCEK whose chain is
  shaped like AMD's (RSA-PSS ARK -> ASK, then an ECDSA P-384 VCEK). Every claim comes from the
  signed bytes. Intel TDX and NVIDIA CC are tested in test_hardware_tdx_nvidia.py.
- F4: PRIVACY_HARDWARE_ATTESTATION needs pinned roots that are real, valid, self-signed CA certs.
- The verdict path matches trusted images on the HARDWARE-SIGNED measurements, not the host's copy.
Real-machine evidence is still required (checklist F5).
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import struct
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from app.core.config import Settings, settings
from app.services.hardware_attestation import (
    HardwareAttestationError,
    get_hardware_verifier,
    parse_root_certs,
)
from app.services.hardware_attestation.nvidia import NvidiaCCVerifier
from app.services.hardware_attestation.sev_snp import REPORT_SIZE, SEVSNPVerifier
from app.services.hardware_attestation.tdx import TDXVerifier
from app.services.hardware_attestation.tpm2 import TPM2Verifier

NONCE = "sev-nonce-778899"
NOW = dt.datetime.now(dt.timezone.utc)
PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=48)


def _cert(subject, key, issuer=None, issuer_key=None, ca=False, pss=False):
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer or subject)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    signer = issuer_key or key
    if pss:
        return builder.sign(signer, hashes.SHA384(), rsa_padding=PSS)
    return builder.sign(signer, hashes.SHA384())


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class Snp:
    """A SEV-SNP platform: ARK (pinned) -> ASK -> VCEK, and a guest that produces reports."""

    def __init__(self):
        ark_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ask_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ark = _cert("ARK-Milan", ark_key, ca=True, pss=True)
        self.ask = _cert("SEV-Milan", ask_key, "ARK-Milan", ark_key, ca=True, pss=True)
        self.vcek_key = ec.generate_private_key(ec.SECP384R1())
        self.vcek = _cert("SEV-VCEK", self.vcek_key, "SEV-Milan", ask_key, pss=True)
        self.measurement = hashlib.sha384(b"launch digest").digest()

    def report(self, nonce=NONCE, policy=0x30000, measurement=None, sig_algo=1, key=None):
        r = bytearray(REPORT_SIZE)
        struct.pack_into("<I", r, 0x00, 3)
        struct.pack_into("<Q", r, 0x08, policy)
        struct.pack_into("<I", r, 0x34, sig_algo)
        r[0x50:0x90] = hashlib.sha512(nonce.encode()).digest()
        r[0x90:0xC0] = measurement or self.measurement
        der = (key or self.vcek_key).sign(bytes(r[:0x2A0]), ec.ECDSA(hashes.SHA384()))
        rr, ss = decode_dss_signature(der)
        r[0x2A0:0x2A0 + 72] = rr.to_bytes(72, "little")
        r[0x2A0 + 72:0x2A0 + 144] = ss.to_bytes(72, "little")
        return bytes(r)

    def evidence(self, report=None, **kw):
        return {"report": (report or self.report(**kw)).hex(), "vcek_cert": _pem(self.vcek), "ask_cert": _pem(self.ask)}

    def verify(self, evidence, nonce=NONCE, expected="default", roots="default"):
        expected = {"measurement": self.measurement.hex()} if expected == "default" else expected
        return SEVSNPVerifier().verify(evidence, nonce, expected, [self.ark] if roots == "default" else roots)


@pytest.fixture
def snp():
    return Snp()


def _code(fn):
    with pytest.raises(HardwareAttestationError) as exc:
        fn()
    return exc.value.code


# ----------------------------------------------------------------------------- F1

def test_dispatcher_and_aliases():
    assert isinstance(get_hardware_verifier("tpm"), TPM2Verifier)
    assert isinstance(get_hardware_verifier("amd_sev_snp"), SEVSNPVerifier)
    assert isinstance(get_hardware_verifier("intel_tdx"), TDXVerifier)
    assert isinstance(get_hardware_verifier("nvidia"), NvidiaCCVerifier)
    assert _code(lambda: get_hardware_verifier("sgx")) == "unsupported_hardware_flavor"


def test_software_path_is_default_and_hardware_is_off():
    s = Settings()
    assert s.PRIVACY_HARDWARE_ATTESTATION is False and s.PRIVACY_SOFTWARE_ATTESTATION is False


# ----------------------------------------------------------------------------- F3 SEV-SNP

def test_sev_snp_golden_path(snp):
    res = snp.verify(snp.evidence())
    assert res.verified is True and res.measurements == {"measurement": snp.measurement.hex()}
    assert res.security_features["policy"] == 0x30000


def test_sev_snp_report_for_another_challenge_is_refused(snp):
    assert _code(lambda: snp.verify(snp.evidence(nonce="old-challenge"))) == "sev_snp_wrong_nonce"


def test_sev_snp_edited_report_bytes_break_the_signature(snp):
    report = bytearray(snp.report(nonce="old-challenge"))
    report[0x50:0x90] = hashlib.sha512(NONCE.encode()).digest()  # swap in the fresh nonce
    assert _code(lambda: snp.verify(snp.evidence(report=bytes(report)))) == "sev_snp_invalid_signature"


def test_sev_snp_debug_policy_and_measurement(snp):
    assert _code(lambda: snp.verify(snp.evidence(policy=0x30000 | (1 << 19)))) == "sev_snp_debug_mode_forbidden"
    other = hashlib.sha384(b"another image").digest()
    assert _code(lambda: snp.verify(snp.evidence(measurement=other))) == "sev_snp_measurement_mismatch"


@pytest.mark.parametrize("roots", ["none", "other"])
def test_sev_snp_chain_must_reach_a_pinned_ark(snp, roots):
    other = _cert("Other", rsa.generate_private_key(public_exponent=65537, key_size=2048), ca=True)
    assert _code(lambda: snp.verify(snp.evidence(), roots=[] if roots == "none" else [other])) == "sev_snp_bad_certificate_chain"


def test_sev_snp_vcek_must_be_p384_and_algo_must_match(snp):
    assert _code(lambda: snp.verify(snp.evidence(sig_algo=2))) == "sev_snp_malformed_report"
    assert _code(lambda: snp.verify({"report": "00" * 10, "vcek_cert": _pem(snp.vcek)})) == "sev_snp_malformed_report"


# TDX and NVIDIA CC: tests/test_hardware_tdx_nvidia.py


# ----------------------------------------------------------------------------- F4

def test_boot_guard_needs_real_pinned_roots(snp):
    with pytest.raises(ValueError, match="PRIVACY_HARDWARE_ROOT_CERTS"):
        Settings(PRIVACY_HARDWARE_ATTESTATION=True, PRIVACY_HARDWARE_ROOT_CERTS="")
    # The earlier guard accepted any non-empty text, e.g. a truncated PEM.
    with pytest.raises(ValueError, match="PRIVACY_HARDWARE_ROOT_CERTS"):
        Settings(PRIVACY_HARDWARE_ATTESTATION=True, PRIVACY_HARDWARE_ROOT_CERTS="-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----")
    for bad in ({"sev_snp": [_pem(snp.vcek)]}, {"sev_snp": [_pem(snp.ask)]}, {"sgx": [_pem(snp.ark)]}, {"sev_snp": []}):
        with pytest.raises(ValueError):  # a leaf, an intermediate (not self-signed), an unknown flavor, empty
            Settings(PRIVACY_HARDWARE_ATTESTATION=True, PRIVACY_HARDWARE_ROOT_CERTS=json.dumps(bad))
    ok = Settings(PRIVACY_HARDWARE_ATTESTATION=True, PRIVACY_HARDWARE_ROOT_CERTS=json.dumps({"amd_sev_snp": [_pem(snp.ark)]}))
    assert set(parse_root_certs(ok.PRIVACY_HARDWARE_ROOT_CERTS)) == {"sev_snp"}


# ----------------------------------------------------------------------------- verdict path

@pytest.mark.asyncio
async def test_verdict_uses_hardware_signed_measurements_not_the_hosts_copy(db_engine, monkeypatch, snp):
    """Confidential = SEV-SNP (or TDX) + NVIDIA GPU, the CPU report committing to the GPU report.
    Images are matched on HARDWARE-SIGNED measurements; each tier accepts only its evidence type."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.enums import UserStatus
    from app.models.privacy import TrustedImage
    from app.models.provider import Provider, ProviderMachine
    from app.models.user import User
    from app.services.attestation_verifier import AttestationVerifierError, gpu_bound_nonce, verify_machine_evidence
    from app.services.privacy_crypto import generate_ed25519_keypair, generate_x25519_keypair
    from tests.test_hardware_tdx_nvidia import Gpu

    gpu = Gpu()
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", False)
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ATTESTATION", True)
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ROOT_CERTS", json.dumps({"sev_snp": [_pem(snp.ark)], "nvidia_cc": [_pem(gpu.root)]}))
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        user = User(email=f"h-{uuid.uuid4().hex[:6]}@x.io", password_hash="pw", email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        provider = Provider(user_id=user.id, display_name="H", status="active")
        db.add(provider)
        await db.flush()
        machine = ProviderMachine(provider_id=provider.id, hostname="cc", agent_version="1", status="registered",
                                  availability="online", asking_price_per_hour=decimal.Decimal("1"), signing_key="k",
                                  secret_hash="s", stake_bonded=decimal.Decimal("6000"))
        db.add(machine)
        trusted = {"cpu.measurement": snp.measurement.hex(), "gpu.1": gpu.values[1].hex()}
        db.add(TrustedImage(image_hash="ab" * 32, version_tag="cc-v1", tier="confidential", expected_pcrs=trusted, status="active"))
        await db.commit()

        async def attest(tier, flavor, evidence, host_claims=None):
            return await verify_machine_evidence(
                db, machine=machine, requested_tier=tier, nonce=NONCE, pcr_measurements=host_claims or {},
                ephemeral_public_key=generate_x25519_keypair()[1], receipt_signing_public_key=generate_ed25519_keypair()[1],
                security_state={"bare_metal": True, "iommu_enabled": True, "kernel_lockdown": True},
                hardware_evidence=evidence, hardware_flavor=flavor,
            )

        async def code(*args):
            with pytest.raises(AttestationVerifierError) as exc:
                await attest(*args)
            return exc.value.code

        gpu_ev = gpu.evidence(nonce=NONCE)
        cc = {"cpu_flavor": "sev_snp", "cpu": snp.evidence(nonce=gpu_bound_nonce(NONCE, gpu_ev)), "gpu": gpu_ev}
        verdict, _ = await attest("confidential", "confidential", cc)
        assert verdict["image_hash"] == "ab" * 32 and verdict["tier_reached"] == "confidential"

        # A guest that booted another image but CLAIMS the trusted measurement is refused.
        evil = {**cc, "cpu": snp.evidence(nonce=gpu_bound_nonce(NONCE, gpu_ev), measurement=hashlib.sha384(b"evil").digest())}
        assert await code("confidential", "confidential", evil, trusted) == "unknown_os_image"
        # A GPU report from somewhere else cannot be paired with this CPU report.
        other_gpu = gpu.evidence(nonce=NONCE, values={1: gpu.values[1], 2: b"\x00" * 48})  # same GPU model, other report
        relayed = {**cc, "gpu": other_gpu}
        assert (await code("confidential", "confidential", relayed)).startswith("hardware_attestation_failed:")
        # Each tier takes only its evidence: no SEV-SNP for Sealed, no TPM / CPU-only for Confidential.
        assert await code("sealed", "confidential", cc) == "hardware_attestation_failed:hardware_tier_mismatch"
        assert await code("confidential", "tpm2", {"quote": {}}) == "hardware_attestation_failed:hardware_tier_mismatch"
        assert await code("confidential", "confidential", {"cpu_flavor": "sev_snp", "cpu": cc["cpu"]}) == "hardware_attestation_failed:hardware_tier_mismatch"
