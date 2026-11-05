"""The chain calls a payout needs (B1): nonce, gas price, broadcast, receipt, token transfer data.

Extends the existing dependency-free JSON-RPC client. The worker takes it as an injectable
``PayoutChain`` so tests can use a fake or a local Hardhat node.
"""

from __future__ import annotations

from typing import Any, Protocol

from eth_utils import keccak

from app.services.chain_reconciler import ChainClientError, JsonRpcChainClient, _selector

TRANSFER_SELECTOR = _selector("transfer(address,uint256)")


def transfer_calldata(to_address: str, amount_raw: int) -> str:
    to = to_address.lower().removeprefix("0x")
    if len(to) != 40 or amount_raw <= 0:
        raise ChainClientError("invalid_transfer_arguments")
    return TRANSFER_SELECTOR + to.rjust(64, "0") + format(int(amount_raw), "x").rjust(64, "0")


def tx_hash_of(raw_tx_hex: str) -> str:
    return "0x" + keccak(bytes.fromhex(raw_tx_hex.removeprefix("0x"))).hex()


class PayoutChain(Protocol):
    async def latest_block(self) -> int: ...
    async def token_balance(self, token_address: str, owner_address: str) -> int: ...
    async def nonce(self, address: str, block_tag: str = "pending") -> int: ...
    async def gas_price(self) -> int: ...
    async def send_raw(self, raw_tx_hex: str) -> str: ...
    async def receipt(self, tx_hash: str) -> dict[str, Any] | None: ...


class JsonRpcPayoutChain(JsonRpcChainClient):
    async def nonce(self, address: str, block_tag: str = "pending") -> int:
        result = await self._rpc("eth_getTransactionCount", [address, block_tag])
        try:
            return int(result, 16)
        except (TypeError, ValueError) as exc:
            raise ChainClientError("nonce_malformed_response") from exc

    async def gas_price(self) -> int:
        result = await self._rpc("eth_gasPrice", [])
        try:
            return int(result, 16)
        except (TypeError, ValueError) as exc:
            raise ChainClientError("gas_price_malformed_response") from exc

    async def send_raw(self, raw_tx_hex: str) -> str:
        """Broadcast. 'already known' / 'nonce too low' for OUR hash is not an error: the caller
        confirms through the receipt, so a re-broadcast after a crash is harmless."""
        try:
            return str(await self._rpc("eth_sendRawTransaction", [raw_tx_hex]))
        except ChainClientError as exc:
            msg = str(exc).lower()
            if "already known" in msg or "known transaction" in msg or "already imported" in msg:
                return tx_hash_of(raw_tx_hex)
            raise

    async def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        if result is None:
            return None
        try:
            return {"status": int(result["status"], 16), "block_number": int(result["blockNumber"], 16)}
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainClientError("receipt_malformed_response") from exc
