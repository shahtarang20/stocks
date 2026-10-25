"""Phase D2 — chain reconciliation against a REAL EVM chain.

Unlike ``test_d2_chain_recon.py`` (which injects a fake client and runs fully
offline), every test in this module talks to an actual hardhat node over real
JSON-RPC, against the actual compiled ``ComputeSettlement`` contract, with real
``lockBudget`` / ``settleJob`` / ``refundJob`` transactions and a real platform
voucher signature. The point is to break the circularity of validating the
``JsonRpcChainClient`` ABI assumptions against fixtures written by the same
hand that wrote the client.

HOW TO RUN
----------
These are OPT-IN integration tests. A plain ``pytest -q`` skips them.

    # one-off, fixture starts and stops its own hardhat node:
    cd /home/root509/stocks/apps/api
    RUN_CHAIN_INTEGRATION=1 .venv/bin/pytest -q tests/test_d2_chain_recon_live.py

    # or reuse a node you already have up:
    cd /home/root509/stocks/contracts && npx hardhat node     # terminal 1
    cd /home/root509/stocks/apps/api \
      && RUN_CHAIN_INTEGRATION=1 .venv/bin/pytest -q tests/test_d2_chain_recon_live.py

Set ``CHAIN_INTEGRATION_NO_AUTOSTART=1`` to forbid the fixture from starting a
node itself (then an absent node is a hard failure, not an autostart).

HONESTY CONTRACT
----------------
* Without ``RUN_CHAIN_INTEGRATION`` the module SKIPS, naming the command above.
* WITH ``RUN_CHAIN_INTEGRATION`` set, an unreachable or undeployable chain is a
  hard FAILURE, never a skip. An opted-in run that silently skips is exactly how
  a fake green happens.
* Nothing here stands in for the chain. If the node is not there, nothing passes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from datetime import timedelta, timezone
from datetime import datetime as dt

import httpx
import pytest
from eth_utils import keccak
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.settlement import EscrowStatus, OnChainEscrow
from app.services.chain_reconciler import (
    FINDING_DRIFT,
    FINDING_ERROR,
    FINDING_MISSING_OFFCHAIN,
    FINDING_OK,
    ChainReconciler,
    JsonRpcChainClient,
)
from tests.test_d2_chain_recon import (
    _make_job,
    _make_user,
    _post_capture_ledger_txn,
)
from app.models.enums import Role

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
RPC_URL = os.environ.get("CHAIN_INTEGRATION_RPC_URL", "http://127.0.0.1:8545")
RPC_HOST, RPC_PORT = "127.0.0.1", 8545

_OPT_IN_VAR = "RUN_CHAIN_INTEGRATION"
_SKIP_REASON = (
    f"{_OPT_IN_VAR} is not set. These tests need a real EVM chain. Run:\n"
    f"  cd {CONTRACTS_DIR} && npx hardhat node\n"
    f"  cd {os.path.join(REPO_ROOT, 'apps', 'api')} && "
    f"RUN_CHAIN_INTEGRATION=1 .venv/bin/pytest -q tests/test_d2_chain_recon_live.py"
)


def _opted_in() -> bool:
    return os.environ.get(_OPT_IN_VAR, "").strip().lower() not in ("", "0", "false", "no")


pytestmark = pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)


# ---------------------------------------------------------------------------
# Real local chain: start (or reuse), deploy, drive real transactions
# ---------------------------------------------------------------------------
def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _chain_id(timeout: float = 2.0) -> str | None:
    """Return the chain id over real JSON-RPC, or None if nothing answers."""
    try:
        resp = httpx.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            timeout=timeout,
        )
        return resp.json().get("result")
    except Exception:
        return None


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop the node AND the hardhat child npx spawned underneath it."""
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=CONTRACTS_DIR, capture_output=True, text=True, timeout=600, **kw
    )


