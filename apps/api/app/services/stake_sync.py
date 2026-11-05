"""Stake sync: ComputeStakeVault (on-chain) -> ``provider_machines.stake_bonded`` (A1 / P1).

The chain is the only source of stake when this is enabled (R4). Rules:

* The vault is read ``STAKE_SYNC_CONFIRMATIONS`` blocks behind the head so a reorganisation
  can never grant stake.
* Only ``bondedAmount`` counts. Unbonding stake does NOT count. Wei -> tokens rounds DOWN to
  2 decimals.
* The vault's ``provider`` address must be one of the machine owner's linked wallets on the
  vault's chain, otherwise the machine is ``stake_unverified`` and its stake is 0 (nobody can
  claim stake someone else deposited).
* A vault ban sets ``is_banned``; it never un-bans.
* "I could not read the chain" keeps the previous value and records the error. It never zeroes
  or raises stake (fail closed on grants, no punishment on outages).

The chain client is injectable (tests use a fake; production uses :class:`JsonRpcStakeClient`).

External interface: ``ComputeStakeVault.stakes(bytes32)`` public mapping getter, returning
``(address provider, uint256 bondedAmount, uint256 unbondingAmount, uint256 unbondingAvailableAt,
bool isBanned)`` (contracts/src/ComputeStakeVault.sol). JSON-RPC ``eth_call`` / ``eth_chainId`` /
``eth_blockNumber``: https://ethereum.org/en/developers/docs/apis/json-rpc/ (checked 2026-10-29).
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.provider import Provider, ProviderMachine
from app.models.reconciliation import StakeSyncRun
from app.models.wallet import LinkedWallet
from app.services.audit import record_audit
from app.services.chain_reconciler import ChainClientError, JsonRpcChainClient, _selector
from app.services.settlement import machine_id_to_bytes32

__all__ = ["VaultStake", "StakeChainClient", "JsonRpcStakeClient", "wei_to_tokens", "run_stake_sync_cycle"]

_ZERO_ADDRESS = "0x" + "00" * 20
_WEI = decimal.Decimal(10) ** 18
_CENT = decimal.Decimal("0.01")


@dataclass(frozen=True)
class VaultStake:
    provider: str  # lower-case 0x address; zero address = machine not registered in the vault
    bonded_wei: int
    unbonding_wei: int
    unbonding_available_at: int
    is_banned: bool


class StakeChainClient(Protocol):
    async def chain_id(self) -> int: ...

    async def latest_block(self) -> int: ...

    async def read_stake(self, vault_address: str, machine_bytes32: str, block_number: int) -> VaultStake: ...


class JsonRpcStakeClient(JsonRpcChainClient):
    """Reads the vault over plain JSON-RPC (reuses the reconciler's transport and error rules)."""

    STAKES_SELECTOR = _selector("stakes(bytes32)")

    def __init__(self, rpc_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(rpc_url if rpc_url is not None else settings.STAKE_SYNC_RPC_URL, **kwargs)

    async def chain_id(self) -> int:
        result = await self._rpc("eth_chainId", [])
        try:
            return int(result, 16)
        except (TypeError, ValueError) as exc:
            raise ChainClientError("chainid_malformed_response") from exc

    async def read_stake(self, vault_address: str, machine_bytes32: str, block_number: int) -> VaultStake:
        data = self.STAKES_SELECTOR + machine_bytes32.lower().removeprefix("0x").rjust(64, "0")
        result = await self._rpc("eth_call", [{"to": vault_address, "data": data}, hex(block_number)])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ChainClientError("stakes_malformed_response")
        words = self._words(result)
        if len(words) < 5:
            # "0x" = no contract code at the address: the vault is not deployed there.
            raise ChainClientError("vault_not_deployed_at_address")
        return VaultStake(
            provider="0x" + f"{words[0]:064x}"[-40:],
            bonded_wei=words[1],
            unbonding_wei=words[2],
            unbonding_available_at=words[3],
            is_banned=words[4] != 0,
        )


def wei_to_tokens(wei: int) -> decimal.Decimal:
    """18-decimal token units -> tokens, rounded DOWN to 2 decimals."""
    return (decimal.Decimal(int(wei)) / _WEI).quantize(_CENT, rounding=decimal.ROUND_DOWN)


async def run_stake_sync_cycle(db: AsyncSession, client: StakeChainClient | None = None) -> dict[str, Any]:
    """One sweep. Writes a :class:`StakeSyncRun` and updates machines. Never raises on chain errors."""
    run = StakeSyncRun(chain_id=settings.STAKE_SYNC_CHAIN_ID, started_at=utcnow(), ok=False)
    db.add(run)
    detail: dict[str, Any] = {"changes": [], "errors": []}
    errors = 0
    changed = 0
    checked = 0

    def _finish() -> dict[str, Any]:
        run.finished_at = utcnow()
        run.machines_checked = checked
        run.machines_changed = changed
        run.error_count = errors
        run.detail = detail
        run.ok = errors == 0
        return {"checked": checked, "changed": changed, "errors": errors, "ok": run.ok}

    try:
        client = client or JsonRpcStakeClient()
        vault = settings.STAKE_SYNC_VAULT_ADDRESS
        if await client.chain_id() != settings.STAKE_SYNC_CHAIN_ID:
            raise ChainClientError("rpc_chain_id_mismatch")
        block = max(0, await client.latest_block() - max(0, settings.STAKE_SYNC_CONFIRMATIONS))
        run.block_number = block
    except ChainClientError as exc:
        errors += 1
        detail["errors"].append({"scope": "chain", "error": str(exc)})
        return _finish()

    rows = (
        await db.execute(
            select(ProviderMachine, Provider.user_id)
            .join(Provider, Provider.id == ProviderMachine.provider_id)
            .with_for_update(of=ProviderMachine)
        )
    ).all()
    wallets: dict[uuid.UUID, set[str]] = {}
    user_ids = {uid for _, uid in rows}
    if user_ids:
        for uid, addr in (
            await db.execute(
                select(LinkedWallet.user_id, LinkedWallet.address).where(
                    LinkedWallet.chain_id == settings.STAKE_SYNC_CHAIN_ID,
                    LinkedWallet.user_id.in_(user_ids),
                )
            )
        ).all():
            wallets.setdefault(uid, set()).add(addr.lower())

    for machine, user_id in rows:
        checked += 1
        try:
            vs = await client.read_stake(vault, machine_id_to_bytes32(machine.id), block)
        except ChainClientError as exc:
            # Keep the previous value; record the error.
            errors += 1
            detail["errors"].append({"machine_id": str(machine.id), "error": str(exc)})
            continue

        registered = vs.provider != _ZERO_ADDRESS
        verified = registered and vs.provider in wallets.get(user_id, set())
        new_stake = wei_to_tokens(vs.bonded_wei) if verified else decimal.Decimal("0.00")
        new_unverified = registered and not verified

        old_stake = machine.stake_bonded or decimal.Decimal("0")
        change: dict[str, Any] = {}
        if old_stake != new_stake:
            change["stake"] = [str(old_stake), str(new_stake)]
            machine.stake_bonded = new_stake
        if bool(machine.stake_unverified) != new_unverified:
            change["stake_unverified"] = new_unverified
            machine.stake_unverified = new_unverified
        if registered and vs.is_banned and not machine.is_banned:
            change["is_banned"] = True
            machine.is_banned = True  # never un-banned automatically
        machine.stake_synced_at = utcnow()

        if change:
            changed += 1
            detail["changes"].append({"machine_id": str(machine.id), **change})
            await record_audit(
                db,
                action="privacy.stake.sync",
                outcome="success",
                resource_type="machine",
                resource_id=str(machine.id),
                reason=f"block={block} {change}",
            )

    return _finish()
