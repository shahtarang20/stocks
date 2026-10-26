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


async def get_latest_transparency_root(db: AsyncSession) -> str:
    res = await db.execute(select(TransparencyLogLeaf.merkle_root).order_by(desc(TransparencyLogLeaf.leaf_index)).limit(1))
    return res.scalar_one_or_none() or EMPTY_ROOT


async def get_log_size(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(TransparencyLogLeaf.id)))).scalar_one())
