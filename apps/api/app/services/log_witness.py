"""Witness Client and Cosignature Aggregator for Transparency Log Checkpoints (WP-E).

Gathers Ed25519 cosignatures from independent witness services configured via PRIVACY_WITNESSES.
Fails closed if the number of valid cosignatures is below PRIVACY_WITNESS_THRESHOLD.

Two users:
- key release asks for cosignatures on the CURRENT checkpoint (guardians require them);
- the worker (``run_witness_cycle``) has the latest checkpoint cosigned periodically and stores the
  cosignatures on its last leaf. A receipt then carries the smallest witnessed checkpoint at or
  above its own tree size plus a consistency proof to it (``build_witness_section_for_receipt``).
  A witness never cosigns a size below one it has seen, so a receipt cannot be cosigned later at
  its own (older) size; this is why receipts use a later witnessed checkpoint, like anchoring.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.privacy import TransparencyLogLeaf
from app.services.privacy_crypto import canonical_json, verify

WITNESS_STATEMENT_VERSION = "ccpp-witness-v1"

logger = logging.getLogger("privacy.log_witness")


class WitnessError(Exception):
    pass


class WitnessThresholdNotMetError(WitnessError):
    pass


def witness_statement(tree_size: int, root_hash: str, log_public_key: str, witness: str) -> bytes:
    """What a witness cosignature covers. Must match apps/witness, apps/guardian and ccverify."""
    return canonical_json({
        "v": WITNESS_STATEMENT_VERSION,
        "tree_size": tree_size,
        "root_hash": str(root_hash).lower(),
        "log_public_key": str(log_public_key).lower(),
        "witness": witness,
    })


def parse_configured_witnesses() -> dict[str, dict[str, str]]:
    raw = settings.PRIVACY_WITNESSES
    if not raw or raw.strip() == "{}":
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


async def gather_witness_cosignatures(
    checkpoint: dict[str, Any],
    *,
    db: AsyncSession,
    tree_size: int,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Query configured witnesses for cosignatures on the given checkpoint."""
    from app.services.transparency_log import generate_consistency_proof, _all_leaf_hashes

    threshold = settings.PRIVACY_WITNESS_THRESHOLD
    witnesses = parse_configured_witnesses()

    if threshold <= 0 or not witnesses:
        return []

    valid_cosignatures: list[dict[str, Any]] = []
    leaves_cache: list[str] | None = None

    async def _get_leaves() -> list[str]:
        nonlocal leaves_cache
        if leaves_cache is None:
            leaves_cache = await _all_leaf_hashes(db, upto=tree_size)
        return leaves_cache

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=4.0)
        should_close_client = True

    try:
        for name, info in witnesses.items():
            if not isinstance(info, dict):
                continue
            base_url = str(info.get("url", "")).rstrip("/")
            pub_key = str(info.get("public_key", "")).lower()
            if not base_url or not pub_key:
                continue

            try:
                # 1. Ask witness what tree size it has seen
                state_resp = await client.get(f"{base_url}/state")
                if state_resp.status_code != 200:
                    logger.warning("witness %s /state failed with HTTP %d", name, state_resp.status_code)
                    continue
                seen_size = int(state_resp.json().get("tree_size") or 0)

                # 2. Compute consistency proof from seen_size to tree_size
                if seen_size == 0 or seen_size == tree_size:
                    consistency_proof = []
                elif 0 < seen_size < tree_size:
                    leaves = await _get_leaves()
                    consistency_proof = generate_consistency_proof(leaves, seen_size, tree_size)
                else:
                    logger.warning("witness %s is ahead of current tree (seen=%d, current=%d)", name, seen_size, tree_size)
                    continue

                # 3. Request cosignature
                cosign_resp = await client.post(
                    f"{base_url}/cosign",
                    json={"checkpoint": checkpoint, "consistency_proof": consistency_proof},
                )
                if cosign_resp.status_code != 200:
                    logger.warning("witness %s refused cosigning (HTTP %d: %s)", name, cosign_resp.status_code, cosign_resp.text)
                    continue

                res_data = cosign_resp.json()
                sig = str(res_data.get("signature", ""))

                # 4. Verify the cosignature locally with the witness's pinned public key
                stmt = witness_statement(checkpoint["tree_size"], checkpoint["root_hash"], checkpoint.get("log_public_key", ""), name)
                if verify(pub_key, stmt, sig):
                    valid_cosignatures.append({
                        "witness": name,
                        "signature": sig,
                        "tree_size": checkpoint["tree_size"],
                        "root_hash": checkpoint["root_hash"],
                        "log_public_key": str(checkpoint.get("log_public_key", "")).lower(),
                    })
                else:
                    logger.error("witness %s returned invalid signature", name)

            except Exception as exc:
                logger.warning("failed to contact witness %s: %s", name, exc)

    finally:
        if should_close_client:
            await client.aclose()

    if len(valid_cosignatures) < threshold:
        raise WitnessThresholdNotMetError(
            f"insufficient witness cosignatures: gathered {len(valid_cosignatures)}, required {threshold}"
        )

    return valid_cosignatures


