"""Integration tests for Phase 10: On-chain Settlement & Smart Contract Reconciliation."""

from __future__ import annotations

import decimal
import uuid
from datetime import timedelta

import pytest
from eth_abi.packed import encode_packed
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.jobs import Job
from app.models.org import Organization, OrganizationMember, Project
from app.models.settlement import EscrowStatus
from app.models.user import User, UserRole


async def _create_test_user(db_engine, email: str, role: Role) -> tuple[User, str]:
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        user = User(
            email=email,
            password_hash="test-hash",
            status=UserStatus.active,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=role))
        await db.commit()

    token, _, _ = create_access_token(
        subject=str(user.id),
        roles=[role.value],
    )
    return user, token


async def _create_job_setup(db_engine, customer: User, provider: User) -> Job:
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        org = Organization(
            name=f"Org-{uuid.uuid4().hex[:6]}",
            owner_id=customer.id,
        )
        db.add(org)
        await db.flush()

        member = OrganizationMember(
            org_id=org.id,
            user_id=customer.id,
            role=Role.customer.value,
        )
        db.add(member)

        project = Project(
            org_id=org.id,
            name="Phase 10 Project",
            owner_id=customer.id,
        )
        db.add(project)
        await db.flush()

        job = Job(
            project_id=project.id,
            customer_id=customer.id,
            status="completed",
            workload_type="inference",
            model="meta-llama/Llama-3-8B-Instruct",
            input={"prompt": "test prompt"},
            isolation_tier="container",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job


@pytest.mark.asyncio
async def test_get_contract_deployment_info(client: AsyncClient, db_engine):
    """Test public contract registry info endpoint."""
    res = await client.get("/v1/settlement/contracts")
    assert res.status_code == 200
    data = res.json()
    assert data["chain_id"] == settings.SETTLEMENT_CHAIN_ID
    assert data["token_symbol"] == "COMPUTE"
    assert data["token_address"] == settings.COMPUTE_TOKEN_ADDRESS
    assert data["settlement_contract_address"] == settings.SETTLEMENT_CONTRACT_ADDRESS
    assert data["platform_signer_address"].startswith("0x")


@pytest.mark.asyncio
async def test_escrow_lock_lifecycle_and_conflict(client: AsyncClient, db_engine):
    """Test budget locking into smart contract escrow and duplicate protection."""
    customer, c_token = await _create_test_user(db_engine, "cust1@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov1@test.com", Role.provider)
    job = await _create_job_setup(db_engine, customer, provider)

    headers = {"Authorization": f"Bearer {c_token}"}
    lock_payload = {
        "job_id": str(job.id),
        "provider_id": str(provider.id),
        "locked_amount_units": str(50 * 10**18),
        "deadline_seconds": 86400,
        "lock_tx_hash": "0x" + "a" * 64,
        "customer_address": "0x1111111111111111111111111111111111111111",
        "provider_address": "0x2222222222222222222222222222222222222222",
    }

    # 1. Lock budget
    res = await client.post("/v1/settlement/escrows", json=lock_payload, headers=headers)
    assert res.status_code == 201, res.text
    escrow = res.json()
    assert escrow["status"] == EscrowStatus.LOCKED.value
    assert escrow["locked_amount_units"] == str(50 * 10**18)
    assert escrow["reconciled"] is False

    # 2. Duplicate lock should be rejected
    res_dup = await client.post("/v1/settlement/escrows", json=lock_payload, headers=headers)
    assert res_dup.status_code == 409

    # 3. Fetch escrow by job
    res_get = await client.get(f"/v1/settlement/escrows/{job.id}", headers=headers)
    assert res_get.status_code == 200
    assert res_get.json()["id"] == escrow["id"]


@pytest.mark.asyncio
async def test_issue_settlement_voucher_cryptographic_verification(client: AsyncClient, db_engine):
    """Test cryptographic voucher generation and signature recovery verification."""
    customer, c_token = await _create_test_user(db_engine, "cust2@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov2@test.com", Role.provider)
    admin, a_token = await _create_test_user(db_engine, "admin2@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)

    headers = {"Authorization": f"Bearer {c_token}"}
    admin_headers = {"Authorization": f"Bearer {a_token}"}
    locked_amt = 100 * 10**18
    lock_payload = {
        "job_id": str(job.id),
        "provider_id": str(provider.id),
        "locked_amount_units": str(locked_amt),
        "deadline_seconds": 86400,
        "lock_tx_hash": "0x" + "b" * 64,
        "customer_address": "0x1111111111111111111111111111111111111111",
        "provider_address": "0x2222222222222222222222222222222222222222",
    }
    await client.post("/v1/settlement/escrows", json=lock_payload, headers=headers)

    # 1. Overbudget voucher must fail
    res_over = await client.post(
        "/v1/settlement/vouchers",
        json={"job_id": str(job.id), "final_cost_units": str(150 * 10**18), "platform_fee_units": str(10 * 10**18)},
        headers=admin_headers,
    )
    assert res_over.status_code == 400

    # 2. Fee exceeding cost must fail
    res_bad_fee = await client.post(
        "/v1/settlement/vouchers",
        json={"job_id": str(job.id), "final_cost_units": str(40 * 10**18), "platform_fee_units": str(50 * 10**18)},
        headers=admin_headers,
    )
    assert res_bad_fee.status_code == 400

    # 3. Valid voucher issuance
    final_cost = 40 * 10**18
    fee = 4 * 10**18
    res_voucher = await client.post(
        "/v1/settlement/vouchers",
        json={"job_id": str(job.id), "final_cost_units": str(final_cost), "platform_fee_units": str(fee)},
        headers=admin_headers,
    )
    assert res_voucher.status_code == 200, res_voucher.text
    voucher = res_voucher.json()
    assert voucher["signature"].startswith("0x")

    # 4. Cryptographic signature verification (matches OpenZeppelin ECDSA.recover)
    job_bytes = bytes.fromhex(voucher["job_bytes32"][2:])
    contract_addr = to_checksum_address(voucher["contract_address"])
    packed = encode_packed(
        ["bytes32", "uint256", "uint256", "uint256", "address"],
        [job_bytes, final_cost, fee, voucher["chain_id"], contract_addr],
    )
    msg_hash = keccak(packed)
    signable = encode_defunct(primitive=msg_hash)
    recovered_address = Account.recover_message(
        signable,
        signature=bytes.fromhex(voucher["signature"][2:]),
    )
    assert recovered_address.lower() == voucher["signer_address"].lower()


@pytest.mark.asyncio
async def test_confirm_settlement_and_reconciliation(client: AsyncClient, db_engine):
    """Test on-chain settlement confirmation and ledger reconciliation stats."""
    customer, c_token = await _create_test_user(db_engine, "cust3@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov3@test.com", Role.provider)
    admin, a_token = await _create_test_user(db_engine, "admin3@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)

    headers = {"Authorization": f"Bearer {c_token}"}
    admin_headers = {"Authorization": f"Bearer {a_token}"}
    lock_payload = {
        "job_id": str(job.id),
        "provider_id": str(provider.id),
        "locked_amount_units": str(60 * 10**18),
        "deadline_seconds": 86400,
        "lock_tx_hash": "0x" + "c" * 64,
        "customer_address": "0x1111111111111111111111111111111111111111",
        "provider_address": "0x2222222222222222222222222222222222222222",
    }
    await client.post("/v1/settlement/escrows", json=lock_payload, headers=headers)
    await client.post(
        "/v1/settlement/vouchers",
        json={"job_id": str(job.id), "final_cost_units": str(45 * 10**18), "platform_fee_units": str(5 * 10**18)},
        headers=admin_headers,
    )

    # Confirm settlement tx
    res_confirm = await client.post(
        "/v1/settlement/confirm",
        json={"job_id": str(job.id), "settle_tx_hash": "0x" + "d" * 64, "block_number": 12345},
        headers=admin_headers,
    )
    assert res_confirm.status_code == 200
    escrow = res_confirm.json()
    assert escrow["status"] == EscrowStatus.SETTLED.value
    assert escrow["reconciled"] is True
    assert escrow["settle_tx_hash"] == "0x" + "d" * 64

    # Check reconciliation summary
    res_recon = await client.get("/v1/settlement/reconciliation", headers=admin_headers)
    assert res_recon.status_code == 200
    recon = res_recon.json()
    assert recon["total_escrows"] >= 1
    assert recon["ledger_parity_verified"] is True

    # Check transactions list
    res_txs = await client.get("/v1/settlement/transactions", headers=admin_headers)
    assert res_txs.status_code == 200
    txs = res_txs.json()
    assert len(txs) >= 2
    settle_tx = next(t for t in txs if t["tx_type"] == "settle")
    assert settle_tx["status"] == "reconciled"
    assert settle_tx["amount_units"] == str(45 * 10**18)


# ===========================================================================
# Audit fixes — settlement authorization (C1/H1/H2) and double-spend (H3)
# ===========================================================================
def _lock_payload(job, provider, tx="0xe" + "e" * 63, amt=10 * 10**18):
    return {
        "job_id": str(job.id),
        "provider_id": str(provider.id),
        "locked_amount_units": str(amt),
        "deadline_seconds": 86400,
        "lock_tx_hash": tx,
        "customer_address": "0x1111111111111111111111111111111111111111",
        "provider_address": "0x2222222222222222222222222222222222222222",
    }


@pytest.mark.asyncio
async def test_settlement_privileged_endpoints_require_admin(client: AsyncClient, db_engine):
    """Non-admins cannot mint vouchers / confirm / refund / read platform-wide data
    (C1). Also proves list_escrows no longer 500s on the old `current_user.role` bug (H1)."""
    customer, c_token = await _create_test_user(db_engine, "cust_authz@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_authz@test.com", Role.provider)
    job = await _create_job_setup(db_engine, customer, provider)
    h = {"Authorization": f"Bearer {c_token}"}

    await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + "e" * 64), headers=h)

    # list_escrows must return 200 (not 500) and be caller-scoped
    assert (await client.get("/v1/settlement/escrows", headers=h)).status_code == 200

    assert (await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(5 * 10**18),
        "platform_fee_units": str(1 * 10**18)}, headers=h)).status_code == 403
    assert (await client.post("/v1/settlement/confirm", json={
        "job_id": str(job.id), "settle_tx_hash": "0x" + "d" * 64, "block_number": 1}, headers=h)).status_code == 403
    assert (await client.post("/v1/settlement/refund", json={
        "job_id": str(job.id), "refund_tx_hash": "0x" + "d" * 64, "block_number": 1}, headers=h)).status_code == 403
    assert (await client.get("/v1/settlement/transactions", headers=h)).status_code == 403
    assert (await client.get("/v1/settlement/reconciliation", headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_escrow_read_is_owner_scoped(client: AsyncClient, db_engine):
    """A stranger cannot read another user's escrow by job id (H2 IDOR)."""
    customer, c_token = await _create_test_user(db_engine, "cust_idor@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_idor@test.com", Role.provider)
    _other, o_token = await _create_test_user(db_engine, "other_idor@test.com", Role.customer)
    job = await _create_job_setup(db_engine, customer, provider)
    await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + "a" * 64),
                      headers={"Authorization": f"Bearer {c_token}"})

    assert (await client.get(f"/v1/settlement/escrows/{job.id}",
            headers={"Authorization": f"Bearer {c_token}"})).status_code == 200
    assert (await client.get(f"/v1/settlement/escrows/{job.id}",
            headers={"Authorization": f"Bearer {o_token}"})).status_code == 404


