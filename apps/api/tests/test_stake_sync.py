"""A1 stake sync (vault -> provider_machines.stake_bonded) with a fake RPC client. WRITTEN, NOT YET RUN."""

from __future__ import annotations

import decimal
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.security import create_access_token, hash_password, hash_token
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.provider import Provider, ProviderMachine
from app.models.reconciliation import StakeSyncRun
from app.models.user import User, UserRole
from app.models.wallet import LinkedWallet
from app.services import stake_sync
from app.services.chain_reconciler import ChainClientError
from app.services.settlement import machine_id_to_bytes32
from app.services.stake_sync import VaultStake, run_stake_sync_cycle, wei_to_tokens

pytestmark = pytest.mark.asyncio
CHAIN = 56
VAULT = "0x" + "11" * 20
WEI = 10**18
ZERO = "0x" + "00" * 20


def _sm(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


class FakeVault:
    """Implements StakeChainClient. ``stakes`` maps machine bytes32 -> VaultStake or an exception."""

    def __init__(self, head=1000, chain=CHAIN):
        self.head, self.chain, self.stakes, self.reads = head, chain, {}, []

    async def chain_id(self): return self.chain
    async def latest_block(self): return self.head

    async def read_stake(self, vault_address, machine_bytes32, block_number):
        self.reads.append((vault_address, machine_bytes32, block_number))
        v = self.stakes.get(machine_bytes32, VaultStake(ZERO, 0, 0, 0, False))
        if isinstance(v, Exception):
            raise v
        return v


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for k, v in dict(STAKE_SYNC_ENABLED=True, STAKE_SYNC_CHAIN_ID=CHAIN, STAKE_SYNC_VAULT_ADDRESS=VAULT,
                     STAKE_SYNC_CONFIRMATIONS=15, STAKE_SYNC_RPC_URL="http://127.0.0.1:1").items():
        monkeypatch.setattr(settings, k, v)


async def _machine(db_engine, *, wallet: str | None = None, wallet_chain=CHAIN, stake="0"):
    async with _sm(db_engine)() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@t.io", password_hash=hash_password("Password123!"), email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        provider = Provider(user_id=user.id, display_name="Host", status="active")
        db.add(provider)
        await db.flush()
        m = ProviderMachine(provider_id=provider.id, hostname="h", agent_version="0.2.0", status="registered", availability="online",
                            asking_price_per_hour=decimal.Decimal("3.00"), signing_key="k", secret_hash=hash_token("s"),
                            stake_bonded=decimal.Decimal(stake), last_heartbeat_at=utcnow())
        db.add(m)
        if wallet:
            db.add(LinkedWallet(user_id=user.id, chain_id=wallet_chain, address=wallet.lower()))
        await db.commit()
        return m.id


async def _cycle(db_engine, client):
    async with _sm(db_engine)() as db:
        out = await run_stake_sync_cycle(db, client)
        await db.commit()
        return out


async def _machine_row(db_engine, mid):
    async with _sm(db_engine)() as db:
        return await db.get(ProviderMachine, mid)


def _vs(provider, bonded=0, unbonding=0, banned=False):
    return VaultStake(provider.lower(), bonded, unbonding, 0, banned)


WALLET = "0x" + "aa" * 20


async def test_deposit_unbond_withdraw_slash(db_engine):
    mid = await _machine(db_engine, wallet=WALLET)
    key = machine_id_to_bytes32(mid)
    fake = FakeVault()
    fake.stakes[key] = _vs(WALLET, 1500 * WEI)  # deposit
    assert (await _cycle(db_engine, fake))["changed"] == 1
    assert (await _machine_row(db_engine, mid)).stake_bonded == decimal.Decimal("1500.00")
    fake.stakes[key] = _vs(WALLET, 1000 * WEI, 500 * WEI)  # unbond: only bonded counts
    await _cycle(db_engine, fake)
    assert (await _machine_row(db_engine, mid)).stake_bonded == decimal.Decimal("1000.00")
    fake.stakes[key] = _vs(WALLET, 1000 * WEI, 0)  # withdraw of the unbonding part: no change
    assert (await _cycle(db_engine, fake))["changed"] == 0
    fake.stakes[key] = _vs(WALLET, 400 * WEI)  # slash
    await _cycle(db_engine, fake)
    assert (await _machine_row(db_engine, mid)).stake_bonded == decimal.Decimal("400.00")


async def test_ban_sets_flag_and_never_unbans(db_engine):
    mid = await _machine(db_engine, wallet=WALLET)
    key = machine_id_to_bytes32(mid)
    fake = FakeVault()
    fake.stakes[key] = _vs(WALLET, 10 * WEI, banned=True)
    await _cycle(db_engine, fake)
    assert (await _machine_row(db_engine, mid)).is_banned is True
    fake.stakes[key] = _vs(WALLET, 10 * WEI, banned=False)
    await _cycle(db_engine, fake)
    assert (await _machine_row(db_engine, mid)).is_banned is True


async def test_provider_mismatch_is_unverified_with_zero_stake(db_engine):
    mid = await _machine(db_engine, wallet=WALLET, stake="50")
    fake = FakeVault()
    fake.stakes[machine_id_to_bytes32(mid)] = _vs("0x" + "bb" * 20, 9000 * WEI)
    await _cycle(db_engine, fake)
    m = await _machine_row(db_engine, mid)
    assert m.stake_bonded == decimal.Decimal("0.00") and m.stake_unverified is True


async def test_no_wallet_and_other_chain_wallet_get_nothing(db_engine):
    no_wallet = await _machine(db_engine)
    other_chain = await _machine(db_engine, wallet=WALLET, wallet_chain=1)
    fake = FakeVault()
    for mid in (no_wallet, other_chain):
        fake.stakes[machine_id_to_bytes32(mid)] = _vs(WALLET, 500 * WEI)
    await _cycle(db_engine, fake)
    for mid in (no_wallet, other_chain):
        m = await _machine_row(db_engine, mid)
        assert m.stake_bonded == decimal.Decimal("0.00") and m.stake_unverified is True


async def test_rpc_error_keeps_old_value_and_records_error(db_engine):
    mid = await _machine(db_engine, wallet=WALLET, stake="700")
    fake = FakeVault()
    fake.stakes[machine_id_to_bytes32(mid)] = ChainClientError("rpc_down")
    out = await _cycle(db_engine, fake)
    assert out["errors"] == 1 and out["ok"] is False
    assert (await _machine_row(db_engine, mid)).stake_bonded == decimal.Decimal("700")


async def test_chain_level_errors_change_nothing(db_engine):
    mid = await _machine(db_engine, wallet=WALLET, stake="700")
    out = await _cycle(db_engine, FakeVault(chain=1))  # wrong chain id from the RPC
    assert out["ok"] is False and out["checked"] == 0
    assert (await _machine_row(db_engine, mid)).stake_bonded == decimal.Decimal("700")


async def test_confirmations_respected(db_engine):
    mid = await _machine(db_engine, wallet=WALLET)
    fake = FakeVault(head=1000)
    fake.stakes[machine_id_to_bytes32(mid)] = _vs(WALLET, WEI)
    await _cycle(db_engine, fake)
    assert fake.reads and all(r[2] == 985 and r[0] == VAULT for r in fake.reads)
    async with _sm(db_engine)() as db:
        assert (await db.execute(select(StakeSyncRun))).scalar_one().block_number == 985


async def test_rounding_down():
    assert wei_to_tokens(1999999999999999999) == decimal.Decimal("1.99")
    assert wei_to_tokens(5 * 10**15) == decimal.Decimal("0.00")
    assert wei_to_tokens(10**18 + 9 * 10**15) == decimal.Decimal("1.00")


async def test_run_row_written(db_engine):
    mid = await _machine(db_engine, wallet=WALLET)
    fake = FakeVault()
    fake.stakes[machine_id_to_bytes32(mid)] = _vs(WALLET, 3 * WEI)
    await _cycle(db_engine, fake)
    async with _sm(db_engine)() as db:
        run = (await db.execute(select(StakeSyncRun))).scalar_one()
    assert run.ok is True and run.machines_checked == 1 and run.machines_changed == 1 and run.finished_at is not None
    assert run.chain_id == CHAIN and run.error_count == 0


async def test_worker_isolation_via_sweep_once(db_engine, monkeypatch):
    """A failing stake sync must not stop lease recovery or raise out of the sweep."""
    from app import worker

    calls = []

    async def recovery(db):
        calls.append("recovery")
        return {}

    async def boom(db):
        calls.append("stake")
        raise RuntimeError("rpc exploded")

    monkeypatch.setattr(worker, "run_recovery_cycle", recovery)
    monkeypatch.setattr(worker, "run_stake_sync_cycle", boom)
    timers = {"anchor": 0.0, "witness": 0.0}
    await worker.sweep_once(_sm(db_engine), timers, 10_000.0)
    assert calls == ["recovery", "stake"]
    assert timers["stake"] == 10_000.0  # a failing sync waits a full interval


async def test_admin_bond_route_conflicts_when_sync_on(client, db_engine):
    async with _sm(db_engine)() as db:
        user = User(email="adm-ss@t.io", password_hash=hash_password("Password123!"), email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=Role.administrator))
        await db.commit()
    token, _, _ = create_access_token(subject=str(user.id), roles=[Role.administrator.value])
    r = await client.post("/v1/privacy/stake/bond", json={"machine_id": str(uuid.uuid4()), "amount": "10", "vault_tx_hash": "0x" + "ab" * 32},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 409 and r.json()["detail"] == "stake_managed_on_chain"
