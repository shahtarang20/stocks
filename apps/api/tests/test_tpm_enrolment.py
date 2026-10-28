"""TPM attestation-key enrolment (WP-F): MakeCredential / ActivateCredential binding, over HTTP.

The host side is emulated here with a Python ActivateCredential (TPM 2.0 Part 1, 24.4-24.5) so
these run anywhere; tests/e2e/test_tpm_swtpm_e2e.py runs the same flow against a real TPM 2.0
implementation (swtpm + tpm2-tools).
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import hmac
import json
import struct
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.decrepit.ciphers.modes import CFB
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.db.base import utcnow
from app.models.enums import UserStatus
from app.models.privacy import MachineTpmEnrolment, TrustedImage
from app.models.provider import Provider, ProviderMachine
from app.models.user import User
from app.services.attestation_verifier import AttestationVerifierError, verify_machine_evidence
from app.services.hardware_attestation.tpm2 import FIXED_PARENT, FIXED_TPM, SENSITIVE_DATA_ORIGIN, SIGN, tpm_name
from app.services.privacy_crypto import generate_ed25519_keypair, generate_x25519_keypair
from app.services.tpm_enrolment import ENROLMENT_TTL, bound_ak_name, kdfa
from tests.test_tpm2_attestation import Machine, _ak_public, _cert, _pem

LOCKS = {"bare_metal": True, "iommu_enabled": True, "kernel_lockdown": True}


def activate_credential(ek_private: rsa.RSAPrivateKey, ak_name: bytes, blob: bytes, encrypted_seed: bytes) -> bytes:
    """What TPM2_ActivateCredential does inside the TPM (for an RSA EK, SHA-256, AES-128-CFB)."""
    seed = ek_private.decrypt(
        encrypted_seed, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=b"IDENTITY\x00")
    )
    hmac_len = struct.unpack(">H", blob[:2])[0]
    outer, enc_identity = blob[2:2 + hmac_len], blob[2 + hmac_len:]
    if not hmac.compare_digest(outer, hmac.new(kdfa(seed, b"INTEGRITY", b"", b"", 256), enc_identity + ak_name, hashlib.sha256).digest()):
        raise ValueError("integrity check failed (wrong AK name)")
    plain = Cipher(algorithms.AES(kdfa(seed, b"STORAGE", ak_name, b"", 128)), CFB(b"\x00" * 16)).decryptor().update(enc_identity)
    return plain[2:2 + struct.unpack(">H", plain[:2])[0]]


def _sm(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _machine(db_engine, key: str) -> ProviderMachine:
    async with _sm(db_engine)() as db:
        user = User(email=f"t-{uuid.uuid4().hex[:6]}@x.io", password_hash="pw", email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        provider = Provider(user_id=user.id, display_name="H", status="active")
        db.add(provider)
        await db.flush()
        m = ProviderMachine(provider_id=provider.id, hostname=f"tpm-{uuid.uuid4().hex[:4]}", agent_version="1", status="registered",
                            availability="online", asking_price_per_hour=decimal.Decimal("1"), signing_key=key, secret_hash="s",
                            stake_bonded=decimal.Decimal("2000"), last_heartbeat_at=utcnow())
        db.add(m)
        await db.commit()
        return m


def _headers(machine_id, key: str, raw: bytes) -> dict[str, str]:
    ts = int(time.time())
    sig = hmac.new(key.encode(), f"{machine_id}\n{ts}\n{hashlib.sha256(raw).hexdigest()}".encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-ComputeCoin-Machine": str(machine_id),
            "X-ComputeCoin-Timestamp": str(ts), "X-ComputeCoin-Signature": sig}


async def _post(client, machine, key, path, body):
    raw = json.dumps(body).encode()
    return await client.post(path, content=raw, headers=_headers(machine.id, key, raw))


class Host:
    """A machine's TPM (from test_tpm2_attestation.Machine, but with the EK private key kept for activation)."""

    def __init__(self, tpm: Machine | None = None):
        self.tpm = tpm or Machine()

    def start_body(self):
        return {"ek_cert": _pem(self.tpm.ek), "ek_intermediates": [_pem(self.tpm.inter)], "ak_public": self.tpm.ak_public.hex()}

    def activate(self, start: dict) -> str:
        return activate_credential(
            self.tpm.ek_key, tpm_name(self.tpm.ak_public), bytes.fromhex(start["credential_blob"]), bytes.fromhex(start["encrypted_secret"])
        ).hex()


@pytest.fixture
def host(monkeypatch):
    h = Host()
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ROOT_CERTS", json.dumps({"tpm2": [_pem(h.tpm.root)]}))
    return h


