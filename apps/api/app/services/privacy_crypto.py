"""Cryptographic primitives for Proof-of-Privacy (ADR-0005), envelope format ``ccpp-v1``.

Only standard, reviewed primitives from the ``cryptography`` package are used:

* **Sealing to a public key** (guardian shares, shares re-wrapped to a host, results to a
  customer): ephemeral X25519 key agreement, HKDF-SHA256, ChaCha20-Poly1305. Only the holder
  of the recipient's PRIVATE key can open it. The same construction is implemented in the
  provider agent and the Python SDK; the three must stay byte-compatible.
* **Data encryption** (customer input): ChaCha20-Poly1305 with a random 32-byte Data Key.
* **Signatures** (attestation verdicts, wipe receipts): Ed25519, so anyone holding the public
  key can verify and nobody but the key holder can sign.
* **Threshold sharing** of the Data Key: Shamir over GF(2^8), polynomial 0x11D, generator 2.

All binary values travel as lowercase hex.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "ccpp-v1"
SEAL_ALG = "X25519-HKDF-SHA256-ChaCha20Poly1305"
DATA_ALG = "ChaCha20Poly1305"

_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw


class PrivacyCryptoError(ValueError):
    """A key, envelope or signature is malformed or does not verify."""


def _hex32(value: Any, what: str) -> bytes:
    try:
        raw = bytes.fromhex(value) if isinstance(value, str) else b""
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise PrivacyCryptoError(f"{what} must be 32 bytes of hex")
    return raw


def is_hex32(value: Any) -> bool:
    try:
        _hex32(value, "value")
        return True
    except PrivacyCryptoError:
        return False


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- keys

def generate_x25519_keypair() -> tuple[str, str]:
    """Return (private_hex, public_hex) for sealing."""
    priv = X25519PrivateKey.generate()
    return priv.private_bytes(_RAW, _RAW_PRIV, serialization.NoEncryption()).hex(), priv.public_key().public_bytes(_RAW, _RAW_PUB).hex()


def x25519_public_from_private(private_hex: str) -> str:
    return X25519PrivateKey.from_private_bytes(_hex32(private_hex, "private key")).public_key().public_bytes(_RAW, _RAW_PUB).hex()


def generate_ed25519_keypair() -> tuple[str, str]:
    """Return (private_seed_hex, public_hex) for signing."""
    priv = Ed25519PrivateKey.generate()
    return priv.private_bytes(_RAW, _RAW_PRIV, serialization.NoEncryption()).hex(), priv.public_key().public_bytes(_RAW, _RAW_PUB).hex()


def ed25519_public_from_private(private_hex: str) -> str:
    return Ed25519PrivateKey.from_private_bytes(_hex32(private_hex, "signing key")).public_key().public_bytes(_RAW, _RAW_PUB).hex()


# --------------------------------------------------------------------------- signatures

def sign(private_hex: str, message: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(_hex32(private_hex, "signing key")).sign(message).hex()


def verify(public_hex: str, message: bytes, signature_hex: Any) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_hex32(public_hex, "verify key")).verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, PrivacyCryptoError, ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- sealing

def _seal_key(shared: bytes, epk: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=VERSION.encode() + b"|" + epk + recipient_pub).derive(shared)


def seal(recipient_public_hex: str, plaintext: bytes, aad: bytes) -> dict[str, str]:
    """Encrypt ``plaintext`` so only the holder of the recipient's private key can read it."""
    recipient_pub = _hex32(recipient_public_hex, "recipient public key")
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes(_RAW, _RAW_PUB)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_seal_key(shared, epk, recipient_pub)).encrypt(nonce, plaintext, aad)
    return {"v": VERSION, "alg": SEAL_ALG, "epk": epk.hex(), "nonce": nonce.hex(), "ct": ct.hex()}


def open_sealed(recipient_private_hex: str, envelope: Any, aad: bytes) -> bytes:
    if not isinstance(envelope, dict) or envelope.get("v") != VERSION or envelope.get("alg") != SEAL_ALG:
        raise PrivacyCryptoError("not a ccpp-v1 sealed envelope")
    priv = X25519PrivateKey.from_private_bytes(_hex32(recipient_private_hex, "private key"))
    epk = _hex32(envelope.get("epk"), "envelope epk")
    try:
        nonce = bytes.fromhex(envelope["nonce"])
        ct = bytes.fromhex(envelope["ct"])
        shared = priv.exchange(X25519PublicKey.from_public_bytes(epk))
        recipient_pub = priv.public_key().public_bytes(_RAW, _RAW_PUB)
        return ChaCha20Poly1305(_seal_key(shared, epk, recipient_pub)).decrypt(nonce, ct, aad)
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise PrivacyCryptoError("sealed envelope does not open with this key") from exc


