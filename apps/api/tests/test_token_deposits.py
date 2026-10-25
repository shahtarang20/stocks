"""COMPUTE deposits are credited exactly once, only from a linked wallet, and reconcile with the chain."""

from __future__ import annotations

import decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.services import token_deposits as svc
from tests.helpers import auth_headers, grant_role, login, register_verify_login

pytestmark = pytest.mark.asyncio
DEPOSIT = "0x" + "d" * 40
TOKEN = "0x" + "a" * 40
UNIT = 10**18


def raw(tokens) -> int:
    return int(decimal.Decimal(str(tokens)) * UNIT)


class FakeChain:
    def __init__(self, latest=100, balance=0):
        self.latest, self.balance, self.logs = latest, balance, []
        self.calls = 0

    async def latest_block(self):
        return self.latest

    async def transfer_logs(self, token, to, a, b):
        self.calls += 1
        assert token == TOKEN and to == DEPOSIT
        return [l for l in self.logs if a <= l["block_number"] <= b]

    async def token_balance(self, token, owner):
        return self.balance

    def add(self, tx, idx, block, sender, tokens):
        self.logs.append({"tx_hash": tx, "log_index": idx, "block_number": block, "from_address": sender.lower(), "amount": raw(tokens)})


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for k, v in dict(
        TOKEN_DEPOSIT_ENABLED=True, CHAIN_RPC_URL="http://127.0.0.1:1", TOKEN_DEPOSIT_ADDRESS=DEPOSIT,
        COMPUTE_TOKEN_ADDRESS=TOKEN, TOKEN_CREDITS_PER_TOKEN="10", TOKEN_DEPOSIT_CONFIRMATIONS=2,
        TOKEN_DEPOSIT_MIN_TOKENS="1", TOKEN_DEPOSIT_MAX_TOKENS="1000", TOKEN_DEPOSIT_FROM_BLOCK=0,
        CHAIN_LOG_CHUNK_BLOCKS=10_000,
    ).items():
        monkeypatch.setattr(settings, k, v)


async def _linked_user(client, email):
    tokens = await register_verify_login(client, email)
    h = auth_headers(tokens["access_token"])
    acct = Account.create()
    ch = await client.post("/v1/wallet/link-challenge", json={"address": acct.address}, headers=h)
    msg = ch.json()["message"]
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    r = await client.post("/v1/wallet/link", json={"address": acct.address, "message": msg, "signature": sig}, headers=h)
    assert r.status_code == 201, r.text
    return h, acct


async def _available(client, h):
    return decimal.Decimal((await client.get("/v1/billing/account", headers=h)).json()["available"])


async def _scan(chain):
    async with get_sessionmaker()() as db:
        return await svc.scan_deposits(db, chain)


async def _reconcile(chain):
    async with get_sessionmaker()() as db:
        return await svc.reconcile(db, chain)


async def test_a_deposit_from_a_linked_wallet_is_credited_at_the_configured_rate(client):
    h, acct = await _linked_user(client, "d1@x.io")
    before = await _available(client, h)
    chain = FakeChain(latest=100)
    chain.add("0x" + "1" * 64, 0, 50, acct.address, "2.5")
    out = await _scan(chain)
    assert out["credited"] == 1
    assert await _available(client, h) - before == decimal.Decimal("25.00")  # 2.5 COMPUTE x 10
    mine = (await client.get("/v1/wallet/deposits", headers=h)).json()
    assert [(d["status"], d["amount"], d["credits"]) for d in mine] == [("credited", "2.500000", "25.00")]

    # The ledger's reference columns are VARCHAR(64) (idempotency key: 255). SQLite does not enforce
    # lengths, Postgres does, and a longer reference crashed the first real run. Check them here.
    from sqlalchemy import select

    from app.models.billing import LedgerTransaction
    from app.models.token_deposit import TokenDeposit

    async with get_sessionmaker()() as db:
        txn = (await db.execute(select(LedgerTransaction).where(LedgerTransaction.reference_type == "token_deposit"))).scalar_one()
        dep = (await db.execute(select(TokenDeposit))).scalar_one()
        assert len(txn.reference_id) <= 64 and txn.reference_id == str(dep.id)
        assert len(txn.idempotency_key) <= 255 and len(txn.reference_type) <= 32