async def _enrol(client, machine, key, host):
    start = await _post(client, machine, key, "/v1/agents/tpm/enrolments", host.start_body())
    assert start.status_code == 200, start.text
    data = start.json()
    return data, await _post(client, machine, key, f"/v1/agents/tpm/enrolments/{data['enrolment_id']}/activate", {"secret": host.activate(data)})


@pytest.mark.asyncio
async def test_enrolment_binds_the_ak_and_activation_needs_the_ek_private_key(client: AsyncClient, db_engine, host):
    machine = await _machine(db_engine, "k1")
    start = (await _post(client, machine, "k1", "/v1/agents/tpm/enrolments", host.start_body())).json()
    assert start["ak_name"] == tpm_name(host.tpm.ak_public).hex()
    # Another TPM (different EK private key) cannot recover the secret.
    with pytest.raises(ValueError):
        activate_credential(rsa.generate_private_key(public_exponent=65537, key_size=2048), tpm_name(host.tpm.ak_public),
                            bytes.fromhex(start["credential_blob"]), bytes.fromhex(start["encrypted_secret"]))
    # The same TPM with a different AK loaded cannot either (the AK name is bound in).
    with pytest.raises(ValueError):
        activate_credential(host.tpm.ek_key, tpm_name(_ak_public(ec.generate_private_key(ec.SECP256R1()))),
                            bytes.fromhex(start["credential_blob"]), bytes.fromhex(start["encrypted_secret"]))
    async with _sm(db_engine)() as db:
        assert await bound_ak_name(db, machine.id) is None  # nothing active before activation
        row = (await db.execute(select(MachineTpmEnrolment))).scalar_one()
        assert row.secret_sha256 and host.activate(start) not in json.dumps(row.__dict__, default=str)  # secret never stored

    r = await _post(client, machine, "k1", f"/v1/agents/tpm/enrolments/{start['enrolment_id']}/activate", {"secret": host.activate(start)})
    assert r.status_code == 200 and r.json()["status"] == "active"
    async with _sm(db_engine)() as db:
        assert await bound_ak_name(db, machine.id) == tpm_name(host.tpm.ak_public).hex()
    # Single use.
    again = await _post(client, machine, "k1", f"/v1/agents/tpm/enrolments/{start['enrolment_id']}/activate", {"secret": host.activate(start)})
    assert again.status_code == 409 and again.json()["detail"] == "tpm_enrolment_not_pending"


@pytest.mark.asyncio
async def test_wrong_secrets_are_counted_and_lock_the_enrolment(client: AsyncClient, db_engine, host):
    machine = await _machine(db_engine, "k2")
    start = (await _post(client, machine, "k2", "/v1/agents/tpm/enrolments", host.start_body())).json()
    path = f"/v1/agents/tpm/enrolments/{start['enrolment_id']}/activate"
    for _ in range(3):
        r = await _post(client, machine, "k2", path, {"secret": "00" * 32})
        assert r.status_code == 409 and r.json()["detail"] == "tpm_activation_failed"
    async with _sm(db_engine)() as db:
        row = await db.get(MachineTpmEnrolment, uuid.UUID(start["enrolment_id"]))
        assert row.attempts == 3 and row.status == "failed"  # committed despite the 409
    r = await _post(client, machine, "k2", path, {"secret": host.activate(start)})
    assert r.json()["detail"] == "tpm_enrolment_not_pending"


@pytest.mark.asyncio
async def test_expired_enrolment_cannot_be_activated(client: AsyncClient, db_engine, host):
    machine = await _machine(db_engine, "k3")
    start = (await _post(client, machine, "k3", "/v1/agents/tpm/enrolments", host.start_body())).json()
    async with _sm(db_engine)() as db:
        row = await db.get(MachineTpmEnrolment, uuid.UUID(start["enrolment_id"]))
        row.expires_at = utcnow() - dt.timedelta(seconds=1)
        await db.commit()
    r = await _post(client, machine, "k3", f"/v1/agents/tpm/enrolments/{start['enrolment_id']}/activate", {"secret": host.activate(start)})
    assert r.json()["detail"] == "tpm_enrolment_expired"
    assert ENROLMENT_TTL == dt.timedelta(minutes=5)


