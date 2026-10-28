"""Transparency log on-chain anchor service (ADR-0005 WP-C / WP9).

Periodically records the Merkle root and leaf count of the transparency log on
the blockchain via ``ComputeStakeVault.commitMerkleRoot(bytes32,uint256)``.

Features:
- Enforces monotonically increasing sizes (never anchors a smaller size).
- Idempotent: re-checking the same size is a no-op without sending a transaction.
- Stores ``anchored_tx_hash`` and ``anchored_block_number`` on the leaf at index size-1.
- Provides receipt anchor sections with consistency proofs from the receipt tree to the anchor tree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from eth_account import Account
from eth_utils import to_checksum_address
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.privacy import TransparencyLogLeaf
from app.services.transparency_log import (
    compute_merkle_root,
    generate_consistency_proof,
    get_consistency_proof,
)

logger = logging.getLogger("computecoin.log_anchor")

COMMIT_MERKLE_ROOT_SELECTOR = "08921672"  # keccak("commitMerkleRoot(bytes32,uint256)")[:4]


class AnchorError(Exception):
    """Raised when on-chain anchoring fails."""


class LogAnchorService:
    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        chain_id: int | None = None,
        vault_address: str | None = None,
        private_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.rpc_url = rpc_url if rpc_url is not None else settings.PRIVACY_ANCHOR_RPC_URL
        self.chain_id = chain_id if chain_id is not None else settings.PRIVACY_ANCHOR_CHAIN_ID
        self.vault_address = (
            vault_address if vault_address is not None else settings.PRIVACY_ANCHOR_VAULT_ADDRESS
        )
        self.private_key = (
            private_key if private_key is not None else settings.PRIVACY_ANCHOR_PRIVATE_KEY
        )
        self._external_client = http_client

    async def _rpc_call(self, client: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        try:
            resp = await client.post(self.rpc_url, json=payload, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise AnchorError(f"RPC call {method} failed: {exc}") from exc

        if "error" in data:
            raise AnchorError(f"RPC {method} returned error: {data['error']}")
        return data.get("result")

    async def anchor_step(self, db: AsyncSession) -> dict[str, Any]:
        """Check current log size and anchor on-chain if new leaves have been appended."""
        res = await db.execute(
            select(TransparencyLogLeaf).order_by(desc(TransparencyLogLeaf.leaf_index)).limit(1)
        )
        latest_leaf = res.scalar_one_or_none()
        if latest_leaf is None:
            return {"status": "empty", "anchored": False, "size": 0}

        current_size = latest_leaf.leaf_index + 1

        res_anchored = await db.execute(
            select(TransparencyLogLeaf)
            .where(TransparencyLogLeaf.anchored_tx_hash.isnot(None))
            .order_by(desc(TransparencyLogLeaf.leaf_index))
            .limit(1)
        )
        last_anchored = res_anchored.scalar_one_or_none()
        last_anchored_size = (last_anchored.leaf_index + 1) if last_anchored else 0

        if current_size <= last_anchored_size:
            return {
                "status": "no_new_leaves",
                "anchored": False,
                "size": current_size,
                "last_anchored_size": last_anchored_size,
            }

        root_hash = latest_leaf.merkle_root
        clean_root = root_hash.removeprefix("0x").lower()
        if len(clean_root) != 64:
            raise AnchorError(f"invalid root hash format: {root_hash}")

        calldata = (
            "0x"
            + COMMIT_MERKLE_ROOT_SELECTOR
            + clean_root
            + hex(current_size)[2:].rjust(64, "0")
        )

        if not self.private_key:
            raise AnchorError("PRIVACY_ANCHOR_PRIVATE_KEY is not configured")
        if not self.vault_address:
            raise AnchorError("PRIVACY_ANCHOR_VAULT_ADDRESS is not configured")
        if not self.rpc_url:
            raise AnchorError("PRIVACY_ANCHOR_RPC_URL is not configured")

        account = Account.from_key(self.private_key)
        sender_address = account.address
        vault_checksum = to_checksum_address(self.vault_address)

        def _int(value: Any) -> int:
            return int(value, 16) if isinstance(value, str) else int(value)

        async def _execute(client: httpx.AsyncClient) -> tuple[str, int]:
            # 0. Sign only for the chain we were configured for (a mis-pointed RPC gets nothing).
            rpc_chain = _int(await self._rpc_call(client, "eth_chainId", []))
            if rpc_chain != int(self.chain_id):
                raise AnchorError(f"RPC is chain {rpc_chain}, configured chain is {self.chain_id}")

            # 1. Nonce. A previous anchor still pending (e.g. it timed out waiting for its receipt)
            #    means we wait for it instead of stacking another transaction on top.
            nonce = _int(await self._rpc_call(client, "eth_getTransactionCount", [sender_address, "pending"]))
            mined = _int(await self._rpc_call(client, "eth_getTransactionCount", [sender_address, "latest"]))
            if nonce > mined:
                raise AnchorError(f"a previous anchor transaction from {sender_address} is still pending")

            # 2. Gas price
            gas_price_hex = await self._rpc_call(client, "eth_gasPrice", [])
            gas_price = int(gas_price_hex, 16) if isinstance(gas_price_hex, str) else int(gas_price_hex)
            if gas_price == 0:
                gas_price = 1_000_000_000  # 1 gwei fallback

            # 3. Estimate gas. A failed estimate means the call would revert (no anchor role, a
            #    smaller size than already anchored, wrong address): send nothing rather than pay
            #    for a transaction that is known to fail.
            est = await self._rpc_call(
                client,
                "eth_estimateGas",
                [{"from": sender_address, "to": vault_checksum, "data": calldata}],
            )
            gas_limit = int(_int(est) * 1.3)

            tx = {
                "to": vault_checksum,
                "data": calldata,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
                "value": 0,
            }

            signed_tx = account.sign_transaction(tx)
            raw_hex = getattr(signed_tx, "raw_transaction", None) or getattr(
                signed_tx, "rawTransaction", None
            )
            raw_tx_str = raw_hex.hex() if hasattr(raw_hex, "hex") else str(raw_hex)
            if not raw_tx_str.startswith("0x"):
                raw_tx_str = "0x" + raw_tx_str

            tx_hash = await self._rpc_call(client, "eth_sendRawTransaction", [raw_tx_str])

            # Wait for receipt
            receipt = None
            for _ in range(30):
                receipt = await self._rpc_call(client, "eth_getTransactionReceipt", [tx_hash])
                if receipt is not None:
                    break
                await asyncio.sleep(0.5)

            if receipt is None:
                raise AnchorError(f"timeout waiting for anchor receipt for tx {tx_hash}")

            status_val = receipt.get("status")
            if status_val in ("0x0", 0, "0"):
                raise AnchorError(f"anchor tx {tx_hash} reverted on-chain")

            block_num_raw = receipt.get("blockNumber", 0)
            block_number = (
                int(block_num_raw, 16) if isinstance(block_num_raw, str) else int(block_num_raw)
            )
            return tx_hash, block_number

        if self._external_client:
            tx_hash, block_number = await _execute(self._external_client)
        else:
            async with httpx.AsyncClient() as client:
                tx_hash, block_number = await _execute(client)

        latest_leaf.anchored_tx_hash = tx_hash
        latest_leaf.anchored_block_number = block_number
        await db.flush()

        logger.info(
            "Anchored transparency log: size=%d root=%s tx=%s block=%d",
            current_size,
            root_hash,
            tx_hash,
            block_number,
        )

        return {
            "status": "anchored",
            "anchored": True,
            "size": current_size,
            "root": root_hash,
            "tx_hash": tx_hash,
            "block_number": block_number,
        }


async def run_anchor_cycle(db: AsyncSession) -> dict[str, Any] | None:
    """Invoked by the background worker. Handles execution and commit."""
    if not getattr(settings, "PRIVACY_ANCHOR_ENABLED", False):
        return None
    service = LogAnchorService()
    res = await service.anchor_step(db)
    if res.get("anchored"):
        await db.commit()
    return res


async def build_anchor_section_for_receipt(
    db: AsyncSession, receipt_tree_size: int, receipt_root: str
) -> dict[str, Any] | None:
    """Build the anchor section for a receipt at READ time.

    Finds the smallest anchored checkpoint whose size >= receipt_tree_size,
    and returns its details along with an RFC 6962 consistency proof.
    """
    res = await db.execute(
        select(TransparencyLogLeaf)
        .where(
            TransparencyLogLeaf.anchored_tx_hash.isnot(None),
            (TransparencyLogLeaf.leaf_index + 1) >= receipt_tree_size,
        )
        .order_by(TransparencyLogLeaf.leaf_index.asc())
        .limit(1)
    )
    anchored_leaf = res.scalar_one_or_none()
    if anchored_leaf is None:
        return None

    anchored_size = anchored_leaf.leaf_index + 1
    anchored_root = anchored_leaf.merkle_root

    consistency_proof: list[str] = []
    if receipt_tree_size < anchored_size:
        proof_data = await get_consistency_proof(db, receipt_tree_size, anchored_size)
        consistency_proof = proof_data.get("consistency_path", [])

    return {
        "tree_size": anchored_size,
        "merkle_root": anchored_root,
        "tx_hash": anchored_leaf.anchored_tx_hash,
        "block_number": anchored_leaf.anchored_block_number,
        "consistency_proof": consistency_proof,
    }
