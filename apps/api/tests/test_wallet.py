"""Wallet linking: an address is bound to an account only by a valid signature."""

from __future__ import annotations

import datetime as dt

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings
from app.db.base import utcnow
from app.services import wallet as wallet_svc
from tests.helpers import auth_headers, register_verify_login

pytestmark = pytest.mark.asyncio
CHAIN = settings.wallet_chain_id


def _sign(acct, message: str) -> str:
    return acct.sign_message(encode_defunct(text=message)).signature.hex()


async def _user(client, email):
    tokens = await register_verify_login(client, email)
    return auth_headers(tokens["access_token"]), tokens


async def _challenge(client, h, address):
    r = await client.post("/v1/wallet/link-challenge", json={"address": address}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["message"]


async def test_link_flow_shows_the_wallet_and_can_be_unlinked(client):
    h, _ = await _user(client, "w1@x.io")
    acct = Account.create()
    assert (await client.get("/v1/wallet", headers=h)).json()["wallet"] is None

    msg = await _challenge(client, h, acct.address)
    r = await client.post("/v1/wallet/link", json={"address": acct.address, "message": msg, "signature": _sign(acct, msg)}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["address"] == acct.address

    got = (await client.get("/v1/wallet", headers=h)).json()
    assert got["wallet"]["address"] == acct.address and got["credits"]["simulated"] is True

    # Linking the same address again is harmless.
    again = await client.post("/v1/wallet/link", json={"address": acct.address, "message": await _challenge(client, h, acct.address), "signature": _sign(acct, await _challenge(client, h, acct.address))}, headers=h)
    assert again.status_code in (200, 201)

    assert (await client.delete("/v1/wallet/link", headers=h)).status_code == 204
    assert (await client.get("/v1/wallet", headers=h)).json()["wallet"] is None


async def test_a_signature_from_a_different_key_is_refused(client):
    h, _ = await _user(client, "w2@x.io")
    owner, attacker = Account.create(), Account.create()
    msg = await _challenge(client, h, owner.address)
    r = await client.post("/v1/wallet/link", json={"address": owner.address, "message": msg, "signature": _sign(attacker, msg)}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "signature_wrong_signer"
    assert (await client.get("/v1/wallet", headers=h)).json()["wallet"] is None


async def test_an_edited_message_is_refused(client):
    h, _ = await _user(client, "w3@x.io")
    acct, other = Account.create(), Account.create()
    msg = await _challenge(client, h, acct.address)
    forged = msg.replace(acct.address, other.address)
    r = await client.post("/v1/wallet/link", json={"address": other.address, "message": forged, "signature": _sign(other, forged)}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "message_invalid"  # the tag no longer matches


async def test_another_accounts_challenge_cannot_be_used(client):
    ha, _ = await _user(client, "w4a@x.io")
    hb, _ = await _user(client, "w4b@x.io")
    acct = Account.create()
    msg_for_a = await _challenge(client, ha, acct.address)
    r = await client.post("/v1/wallet/link", json={"address": acct.address, "message": msg_for_a, "signature": _sign(acct, msg_for_a)}, headers=hb)
    assert r.status_code == 400 and r.json()["detail"] == "message_mismatch"


async def test_an_expired_message_is_refused(client):
    h, tokens = await _user(client, "w5@x.io")
    uid = (await client.get("/auth/me", headers=h)).json()["id"]
    import uuid

    acct = Account.create()
    old = wallet_svc.build_message(uuid.UUID(uid), acct.address, CHAIN, utcnow() - dt.timedelta(hours=1))
    r = await client.post("/v1/wallet/link", json={"address": acct.address, "message": old, "signature": _sign(acct, old)}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "message_expired"


async def test_one_address_cannot_belong_to_two_accounts_nor_one_account_to_two_wallets(client):
    ha, _ = await _user(client, "w6a@x.io")
    hb, _ = await _user(client, "w6b@x.io")
    a1, a2 = Account.create(), Account.create()
    m = await _challenge(client, ha, a1.address)
    assert (await client.post("/v1/wallet/link", json={"address": a1.address, "message": m, "signature": _sign(a1, m)}, headers=ha)).status_code == 201

    m2 = await _challenge(client, hb, a1.address)
    stolen = await client.post("/v1/wallet/link", json={"address": a1.address, "message": m2, "signature": _sign(a1, m2)}, headers=hb)
    assert stolen.status_code == 409 and stolen.json()["detail"] == "address_linked_to_another_account"

    m3 = await _challenge(client, ha, a2.address)
    second = await client.post("/v1/wallet/link", json={"address": a2.address, "message": m3, "signature": _sign(a2, m3)}, headers=ha)
    assert second.status_code == 409 and second.json()["detail"] == "account_already_has_a_wallet"


async def test_bad_inputs_are_clean_errors(client):
    h, _ = await _user(client, "w7@x.io")
    for bad in ["", "0x123", "not-an-address", "0x" + "z" * 40]:
        r = await client.post("/v1/wallet/link-challenge", json={"address": bad}, headers=h)
        assert r.status_code == 422, (bad, r.status_code)
    acct = Account.create()
    wrong = await client.post("/v1/wallet/link-challenge", json={"address": acct.address, "chain_id": CHAIN + 1}, headers=h)
    assert wrong.status_code == 422 and wrong.json()["detail"] == "wrong_chain"
    garbage = await client.post("/v1/wallet/link", json={"address": acct.address, "message": "hello", "signature": "0x00"}, headers=h)
    assert garbage.status_code == 400
    client.cookies.clear()  # drop the login cookies so this request is genuinely anonymous
    assert (await client.get("/v1/wallet")).status_code in (401, 403)


async def test_linking_needs_a_verified_email(client):
    reg = await client.post("/auth/register", json={"email": "w8@x.io", "password": "Sup3rStrongPw!"})
    login = await client.post("/auth/login", json={"email": "w8@x.io", "password": "Sup3rStrongPw!"})
    h = auth_headers(login.json()["access_token"])
    r = await client.post("/v1/wallet/link-challenge", json={"address": Account.create().address}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == "email_not_verified"


async def test_token_balance_comes_from_the_chain_and_failure_never_breaks_the_page(client, monkeypatch):
    from app.services.chain_reconciler import ChainClientError, JsonRpcChainClient

    h, _ = await _user(client, "w9@x.io")
    acct = Account.create()
    m = await _challenge(client, h, acct.address)
    await client.post("/v1/wallet/link", json={"address": acct.address, "message": m, "signature": _sign(acct, m)}, headers=h)

    # Chain not configured: the page still works and says so.
    monkeypatch.setattr(settings, "CHAIN_RPC_URL", "")
    got = (await client.get("/v1/wallet", headers=h)).json()
    assert got["chain"]["configured"] is False and got["token_balance"] is None

    monkeypatch.setattr(settings, "CHAIN_RPC_URL", "http://127.0.0.1:1")

    async def ok(self, token, owner):
        assert owner.lower() == acct.address.lower()
        return 1234 * 10**18 + 5 * 10**17

    monkeypatch.setattr(JsonRpcChainClient, "token_balance", ok)
    assert (await client.get("/v1/wallet", headers=h)).json()["token_balance"] == "1234.500000"

    async def boom(self, token, owner):
        raise ChainClientError("rpc_transport_error:eth_call")

    monkeypatch.setattr(JsonRpcChainClient, "token_balance", boom)
    r = await client.get("/v1/wallet", headers=h)
    assert r.status_code == 200
    assert r.json()["token_balance"] is None and r.json()["token_balance_error"] == "chain_unavailable"
