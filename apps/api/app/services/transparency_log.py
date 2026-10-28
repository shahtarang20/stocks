"""Append-only Merkle Transparency Log for Proof-of-Privacy (ADR-0005).

Tree hashing and audit paths follow RFC 6962 section 2.1 exactly (the same construction as
Certificate Transparency): ``MTH`` splits at the largest power of two below ``n``, and proofs are
verified with the RFC 9162 section 2.1.3.2 algorithm, which binds the leaf index AND the tree size,
so a proof for one leaf cannot be passed off as a proof for another.

Leaf hash: ``SHA-256(0x00 || event_type || ":" || canonical_json(payload))``. A verifier recomputes
it from the payload, so a proof always says what was logged, not just that "something" was.

Entries: IMAGE_HASH, ATTESTATION_VERDICT, KEY_RELEASE, WIPE_RECEIPT, TAMPER_EVIDENCE.
Not built yet: periodic anchoring of the root on-chain (the ``anchored_*`` columns stay empty).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.privacy import TransparencyLogLeaf

EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


def canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_leaf(event_type: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(b"\x00")
    h.update(event_type.encode("utf-8"))
    h.update(b":")
    h.update(canonical_json_bytes(payload))
    return h.hexdigest()


def hash_internal_node(left_hex: str, right_hex: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)).hexdigest()


def _split(n: int) -> int:
    """Largest power of two strictly less than n (n >= 2)."""
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def compute_merkle_root(leaf_hashes: list[str]) -> str:
    """RFC 6962 MTH over already-hashed leaves."""
    n = len(leaf_hashes)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaf_hashes[0]
    k = _split(n)
    return hash_internal_node(compute_merkle_root(leaf_hashes[:k]), compute_merkle_root(leaf_hashes[k:]))


def _path(m: int, leaves: list[str]) -> list[str]:
    n = len(leaves)
    if n <= 1:
        return []
    k = _split(n)
    if m < k:
        return _path(m, leaves[:k]) + [compute_merkle_root(leaves[k:])]
    return _path(m - k, leaves[k:]) + [compute_merkle_root(leaves[:k])]


def generate_inclusion_proof(leaf_hashes: list[str], leaf_index: int) -> dict[str, Any]:
    n = len(leaf_hashes)
    if not 0 <= leaf_index < n:
        raise ValueError(f"leaf index {leaf_index} out of range (tree size {n})")
    return {
        "leaf_index": leaf_index,
        "tree_size": n,
        "leaf_hash": leaf_hashes[leaf_index],
        "root_hash": compute_merkle_root(leaf_hashes),
        "audit_path": _path(leaf_index, leaf_hashes),
    }


def verify_inclusion_proof(leaf_hash: str, leaf_index: int, tree_size: int, expected_root: str, audit_path: list[str]) -> bool:
    """RFC 9162 section 2.1.3.2."""
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


def _subproof(m: int, leaves: list[str], b: bool) -> list[str]:
    """RFC 6962 Section 2.1.2 SUBPROOF."""
    n = len(leaves)
    if m == n:
        if b:
            return []
        return [compute_merkle_root(leaves)]
    k = _split(n)
    if m <= k:
        return _subproof(m, leaves[:k], b) + [compute_merkle_root(leaves[k:])]
    else:
        return _subproof(m - k, leaves[k:], False) + [compute_merkle_root(leaves[:k])]


def generate_consistency_proof(leaves: list[str], from_size: int, to_size: int) -> list[str]:
    """RFC 6962 Section 2.1.2 consistency proof between tree sizes from_size and to_size."""
    if not (0 < from_size <= to_size <= len(leaves)):
        raise ValueError(f"invalid tree sizes: from={from_size}, to={to_size}, total={len(leaves)}")
    if from_size == to_size:
        return []
    return _subproof(from_size, leaves[:to_size], True)


def verify_consistency_proof(
    first_size: int,
    second_size: int,
    first_root: str,
    second_root: str,
    proof: list[str],
) -> bool:
    """RFC 9162 Section 2.1.4.2 consistency proof verifier."""
    try:
        m, n = first_size, second_size
        if m < 0 or n < 0 or m > n:
            return False
        if m == 0:
            return len(proof) == 0
        if m == n:
            return len(proof) == 0 and first_root.lower() == second_root.lower()

        fn = m - 1
        sn = n - 1
        while fn & 1:
            fn >>= 1
            sn >>= 1

        p = list(proof)
        if fn == 0:
            fr = first_root
            sr = first_root
        else:
            if not p:
                return False
            fr = p[0]
            sr = p[0]
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


async def get_consistency_proof(db: AsyncSession, from_size: int, to_size: int) -> dict[str, Any]:
    """Calculate an RFC 6962 consistency proof between two log sizes."""
    leaves = await _all_leaf_hashes(db, upto=to_size)
    if not (0 < from_size <= to_size <= len(leaves)):
        raise ValueError(f"invalid tree sizes: from={from_size}, to={to_size}, available={len(leaves)}")
    root1 = compute_merkle_root(leaves[:from_size])
    root2 = compute_merkle_root(leaves[:to_size])
    proof = generate_consistency_proof(leaves, from_size, to_size)
    return {
        "from_size": from_size,
        "to_size": to_size,
        "first_root": root1,
        "second_root": root2,
        "consistency_path": proof,
    }


async def _lock_log(db: AsyncSession) -> None:
    """Serialise appends. Two concurrent appends would otherwise pick the same next index."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('transparency_log', 0))"))


