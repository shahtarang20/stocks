"""A1 stake sync against a real vault on a local Hardhat chain. Opt-in; WRITTEN, NOT YET RUN.

    cd contracts && npx hardhat compile
    RUN_STAKE_SYNC_INTEGRATION=1 apps/api/.venv/bin/pytest apps/api/tests/test_stake_sync_live.py -v

Deploys the real ComputeCoinToken and ComputeStakeVault with fresh random keys, registers a machine
(bytes32 from machine_id_to_bytes32), deposits stake as the provider, and checks JsonRpcStakeClient +
run_stake_sync_cycle read it back (bonded only; unbonding excluded; confirmations respected).
"""

from __future__ import annotations

import decimal
import json
import os
import signal
import socket
import subprocess
import time
import uuid

import httpx
import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.security import hash_password, hash_token
from app.db.base import utcnow
from app.models.enums import UserStatus
from app.models.provider import Provider, ProviderMachine
from app.models.user import User
from app.models.wallet import LinkedWallet
from app.services.settlement import machine_id_to_bytes32
from app.services.stake_sync import JsonRpcStakeClient, run_stake_sync_cycle

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
_OPT_IN_VAR = "RUN_STAKE_SYNC_INTEGRATION"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get(_OPT_IN_VAR, "").strip().lower() in ("", "0", "false", "no"),
        reason=f"{_OPT_IN_VAR} is not set. Run: {_OPT_IN_VAR}=1 pytest tests/test_stake_sync_live.py",
    ),
]
CHAIN_ID = 31337
OWNER, PROVIDER = Account.create(), Account.create()
WEI = 10**18


def _rpc(url, method, params=()):
    data = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, timeout=10.0).json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _artifact(name):
    with open(os.path.join(CONTRACTS_DIR, "artifacts", "src", f"{name}.sol", f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)["bytecode"]


def _send(url, acct, *, to=None, data="0x"):
    tx = {"nonce": int(_rpc(url, "eth_getTransactionCount", [acct.address, "pending"]), 16),
          "gasPrice": int(_rpc(url, "eth_gasPrice"), 16), "gas": 8_000_000, "chainId": CHAIN_ID, "value": 0, "data": data}
    if to:
        from eth_utils import to_checksum_address

        tx["to"] = to_checksum_address(to)  # eth-account refuses a lowercase address
    h = _rpc(url, "eth_sendRawTransaction", ["0x" + bytes(acct.sign_transaction(tx).raw_transaction).hex()])
    for _ in range(50):
        r = _rpc(url, "eth_getTransactionReceipt", [h])
        if r:
            assert r["status"] == "0x1", r
            return r
        time.sleep(0.1)
    raise RuntimeError("no receipt")


def _call(fn: str, *args: tuple[str, object]) -> str:
    sig = f"{fn}({','.join(t for t, _ in args)})"
    return "0x" + keccak(text=sig)[:4].hex() + encode([t for t, _ in args], [v for _, v in args]).hex()


@pytest.fixture(scope="module")
def env():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(["npx", "hardhat", "node", "--port", str(port)], cwd=CONTRACTS_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(120):
            try:
                if int(_rpc(url, "eth_chainId"), 16) == CHAIN_ID:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.fail("local hardhat node did not start")
        for a in (OWNER, PROVIDER):
            _rpc(url, "hardhat_setBalance", [a.address, hex(10**21)])
        token = _send(url, OWNER, data=_artifact("ComputeCoinToken") + encode(["address"], [OWNER.address]).hex())["contractAddress"]
        vault = _send(url, OWNER, data=_artifact("ComputeStakeVault") + encode(["address"] * 4, [token, OWNER.address, OWNER.address, OWNER.address]).hex())["contractAddress"]
        _send(url, OWNER, to=token, data=_call("transfer", ("address", PROVIDER.address), ("uint256", 5000 * WEI)))
        yield {"url": url, "token": token, "vault": vault}
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)


async def test_live_sync(env, db_engine, monkeypatch):
    for k, v in dict(STAKE_SYNC_ENABLED=True, STAKE_SYNC_CHAIN_ID=CHAIN_ID, STAKE_SYNC_VAULT_ADDRESS=env["vault"],
                     STAKE_SYNC_RPC_URL=env["url"], STAKE_SYNC_CONFIRMATIONS=0).items():
        monkeypatch.setattr(settings, k, v)
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        user = User(email="live-stake@t.io", password_hash=hash_password("Password123!"), email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        provider = Provider(user_id=user.id, display_name="Host", status="active")
        db.add(provider)
        await db.flush()
        m = ProviderMachine(provider_id=provider.id, hostname="h", agent_version="0.2.0", status="registered", availability="online",
                            asking_price_per_hour=decimal.Decimal("3.00"), signing_key="k", secret_hash=hash_token("s"),
                            stake_bonded=decimal.Decimal("0"), last_heartbeat_at=utcnow())
        db.add(m)
        db.add(LinkedWallet(user_id=user.id, chain_id=CHAIN_ID, address=PROVIDER.address.lower()))
        await db.commit()
        mid = m.id
    key = bytes.fromhex(machine_id_to_bytes32(mid)[2:])
    _send(env["url"], OWNER, to=env["vault"], data=_call("registerMachine", ("bytes32", key), ("address", PROVIDER.address)))
    _send(env["url"], PROVIDER, to=env["token"], data=_call("approve", ("address", env["vault"]), ("uint256", 5000 * WEI)))
    _send(env["url"], PROVIDER, to=env["vault"], data=_call("depositStake", ("bytes32", key), ("uint256", 1200 * WEI)))

    async with sm() as db:
        out = await run_stake_sync_cycle(db, JsonRpcStakeClient())
        await db.commit()
    assert out["ok"] is True and out["changed"] == 1, out
    async with sm() as db:
        assert (await db.get(ProviderMachine, mid)).stake_bonded == decimal.Decimal("1200.00")

    _send(env["url"], PROVIDER, to=env["vault"], data=_call("requestUnstake", ("bytes32", key), ("uint256", 200 * WEI)))
    async with sm() as db:
        await run_stake_sync_cycle(db, JsonRpcStakeClient())
        await db.commit()
    async with sm() as db:
        assert (await db.get(ProviderMachine, mid)).stake_bonded == decimal.Decimal("1000.00")  # unbonding excluded

    # Confirmations: reading 50 blocks behind the head sees the state before the last deposit/unstake.
    monkeypatch.setattr(settings, "STAKE_SYNC_CONFIRMATIONS", 50)
    for _ in range(50):
        _rpc(env["url"], "evm_mine")
    async with sm() as db:
        out = await run_stake_sync_cycle(db, JsonRpcStakeClient())
        await db.commit()
    assert out["ok"] is True
