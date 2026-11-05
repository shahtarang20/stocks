"""B1 withdrawals end to end on a real EVM. Opt-in; WRITTEN, NOT YET RUN. Needs Node and compiled contracts:

    cd contracts && npx hardhat compile
    RUN_WITHDRAWALS_INTEGRATION=1 apps/api/.venv/bin/pytest apps/api/tests/test_b1_withdrawals_live.py -v

Starts a LOCAL Hardhat node (chain 31337, own port), deploys the real ComputeCoinToken with a fresh
random owner and funds a fresh random hot wallet with gas and tokens (hardhat_setBalance + a token
transfer). Signing uses ``LocalKeySigner`` (a test double with a fresh random key); the KMS path is
covered separately by tests/test_b1_kms_signer.py. Nothing touches a public network, no key is reused.
Covers: request -> payout -> confirmations -> paid_out; revert -> refund; reorg below confirmations;
reconciliation mismatch -> pause; unlinked wallet and wrong-chain wallet refused.
"""

from __future__ import annotations

import decimal
import json
import os
import signal
import socket
import subprocess
import time

import httpx
import pytest
from eth_abi import encode
from eth_account import Account
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import BillingAccount
from app.models.wallet import LinkedWallet
from app.models.withdrawal import Withdrawal
from app.services import chain_reconciler
from app.services import withdrawals as svc
from app.services.withdrawal_chain import JsonRpcPayoutChain, transfer_calldata
from app.services.withdrawal_pause import is_paused
from tests.helpers import get_user_id, register_verify_login

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
_OPT_IN_VAR = "RUN_WITHDRAWALS_INTEGRATION"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get(_OPT_IN_VAR, "").strip().lower() in ("", "0", "false", "no"),
        reason=f"{_OPT_IN_VAR} is not set. Run: {_OPT_IN_VAR}=1 pytest tests/test_b1_withdrawals_live.py",
    ),
]
CHAIN_ID = 31337
HOT = Account.create()  # fresh random keys, generated per test run
OWNER = Account.create()


class LocalKeySigner:
    """Test double for the KMS signer: signs with a fresh random key held in memory."""

    address = HOT.address.lower()

    async def sign_transaction(self, tx):
        return "0x" + bytes(Account.sign_transaction(tx, HOT.key).raw_transaction).hex()


def _rpc(url, method, params=()):
    data = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, timeout=10.0).json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _artifact(name: str) -> str:
    path = os.path.join(CONTRACTS_DIR, "artifacts", "src", f"{name}.sol", f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["bytecode"]


def _send(url, key, *, to=None, data="0x", gas=8_000_000):
    acct = Account.from_key(key)
    tx = {
        "nonce": int(_rpc(url, "eth_getTransactionCount", [acct.address, "pending"]), 16),
        "gasPrice": int(_rpc(url, "eth_gasPrice"), 16), "gas": gas, "chainId": CHAIN_ID, "value": 0, "data": data,
    }
    if to:
        from eth_utils import to_checksum_address

        tx["to"] = to_checksum_address(to)  # eth-account refuses a lowercase address
    tx_hash = _rpc(url, "eth_sendRawTransaction", ["0x" + bytes(acct.sign_transaction(tx).raw_transaction).hex()])
    for _ in range(50):
        receipt = _rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            return receipt
        time.sleep(0.1)
    raise RuntimeError("no receipt")


def _mine(url, n):
    for _ in range(n):
        _rpc(url, "evm_mine")


@pytest.fixture(scope="module")
def chain_env():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["npx", "hardhat", "node", "--port", str(port)], cwd=CONTRACTS_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        for _ in range(120):
            try:
                if int(_rpc(url, "eth_chainId"), 16) == CHAIN_ID:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.fail("local hardhat node did not start")
        for a in (OWNER, HOT):
            _rpc(url, "hardhat_setBalance", [a.address, hex(10**21)])
        token = _send(url, OWNER.key, data=_artifact("ComputeCoinToken") + encode(["address"], [OWNER.address]).hex())["contractAddress"]
        funded = _send(url, OWNER.key, to=token, data=transfer_calldata(HOT.address, 1000 * 10**18))
        assert funded["status"] == "0x1"
        yield {"url": url, "token": token}
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)


@pytest.fixture(autouse=True)
def configured(monkeypatch, chain_env):
    for k, v in dict(
        WITHDRAWALS_ENABLED=True, CHAIN_RPC_URL=chain_env["url"], WITHDRAWALS_KMS_KEY_ID="local-test",
        WITHDRAWALS_HOT_WALLET_ADDRESS=HOT.address, COMPUTE_TOKEN_ADDRESS=chain_env["token"],
        SETTLEMENT_CHAIN_ID=CHAIN_ID, WALLET_CHAIN_ID=0, TOKEN_CREDITS_PER_TOKEN="10",
        WITHDRAWALS_MIN_CREDITS="5.00", WITHDRAWALS_MAX_PER_TX_CREDITS="50.00", WITHDRAWALS_DAILY_USER_CREDITS="80.00",
        WITHDRAWALS_DAILY_GLOBAL_CREDITS="500.00", WITHDRAWALS_CONFIRMATIONS=3, WITHDRAWALS_TIMEOUT_SECONDS=3600,
        WITHDRAWALS_GAS_LIMIT=100_000, WITHDRAWALS_RECON_FROM_BLOCK=0, BILLING_SIGNUP_GRANT_MINOR=100_00,
    ).items():
        monkeypatch.setattr(settings, k, v)


