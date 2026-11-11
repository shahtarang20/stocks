"""An administrator's payout/refund confirmation must agree with what the contract reports.

The tx hash and block number in the request prove nothing on their own, so a confirmation is refused when the chain
does not show the escrow in the expected state, when the chain cannot be read, and (outside development) when there is
no chain endpoint to check against."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.models.enums import Role
from app.models.settlement import EscrowStatus
from app.services import settlement as settlement_service
from app.services.chain_reconciler import ChainClientError, ChainEscrowState

from tests.test_phase10_settlement import _create_job_setup, _create_test_user


class _FakeChain:
    def __init__(self, state=None, error=None):
        self._state, self._error = state, error
        self.calls = []

    async def fetch_escrow(self, *, contract_address, job_bytes32, customer_address=None):
        self.calls.append((contract_address, job_bytes32, customer_address))
        if self._error:
            raise self._error
        return self._state


async def _locked_escrow(client: AsyncClient, db_engine, tag: str):
    customer, c_token = await _create_test_user(db_engine, f"cust-{tag}@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, f"prov-{tag}@test.com", Role.provider)
    _, a_token = await _create_test_user(db_engine, f"admin-{tag}@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)
    lock = {
        "job_id": str(job.id),
        "provider_id": str(provider.id),
        "locked_amount_units": str(60 * 10**18),
        "deadline_seconds": 86400,
        "lock_tx_hash": "0x" + "c" * 64,
        "customer_address": "0x1111111111111111111111111111111111111111",
        "provider_address": "0x2222222222222222222222222222222222222222",
    }
    assert (await client.post("/v1/settlement/escrows", json=lock, headers={"Authorization": f"Bearer {c_token}"})).status_code in (200, 201)
    admin = {"Authorization": f"Bearer {a_token}"}
    voucher = await client.post(
        "/v1/settlement/vouchers",
        json={"job_id": str(job.id), "final_cost_units": str(45 * 10**18), "platform_fee_units": str(5 * 10**18)},
        headers=admin,
    )
    assert voucher.status_code in (200, 201), voucher.text
    return job, admin


def _confirm_body(job):
    return {"job_id": str(job.id), "settle_tx_hash": "0x" + "d" * 64, "block_number": 12345}


@pytest.fixture
def rpc_configured(monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_RPC_URL", "http://chain.invalid")


@pytest.mark.asyncio
async def test_settlement_is_refused_when_the_contract_does_not_show_it_settled(client, db_engine, rpc_configured, monkeypatch):
    job, admin = await _locked_escrow(client, db_engine, "s1")
    chain = _FakeChain(ChainEscrowState(job_bytes32="0x" + "0" * 64, status=EscrowStatus.LOCKED.value, locked_amount_units=1))
    monkeypatch.setattr(settlement_service, "build_default_client", lambda: chain)

    res = await client.post("/v1/settlement/confirm", json=_confirm_body(job), headers=admin)
    assert res.status_code == 409, res.text
    assert chain.calls and chain.calls[0][2] == "0x1111111111111111111111111111111111111111"
    # Nothing was recorded: still LOCKED and not reconciled.
    escrow = (await client.get(f"/v1/settlement/escrows/{job.id}", headers=admin)).json()
    assert escrow["status"] == EscrowStatus.LOCKED.value and escrow["reconciled"] is False


@pytest.mark.asyncio
async def test_settlement_is_recorded_when_the_contract_agrees(client, db_engine, rpc_configured, monkeypatch):
    job, admin = await _locked_escrow(client, db_engine, "s2")
    chain = _FakeChain(ChainEscrowState(job_bytes32="0x" + "0" * 64, status=EscrowStatus.SETTLED.value, settled_amount_units=1))
    monkeypatch.setattr(settlement_service, "build_default_client", lambda: chain)

    res = await client.post("/v1/settlement/confirm", json=_confirm_body(job), headers=admin)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == EscrowStatus.SETTLED.value and res.json()["reconciled"] is True


@pytest.mark.asyncio
async def test_unreadable_chain_is_a_503_and_records_nothing(client, db_engine, rpc_configured, monkeypatch):
    job, admin = await _locked_escrow(client, db_engine, "s3")
    monkeypatch.setattr(settlement_service, "build_default_client", lambda: _FakeChain(error=ChainClientError("rpc down")))

    res = await client.post("/v1/settlement/confirm", json=_confirm_body(job), headers=admin)
    assert res.status_code == 503, res.text
    escrow = (await client.get(f"/v1/settlement/escrows/{job.id}", headers=admin)).json()
    assert escrow["status"] == EscrowStatus.LOCKED.value


@pytest.mark.asyncio
async def test_missing_onchain_escrow_refuses_the_refund_confirmation(client, db_engine, rpc_configured, monkeypatch):
    job, admin = await _locked_escrow(client, db_engine, "s4")
    monkeypatch.setattr(settlement_service, "build_default_client", lambda: _FakeChain(state=None))

    res = await client.post(
        "/v1/settlement/refund",
        json={"job_id": str(job.id), "refund_tx_hash": "0x" + "e" * 64, "block_number": 9},
        headers=admin,
    )
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_production_without_a_chain_endpoint_refuses_to_record(client, db_engine, monkeypatch):
    job, admin = await _locked_escrow(client, db_engine, "s5")
    monkeypatch.setattr(settings, "CHAIN_RPC_URL", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    res = await client.post("/v1/settlement/confirm", json=_confirm_body(job), headers=admin)
    assert res.status_code == 503, res.text
    assert "chain_verification_unavailable" in res.text
