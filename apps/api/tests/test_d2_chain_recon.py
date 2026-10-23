"""Phase D2 — on-chain <-> off-chain reconciliation.

Every test runs fully OFFLINE: a fake chain client stands in for JSON-RPC, so
the suite proves the comparison logic without touching a node. The point of
these tests is that ``ok`` is only ever produced by a real comparison — never by
a flag, and never by an error being swallowed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.routers import chain_recon as chain_recon_router
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import utcnow
from app.main import app
from app.models.enums import Role, UserStatus
from app.models.jobs import Job
from app.models.org import Organization, OrganizationMember, Project
from app.models.reconciliation import ChainReconciliationRun
from app.models.settlement import (
    EscrowStatus,
    OnChainEscrow,
    OnChainTransaction,
    OnChainTxStatus,
    OnChainTxType,
)
from app.models.user import User, UserRole
from app.services import ledger as ledger_service
from app.services.chain_reconciler import (
    FINDING_DRIFT,
    FINDING_ERROR,
    FINDING_MISSING_OFFCHAIN,
    FINDING_MISSING_ONCHAIN,
    FINDING_OK,
    ChainClientError,
    ChainEscrowState,
    ChainReconciler,
)

CONTRACT = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
BASE = "/v1/chain/reconciliation"


# ---------------------------------------------------------------------------
# Fake chain client (the injectable seam the reconciler is built around)
# ---------------------------------------------------------------------------
class FakeChainClient:
    """In-memory stand-in for the settlement contract."""

    def __init__(
        self,
        states: dict[str, ChainEscrowState] | None = None,
        *,
        extra_job_ids: list[str] | None = None,
        fetch_error: Exception | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.states = {k.lower(): v for k, v in (states or {}).items()}
        self.extra_job_ids = [j.lower() for j in (extra_job_ids or [])]
        self.fetch_error = fetch_error
        self.list_error = list_error
        self.fetch_calls = 0
        self.fetch_customer_addresses: list[str | None] = []
        self.listed_contracts: list[str] = []

    async def fetch_escrow(
        self,
        *,
        contract_address: str,
        job_bytes32: str,
        customer_address: str | None = None,
    ):
        self.fetch_calls += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetch_customer_addresses.append(customer_address)
        return self.states.get(job_bytes32.lower())

    async def list_escrow_job_ids(self, *, contract_address: str) -> list[str]:
        self.listed_contracts.append(contract_address)
        if self.list_error is not None:
            raise self.list_error
        return list(self.states.keys()) + self.extra_job_ids


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mount_router():
    """Mount the D2 router if the Integrator has not wired it into main yet."""
    if not any(
        getattr(r, "path", "").startswith(BASE) for r in app.router.routes
    ):
        app.include_router(chain_recon_router.router)
    yield


@pytest.fixture(autouse=True)
def _enable_reconciliation():
    previous = settings.CHAIN_RECONCILE_ENABLED
    settings.CHAIN_RECONCILE_ENABLED = True
    yield
    settings.CHAIN_RECONCILE_ENABLED = previous


@pytest.fixture(autouse=True)
def _clear_client_override():
    yield
    chain_recon_router.set_chain_client(None)


async def _make_user(db_engine, email: str, role: Role) -> tuple[User, str]:
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
    token, _, _ = create_access_token(subject=str(user.id), roles=[role.value])
    return user, token


async def _make_job(db_engine, customer: User) -> Job:
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        org = Organization(name=f"Org-{uuid.uuid4().hex[:6]}", owner_id=customer.id)
        db.add(org)
        await db.flush()
        db.add(
            OrganizationMember(
                org_id=org.id, user_id=customer.id, role=Role.customer.value
            )
        )
        project = Project(org_id=org.id, name="D2", owner_id=customer.id)
        db.add(project)
        await db.flush()
        job = Job(
            project_id=project.id,
            customer_id=customer.id,
            status="completed",
            workload_type="inference",
            model="meta-llama/Llama-3-8B-Instruct",
            input={"prompt": "d2"},
            isolation_tier="container",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job


async def _make_escrow(
    db_engine,
    customer: User,
    provider: User,
    job: Job,
    *,
    status: str = EscrowStatus.LOCKED.value,
    locked: str = "1000",
    final_cost: str | None = None,
) -> OnChainEscrow:
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        escrow = OnChainEscrow(
            job_id=job.id,
            customer_id=customer.id,
            provider_id=provider.id,
            chain_id=settings.SETTLEMENT_CHAIN_ID,
            contract_address=CONTRACT,
            token_address=TOKEN,
            job_bytes32="0x" + uuid.uuid4().hex + uuid.uuid4().hex,
            customer_address="0x" + "33" * 20,
            provider_address="0x" + "44" * 20,
            locked_amount_units=locked,
            deadline=utcnow() + timedelta(days=1),
            status=status,
            lock_tx_hash="0x" + uuid.uuid4().hex * 2,
            final_cost_units=final_cost,
        )
        db.add(escrow)
        await db.commit()
        await db.refresh(escrow)
        return escrow


async def _post_capture_ledger_txn(db_engine, customer: User, job: Job) -> uuid.UUID:
    """Post a REAL balanced capture through the ledger service (no raw writes)."""
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        customer_acct = await ledger_service.get_or_create_customer_account(
            db, customer.id
        )
        revenue = await ledger_service.get_platform_account(db, "platform_revenue")
        txn = await ledger_service.post_transaction(
            db,
            kind="capture",
            legs=[(customer_acct, "debit", 500), (revenue, "credit", 500)],
            idempotency_key=f"d2-capture:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
        )
        await db.commit()
        return txn.id


async def _run_reconciler(db_engine, client_impl) -> ChainReconciliationRun:
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        run = await ChainReconciler(client_impl).run(db)
        await db.commit()
        await db.refresh(run)
        return run


def _findings(run: ChainReconciliationRun) -> list[dict]:
    return (run.detail or {}).get("findings", [])


async def _setup(db_engine, **escrow_kwargs):
    customer, _ = await _make_user(db_engine, f"cust-{uuid.uuid4().hex[:8]}@d2.test", Role.customer)
    provider, _ = await _make_user(db_engine, f"prov-{uuid.uuid4().hex[:8]}@d2.test", Role.provider)
    job = await _make_job(db_engine, customer)
    escrow = await _make_escrow(db_engine, customer, provider, job, **escrow_kwargs)
    return customer, provider, job, escrow


# ---------------------------------------------------------------------------
# Comparison semantics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_matching_locked_escrow_reports_ok(db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.LOCKED.value,
                locked_amount_units=1000,
            )
        }
    )
    run = await _run_reconciler(db_engine, chain)

    assert chain.fetch_calls == 1, "the reconciler must actually read the chain"
    assert run.escrows_checked == 1
    assert run.drift_count == 0
    assert run.ok is True
    assert [f["classification"] for f in _findings(run)] == [FINDING_OK]


@pytest.mark.asyncio
async def test_settled_escrow_matching_chain_and_posted_ledger_is_ok(db_engine):
    customer, _, job, escrow = await _setup(
        db_engine,
        status=EscrowStatus.SETTLED.value,
        locked="1000",
        final_cost="500",
    )
    ledger_txn_id = await _post_capture_ledger_txn(db_engine, customer, job)
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        db.add(
            OnChainTransaction(
                chain_id=settings.SETTLEMENT_CHAIN_ID,
                tx_hash="0x" + uuid.uuid4().hex * 2,
                tx_type=OnChainTxType.SETTLE.value,
                sender_address="0x" + "55" * 20,
                amount_units="500",
                status=OnChainTxStatus.CONFIRMED.value,
                ledger_txn_id=ledger_txn_id,
                job_id=job.id,
            )
        )
        await db.commit()

    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.SETTLED.value,
                locked_amount_units=1000,
                settled_amount_units=500,
            )
        }
    )
    run = await _run_reconciler(db_engine, chain)
    assert run.ok is True, _findings(run)
    assert run.drift_count == 0


@pytest.mark.asyncio
async def test_settled_amount_mismatch_is_drift_and_increments_drift_count(db_engine):
    _, _, _, escrow = await _setup(
        db_engine,
        status=EscrowStatus.SETTLED.value,
        locked="1000",
        final_cost="500",
    )
    # Chain says 777 was settled; our DB says 500. That is real money drift.
    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.SETTLED.value,
                locked_amount_units=1000,
                settled_amount_units=777,
            )
        }
    )
    run = await _run_reconciler(db_engine, chain)

    assert run.escrows_checked == 1
    assert run.drift_count == 1
    assert run.ok is False
    finding = _findings(run)[0]
    assert finding["classification"] == FINDING_DRIFT
    assert "settled_amount_mismatch" in finding["reasons"]
    assert finding["expected"]["settled_amount_units"] == "500"
    assert finding["actual"]["settled_amount_units"] == "777"


@pytest.mark.asyncio
async def test_status_mismatch_is_drift(db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    # DB still thinks it is locked; the contract has already refunded it.
    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.REFUNDED.value,
                locked_amount_units=1000,
                refunded_amount_units=1000,
            )
        }
    )
    run = await _run_reconciler(db_engine, chain)
    assert run.ok is False
    assert run.drift_count == 1
    assert "status_mismatch" in _findings(run)[0]["reasons"]


@pytest.mark.asyncio
async def test_settled_escrow_without_posted_ledger_txn_is_drift(db_engine):
    """A settled escrow with no off-chain capture means money moved on-chain only."""
    _, _, _, escrow = await _setup(
        db_engine,
        status=EscrowStatus.SETTLED.value,
        locked="1000",
        final_cost="500",
    )
    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.SETTLED.value,
                locked_amount_units=1000,
                settled_amount_units=500,
            )
        }
    )
    run = await _run_reconciler(db_engine, chain)
    assert run.ok is False
    assert "ledger_txn_missing_or_unposted" in _findings(run)[0]["reasons"]


@pytest.mark.asyncio
async def test_escrow_in_db_but_absent_on_chain_is_flagged(db_engine):
    await _setup(db_engine, locked="1000")
    chain = FakeChainClient({})  # contract knows nothing about it
    run = await _run_reconciler(db_engine, chain)

    assert run.ok is False
    assert run.drift_count == 1
    assert _findings(run)[0]["classification"] == FINDING_MISSING_ONCHAIN


@pytest.mark.asyncio
async def test_escrow_on_chain_but_missing_in_db_is_flagged(db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    orphan = "0x" + "ab" * 32
    chain = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.LOCKED.value,
                locked_amount_units=1000,
            )
        },
        extra_job_ids=[orphan],
    )
    run = await _run_reconciler(db_engine, chain)

    classifications = [f["classification"] for f in _findings(run)]
    assert FINDING_MISSING_OFFCHAIN in classifications
    assert run.ok is False
    assert run.drift_count == 1
    orphan_finding = next(
        f for f in _findings(run) if f["classification"] == FINDING_MISSING_OFFCHAIN
    )
    assert orphan_finding["job_bytes32"] == orphan


@pytest.mark.asyncio
async def test_chain_client_error_never_reports_ok(db_engine):
    """An unreadable chain must produce an error run, not a false pass."""
    await _setup(db_engine, locked="1000")
    chain = FakeChainClient(
        {}, fetch_error=ChainClientError("rpc_transport_error:eth_call")
    )
    run = await _run_reconciler(db_engine, chain)

    assert run.ok is False
    assert run.detail["error_count"] >= 1
    assert _findings(run)[0]["classification"] == FINDING_ERROR
    assert run.drift_count == 1


@pytest.mark.asyncio
async def test_empty_chain_and_empty_db_is_ok(db_engine):
    """Nothing to compare is genuinely ok — and is recorded as zero checked."""
    run = await _run_reconciler(db_engine, FakeChainClient({}))
    assert run.escrows_checked == 0
    assert run.drift_count == 0
    assert run.ok is True


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_can_trigger_list_and_fetch_run(client: AsyncClient, db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    _, admin_token = await _make_user(
        db_engine, f"admin-{uuid.uuid4().hex[:8]}@d2.test", Role.administrator
    )
    chain_recon_router.set_chain_client(
        FakeChainClient(
            {
                escrow.job_bytes32: ChainEscrowState(
                    job_bytes32=escrow.job_bytes32,
                    status=EscrowStatus.LOCKED.value,
                    locked_amount_units=1000,
                )
            }
        )
    )
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(f"{BASE}/runs", headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["escrows_checked"] == 1
    run_id = body["id"]

    listing = await client.get(f"{BASE}/runs", headers=headers)
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()] == [run_id]

    detail = await client.get(f"{BASE}/runs/{run_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["detail"]["findings"][0]["classification"] == FINDING_OK


@pytest.mark.asyncio
async def test_api_reports_drift_not_ok(client: AsyncClient, db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    _, admin_token = await _make_user(
        db_engine, f"admin-{uuid.uuid4().hex[:8]}@d2.test", Role.administrator
    )
    chain_recon_router.set_chain_client(
        FakeChainClient(
            {
                escrow.job_bytes32: ChainEscrowState(
                    job_bytes32=escrow.job_bytes32,
                    status=EscrowStatus.LOCKED.value,
                    locked_amount_units=999,
                )
            }
        )
    )
    resp = await client.post(
        f"{BASE}/runs", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["drift_count"] == 1


@pytest.mark.asyncio
async def test_idempotency_key_does_not_run_twice(client: AsyncClient, db_engine):
    _, _, _, escrow = await _setup(db_engine, locked="1000")
    _, admin_token = await _make_user(
        db_engine, f"admin-{uuid.uuid4().hex[:8]}@d2.test", Role.administrator
    )
    fake = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.LOCKED.value,
                locked_amount_units=1000,
            )
        }
    )
    chain_recon_router.set_chain_client(fake)
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Idempotency-Key": "sweep-2026-10-23-a",
    }
    first = await client.post(f"{BASE}/runs", headers=headers)
    second = await client.post(f"{BASE}/runs", headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert fake.fetch_calls == 1, "retry must not re-sweep the chain"

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        rows = (await db.scalars(select(ChainReconciliationRun))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_non_admin_is_forbidden(client: AsyncClient, db_engine):
    _, customer_token = await _make_user(
        db_engine, f"cust-{uuid.uuid4().hex[:8]}@d2.test", Role.customer
    )
    headers = {"Authorization": f"Bearer {customer_token}"}
    assert (await client.post(f"{BASE}/runs", headers=headers)).status_code == 403
    assert (await client.get(f"{BASE}/runs", headers=headers)).status_code == 403
    assert (
        await client.get(f"{BASE}/runs/{uuid.uuid4()}", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_anonymous_is_rejected(client: AsyncClient, db_engine):
    assert (await client.post(f"{BASE}/runs")).status_code == 401


@pytest.mark.asyncio
async def test_disabled_flag_blocks_endpoints(client: AsyncClient, db_engine):
    _, admin_token = await _make_user(
        db_engine, f"admin-{uuid.uuid4().hex[:8]}@d2.test", Role.administrator
    )
    settings.CHAIN_RECONCILE_ENABLED = False
    resp = await client.post(
        f"{BASE}/runs", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "chain_reconciliation_disabled"


@pytest.mark.asyncio
async def test_unknown_run_id_is_404(client: AsyncClient, db_engine):
    _, admin_token = await _make_user(
        db_engine, f"admin-{uuid.uuid4().hex[:8]}@d2.test", Role.administrator
    )
    resp = await client.get(
        f"{BASE}/runs/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# JSON-RPC client (the production implementation, exercised without a node)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_jsonrpc_client_rejects_non_http_scheme():
    from app.services.chain_reconciler import JsonRpcChainClient

    with pytest.raises(ChainClientError):
        JsonRpcChainClient("file:///etc/passwd")
    with pytest.raises(ChainClientError):
        JsonRpcChainClient("")


CUSTOMER_ADDR = "0x" + "ab" * 20
JOB_B32 = "0x" + "cd" * 32


def _escrow_getter_return(
    *,
    customer: str = CUSTOMER_ADDR,
    provider: str = "0x" + "ef" * 20,
    locked: int = 1000,
    deadline: int = 1_900_000_000,
    nonce: int = 0,
    settled: bool = False,
) -> str:
    """Encode the SIX words ``escrows(bytes32)`` really returns.

    ``EscrowLock`` is (address customer, address provider, uint256 lockedAmount,
    uint256 deadline, uint256 nonce, bool settled) — see
    contracts/src/ComputeSettlement.sol. There is no settled/refunded amount and
    no status code anywhere in the struct.
    """
    words = [
        int(customer, 16),
        int(provider, 16),
        locked,
        deadline,
        nonce,
        1 if settled else 0,
    ]
    return "0x" + "".join(f"{w:064x}" for w in words)


def _settled_log(job_b32: str, payout: int, fee: int, refund: int) -> dict:
    from app.services.chain_reconciler import JsonRpcChainClient

    return {
        "topics": [JsonRpcChainClient.SETTLED_TOPIC, job_b32, "0x" + "00" * 32],
        "data": "0x" + "".join(f"{w:064x}" for w in (payout, fee, refund)),
    }


@pytest.mark.asyncio
async def test_jsonrpc_client_keys_escrows_by_customer_and_job():
    """The mapping key is keccak(abi.encodePacked(customer, jobId)), not jobId.

    Calling ``escrows(jobId)`` hits an unoccupied slot on a real node, so every
    escrow would be reported missing-on-chain forever.
    """
    from app.services.chain_reconciler import JsonRpcChainClient
    from eth_utils import keccak

    client = JsonRpcChainClient("http://127.0.0.1:8545")
    seen: dict[str, str] = {}

    async def fake_rpc(method, params):
        assert method == "eth_call"
        seen["data"] = params[0]["data"]
        return _escrow_getter_return()

    client._rpc = fake_rpc  # type: ignore[assignment]
    await client.fetch_escrow(
        contract_address=CONTRACT, job_bytes32=JOB_B32, customer_address=CUSTOMER_ADDR
    )
    expected_key = keccak(
        bytes.fromhex(CUSTOMER_ADDR[2:] + JOB_B32[2:])
    ).hex()
    assert seen["data"] == JsonRpcChainClient.ESCROW_SELECTOR + expected_key
    assert seen["data"] != JsonRpcChainClient.ESCROW_SELECTOR + JOB_B32[2:]


@pytest.mark.asyncio
async def test_jsonrpc_client_decodes_real_escrow_struct_as_locked():
    from app.services.chain_reconciler import JsonRpcChainClient

    client = JsonRpcChainClient("http://127.0.0.1:8545")

    async def fake_rpc(method, params):
        return _escrow_getter_return(locked=1000, settled=False)

    client._rpc = fake_rpc  # type: ignore[assignment]
    state = await client.fetch_escrow(
        contract_address=CONTRACT, job_bytes32=JOB_B32, customer_address=CUSTOMER_ADDR
    )
    assert state is not None
    assert state.status == EscrowStatus.LOCKED.value
    assert state.locked_amount_units == 1000


@pytest.mark.asyncio
async def test_jsonrpc_client_reads_settled_amount_from_event_log():
    """The struct has no settled amount — it has to come from JobSettled."""
    from app.services.chain_reconciler import JsonRpcChainClient

    client = JsonRpcChainClient("http://127.0.0.1:8545")

    async def fake_rpc(method, params):
        if method == "eth_call":
            return _escrow_getter_return(locked=1000, settled=True)
        topic = params[0]["topics"][0]
        if topic == JsonRpcChainClient.SETTLED_TOPIC:
            return [_settled_log(JOB_B32, 400, 100, 500)]
        return []

    client._rpc = fake_rpc  # type: ignore[assignment]
    state = await client.fetch_escrow(
        contract_address=CONTRACT, job_bytes32=JOB_B32, customer_address=CUSTOMER_ADDR
    )
    assert state is not None
    assert state.status == EscrowStatus.SETTLED.value
    assert state.settled_amount_units == 500  # providerPayout + platformFee


def test_settled_topic_matches_the_five_parameter_event():
    """JobSettled has FIVE parameters; a four-parameter topic matches no log."""
    from app.services.chain_reconciler import JsonRpcChainClient, _topic

    assert JsonRpcChainClient.SETTLED_TOPIC == _topic(
        "JobSettled(bytes32,address,uint256,uint256,uint256)"
    )
    assert JsonRpcChainClient.SETTLED_TOPIC != _topic(
        "JobSettled(bytes32,address,uint256,uint256)"
    )


@pytest.mark.asyncio
async def test_jsonrpc_client_treats_empty_slot_as_absent():
    from app.services.chain_reconciler import JsonRpcChainClient

    client = JsonRpcChainClient("http://127.0.0.1:8545")

    async def fake_rpc(method, params):
        return "0x" + "0" * (64 * 6)  # zero customer == no such escrow

    client._rpc = fake_rpc  # type: ignore[assignment]
    assert (
        await client.fetch_escrow(
            contract_address=CONTRACT,
            job_bytes32=JOB_B32,
            customer_address=CUSTOMER_ADDR,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Regression: a sweep that read nothing is NOT ok (D2 "green dashboard" bug)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_with_no_matching_escrows_is_not_ok(db_engine):
    """Zero rows for the configured chain must never report ok=true.

    Before the fix, ``_collect_findings`` only contacted the chain for escrows
    already present in ``on_chain_escrows`` for this chain_id. With no matching
    rows it made ZERO RPC calls, produced zero findings and zero errors, and the
    run was recorded ok=true — a green reconciliation for a chain that was never
    queried (a chain_id mismatch or a fresh replica is enough to trigger it).
    """
    client = FakeChainClient()
    run = await _run_reconciler(db_engine, client)

    assert run.escrows_checked == 0
    # The configured contract MUST have been queried; "nothing in the table" is
    # not evidence that the chain holds nothing.
    assert client.listed_contracts == [settings.SETTLEMENT_CONTRACT_ADDRESS]
    assert run.ok is True  # a real read happened and found nothing


@pytest.mark.asyncio
async def test_sweep_that_cannot_read_the_chain_is_not_ok(db_engine):
    """With no rows AND an unreachable node, ok must stay false."""
    client = FakeChainClient(list_error=ChainClientError("rpc down"))
    run = await _run_reconciler(db_engine, client)

    assert client.listed_contracts == [settings.SETTLEMENT_CONTRACT_ADDRESS]
    assert run.ok is False
    errors = (run.detail or {}).get("errors", [])
    assert any("no_chain_read_performed" in e or "rpc down" in e for e in errors)


@pytest.mark.asyncio
async def test_sweep_passes_customer_address_to_the_chain_client(db_engine):
    """The escrow key needs the customer address; without it the key is wrong."""
    customer, provider, job, escrow = await _setup(db_engine)
    client = FakeChainClient(
        {
            escrow.job_bytes32: ChainEscrowState(
                job_bytes32=escrow.job_bytes32,
                status=EscrowStatus.LOCKED.value,
                locked_amount_units=1000,
            )
        }
    )
    await _run_reconciler(db_engine, client)
    assert client.fetch_customer_addresses == [escrow.customer_address]
