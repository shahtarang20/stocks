"""Cryptographic primitives for the external guardian service (ADR-0005, ccpp-v1)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "ccpp-v1"
# Verdict format v2: job_id is signed (must match apps/api attestation_verifier.VERDICT_FIELDS).
VERDICT_FIELDS = (
    "verdict_id",
    "job_id",
    "machine_id",
    "image_hash",
    "tier_reached",
    "ephemeral_public_key",
    "receipt_signing_public_key",
    "nonce",
    "issued_at",
    "expires_at",
    "verifier_public_key",
)


class GuardianCryptoError(Exception):
    pass


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def share_aad(guardian_name: str) -> bytes:
    return f"{VERSION}|guardian-share|{guardian_name}".encode("utf-8")


def release_aad(job_id: str, guardian_name: str) -> bytes:
    return f"{VERSION}|host-release|{job_id}|{guardian_name}".encode("utf-8")


def is_hex32(value: Any) -> bool:
    try:
        return isinstance(value, str) and len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def x25519_public_from_private(private_key_hex: str) -> str:
    priv = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return priv.public_key().public_bytes_raw().hex()


SEAL_ALG = "X25519-HKDF-SHA256-ChaCha20Poly1305"


def _seal_key(shared: bytes, epk: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=VERSION.encode() + b"|" + epk + recipient_pub).derive(shared)


def open_sealed(private_key_hex: str, envelope: dict[str, Any], aad: bytes) -> bytes:
    if not isinstance(envelope, dict) or envelope.get("v") != VERSION or envelope.get("alg") != SEAL_ALG:
        raise GuardianCryptoError(f"not a {VERSION} sealed envelope")
    try:
        priv = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        epk = bytes.fromhex(envelope["epk"])
        nonce = bytes.fromhex(envelope["nonce"])
        ct = bytes.fromhex(envelope["ct"])
        shared = priv.exchange(x25519.X25519PublicKey.from_public_bytes(epk))
        recipient_pub = priv.public_key().public_bytes_raw()
        return ChaCha20Poly1305(_seal_key(shared, epk, recipient_pub)).decrypt(nonce, ct, aad)
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise GuardianCryptoError(f"sealed envelope does not open with this key: {exc}") from exc


def seal(recipient_public_key_hex: str, plaintext: bytes, aad: bytes) -> dict[str, str]:
    recipient_pub = bytes.fromhex(recipient_public_key_hex)
    eph = x25519.X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes_raw()
    shared = eph.exchange(x25519.X25519PublicKey.from_public_bytes(recipient_pub))
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_seal_key(shared, epk, recipient_pub)).encrypt(nonce, plaintext, aad)
    return {
        "v": VERSION,
        "alg": SEAL_ALG,
        "epk": epk.hex(),
        "nonce": nonce.hex(),
        "ct": ct.hex(),
    }


def verify_signature(public_key_hex: str, data: bytes, signature_hex: str) -> bool:
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_verdict_signature(verdict: dict[str, Any], verifier_public_key_hex: str) -> bool:
    if not isinstance(verdict, dict) or verdict.get("verifier_public_key") != verifier_public_key_hex:
        return False
    if any(f not in verdict for f in VERDICT_FIELDS):
        return False
    msg = canonical_json({f: verdict[f] for f in VERDICT_FIELDS})
    return verify_signature(verifier_public_key_hex, msg, str(verdict.get("signature", "")))


def verdict_is_current(verdict: dict[str, Any], now: dt.datetime | None = None) -> bool:
    try:
        now_ts = now or dt.datetime.now(dt.timezone.utc)
        expires_at = dt.datetime.fromisoformat(verdict["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        return expires_at > now_ts
    except (KeyError, ValueError, TypeError):
        return False


def hash_leaf(event_type: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(b"\x00")
    h.update(event_type.encode("utf-8"))
    h.update(b":")
    h.update(canonical_json(payload))
    return h.hexdigest()


def hash_internal_node(left_hex: str, right_hex: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)).hexdigest()


def verify_inclusion_proof(leaf_hash: str, leaf_index: int, tree_size: int, expected_root: str, audit_path: list[str]) -> bool:
    """RFC 9162 section 2.1.3.2 inclusion proof verifier."""
    try:
        if not isinstance(leaf_index, int) or not isinstance(tree_size, int) or not 0 <= leaf_index < tree_size:
            return False
        fn, sn, r = leaf_index, tree_size - 1, leaf_hash
        for p in audit_path:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                r = hash_internal_node(p, r)
                if not fn & 1:
                    while not fn & 1 and fn != 0:
                        fn >>= 1
                        sn >>= 1
            else:
                r = hash_internal_node(r, p)
            fn >>= 1
            sn >>= 1
        return sn == 0 and r.lower() == str(expected_root).lower()
    except (ValueError, TypeError):
        return False


CHECKPOINT_VERSION = "ccpp-log-v1"
CHECKPOINT_FIELDS = ("v", "tree_size", "root_hash", "timestamp", "log_public_key")


def verify_checkpoint(checkpoint: Any, log_public_key_hex: str) -> bool:
    """A transparency-log checkpoint signed by the pinned log key (see apps/api transparency_log)."""
    if not isinstance(checkpoint, dict) or checkpoint.get("v") != CHECKPOINT_VERSION:
        return False
    if checkpoint.get("log_public_key") != log_public_key_hex or any(f not in checkpoint for f in CHECKPOINT_FIELDS):
        return False
    if not isinstance(checkpoint["tree_size"], int) or checkpoint["tree_size"] < 1:
        return False
    msg = canonical_json({f: checkpoint[f] for f in CHECKPOINT_FIELDS})
    return verify_signature(log_public_key_hex, msg, str(checkpoint.get("signature", "")))


def verify_consistency_proof(first_size: int, second_size: int, first_root: str, second_root: str, proof: list[str]) -> bool:
    """RFC 9162 section 2.1.4.2. Same algorithm as apps/api transparency_log."""
    try:
        m, n = first_size, second_size
        if m < 0 or n < 0 or m > n:
            return False
        if m == 0:
            return len(proof) == 0
        if m == n:
            return len(proof) == 0 and first_root.lower() == second_root.lower()
        fn, sn = m - 1, n - 1
        while fn & 1:
            fn >>= 1
            sn >>= 1
        p = list(proof)
        if fn == 0:
            fr = sr = first_root
        else:
            if not p:
                return False
            fr = sr = p[0]
            p = p[1:]
        for c in p:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                fr = hash_internal_node(c, fr)
                sr = hash_internal_node(c, sr)
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
            else:
                sr = hash_internal_node(sr, c)
            fn >>= 1
            sn >>= 1
        return sn == 0 and fr.lower() == first_root.lower() and sr.lower() == second_root.lower()
    except (ValueError, TypeError, IndexError):
        return False