async def _user(client, email, chain_id=CHAIN_ID):
    await register_verify_login(client, email)
    uid = await get_user_id(email)
    addr = Account.create().address.lower()
    async with get_sessionmaker()() as db:
        db.add(LinkedWallet(user_id=uid, chain_id=chain_id, address=addr))
        await db.commit()
    return uid, addr


async def _request(uid, dest, key="k1", amount="10.00"):
    async with get_sessionmaker()() as db:
        wd, _ = await svc.request_withdrawal(db, user_id=uid, amount=decimal.Decimal(amount), destination=dest, idempotency_key=key)
        await db.commit()
        return wd


async def _cycle(chain):
    async with get_sessionmaker()() as db:
        out = await svc.run_payout_cycle(db, chain, LocalKeySigner())
        await db.commit()
        return out


async def _get(wid):
    async with get_sessionmaker()() as db:
        return await db.get(Withdrawal, wid)


async def _balance(account_type):
    async with get_sessionmaker()() as db:
        return int((await db.execute(select(BillingAccount.balance_minor).where(BillingAccount.type == account_type, BillingAccount.owner_id.is_(None)))).scalar_one_or_none() or 0)


def _token_balance(env, addr):
    out = _rpc(env["url"], "eth_call", [{"to": env["token"], "data": "0x70a08231" + addr.lower().removeprefix("0x").rjust(64, "0")}, "latest"])
    return int(out, 16)


async def test_request_payout_confirmations(client, chain_env):
    uid, addr = await _user(client, "wl1@x.io")
    wd = await _request(uid, addr)
    chain = JsonRpcPayoutChain()
    assert (await _cycle(chain))["sent"] == 1
    w = await _get(wd.id)
    assert w.status == "broadcast" and w.tx_hash
    assert _token_balance(chain_env, addr) == 1 * 10**18  # 10 credits at rate 10
    await _cycle(chain)  # mined but fewer than 3 confirmations
    assert (await _get(wd.id)).status == "broadcast"
    _mine(chain_env["url"], 4)
    await _cycle(chain)
    assert (await _get(wd.id)).status == "paid_out"
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 10_00


async def test_revert_refunds(client, chain_env, monkeypatch):
    uid, addr = await _user(client, "wl2@x.io")
    wd = await _request(uid, addr)
    # Enough gas to be mined, too little for the token transfer: the transaction reverts (out of gas).
    monkeypatch.setattr(settings, "WITHDRAWALS_GAS_LIMIT", 30_000)
    chain = JsonRpcPayoutChain()
    await _cycle(chain)
    await _cycle(chain)
    w = await _get(wd.id)
    assert w.status == "refunded" and w.refund_reason == "reverted"
    assert _token_balance(chain_env, addr) == 0
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 0


async def test_reorg_below_confirmations_never_pays_out(client, chain_env):
    uid, addr = await _user(client, "wl3@x.io")
    wd = await _request(uid, addr)
    chain = JsonRpcPayoutChain()
    snap = _rpc(chain_env["url"], "evm_snapshot")
    await _cycle(chain)  # sent and mined (automine)
    await _cycle(chain)  # receipt seen, below 3 confirmations
    assert (await _get(wd.id)).status == "broadcast"
    assert _rpc(chain_env["url"], "evm_revert", [snap]) is True  # the block with the payout disappears
    await _cycle(chain)
    w = await _get(wd.id)
    assert w.status == "broadcast" and w.receipt_block is None  # waits; never "paid out" without a receipt
    assert await _balance("paid_out") == 0 and await _balance("withdrawal_pending") == 10_00


async def test_reconciliation_mismatch_pauses(client, chain_env, monkeypatch):
    # The chain is shared by the tests of this module but every test has its own database: payouts made by the earlier tests
    # are on the chain and unknown to this test's database. Reconcile only from this test's first block.
    monkeypatch.setattr(settings, "WITHDRAWALS_RECON_FROM_BLOCK", int(_rpc(chain_env["url"], "eth_blockNumber"), 16) + 1)
    uid, addr = await _user(client, "wl4@x.io")
    wd = await _request(uid, addr)
    chain = JsonRpcPayoutChain()
    await _cycle(chain)
    _mine(chain_env["url"], 4)
    await _cycle(chain)
    assert (await _get(wd.id)).status == "paid_out"
    async with get_sessionmaker()() as db:
        ok = await chain_reconciler.reconcile_withdrawals(db, chain)
        await db.commit()
    assert ok["ok"] is True, ok
    # The hot wallet pays someone outside the withdrawal flow: that must pause withdrawals.
    _send(chain_env["url"], HOT.key, to=chain_env["token"], data=transfer_calldata(Account.create().address, 10**18), gas=100_000)
    _mine(chain_env["url"], 4)  # reconciliation only reads blocks that already have WITHDRAWALS_CONFIRMATIONS on top
    async with get_sessionmaker()() as db:
        bad = await chain_reconciler.reconcile_withdrawals(db, chain)
        await db.commit()
    assert bad["ok"] is False and any(f["kind"] == "unknown_payout" for f in bad["findings"])
    async with get_sessionmaker()() as db:
        assert await is_paused(db)


async def test_unlinked_and_wrong_chain_wallet_refused(client):
    uid, addr = await _user(client, "wl5@x.io")
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, "0x" + "9" * 40, "u1")
    assert e.value.code == "destination_not_linked_wallet"
    uid2, addr2 = await _user(client, "wl6@x.io", chain_id=1)  # linked, but on another chain
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid2, addr2, "u2")
    assert e.value.code == "wallet_not_linked"
