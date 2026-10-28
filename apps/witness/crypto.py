"""Cryptographic primitives for the log witness service (ADR-0005 WP21)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

CHECKPOINT_VERSION = "ccpp-log-v1"
CHECKPOINT_FIELDS = ("v", "tree_size", "root_hash", "timestamp", "log_public_key")

WITNESS_STATEMENT_VERSION = "ccpp-witness-v1"
WITNESS_STATEMENT_FIELDS = ("v", "tree_size", "root_hash", "log_public_key", "witness")


class WitnessCryptoError(Exception):
    pass


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def is_hex32(value: Any) -> bool:
    try:
        return isinstance(value, str) and len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def ed25519_public_from_private(private_seed_hex: str) -> str:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_seed_hex))
    return priv.public_key().public_bytes_raw().hex()


def sign_ed25519(private_seed_hex: str, data: bytes) -> str:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_seed_hex))
    return priv.sign(data).hex()


def verify_signature(public_key_hex: str, data: bytes, signature_hex: str) -> bool:
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_checkpoint(checkpoint: Any, log_public_key_hex: str) -> bool:
    """A transparency-log checkpoint signed by the pinned log key."""
    if not isinstance(checkpoint, dict) or checkpoint.get("v") != CHECKPOINT_VERSION:
        return False
    if str(checkpoint.get("log_public_key", "")).lower() != log_public_key_hex.lower():
        return False
    if any(f not in checkpoint for f in CHECKPOINT_FIELDS):
        return False
    if not isinstance(checkpoint["tree_size"], int) or checkpoint["tree_size"] < 1:
        return False
    msg = canonical_json({f: checkpoint[f] for f in CHECKPOINT_FIELDS})
    return verify_signature(log_public_key_hex, msg, str(checkpoint.get("signature", "")))


def hash_internal_node(left_hex: str, right_hex: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)).hexdigest()


def verify_consistency_proof(
    first_size: int,
    second_size: int,
    first_root: str,
    second_root: str,
    proof: list[str],
) -> bool:
    """RFC 9162 section 2.1.4.2 consistency proof verifier."""
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