@pytest.mark.asyncio
async def test_untrusted_ek_and_unsuitable_ak_are_refused(client: AsyncClient, db_engine, host, monkeypatch):
    machine = await _machine(db_engine, "k4")
    stranger = Machine()  # an EK from a vendor root that is not pinned
    body = Host(stranger).start_body()
    r = await _post(client, machine, "k4", "/v1/agents/tpm/enrolments", body)
    assert r.status_code == 409 and r.json()["detail"] == "tpm_ek_not_trusted"

    loose = _ak_public(host.tpm.ak, attrs=FIXED_TPM | FIXED_PARENT | SENSITIVE_DATA_ORIGIN | SIGN)  # not restricted
    r = await _post(client, machine, "k4", "/v1/agents/tpm/enrolments", dict(host.start_body(), ak_public=loose.hex()))
    assert r.json()["detail"] == "tpm2_bad_ak"

    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ROOT_CERTS", "")  # nothing pinned: nothing enrols
    r = await _post(client, machine, "k4", "/v1/agents/tpm/enrolments", host.start_body())
    assert r.json()["detail"] == "tpm_ek_not_trusted"


@pytest.mark.asyncio
async def test_one_ek_vouches_for_one_machine_and_others_cannot_activate_it(client: AsyncClient, db_engine, host):
    a, b = await _machine(db_engine, "ka"), await _machine(db_engine, "kb")
    start_a = (await _post(client, a, "ka", "/v1/agents/tpm/enrolments", host.start_body())).json()
    stolen = await _post(client, b, "kb", f"/v1/agents/tpm/enrolments/{start_a['enrolment_id']}/activate", {"secret": host.activate(start_a)})
    assert stolen.json()["detail"] == "tpm_enrolment_not_found"
    ok = await _post(client, a, "ka", f"/v1/agents/tpm/enrolments/{start_a['enrolment_id']}/activate", {"secret": host.activate(start_a)})
    assert ok.status_code == 200
    r = await _post(client, b, "kb", "/v1/agents/tpm/enrolments", host.start_body())
    assert r.status_code == 409 and r.json()["detail"] == "tpm_ek_enrolled_elsewhere"


@pytest.mark.asyncio
async def test_re_enrolment_replaces_the_active_ak(client: AsyncClient, db_engine, host):
    machine = await _machine(db_engine, "k5")
    _, first = await _enrol(client, machine, "k5", host)
    assert first.status_code == 200
    host.tpm.ak = ec.generate_private_key(ec.SECP256R1())
    host.tpm.ak_public = _ak_public(host.tpm.ak)
    _, second = await _enrol(client, machine, "k5", host)
    assert second.status_code == 200
    async with _sm(db_engine)() as db:
        assert await bound_ak_name(db, machine.id) == tpm_name(host.tpm.ak_public).hex()
        statuses = sorted(r.status for r in (await db.execute(select(MachineTpmEnrolment))).scalars())
    assert statuses == ["active", "replaced"]


@pytest.mark.asyncio
async def test_hardware_verdict_needs_an_enrolled_ak(client: AsyncClient, db_engine, host, monkeypatch):
    """The verdict path: refused before enrolment, issued after, refused for a quote from another AK."""
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", False)
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ATTESTATION", True)
    machine = await _machine(db_engine, "k6")
    tpm = host.tpm
    async with _sm(db_engine)() as db:
        db.add(TrustedImage(image_hash="cd" * 32, version_tag="tpm-v1", tier="sealed", status="active",
                            expected_pcrs={f"pcr{i}": tpm.pcrs[i].hex() for i in (0, 4, 7)}))
        await db.commit()

    async def attest(evidence, nonce):
        async with _sm(db_engine)() as db:
            m = await db.get(ProviderMachine, machine.id)
            return await verify_machine_evidence(
                db, machine=m, requested_tier="sealed", nonce=nonce, pcr_measurements={},
                ephemeral_public_key=generate_x25519_keypair()[1], receipt_signing_public_key=generate_ed25519_keypair()[1],
                security_state=LOCKS, hardware_evidence=evidence, hardware_flavor="tpm2",
            )

    nonce = "n-" + uuid.uuid4().hex
    with pytest.raises(AttestationVerifierError) as exc:
        await attest(tpm.evidence(nonce=nonce), nonce)
    assert exc.value.code == "hardware_attestation_failed:tpm2_ak_not_enrolled"

    _, activated = await _enrol(client, machine, "k6", host)
    assert activated.status_code == 200
    verdict, _ = await attest(tpm.evidence(nonce=nonce), nonce)
    assert verdict["image_hash"] == "cd" * 32 and verdict["tier_reached"] == "sealed"

    rogue = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(AttestationVerifierError) as exc:
        await attest(tpm.evidence(nonce=nonce, ak=rogue, ak_public=_ak_public(rogue)), nonce)
    assert exc.value.code == "hardware_attestation_failed:tpm2_unbound_ak"