async def test_credits_are_rounded_down_never_up(client, monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_CREDITS_PER_TOKEN", "3.333")
    h, acct = await _linked_user(client, "d2@x.io")
    before = await _available(client, h)
    chain = FakeChain()
    chain.add("0x" + "2" * 64, 0, 10, acct.address, "1.5")  # 1.5 x 3.333 = 4.9995 -> 4.99
    await _scan(chain)
    assert await _available(client, h) - before == decimal.Decimal("4.99")


async def test_the_same_transfer_is_never_credited_twice(client):
    h, acct = await _linked_user(client, "d3@x.io")
    before = await _available(client, h)
    chain = FakeChain()
    chain.add("0x" + "3" * 64, 0, 10, acct.address, "5")
    await _scan(chain)
    again = await _scan(chain)  # cursor has moved, nothing to read
    assert again["credited"] == 0
    # Even if the same log is handed over again (cursor reset, restart, second scanner), it is a no-op.
    async with get_sessionmaker()() as db:
        assert await svc.process_transfer(db, chain.logs[0]) is None
        await db.commit()
    assert await _available(client, h) - before == decimal.Decimal("50.00")
    assert len((await client.get("/v1/wallet/deposits", headers=h)).json()) == 1


async def test_a_rescan_from_the_start_does_not_double_credit(client):
    h, acct = await _linked_user(client, "d4@x.io")
    before = await _available(client, h)
    chain = FakeChain()
    chain.add("0x" + "4" * 64, 0, 10, acct.address, "3")
    await _scan(chain)
    async with get_sessionmaker()() as db:
        from app.models.system import SystemConfiguration

        row = await db.get(SystemConfiguration, svc.CURSOR_KEY)
        row.value = {"next_block": 0}  # force a full rescan
        await db.commit()
    out = await _scan(chain)
    assert out["credited"] == 0 and out["already_seen"] == 1
    assert await _available(client, h) - before == decimal.Decimal("30.00")


async def test_only_blocks_with_enough_confirmations_are_read(client):
    h, acct = await _linked_user(client, "d5@x.io")
    chain = FakeChain(latest=11)  # confirmations=2 -> safe head 9
    chain.add("0x" + "5" * 64, 0, 10, acct.address, "2")
    assert (await _scan(chain))["credited"] == 0
    chain.latest = 12  # block 10 is now 2 deep
    assert (await _scan(chain))["credited"] == 1


async def test_a_transfer_from_an_unlinked_address_credits_nothing(client):
    h, _ = await _linked_user(client, "d6@x.io")
    before = await _available(client, h)
    stranger = Account.create()
    chain = FakeChain()
    chain.add("0x" + "6" * 64, 0, 10, stranger.address, "100")
    out = await _scan(chain)
    assert out["unclaimed"] == 1 and out["credited"] == 0
    assert await _available(client, h) == before
    rec = await _reconcile(FakeChain(balance=raw(100)))
    assert rec["unclaimed_tokens"] == "100" or rec["unclaimed_tokens"].startswith("100")  # the tokens are still accounted for


async def test_too_small_and_too_large_deposits_are_not_credited(client):
    h, acct = await _linked_user(client, "d7@x.io")
    before = await _available(client, h)
    chain = FakeChain()
    chain.add("0x" + "7" * 64, 0, 10, acct.address, "0.5")      # below the 1 COMPUTE minimum
    chain.add("0x" + "8" * 64, 0, 11, acct.address, "5000")     # above the 1000 maximum
    out = await _scan(chain)
    assert out["rejected"] == 2 and out["credited"] == 0
    assert await _available(client, h) == before
    mine = (await client.get("/v1/wallet/deposits", headers=h)).json()
    assert sorted(d["reason"] for d in mine) == ["above_maximum", "below_minimum"]


async def test_a_deposit_worth_less_than_a_cent_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_CREDITS_PER_TOKEN", "0.001")
    h, acct = await _linked_user(client, "d8@x.io")
    chain = FakeChain()
    chain.add("0x" + "9" * 64, 0, 10, acct.address, "1")  # 0.001 credits -> 0 cents
    out = await _scan(chain)
    assert out["rejected"] == 1
    assert (await client.get("/v1/wallet/deposits", headers=h)).json()[0]["reason"] == "too_small"


async def test_a_deleted_account_cannot_be_credited(client):
    h, acct = await _linked_user(client, "d9@x.io")
    r = await client.post("/auth/delete-account", json={"password": "Sup3rStrongPw!", "acknowledge_forfeit": True}, headers=h)
    assert r.status_code == 200, r.text
    chain = FakeChain()
    chain.add("0x" + "a" * 64, 0, 10, acct.address, "10")
    out = await _scan(chain)
    assert out["unclaimed"] == 1 and out["credited"] == 0


async def test_scanning_walks_the_range_in_chunks_and_loses_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_LOG_CHUNK_BLOCKS", 3)
    h, acct = await _linked_user(client, "d10@x.io")
    before = await _available(client, h)
    chain = FakeChain(latest=40)
    for i, block in enumerate([0, 2, 3, 11, 25, 38]):
        chain.add("0x" + f"{i + 1:064x}", 0, block, acct.address, "1")
    out = await _scan(chain)
    assert out["credited"] == 6 and chain.calls > 5
    assert await _available(client, h) - before == decimal.Decimal("60.00")


async def test_nothing_happens_unless_the_feature_is_configured(client, monkeypatch):
    for bad in (dict(TOKEN_DEPOSIT_ENABLED=False), dict(TOKEN_CREDITS_PER_TOKEN=""), dict(TOKEN_CREDITS_PER_TOKEN="abc"),
                dict(TOKEN_CREDITS_PER_TOKEN="0"), dict(TOKEN_DEPOSIT_ADDRESS=""), dict(CHAIN_RPC_URL=""),
                dict(TOKEN_DEPOSIT_MIN_TOKENS="5", TOKEN_DEPOSIT_MAX_TOKENS="1")):
        with monkeypatch.context() as m:
            for k, v in bad.items():
                m.setattr(settings, k, v)
            assert settings.token_deposits_enabled is False, bad
            assert (await _scan(FakeChain())) == {"enabled": False}


async def test_reconciliation_agrees_when_the_chain_holds_what_was_credited(client):
    h, acct = await _linked_user(client, "d11@x.io")
    chain = FakeChain(balance=raw("7"))
    chain.add("0x" + "b" * 64, 0, 10, acct.address, "7")
    await _scan(chain)
    rec = await _reconcile(chain)
    assert rec["ok"] is True and rec["tokens_ok"] and rec["ledger_ok"]
    assert rec["credits_issued"] == rec["ledger_credits"] == "70.00"


async def test_reconciliation_flags_credits_issued_against_tokens_that_are_gone(client):
    h, acct = await _linked_user(client, "d12@x.io")
    chain = FakeChain(balance=raw("7"))
    chain.add("0x" + "c" * 64, 0, 10, acct.address, "7")
    await _scan(chain)
    chain.balance = raw("3")  # tokens left the deposit address (reorg, or a withdrawal) after crediting
    rec = await _reconcile(chain)
    assert rec["ok"] is False and rec["tokens_ok"] is False


async def test_reconciliation_flags_a_deposit_row_that_no_longer_matches_the_ledger(client):
    h, acct = await _linked_user(client, "d13@x.io")
    chain = FakeChain(balance=raw("7"))
    chain.add("0x" + "e" * 64, 0, 10, acct.address, "7")
    await _scan(chain)
    async with get_sessionmaker()() as db:
        from sqlalchemy import select

        from app.models.token_deposit import TokenDeposit

        row = (await db.execute(select(TokenDeposit))).scalars().first()
        row.credits_minor = row.credits_minor + 100  # someone edits the record
        await db.commit()
    rec = await _reconcile(chain)
    assert rec["ok"] is False and rec["ledger_ok"] is False


async def test_users_only_see_their_own_deposits_and_admin_endpoints_are_admin_only(client, monkeypatch):
    ha, a = await _linked_user(client, "d14a@x.io")
    hb, b = await _linked_user(client, "d14b@x.io")
    chain = FakeChain(balance=raw("9"))
    chain.add("0x" + "f" * 64, 0, 10, a.address, "4")
    chain.add("0x" + "1" * 63 + "2", 0, 11, b.address, "5")
    await _scan(chain)
    assert [d["amount"] for d in (await client.get("/v1/wallet/deposits", headers=ha)).json()] == ["4.000000"]
    assert [d["amount"] for d in (await client.get("/v1/wallet/deposits", headers=hb)).json()] == ["5.000000"]

    for path, method in (("/v1/admin/token-deposits", "get"), ("/v1/admin/token-deposits/reconciliation", "get"), ("/v1/admin/token-deposits/scan", "post")):
        assert (await getattr(client, method)(path, headers=ha)).status_code == 403

    await register_verify_login(client, "d14adm@x.io")
    await grant_role("d14adm@x.io", Role.administrator)
    adm = auth_headers((await login(client, "d14adm@x.io"))["access_token"])
    monkeypatch.setattr(svc, "JsonRpcChainClient", lambda: chain)
    listed = (await client.get("/v1/admin/token-deposits", headers=adm)).json()
    assert len(listed) == 2 and all(d["status"] == "credited" for d in listed)
    assert (await client.get("/v1/admin/token-deposits/reconciliation", headers=adm)).json()["ok"] is True
    assert (await client.post("/v1/admin/token-deposits/scan", headers=adm)).json()["enabled"] is True
    assert (await client.get("/v1/admin/token-deposits?status=bogus", headers=adm)).status_code == 422


async def test_admin_endpoints_say_so_when_the_feature_is_off(client, monkeypatch):
    await register_verify_login(client, "d15@x.io")
    await grant_role("d15@x.io", Role.administrator)
    adm = auth_headers((await login(client, "d15@x.io"))["access_token"])
    monkeypatch.setattr(settings, "TOKEN_DEPOSIT_ENABLED", False)
    for path, method in (("/v1/admin/token-deposits/reconciliation", "get"), ("/v1/admin/token-deposits/scan", "post")):
        r = await getattr(client, method)(path, headers=adm)
        assert r.status_code == 409 and r.json()["detail"] == "token_deposits_disabled"
    assert (await client.get("/v1/wallet", headers=adm)).json()["deposits"] == {"enabled": False}


async def test_the_wallet_page_gets_the_deposit_instructions_when_enabled(client):
    tokens = await register_verify_login(client, "d16@x.io")
    info = (await client.get("/v1/wallet", headers=auth_headers(tokens["access_token"]))).json()["deposits"]
    assert info == {"enabled": True, "address": DEPOSIT, "credits_per_token": "10", "confirmations": 2, "min_tokens": "1", "max_tokens": "1000"}