@pytest.fixture(scope="session")
def live_chain():
    """A real hardhat node with the real contracts deployed and driven.

    Reuses a node already listening on 8545; otherwise starts one and tears it
    down at the end of the session. Any failure here FAILS — an opted-in run
    must never go quiet.
    """
    started: subprocess.Popen | None = None
    if not _port_open(RPC_HOST, RPC_PORT):
        if os.environ.get("CHAIN_INTEGRATION_NO_AUTOSTART", "").strip() not in ("", "0"):
            pytest.fail(
                f"No EVM chain on {RPC_URL} and autostart is disabled. "
                f"Start one: cd {CONTRACTS_DIR} && npx hardhat node"
            )
        # Own process group: `npx` spawns hardhat as a child, so terminating
        # only the npx wrapper would leave a live node behind on 8545.
        started = subprocess.Popen(
            ["npx", "hardhat", "node"],
            cwd=CONTRACTS_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.time() + 60
        while time.time() < deadline and not _port_open(RPC_HOST, RPC_PORT):
            if started.poll() is not None:
                out = started.stdout.read() if started.stdout else ""
                pytest.fail(f"`npx hardhat node` exited while starting:\n{out}")
            time.sleep(0.25)

    try:
        chain_id = _chain_id()
        if chain_id is None:
            pytest.fail(
                f"Opted in via {_OPT_IN_VAR} but no JSON-RPC answered at {RPC_URL}. "
                f"Start one: cd {CONTRACTS_DIR} && npx hardhat node"
            )
        if int(chain_id, 16) != 31337:
            pytest.fail(
                f"{RPC_URL} reports chainId {int(chain_id, 16)}; these tests "
                "require the local hardhat chain (31337). Refusing to run "
                "escrow transactions against an unknown chain."
            )

        deploy = _run(
            ["npx", "hardhat", "run", "scripts/deploy-settlement.js", "--network", "localhost"]
        )
        if deploy.returncode != 0:
            pytest.fail(
                "Deploying ComputeSettlement to the local chain failed:\n"
                f"{deploy.stdout}\n{deploy.stderr}"
            )

        scenario = _run(["node", "scripts/local-chain-scenario.mjs"])
        if scenario.returncode != 0:
            pytest.fail(
                "Driving on-chain escrow state failed:\n"
                f"{scenario.stdout}\n{scenario.stderr}"
            )
        try:
            data = json.loads(scenario.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"local-chain-scenario.mjs produced non-JSON output: {exc}\n{scenario.stdout}")

        yield data
    finally:
        if started is not None:
            _kill_group(started)


@pytest.fixture
def chain_settings(live_chain):
    """Point the app's settlement config at the freshly deployed contract."""
    prev_addr = settings.SETTLEMENT_CONTRACT_ADDRESS
    prev_chain = settings.SETTLEMENT_CHAIN_ID
    settings.SETTLEMENT_CONTRACT_ADDRESS = live_chain["settlement_address"]
    settings.SETTLEMENT_CHAIN_ID = live_chain["chain_id"]
    yield live_chain
    settings.SETTLEMENT_CONTRACT_ADDRESS = prev_addr
    settings.SETTLEMENT_CHAIN_ID = prev_chain


def _client(live_chain) -> JsonRpcChainClient:
    """The REAL JSON-RPC client, pointed at the REAL chain."""
    return JsonRpcChainClient(live_chain["rpc_url"])


def _rpc(live_chain, method: str, params: list) -> object:
    resp = httpx.post(
        live_chain["rpc_url"],
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=10.0,
    )
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


# ---------------------------------------------------------------------------
# DB rows mirroring the real chain
# ---------------------------------------------------------------------------
async def _make_escrow_row(
    db_engine,
    live_chain,
    sc: dict,
    *,
    status: str | None = None,
    locked: str | None = None,
    final_cost: str | None = None,
) -> OnChainEscrow:
    """One ``on_chain_escrows`` row mirroring a real on-chain escrow.

    Overrides exist so a test can deliberately drift the row away from the
    chain; with no overrides the row is a faithful mirror.
    """
    customer, _ = await _make_user(
        db_engine, f"cust-{uuid.uuid4().hex[:8]}@d2live.test", Role.customer
    )
    provider, _ = await _make_user(
        db_engine, f"prov-{uuid.uuid4().hex[:8]}@d2live.test", Role.provider
    )
    job = await _make_job(db_engine, customer)

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        escrow = OnChainEscrow(
            job_id=job.id,
            customer_id=customer.id,
            provider_id=provider.id,
            chain_id=live_chain["chain_id"],
            contract_address=live_chain["settlement_address"],
            token_address=live_chain["token_address"],
            job_bytes32=sc["job_bytes32"],
            customer_address=sc["customer_address"],
            provider_address=sc["provider_address"],
            locked_amount_units=locked if locked is not None else sc["locked_amount_units"],
            deadline=dt.fromtimestamp(sc["deadline"], tz=timezone.utc),
            status=status if status is not None else sc["status"],
            lock_tx_hash=sc["lock_tx_hash"],
            final_cost_units=(
                final_cost if final_cost is not None else sc.get("final_cost_units")
            ),
            platform_fee_units=sc.get("platform_fee_units"),
            settle_tx_hash=sc.get("settle_tx_hash"),
            refund_tx_hash=sc.get("refund_tx_hash"),
        )
        db.add(escrow)
        await db.commit()
        await db.refresh(escrow)

    # A settled escrow must have a posted off-chain capture behind it, or the
    # reconciler (correctly) reports drift for a reason unrelated to the chain.
    if escrow.status == EscrowStatus.SETTLED.value:
        await _post_capture_ledger_txn(db_engine, customer, job)
    return escrow


async def _reconcile(db_engine, client):
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        run = await ChainReconciler(client, chain_id=settings.SETTLEMENT_CHAIN_ID).run(db)
        await db.commit()
        await db.refresh(run)
        return run


def _findings(run) -> list[dict]:
    return (run.detail or {}).get("findings", [])


def _by_job(run, job_b32: str) -> dict:
    wanted = job_b32.lower()
    for f in _findings(run):
        if (f.get("job_bytes32") or "").lower() == wanted:
            return f
    raise AssertionError(f"no finding for {job_b32}; got {_findings(run)}")


# ===========================================================================
# 1. The three ABI facts, checked against the ACTUAL deployed contract
# ===========================================================================
def test_abi_escrow_key_is_keccak_of_packed_customer_and_job(chain_settings):
    """escrows is keyed by keccak256(abi.encodePacked(customer, jobId))."""
    live = chain_settings
    sc = live["scenarios"]["locked_match"]
    client = _client(live)

    derived = client.escrow_id(sc["customer_address"], sc["job_bytes32"])

    # What the contract itself says, read over real JSON-RPC.
    selector = "0x" + keccak(text="getEscrowId(address,bytes32)")[:4].hex()
    data = (
        selector
        + sc["customer_address"][2:].lower().rjust(64, "0")
        + sc["job_bytes32"][2:]
    )
    on_chain = _rpc(
        live, "eth_call", [{"to": live["settlement_address"], "data": data}, "latest"]
    )

    assert derived.lower() == str(on_chain).lower(), (
        "client escrow key derivation disagrees with the deployed contract"
    )
    assert derived.lower() == sc["escrow_id"].lower()
    # And the naive "keyed by jobId" assumption really is wrong.
    assert derived.lower() != sc["job_bytes32"].lower()


def test_abi_escrow_getter_returns_six_words(chain_settings):
    """escrows(bytes32) returns SIX words: customer, provider, lockedAmount,
    deadline, nonce, settled — decoded here from the real return data."""
    live = chain_settings
    sc = live["scenarios"]["locked_match"]
    client = _client(live)

    key = client.escrow_id(sc["customer_address"], sc["job_bytes32"])
    raw = _rpc(
        live,
        "eth_call",
        [
            {
                "to": live["settlement_address"],
                "data": client.ESCROW_SELECTOR + key[2:],
            },
            "latest",
        ],
    )
    body = str(raw)[2:]
    assert len(body) == 64 * 6, (
        f"expected a six-word EscrowLock struct, got {len(body) // 64} words"
    )

    words = JsonRpcChainClient._words(str(raw))
    customer, provider, locked, deadline, nonce, settled = words
    assert f"0x{customer:040x}".lower() == sc["customer_address"].lower()
    assert f"0x{provider:040x}".lower() == sc["provider_address"].lower()
    assert locked == int(sc["locked_amount_units"])
    assert deadline == sc["deadline"]
    assert nonce == 0
    assert settled == 0


def test_abi_jobsettled_topic_matches_a_real_emitted_log(chain_settings):
    """The five-parameter JobSettled topic matches a log the chain emitted."""
    live = chain_settings
    sc = live["scenarios"]["settled_match"]

    assert sc["settled_log_topics"], "scenario did not capture a JobSettled log"
    assert sc["settled_log_topics"][0].lower() == JsonRpcChainClient.SETTLED_TOPIC.lower()

    logs = _rpc(
        live,
        "eth_getLogs",
        [
            {
                "address": live["settlement_address"],
                "fromBlock": "0x0",
                "toBlock": "latest",
                "topics": [JsonRpcChainClient.SETTLED_TOPIC],
            }
        ],
    )
    assert isinstance(logs, list) and logs, "the real topic matched no emitted log"
    jobs = {str(entry["topics"][1]).lower() for entry in logs}
    assert sc["job_bytes32"].lower() in jobs

    # A four-parameter JobSettled topic — the pre-audit guess — matches nothing.
    wrong = "0x" + keccak(text="JobSettled(bytes32,address,uint256,uint256)").hex()
    assert wrong.lower() != JsonRpcChainClient.SETTLED_TOPIC.lower()
    assert _rpc(
        live,
        "eth_getLogs",
        [
            {
                "address": live["settlement_address"],
                "fromBlock": "0x0",
                "toBlock": "latest",
                "topics": [wrong],
            }
        ],
    ) == []


def test_real_client_reads_every_real_escrow_state(chain_settings):
    """The REAL client decodes locked / settled / refunded from the REAL chain."""
    import asyncio

    live = chain_settings
    client = _client(live)

    async def go():
        out = {}
        for label, sc in live["scenarios"].items():
            out[label] = await client.fetch_escrow(
                contract_address=live["settlement_address"],
                job_bytes32=sc["job_bytes32"],
                customer_address=sc["customer_address"],
            )
        return out

    states = asyncio.run(go())

    locked = states["locked_match"]
    assert locked is not None and locked.status == EscrowStatus.LOCKED.value
    assert locked.locked_amount_units == int(
        live["scenarios"]["locked_match"]["locked_amount_units"]
    )

    settled = states["settled_match"]
    sc = live["scenarios"]["settled_match"]
    assert settled is not None and settled.status == EscrowStatus.SETTLED.value
    # providerPayout + platformFee, read out of the real JobSettled log.
    assert settled.settled_amount_units == int(sc["final_cost_units"])
    assert settled.locked_amount_units == int(sc["locked_amount_units"])

    refunded = states["refunded_match"]
    rsc = live["scenarios"]["refunded_match"]
    assert refunded is not None and refunded.status == EscrowStatus.REFUNDED.value
    assert refunded.refunded_amount_units == int(rsc["refunded_amount_units"])


def test_absent_escrow_on_real_chain_reads_as_none(chain_settings):
    """An escrow that was never locked reads as None, not as a zero-value lock."""
    import asyncio

    live = chain_settings
    client = _client(live)
    never = "0x" + keccak(text="d2-live-never-locked").hex()

    state = asyncio.run(
        client.fetch_escrow(
            contract_address=live["settlement_address"],
            job_bytes32=never,
            customer_address=live["scenarios"]["locked_match"]["customer_address"],
        )
    )
    assert state is None


# ===========================================================================
# 2. The REAL reconciler against the REAL chain
# ===========================================================================
async def test_db_matching_real_chain_reconciles_ok(db_engine, chain_settings):
    live = chain_settings
    for sc in live["scenarios"].values():
        await _make_escrow_row(db_engine, live, sc)

    run = await _reconcile(db_engine, _client(live))

    assert run.escrows_checked == len(live["scenarios"])
    assert run.detail["errors"] == [], run.detail["errors"]
    assert run.drift_count == 0, _findings(run)
    assert run.ok is True
    assert all(f["classification"] == FINDING_OK for f in _findings(run))


async def test_wrong_locked_amount_is_reported_as_drift(db_engine, chain_settings):
    live = chain_settings
    sc = live["scenarios"]["locked_match"]
    wrong = str(int(sc["locked_amount_units"]) + 1)
    await _make_escrow_row(db_engine, live, sc, locked=wrong)
    for label in ("settled_match", "refunded_match", "orphan_settled", "orphan_locked"):
        await _make_escrow_row(db_engine, live, live["scenarios"][label])

    run = await _reconcile(db_engine, _client(live))

    finding = _by_job(run, sc["job_bytes32"])
    assert finding["classification"] == FINDING_DRIFT
    assert "locked_amount_mismatch" in finding["reasons"]
    assert finding["expected"]["locked_amount_units"] == wrong
    assert finding["actual"]["locked_amount_units"] == sc["locked_amount_units"]
    assert run.ok is False


async def test_db_says_settled_while_chain_says_locked_is_drift(db_engine, chain_settings):
    live = chain_settings
    sc = live["scenarios"]["locked_match"]
    await _make_escrow_row(
        db_engine,
        live,
        sc,
        status=EscrowStatus.SETTLED.value,
        final_cost=sc["locked_amount_units"],
    )
    for label in ("settled_match", "refunded_match", "orphan_settled", "orphan_locked"):
        await _make_escrow_row(db_engine, live, live["scenarios"][label])

    run = await _reconcile(db_engine, _client(live))

    finding = _by_job(run, sc["job_bytes32"])
    assert finding["classification"] == FINDING_DRIFT
    assert "status_mismatch" in finding["reasons"]
    assert finding["db_status"] == EscrowStatus.SETTLED.value
    assert finding["chain_status"] == EscrowStatus.LOCKED.value
    assert run.ok is False


async def test_escrow_on_chain_with_no_db_row_is_missing_offchain(
    db_engine, chain_settings
):
    """Both a settled and a still-OPEN orphan must surface.

    The still-open one matters most: those are customer tokens genuinely held
    by the contract that our books have no record of.
    """
    live = chain_settings
    for label in ("locked_match", "settled_match", "refunded_match"):
        await _make_escrow_row(db_engine, live, live["scenarios"][label])

    run = await _reconcile(db_engine, _client(live))

    missing = {
        f["job_bytes32"].lower()
        for f in _findings(run)
        if f["classification"] == FINDING_MISSING_OFFCHAIN
    }
    assert live["scenarios"]["orphan_settled"]["job_bytes32"].lower() in missing
    assert live["scenarios"]["orphan_locked"]["job_bytes32"].lower() in missing, (
        "an escrow still LOCKED on-chain with no DB row was not reported"
    )
    assert run.ok is False


async def test_unreachable_rpc_is_an_error_not_a_silent_pass(db_engine, chain_settings):
    """Pointed at a dead port, the run must be ok=False with an error finding."""
    live = chain_settings
    sc = live["scenarios"]["locked_match"]
    await _make_escrow_row(db_engine, live, sc)

    # A port deliberately chosen to have nothing behind it.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    assert not _port_open("127.0.0.1", dead_port)

    dead = JsonRpcChainClient(f"http://127.0.0.1:{dead_port}", timeout_seconds=2.0)
    run = await _reconcile(db_engine, dead)

    assert run.ok is False
    assert run.detail["error_count"] >= 1
    classifications = {f["classification"] for f in _findings(run)}
    assert FINDING_ERROR in classifications
    assert FINDING_OK not in classifications, "an unreadable chain must never read as ok"


# ---------------------------------------------------------------------------
# Voucher nonce — the signer must mint at the CONTRACT's current value
# ---------------------------------------------------------------------------
def _scenario(live_chain, key: str) -> dict:
    sc = live_chain["scenarios"].get(key)
    if sc is None:
        pytest.fail(f"scenario {key!r} missing from local-chain-scenario.mjs output")
    return sc


@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
def test_escrow_nonce_is_read_from_the_contract(chain_settings):
    """The client reads escrowNonce() rather than assuming a value.

    A fresh escrow sits at 0, which is exactly why a hardcoded 0 looked correct
    in every test that never invalidated anything.
    """
    sc = _scenario(chain_settings, "locked_match")
    client = _client(chain_settings)
    import asyncio

    nonce = asyncio.get_event_loop().run_until_complete(
        client.fetch_escrow_nonce(
            contract_address=chain_settings["settlement_address"],
            job_bytes32=sc["job_bytes32"],
            customer_address=sc["customer_address"],
        )
    )
    assert nonce == 0, f"a freshly locked escrow should be at nonce 0, got {nonce}"


@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
def test_invalidate_vouchers_advances_the_nonce_the_signer_must_use(chain_settings):
    """invalidateVouchers() bumps the nonce, and we must follow it.

    This is the failure the hardcoded nonce caused: governance invalidates a
    bad voucher, the chain moves to 1, and a signer stuck at 0 mints vouchers
    that revert with "Stale voucher nonce" forever. The escrow could then never
    be settled -- the emergency lever was a one-way trap.
    """
    import asyncio

    sc = _scenario(chain_settings, "orphan_locked")
    client = _client(chain_settings)
    addr = chain_settings["settlement_address"]

    def nonce_now() -> int:
        return asyncio.get_event_loop().run_until_complete(
            client.fetch_escrow_nonce(
                contract_address=addr,
                job_bytes32=sc["job_bytes32"],
                customer_address=sc["customer_address"],
            )
        )

    before = nonce_now()

    # Owner (hardhat account #0 deployed the contract) invalidates the voucher.
    owner = _rpc(chain_settings, "eth_accounts", [])[0]
    selector = "0x" + keccak(text="invalidateVouchers(address,bytes32)")[:4].hex()
    cust = sc["customer_address"].lower().removeprefix("0x").rjust(64, "0")
    job = sc["job_bytes32"].lower().removeprefix("0x").rjust(64, "0")
    tx = _rpc(
        chain_settings,
        "eth_sendTransaction",
        [{"from": owner, "to": addr, "data": selector + cust + job}],
    )
    receipt = _rpc(chain_settings, "eth_getTransactionReceipt", [tx])
    assert receipt and receipt.get("status") == "0x1", f"invalidateVouchers reverted: {receipt}"

    after = nonce_now()
    assert after == before + 1, (
        f"the contract advanced its nonce {before} -> {after}; a signer pinned "
        "to a constant would now mint vouchers that revert on-chain"
    )


# ---------------------------------------------------------------------------
# Wallet page: the ERC-20 balance read, checked against the REAL deployed token
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
def test_token_balance_matches_an_independent_erc20_call(chain_settings):
    import asyncio

    sc = _scenario(chain_settings, "locked_match")
    client = _client(chain_settings)
    token = chain_settings["token_address"]
    # 0x70a08231 is the standard ERC-20 balanceOf(address) selector, written out here
    # on purpose so this test does not share the production code's own encoding.
    for who in (sc["customer_address"], chain_settings["settlement_address"]):
        raw = _rpc(chain_settings, "eth_call", [{"to": token, "data": "0x70a08231" + who.lower()[2:].rjust(64, "0")}, "latest"])
        got = asyncio.get_event_loop().run_until_complete(client.token_balance(token, who))
        assert got == int(raw, 16), f"{who}: client {got} != independent {int(raw, 16)}"


@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
def test_token_balance_of_a_fresh_address_is_zero_and_escrowed_tokens_are_visible(chain_settings):
    import asyncio

    client = _client(chain_settings)
    token = chain_settings["token_address"]
    nobody = "0x" + "ab" * 20
    assert asyncio.get_event_loop().run_until_complete(client.token_balance(token, nobody)) == 0
    # The settlement contract holds the locked budgets, so its balance is positive.
    held = asyncio.get_event_loop().run_until_complete(client.token_balance(token, chain_settings["settlement_address"]))
    assert held > 0


@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
def test_token_balance_where_no_token_is_deployed_is_an_explicit_error(chain_settings):
    import asyncio

    from app.services.chain_reconciler import ChainClientError

    client = _client(chain_settings)
    with pytest.raises(ChainClientError, match="token_not_deployed"):
        asyncio.get_event_loop().run_until_complete(client.token_balance("0x" + "cd" * 20, "0x" + "ab" * 20))