def is_sealed_envelope(envelope: Any) -> bool:
    return (
        isinstance(envelope, dict)
        and envelope.get("v") == VERSION
        and envelope.get("alg") == SEAL_ALG
        and is_hex32(envelope.get("epk"))
        and isinstance(envelope.get("nonce"), str)
        and len(envelope["nonce"]) == 24
        and isinstance(envelope.get("ct"), str)
        and len(envelope["ct"]) >= 32
    )


def is_data_envelope(envelope: Any) -> bool:
    return (
        isinstance(envelope, dict)
        and envelope.get("v") == VERSION
        and envelope.get("alg") == DATA_ALG
        and isinstance(envelope.get("nonce"), str)
        and len(envelope["nonce"]) == 24
        and isinstance(envelope.get("ct"), str)
        and len(envelope["ct"]) >= 32
    )


def encrypt_data(data_key: bytes, plaintext: bytes, aad: bytes) -> dict[str, str]:
    """Symmetric AEAD with the job's Data Key (used for customer input)."""
    if len(data_key) != 32:
        raise PrivacyCryptoError("data key must be 32 bytes")
    nonce = os.urandom(12)
    return {"v": VERSION, "alg": DATA_ALG, "nonce": nonce.hex(), "ct": ChaCha20Poly1305(data_key).encrypt(nonce, plaintext, aad).hex()}


def decrypt_data(data_key: bytes, envelope: Any, aad: bytes) -> bytes:
    if not is_data_envelope(envelope):
        raise PrivacyCryptoError("not a ccpp-v1 data envelope")
    try:
        return ChaCha20Poly1305(data_key).decrypt(bytes.fromhex(envelope["nonce"]), bytes.fromhex(envelope["ct"]), aad)
    except (InvalidTag, ValueError) as exc:
        raise PrivacyCryptoError("data envelope does not decrypt with this key") from exc


INPUT_AAD = f"{VERSION}|job-input".encode()


def output_aad(job_id: str) -> bytes:
    return f"{VERSION}|job-output|{job_id}".encode()


def share_aad(guardian: str) -> bytes:
    """AAD for a Data Key share sealed by the customer to a guardian."""
    return f"{VERSION}|guardian-share|{guardian}".encode()


def release_aad(job_id: str, guardian: str) -> bytes:
    """AAD for a share re-sealed by a guardian to the host's in-RAM key, bound to one job."""
    return f"{VERSION}|host-release|{job_id}|{guardian}".encode()


# --------------------------------------------------------------------------- Shamir over GF(2^8)

def _gf_tables() -> tuple[list[int], list[int]]:
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = exp[i + 255] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # primitive polynomial x^8+x^4+x^3+x^2+1; 2 generates the field
    return exp, log


_EXP, _LOG = _gf_tables()


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("GF(256) division by zero")
    return 0 if a == 0 else _EXP[(_LOG[a] - _LOG[b]) % 255]


def split_secret(secret: bytes, n: int = 3, k: int = 2) -> list[tuple[int, bytes]]:
    """Split ``secret`` into ``n`` shares (x = 1..n); any ``k`` reconstruct it, fewer reveal nothing."""
    if not 1 <= k <= n <= 255:
        raise ValueError("need 1 <= k <= n <= 255")
    shares = [bytearray() for _ in range(n)]
    for byte in secret:
        coeffs = [byte] + [secrets.randbelow(256) for _ in range(k - 1)]
        for x in range(1, n + 1):
            acc, power = 0, 1
            for c in coeffs:
                acc ^= _mul(c, power)
                power = _mul(power, x)
            shares[x - 1].append(acc)
    return [(x, bytes(shares[x - 1])) for x in range(1, n + 1)]


def reconstruct_secret(shares: list[tuple[int, bytes]], k: int = 2) -> bytes:
    """Lagrange interpolation at x = 0 over the first ``k`` shares."""
    if len(shares) < k:
        raise ValueError(f"need at least {k} shares, got {len(shares)}")
    use = shares[:k]
    xs = [x for x, _ in use]
    if len(set(xs)) != k or any(not 1 <= x <= 255 for x in xs):
        raise ValueError("share x-coordinates must be distinct and in 1..255")
    length = len(use[0][1])
    if any(len(s) != length for _, s in use):
        raise ValueError("all shares must have the same length")
    weights = []
    for j in range(k):
        num = den = 1
        for m in range(k):
            if m != j:
                num = _mul(num, xs[m])
                den = _mul(den, xs[j] ^ xs[m])
        weights.append(_div(num, den))
    out = bytearray(length)
    for i in range(length):
        acc = 0
        for j in range(k):
            acc ^= _mul(use[j][1][i], weights[j])
        out[i] = acc
    return bytes(out)
