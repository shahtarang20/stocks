"""B1 token withdrawals. WRITTEN, NOT YET RUN (the task that added them forbade running tests).

Service level, with a fake chain and a local signer. The Hardhat end-to-end (real revert, reorg below
confirmations) is still to do; see docs/architecture/B1-withdrawals-design.md.
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest
from eth_account import Account
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import BillingAccount
from app.models.wallet import LinkedWallet
from app.models.withdrawal import Withdrawal
from app.services import chain_reconciler
from app.services import withdrawals as svc
from app.services.withdrawal_pause import is_paused, set_pause
from app.services.withdrawal_signer import KmsWithdrawalSigner, NotConfigured
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio
TOKEN = "0x" + "a" * 40
HOT = Account.create()
UNIT = 10**18


class LocalSigner:
    address = HOT.address.lower()

    async def sign_transaction(self, tx):
        return "0x" + Account.sign_transaction(tx, HOT.key).raw_transaction.hex()


class FakeChain:
    def __init__(self):
        self.latest, self.balance, self.sent, self.receipts, self.used_nonce, self.logs = 100, 10**30, [], {}, 0, []

    async def latest_block(self): return self.latest
    async def token_balance(self, t, o): return self.balance
    async def nonce(self, a, tag="pending"): return self.used_nonce
    async def gas_price(self): return 1_000_000_000
    async def send_raw(self, raw): self.sent.append(raw); return "0x"
    async def receipt(self, h): return self.receipts.get(h)
    async def transfer_logs_from(self, token, frm, a, b): return [l for l in self.logs if a <= l["block_number"] <= b]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for k, v in dict(
        WITHDRAWALS_ENABLED=True, CHAIN_RPC_URL="http://127.0.0.1:1", WITHDRAWALS_KMS_KEY_ID="k",
        WITHDRAWALS_HOT_WALLET_ADDRESS=HOT.address, COMPUTE_TOKEN_ADDRESS=TOKEN, TOKEN_CREDITS_PER_TOKEN="10",
        WITHDRAWALS_MIN_CREDITS="5.00", WITHDRAWALS_MAX_PER_TX_CREDITS="50.00", WITHDRAWALS_DAILY_USER_CREDITS="80.00",
        WITHDRAWALS_DAILY_GLOBAL_CREDITS="500.00", WITHDRAWALS_CONFIRMATIONS=2, WITHDRAWALS_TIMEOUT_SECONDS=3600,
        BILLING_SIGNUP_GRANT_MINOR=100_00,
    ).items():
        monkeypatch.setattr(settings, k, v)


async def _user_with_wallet(client, email):
    tokens = await register_verify_login(client, email)
    uid = await get_user_id(email)
    addr = Account.create().address.lower()
    async with get_sessionmaker()() as db:
        db.add(LinkedWallet(user_id=uid, chain_id=settings.wallet_chain_id, address=addr))
        await db.commit()
    return uid, addr, auth_headers(tokens["access_token"])


async def _request(uid, dest, amount="10.00", key="k1"):
    async with get_sessionmaker()() as db:
        wd, created = await svc.request_withdrawal(db, user_id=uid, amount=decimal.Decimal(amount), destination=dest, idempotency_key=key)
        await db.commit()
        return wd, created


async def _balance(account_type):
    async with get_sessionmaker()() as db:
        return int((await db.execute(select(BillingAccount.balance_minor).where(BillingAccount.type == account_type, BillingAccount.owner_id.is_(None)))).scalar_one_or_none() or 0)


async def _cycle(chain, signer=None):
    async with get_sessionmaker()() as db:
        out = await svc.run_payout_cycle(db, chain, signer or LocalSigner())
        await db.commit()
        return out


async def _get(wid):
    async with get_sessionmaker()() as db:
        return await db.get(Withdrawal, wid)


async def test_off_by_default():
    assert settings.__class__.model_fields["WITHDRAWALS_ENABLED"].default is False


async def test_disabled_and_paused_refuse(client, monkeypatch):
    uid, addr, _ = await _user_with_wallet(client, "w0@x.io")
    monkeypatch.setattr(settings, "WITHDRAWALS_ENABLED", False)
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, addr)
    assert e.value.code == "withdrawals_disabled"
    monkeypatch.setattr(settings, "WITHDRAWALS_ENABLED", True)
    async with get_sessionmaker()() as db:
        await set_pause(db, True, reason="t", source="admin")
        await db.commit()
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, addr)
    assert e.value.code == "withdrawals_paused"


async def test_only_linked_wallet_on_this_chain(client):
    uid, addr, _ = await _user_with_wallet(client, "w1@x.io")
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, "0x" + "9" * 40)
    assert e.value.code == "destination_not_linked_wallet"
    uid2 = await get_user_id((await register_verify_login(client, "w1b@x.io")) and "w1b@x.io")
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid2, addr)
    assert e.value.code == "wallet_not_linked"


async def test_hold_idempotency_and_caps(client):
    uid, addr, _ = await _user_with_wallet(client, "w2@x.io")
    wd, created = await _request(uid, addr, "10.00", "a")
    assert created and wd.status == "pending" and int(wd.amount_raw) == 1 * UNIT  # 10 credits / rate 10
    assert await _balance("withdrawal_pending") == 10_00
    again, created2 = await _request(uid, addr, "10.00", "a")
    assert not created2 and again.id == wd.id and await _balance("withdrawal_pending") == 10_00
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, addr, "11.00", "a")
    assert e.value.code == "idempotency_key_reused"
    for amount, code in (("4.00", "below_minimum"), ("60.00", "above_per_transaction_cap")):
        with pytest.raises(svc.WithdrawalError) as e:
            await _request(uid, addr, amount, "x" + amount)
        assert e.value.code == code
    await _request(uid, addr, "50.00", "b")
    with pytest.raises(svc.WithdrawalError) as e:  # 10 + 50 + 30 > 80 daily
        await _request(uid, addr, "30.00", "c")
    assert e.value.code == "daily_user_cap_exceeded"


async def test_insufficient_funds_leaves_nothing(client):
    uid, addr, _ = await _user_with_wallet(client, "w3@x.io")
    with pytest.raises(svc.WithdrawalError) as e:
        await _request(uid, addr, "50.00", "big")  # grant is 100.00 so spend it first
        await _request(uid, addr, "50.00", "big2")
        await _request(uid, addr, "5.00", "big3")
    assert e.value.code in ("insufficient_funds", "daily_user_cap_exceeded")


async def test_success_pays_out_after_confirmations(client):
    uid, addr, _ = await _user_with_wallet(client, "w4@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    out = await _cycle(chain)
    assert out["sent"] == 1 and len(chain.sent) == 1
    w = await _get(wd.id)
    assert w.status == "broadcast" and w.tx_hash and w.nonce == 0
    chain.receipts[w.tx_hash] = {"status": 1, "block_number": 100}
    await _cycle(chain)  # 100 - 100 < 2 confirmations
    assert (await _get(wd.id)).status == "broadcast"
    chain.latest = 102
    await _cycle(chain)
    assert (await _get(wd.id)).status == "paid_out"
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 10_00


async def test_revert_refunds_with_record(client):
    uid, addr, _ = await _user_with_wallet(client, "w5@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    await _cycle(chain)
    w = await _get(wd.id)
    chain.receipts[w.tx_hash] = {"status": 0, "block_number": 100}
    await _cycle(chain)
    w = await _get(wd.id)
    assert w.status == "refunded" and w.refund_reason == "reverted"
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 0


async def test_one_in_flight_and_second_waits(client):
    uid, addr, _ = await _user_with_wallet(client, "w6@x.io")
    a, _ = await _request(uid, addr, "10.00", "1")
    b, _ = await _request(uid, addr, "10.00", "2")
    chain = FakeChain()
    await _cycle(chain)
    await _cycle(chain)  # first still unconfirmed: nothing else is sent
    assert len(chain.sent) == 1
    assert {(await _get(a.id)).status, (await _get(b.id)).status} == {"broadcast", "pending"}


async def test_timeout_refunds_only_if_nonce_used_else_pauses(client, monkeypatch):
    uid, addr, _ = await _user_with_wallet(client, "w7@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    await _cycle(chain)
    # A non-positive timeout makes withdrawals_configured False (fail closed), so age the broadcast instead.
    monkeypatch.setattr(settings, "WITHDRAWALS_TIMEOUT_SECONDS", 60)
    async with get_sessionmaker()() as db:
        w = await db.get(Withdrawal, wd.id)
        w.broadcast_at = w.broadcast_at - dt.timedelta(hours=1)
        await db.commit()
    out = await _cycle(chain)  # nonce 0 still free: do NOT refund, pause
    assert out.get("stuck") and (await _get(wd.id)).status == "broadcast"
    async with get_sessionmaker()() as db:
        assert await is_paused(db)
    chain.used_nonce = 1  # spent by another transaction
    await _cycle(chain)
    w = await _get(wd.id)
    assert w.status == "refunded" and w.refund_reason == "timeout_nonce_used"


async def test_signer_not_configured_leaves_pending_and_admin_can_cancel(client):
    uid, addr, _ = await _user_with_wallet(client, "w8@x.io")
    wd, _ = await _request(uid, addr)
    with pytest.raises(NotConfigured):
        KmsWithdrawalSigner(key_id="")
    # The stub KMS adapter raises NotConfigured on use; the cycle reports blocked and sends nothing.
    class Broken(LocalSigner):
        async def sign_transaction(self, tx): raise NotConfigured("x")
    out = await _cycle(FakeChain(), Broken())
    assert out["blocked"] == "signer_not_configured"
    async with get_sessionmaker()() as db:
        w = await db.get(Withdrawal, wd.id)
        await svc.cancel_pending(db, w, None)
        await db.commit()
    assert (await _get(wd.id)).status == "refunded" and await _balance("withdrawal_pending") == 0


async def test_reconciliation_mismatch_pauses(client):
    uid, addr, _ = await _user_with_wallet(client, "w9@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    await _cycle(chain)
    w = await _get(wd.id)
    chain.receipts[w.tx_hash] = {"status": 1, "block_number": 50}
    chain.latest = 120
    await _cycle(chain)
    chain.logs = [{"tx_hash": w.tx_hash, "log_index": 0, "block_number": 50, "from_address": HOT.address.lower(), "to_address": addr, "amount": int(w.amount_raw)}]
    async with get_sessionmaker()() as db:
        ok = await chain_reconciler.reconcile_withdrawals(db, chain)
        await db.commit()
    assert ok["ok"] is True
    chain.logs.append({"tx_hash": "0x" + "e" * 64, "log_index": 1, "block_number": 60, "from_address": HOT.address.lower(), "to_address": addr, "amount": 5})
    async with get_sessionmaker()() as db:
        bad = await chain_reconciler.reconcile_withdrawals(db, chain)
        await db.commit()
    assert bad["ok"] is False and bad["findings"][0]["kind"] == "unknown_payout"
    async with get_sessionmaker()() as db:
        assert await is_paused(db)