async def _all_leaf_hashes(db: AsyncSession, upto: int | None = None) -> list[str]:
    q = select(TransparencyLogLeaf.leaf_hash).order_by(TransparencyLogLeaf.leaf_index.asc())
    if upto is not None:
        q = q.where(TransparencyLogLeaf.leaf_index < upto)
    return list((await db.execute(q)).scalars().all())


async def append_transparency_log_entry(db: AsyncSession, event_type: str, payload: dict) -> TransparencyLogLeaf:
    """Append one entry and record the root of the tree that now includes it.

    Recomputes the root over every leaf: O(n) per append. Fine at today's volume; replace with
    stored subtree hashes before the log reaches millions of entries.
    """
    await _lock_log(db)
    hashes = await _all_leaf_hashes(db)
    leaf_hash = hash_leaf(event_type, payload)
    leaf = TransparencyLogLeaf(
        leaf_index=len(hashes),
        event_type=event_type,
        leaf_hash=leaf_hash,
        payload=payload,
        merkle_root=compute_merkle_root(hashes + [leaf_hash]),
    )
    db.add(leaf)
    await db.flush()
    return leaf


async def get_inclusion_proofs(db: AsyncSession, leaf_indices: list[int]) -> tuple[str, int, dict[int, dict[str, Any]]]:
    """Proofs for several leaves against ONE snapshot of the log, so they all share one root.

    Each proof carries the leaf's event type and payload, so a verifier can recompute the hash.
    """
    res = await db.execute(select(TransparencyLogLeaf).order_by(TransparencyLogLeaf.leaf_index.asc()))
    leaves = list(res.scalars().all())
    hashes = [leaf.leaf_hash for leaf in leaves]
    proofs: dict[int, dict[str, Any]] = {}
    for idx in leaf_indices:
        proof = generate_inclusion_proof(hashes, idx)
        proof["event_type"] = leaves[idx].event_type
        proof["payload"] = leaves[idx].payload
        proofs[idx] = proof
    return compute_merkle_root(hashes), len(hashes), proofs


async def get_inclusion_proof_for_leaf(db: AsyncSession, leaf_index: int) -> dict[str, Any]:
    _, _, proofs = await get_inclusion_proofs(db, [leaf_index])
    return proofs[leaf_index]


# --------------------------------------------------------------------------- signed checkpoints

CHECKPOINT_VERSION = "ccpp-log-v1"
CHECKPOINT_FIELDS = ("v", "tree_size", "root_hash", "timestamp", "log_public_key")


def checkpoint_signing_bytes(checkpoint: dict[str, Any]) -> bytes:
    """What a checkpoint signature covers. Must match apps/guardian/crypto.py."""
    return canonical_json_bytes({f: checkpoint[f] for f in CHECKPOINT_FIELDS})


def sign_checkpoint(tree_size: int, root_hash: str, timestamp: str) -> dict[str, Any]:
    from app.services.privacy_crypto import sign
    from app.services.privacy_keys import log_public_key, log_signing_key

    cp = {"v": CHECKPOINT_VERSION, "tree_size": tree_size, "root_hash": root_hash, "timestamp": timestamp, "log_public_key": log_public_key()}
    cp["signature"] = sign(log_signing_key(), checkpoint_signing_bytes(cp))
    return cp


async def get_proofs_at_size(
    db: AsyncSession, leaf_index: int, tree_size: int, from_size: int = 0
) -> dict[str, Any]:
    """Inclusion proof for ``leaf_index`` and a consistency proof from ``from_size``, both against
    the SAME tree of ``tree_size`` leaves, plus a signed checkpoint for that tree.

    Pinning the size makes the bundle immune to appends that happen while it is built.
    """
    from app.db.base import utcnow

    hashes = await _all_leaf_hashes(db, upto=tree_size)
    if len(hashes) != tree_size or not 0 <= leaf_index < tree_size:
        raise ValueError(f"leaf {leaf_index} / size {tree_size} not available (log has {len(hashes)})")
    if not 0 <= from_size <= tree_size:
        raise ValueError(f"invalid consistency base {from_size} for size {tree_size}")
    root = compute_merkle_root(hashes)
    cp = sign_checkpoint(tree_size, root, utcnow().isoformat())
    bundle = {
        "checkpoint": cp,
        "inclusion_proof": generate_inclusion_proof(hashes, leaf_index),
        "consistency_proof": generate_consistency_proof(hashes, from_size, tree_size) if 0 < from_size < tree_size else [],
        "consistency_from_root": compute_merkle_root(hashes[:from_size]) if from_size > 0 else None,
    }
    from app.services.log_witness import gather_witness_cosignatures
    cosignatures = await gather_witness_cosignatures(cp, db=db, tree_size=tree_size)
    bundle["witness_cosignatures"] = cosignatures
    cp["witness_cosignatures"] = cosignatures
    return bundle


async def get_signed_checkpoint(db: AsyncSession) -> dict[str, Any]:
    from app.db.base import utcnow

    hashes = await _all_leaf_hashes(db)
    return sign_checkpoint(len(hashes), compute_merkle_root(hashes), utcnow().isoformat())


async def get_latest_transparency_root(db: AsyncSession) -> str:
    res = await db.execute(select(TransparencyLogLeaf.merkle_root).order_by(desc(TransparencyLogLeaf.leaf_index)).limit(1))
    return res.scalar_one_or_none() or EMPTY_ROOT


async def get_log_size(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(TransparencyLogLeaf.id)))).scalar_one())
