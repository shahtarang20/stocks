"""TPM attestation-key enrolment (WP-F): bind a machine's AK to its EK by credential activation.

An EK is a restricted DECRYPT key, so it never signs an AK. The standard binding (TCG, used by
Keylime / go-attestation) is:

1. the host sends its EK certificate (chained to a PINNED TPM vendor root) and its AK's TPMT_PUBLIC;
2. the platform runs TPM2_MakeCredential: a random secret is encrypted so that it can only be
   recovered by TPM2_ActivateCredential on the TPM holding that EK's private key AND with an
   object loaded whose name is exactly the AK's name;
3. the host returns the secret; the AK is then "active" for the machine and its quotes can back
   a hardware verdict (``bound_ak_name``).

MakeCredential here follows TPM 2.0 Part 1 sections 24.4-24.5 and Part 3 TPM2_MakeCredential for
an RSA-2048 EK with SHA-256 name algorithm and AES-128-CFB (the default EK template), and is
tested against a real TPM 2.0 implementation (swtpm + tpm2_activatecredential).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import struct
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # TPM credential protection mandates AES-CFB, which cryptography now files under "decrepit"
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover - older cryptography
    from cryptography.hazmat.primitives.ciphers.modes import CFB
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.privacy import MachineTpmEnrolment
from app.models.provider import ProviderMachine
from app.services.hardware_attestation import (
    HardwareAttestationError,
    load_pem_certificate,
    parse_root_certs,
    verify_chain,
)
from app.services.hardware_attestation.tpm2 import parse_ak_public, tpm_name

ENROLMENT_TTL = dt.timedelta(minutes=5)
MAX_ACTIVATION_ATTEMPTS = 3
SECRET_BYTES = 32


class TpmEnrolmentError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


# --------------------------------------------------------------------------- TPM2_MakeCredential

def kdfa(key: bytes, label: bytes, context_u: bytes, context_v: bytes, bits: int) -> bytes:
    """TPM 2.0 KDFa with HMAC-SHA256 (Part 1, 11.4.10.2): counter mode, label NUL-terminated."""
    out = b""
    counter = 1
    while len(out) * 8 < bits:
        out += hmac.new(
            key, struct.pack(">I", counter) + label + b"\x00" + context_u + context_v + struct.pack(">I", bits), hashlib.sha256
        ).digest()
        counter += 1
    return out[: bits // 8]


def make_credential(ek_public: rsa.RSAPublicKey, ak_name: bytes, secret: bytes) -> tuple[bytes, bytes]:
    """Return (credentialBlob, secret) as TPM2B_ID_OBJECT and TPM2B_ENCRYPTED_SECRET contents.

    Only TPM2_ActivateCredential on the TPM owning ``ek_public``, with the object named ``ak_name``
    loaded, recovers ``secret``."""
    if not isinstance(ek_public, rsa.RSAPublicKey) or ek_public.key_size != 2048:
        raise TpmEnrolmentError("tpm_unsupported_ek", "only RSA-2048 EKs are supported")
    if not 0 < len(secret) <= 32:
        raise TpmEnrolmentError("tpm_bad_secret", "credential must be 1..32 bytes")
    seed = os.urandom(32)  # nameAlg digest size
    encrypted_seed = ek_public.encrypt(
        seed, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=b"IDENTITY\x00")
    )
    sym_key = kdfa(seed, b"STORAGE", ak_name, b"", 128)
    plaintext = struct.pack(">H", len(secret)) + secret  # TPM2B_DIGEST
    enc_identity = Cipher(algorithms.AES(sym_key), CFB(b"\x00" * 16)).encryptor().update(plaintext)
    hmac_key = kdfa(seed, b"INTEGRITY", b"", b"", 256)
    outer_hmac = hmac.new(hmac_key, enc_identity + ak_name, hashlib.sha256).digest()
    credential_blob = struct.pack(">H", len(outer_hmac)) + outer_hmac + enc_identity
    return credential_blob, encrypted_seed


# --------------------------------------------------------------------------- enrolment

def _tpm_roots():
    try:
        return parse_root_certs(settings.PRIVACY_HARDWARE_ROOT_CERTS).get("tpm2", [])
    except ValueError:
        return []


async def start_enrolment(
    db: AsyncSession,
    *,
    machine: ProviderMachine,
    ek_cert_pem: str,
    ek_intermediates: list[str],
    ak_public_hex: str,
    now: dt.datetime | None = None,
) -> tuple[MachineTpmEnrolment, bytes, bytes]:
    """Check the EK chain and the AK, then issue a credential challenge.

    Returns (enrolment, credential_blob, encrypted_seed). The secret itself is never stored."""
    ts = now or utcnow()
    if machine.is_banned:
        raise TpmEnrolmentError("machine_is_banned")
    roots = _tpm_roots()
    try:
        ek_cert = load_pem_certificate(ek_cert_pem, "EK certificate")
        intermediates = [load_pem_certificate(p, "EK intermediate") for p in ek_intermediates or []]
        verify_chain(ek_cert, intermediates, roots, code="tpm_ek_not_trusted", now=ts)
        ak_public = bytes.fromhex(ak_public_hex)
        parse_ak_public(ak_public)  # restricted, fixedTPM signing key, SHA-256 name
    except HardwareAttestationError as exc:
        raise TpmEnrolmentError(exc.code, exc.detail) from exc
    except ValueError as exc:
        raise TpmEnrolmentError("tpm_bad_ak", "ak_public is not hex") from exc

    ek_public = ek_cert.public_key()
    ek_spki = ek_public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ek_fingerprint = hashlib.sha256(ek_spki).hexdigest()
    other = (
        await db.execute(
            select(MachineTpmEnrolment.id).where(
                MachineTpmEnrolment.ek_fingerprint == ek_fingerprint,
                MachineTpmEnrolment.status == "active",
                MachineTpmEnrolment.machine_id != machine.id,
            )
        )
    ).first()
    if other is not None:
        # One physical TPM cannot vouch for two machine accounts.
        raise TpmEnrolmentError("tpm_ek_enrolled_elsewhere", "this EK is already enrolled for another machine")

    name = tpm_name(ak_public)
    secret = os.urandom(SECRET_BYTES)
    credential_blob, encrypted_seed = make_credential(ek_public, name, secret)
    enrolment = MachineTpmEnrolment(
        id=uuid.uuid4(),
        machine_id=machine.id,
        status="pending",
        ek_fingerprint=ek_fingerprint,
        ek_cert_pem=ek_cert_pem,
        ak_public=ak_public.hex(),
        ak_name=name.hex(),
        secret_sha256=hashlib.sha256(secret).hexdigest(),
        attempts=0,
        expires_at=ts + ENROLMENT_TTL,
    )
    db.add(enrolment)
    await db.flush()
    return enrolment, credential_blob, encrypted_seed


async def activate_enrolment(
    db: AsyncSession,
    *,
    machine: ProviderMachine,
    enrolment_id: uuid.UUID,
    secret_hex: str,
    now: dt.datetime | None = None,
) -> MachineTpmEnrolment:
    """The host proves it recovered the secret: the AK becomes the machine's active AK."""
    ts = now or utcnow()
    enrolment = (
        await db.execute(
            select(MachineTpmEnrolment).where(MachineTpmEnrolment.id == enrolment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if enrolment is None or enrolment.machine_id != machine.id:
        raise TpmEnrolmentError("tpm_enrolment_not_found")
    if enrolment.status != "pending":
        raise TpmEnrolmentError("tpm_enrolment_not_pending")
    expires = enrolment.expires_at if enrolment.expires_at.tzinfo else enrolment.expires_at.replace(tzinfo=dt.timezone.utc)
    if ts > expires:
        enrolment.status = "failed"
        raise TpmEnrolmentError("tpm_enrolment_expired")
    enrolment.attempts += 1
    try:
        given = hashlib.sha256(bytes.fromhex(secret_hex)).hexdigest()
    except ValueError:
        given = ""
    if not hmac.compare_digest(given, enrolment.secret_sha256):
        if enrolment.attempts >= MAX_ACTIVATION_ATTEMPTS:
            enrolment.status = "failed"
        raise TpmEnrolmentError("tpm_activation_failed", "the secret does not match: wrong TPM or wrong AK")
    if machine.is_banned:
        raise TpmEnrolmentError("machine_is_banned")
    await db.execute(
        update(MachineTpmEnrolment)
        .where(MachineTpmEnrolment.machine_id == machine.id, MachineTpmEnrolment.status == "active")
        .values(status="replaced")
    )
    enrolment.status = "active"
    enrolment.activated_at = ts
    await db.flush()
    return enrolment


async def bound_ak_name(db: AsyncSession, machine_id: uuid.UUID) -> str | None:
    """The machine's active, credential-activated AK name (hex), or None."""
    return (
        await db.execute(
            select(MachineTpmEnrolment.ak_name)
            .where(MachineTpmEnrolment.machine_id == machine_id, MachineTpmEnrolment.status == "active")
            .order_by(MachineTpmEnrolment.activated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
