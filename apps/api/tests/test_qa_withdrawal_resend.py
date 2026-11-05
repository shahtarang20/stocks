"""QA coverage run: a withdrawal left in "sending" by a crash must recover when the node refuses the re-send.

The payout worker stores the signed transaction BEFORE broadcasting it, and re-sends the SAME bytes after a crash. A real
node answers that re-send with an RPC error ("already known" while the tx is in the mempool, "nonce too low" once it is
mined). The old code let the error escape, so the withdrawal stayed "sending" forever and, with one payout in flight at a
time, blocked every later withdrawal. Found by the opt-in hardhat run (test_b1_withdrawals_live)."""

from __future__ import annotations

import pytest

from app.services.chain_reconciler import ChainClientError
from tests.test_b1_withdrawals import (  # noqa: F401  (configured is an autouse fixture)
    FakeChain,
    _balance,
    _cycle,
    _get,
    _request,
    _user_with_wallet,
    configured,
)

pytestmark = pytest.mark.asyncio


class RefusingResendChain(FakeChain):
    """The first broadcast reaches the node but the answer is lost; the re-send is refused with `refusal`."""

    def __init__(self, refusal: str):
        super().__init__()
        self.refusal, self.calls = refusal, 0

    async def send_raw(self, raw):
        self.calls += 1
        self.sent.append(raw)
        if self.calls == 1:
            raise ChainClientError("rpc_transport_error:eth_sendRawTransaction")  # crash window: sent, status not saved
        raise ChainClientError(f"rpc_error:eth_sendRawTransaction:{{'message': '{self.refusal}'}}")


@pytest.mark.parametrize("refusal", ["already known", "Nonce too low. Expected nonce to be 1 but got 0", "known transaction: abc"])
async def test_resend_refused_by_node_is_treated_as_broadcast(client, refusal):
    uid, addr, _ = await _user_with_wallet(client, f"w-{abs(hash(refusal)) % 10**6}@x.io")
    wd, _ = await _request(uid, addr)
    chain = RefusingResendChain(refusal)
    first = await _cycle(chain)  # signs, stores, broadcast "fails": status stays sending
    assert "error" in first
    assert (await _get(wd.id)).status == "sending"
    tx_hash = (await _get(wd.id)).tx_hash
    await _cycle(chain)  # the re-send is refused: must NOT raise, must move to broadcast
    assert (await _get(wd.id)).status == "broadcast"
    chain.receipts[tx_hash] = {"status": 1, "block_number": 100}
    chain.latest = 102
    await _cycle(chain)
    assert (await _get(wd.id)).status == "paid_out"
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 10_00


async def test_resend_refused_and_already_reverted_refunds_in_same_pass(client):
    uid, addr, _ = await _user_with_wallet(client, "w-rev@x.io")
    wd, _ = await _request(uid, addr)
    chain = RefusingResendChain("already known")
    await _cycle(chain)
    chain.receipts[(await _get(wd.id)).tx_hash] = {"status": 0, "block_number": 100}
    await _cycle(chain)
    w = await _get(wd.id)
    assert w.status == "refunded" and w.refund_reason == "reverted"
    assert await _balance("withdrawal_pending") == 0 and await _balance("paid_out") == 0


async def test_other_rpc_errors_still_raise_and_keep_sending(client):
    uid, addr, _ = await _user_with_wallet(client, "w-oth@x.io")
    wd, _ = await _request(uid, addr)

    class Boom(RefusingResendChain):
        async def send_raw(self, raw):
            self.calls += 1
            raise ChainClientError("rpc_transport_error:eth_sendRawTransaction")

    chain = Boom("n/a")
    await _cycle(chain)
    assert (await _get(wd.id)).status == "sending"
    with pytest.raises(ChainClientError):
        await _cycle(chain)  # a genuine failure is NOT swallowed
    assert (await _get(wd.id)).status == "sending"