@pytest.mark.asyncio
async def test_create_escrow_requires_job_ownership(client: AsyncClient, db_engine):
    """A third party cannot create/squat an escrow on a job they do not own."""
    customer, _ = await _create_test_user(db_engine, "cust_own@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_own@test.com", Role.provider)
    _attacker, x_token = await _create_test_user(db_engine, "atk_own@test.com", Role.customer)
    job = await _create_job_setup(db_engine, customer, provider)
    res = await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + "b" * 64),
                            headers={"Authorization": f"Bearer {x_token}"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_confirm_refund_rejects_settled_escrow(client: AsyncClient, db_engine):
    """A SETTLED escrow cannot also be refunded (H3 escrow double-spend)."""
    customer, c_token = await _create_test_user(db_engine, "cust_dbl@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_dbl@test.com", Role.provider)
    _admin, a_token = await _create_test_user(db_engine, "admin_dbl@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)
    ch = {"Authorization": f"Bearer {c_token}"}
    ah = {"Authorization": f"Bearer {a_token}"}

    await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + "c" * 64, 50 * 10**18), headers=ch)
    await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(40 * 10**18), "platform_fee_units": str(4 * 10**18)}, headers=ah)
    settled = await client.post("/v1/settlement/confirm", json={
        "job_id": str(job.id), "settle_tx_hash": "0x" + "d" * 64, "block_number": 1}, headers=ah)
    assert settled.status_code == 200
    refund = await client.post("/v1/settlement/refund", json={
        "job_id": str(job.id), "refund_tx_hash": "0x" + "f" * 64, "block_number": 2}, headers=ah)
    assert refund.status_code == 409


@pytest.mark.asyncio
async def test_voucher_cannot_be_reissued(client: AsyncClient, db_engine):
    """One escrow yields at most one valid payout voucher (money F6)."""
    customer, c_token = await _create_test_user(db_engine, "cust_vr@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_vr@test.com", Role.provider)
    _admin, a_token = await _create_test_user(db_engine, "admin_vr@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)
    ch = {"Authorization": f"Bearer {c_token}"}
    ah = {"Authorization": f"Bearer {a_token}"}
    await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + "9" * 64, 50 * 10**18), headers=ch)
    v1 = await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(40 * 10**18), "platform_fee_units": str(4 * 10**18)}, headers=ah)
    assert v1.status_code == 200
    v2 = await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(30 * 10**18), "platform_fee_units": str(3 * 10**18)}, headers=ah)
    assert v2.status_code == 409
