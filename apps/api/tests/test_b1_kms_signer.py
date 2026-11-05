"""KmsWithdrawalSigner + AwsKmsAdapter helpers with a fake KMS (no network, no boto3). WRITTEN, NOT YET RUN.

The fake "KMS" signs the digest with a fresh random key and returns a DER signature, exactly the shape
AWS KMS returns, including a randomly high-s form, so low-s normalisation and the recovery id search are
exercised. The raw transaction must equal what eth_account's own Account.sign_transaction produces.
"""

from __future__ import annotations

import pytest
from eth_account import Account
from eth_keys import keys

from app.core.config import settings
from app.services import withdrawal_signer as ws

KEY = Account.create()
TX = {
    "chainId": 31337, "nonce": 3, "gasPrice": 10**9, "gas": 100_000,
    "to": "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB",  # checksummed: eth-account rejects lowercase "value": 0, "data": "0xa9059cbb" + "00" * 64,
}


def _der_int(n: int) -> bytes:
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + bytes([len(b)]) + b


def _der_sig(r: int, s: int) -> bytes:
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


class FakeKmsClient:
    """Mimics boto3 kms.get_public_key / kms.sign, returning the HIGH-s twin to force normalisation."""

    def __init__(self):
        self.pk = keys.PrivateKey(bytes(KEY.key))
        self.sign_calls = []

    def get_public_key(self, KeyId):
        return {"PublicKey": bytes.fromhex("3056301006072a8648ce3d020106052b8104000a034200") + b"\x04" + self.pk.public_key.to_bytes()}

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "DIGEST" and SigningAlgorithm == "ECDSA_SHA_256" and len(Message) == 32
        self.sign_calls.append(Message)
        sig = self.pk.sign_msg_hash(Message)
        s = sig.s if sig.s > ws.SECP256K1_N // 2 else ws.SECP256K1_N - sig.s  # high-s
        return {"Signature": _der_sig(sig.r, s)}


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(settings, "WITHDRAWALS_HOT_WALLET_ADDRESS", KEY.address)


def test_der_roundtrip_and_malformed():
    assert ws.parse_der_signature(_der_sig(5, 2**255)) == (5, 2**255)
    for bad in (b"", b"\x31\x00", _der_sig(1, 2) + b"\x00", b"\x30\x03\x02\x01"):
        with pytest.raises(ws.SignerError):
            ws.parse_der_signature(bad)


def test_low_s():
    r, s = ws.normalise_low_s(7, ws.SECP256K1_N - 5)
    assert (r, s) == (7, 5) and ws.normalise_low_s(7, 5) == (7, 5)


@pytest.mark.asyncio
async def test_address_and_signature_match_eth_account():
    client = FakeKmsClient()
    adapter = ws.AwsKmsAdapter(client=client)
    assert await adapter.public_address("k") == KEY.address.lower()
    signer = ws.KmsWithdrawalSigner(adapter=adapter, key_id="k")
    raw = await signer.sign_transaction(dict(TX))
    expected = "0x" + bytes(Account.sign_transaction(dict(TX), KEY.key).raw_transaction).hex()
    assert raw == expected  # ECDSA with RFC6979 is deterministic, low-s: byte-identical
    assert len(client.sign_calls) == 1


@pytest.mark.asyncio
async def test_key_that_is_not_the_hot_wallet_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "WITHDRAWALS_HOT_WALLET_ADDRESS", Account.create().address)
    signer = ws.KmsWithdrawalSigner(adapter=ws.AwsKmsAdapter(client=FakeKmsClient()), key_id="k")
    with pytest.raises(ws.SignerError, match="kms_key_does_not_match_hot_wallet"):
        await signer.sign_transaction(dict(TX))


@pytest.mark.asyncio
async def test_typed_transactions_refused_and_kms_errors_wrapped():
    signer = ws.KmsWithdrawalSigner(adapter=ws.AwsKmsAdapter(client=FakeKmsClient()), key_id="k")
    with pytest.raises(ws.SignerError):
        await signer.sign_transaction(dict(TX, maxFeePerGas=1))

    class Boom(FakeKmsClient):
        def sign(self, **kw):
            raise RuntimeError("AccessDenied")

    with pytest.raises(ws.SignerError, match="kms_sign_failed"):
        await ws.AwsKmsAdapter(client=Boom()).sign_digest("k", b"\x01" * 32)