async def run_witness_cycle(db: AsyncSession) -> dict[str, Any] | None:
    """Have the latest checkpoint cosigned and store the cosignatures on its last leaf.

    Called by the worker. Reads first, ends the read transaction, then talks to the witnesses, so
    no database transaction is held open across the network calls. Raises WitnessThresholdNotMetError
    when too few witnesses cosign (nothing is stored; the next cycle tries again)."""
    from app.db.base import utcnow
    from app.services.transparency_log import sign_checkpoint

    if settings.PRIVACY_WITNESS_THRESHOLD <= 0:
        return None
    latest = (
        await db.execute(select(TransparencyLogLeaf).order_by(desc(TransparencyLogLeaf.leaf_index)).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return {"status": "empty", "witnessed": False, "size": 0}
    size = latest.leaf_index + 1
    if latest.witness_cosignatures:
        return {"status": "already_witnessed", "witnessed": False, "size": size}
    checkpoint = sign_checkpoint(size, latest.merkle_root, utcnow().isoformat())
    await db.commit()  # end the read transaction before the network calls
    cosignatures = await gather_witness_cosignatures(checkpoint, db=db, tree_size=size)
    await db.commit()  # gather read the leaf hashes; end that transaction too
    latest.witness_cosignatures = {
        "checkpoint": checkpoint,
        "cosignatures": [{"witness": c["witness"], "signature": c["signature"]} for c in cosignatures],
    }
    await db.commit()
    return {"status": "witnessed", "witnessed": True, "size": size, "witnesses": [c["witness"] for c in cosignatures]}


async def build_witness_section_for_receipt(db: AsyncSession, receipt_tree_size: int) -> dict[str, Any] | None:
    """The smallest witnessed checkpoint whose size >= the receipt's tree size, with a consistency
    proof from the receipt's tree to it. Read-only; never calls a witness."""
    from app.services.transparency_log import get_consistency_proof

    if receipt_tree_size <= 0:
        return None
    leaf = (
        await db.execute(
            select(TransparencyLogLeaf)
            .where(
                TransparencyLogLeaf.witness_cosignatures.isnot(None),
                (TransparencyLogLeaf.leaf_index + 1) >= receipt_tree_size,
            )
            .order_by(TransparencyLogLeaf.leaf_index.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if leaf is None:
        return None
    witnessed = leaf.witness_cosignatures or {}
    checkpoint = witnessed.get("checkpoint") or {}
    size = leaf.leaf_index + 1
    consistency: list[str] = []
    if receipt_tree_size < size:
        consistency = (await get_consistency_proof(db, receipt_tree_size, size))["consistency_path"]
    return {
        "checkpoint": {k: v for k, v in checkpoint.items() if k != "witness_cosignatures"},
        "consistency_proof": consistency,
        "cosignatures": list(witnessed.get("cosignatures") or []),
    }
