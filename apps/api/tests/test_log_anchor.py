"""Tests for transparency log on-chain anchoring (WP-C / C1-C5)."""

from __future__ import annotations

import decimal
import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from eth_account import Account
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.enums import Role, UserStatus
from app.models.jobs import Job
from app.models.privacy import PrivacyReceipt, TransparencyLogLeaf
from app.models.provider import HardwareInventory, Provider, ProviderCapability, ProviderMachine
from app.models.user import User, UserRole
from app.services.log_anchor import (
    AnchorError,
    COMMIT_MERKLE_ROOT_SELECTOR,
    LogAnchorService,
    build_anchor_section_for_receipt,
    run_anchor_cycle,
)
from app.services.privacy_receipt import create_privacy_receipt_for_job
from app.services.transparency_log import append_transparency_log_entry


# ---------------------------------------------------------------------------
# C1. Configuration & Boot Guard
# ---------------------------------------------------------------------------

def test_anchor_config_boot_guard():
    """C1: In non-dev environments, enabling PRIVACY_ANCHOR_ENABLED requires

    valid RPC URL, non-local chain ID, valid vault address, and secure private key.
    """
    # 1. Dev environment allows default values
    dev_settings = Settings(
        ENVIRONMENT="development",
        PRIVACY_ANCHOR_ENABLED=False,
    )
    assert dev_settings.PRIVACY_ANCHOR_ENABLED is False

    # 2. Prod with anchor enabled but missing/insecure settings fails boot guard
    with pytest.raises(ValueError) as exc:
        Settings(
            ENVIRONMENT="production",
            AUTH_SECRET="strong-secret-value-for-production-environment-1234",
            SESSION_SECRET="strong-session-secret-value-for-production-1234",
            SETTLEMENT_ENABLED=False,
            PRIVACY_ENABLED=False,
            PRIVACY_ANCHOR_ENABLED=True,
            PRIVACY_ANCHOR_RPC_URL="",
            PRIVACY_ANCHOR_VAULT_ADDRESS="0x123",  # invalid address
            PRIVACY_ANCHOR_PRIVATE_KEY="",
            PRIVACY_ANCHOR_CHAIN_ID=31337,  # local dev chain
        )
    err = str(exc.value)
    assert "PRIVACY_ANCHOR_RPC_URL" in err
    assert "PRIVACY_ANCHOR_VAULT_ADDRESS" in err
    assert "PRIVACY_ANCHOR_PRIVATE_KEY" in err
    assert "PRIVACY_ANCHOR_CHAIN_ID" in err

    # 3. Prod with all valid anchor settings boots successfully
    prod_valid = Settings(
        ENVIRONMENT="production",
        AUTH_SECRET="strong-secret-value-for-production-environment-1234",
        SESSION_SECRET="strong-session-secret-value-for-production-1234",
        SETTLEMENT_ENABLED=False,
        PRIVACY_ENABLED=False,
        PRIVACY_ANCHOR_ENABLED=True,
        PRIVACY_ANCHOR_RPC_URL="https://bsc-dataseed.binance.org/",
        PRIVACY_ANCHOR_CHAIN_ID=56,
        PRIVACY_ANCHOR_VAULT_ADDRESS="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        PRIVACY_ANCHOR_PRIVATE_KEY="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
    )
    assert prod_valid.PRIVACY_ANCHOR_ENABLED is True


_PROD_ANCHOR = dict(
    ENVIRONMENT="production",
    AUTH_SECRET="strong-secret-value-for-production-environment-1234",
    SESSION_SECRET="strong-session-secret-value-for-production-1234",
    SETTLEMENT_ENABLED=False,
    PRIVACY_ENABLED=False,
    PRIVACY_ANCHOR_ENABLED=True,
    PRIVACY_ANCHOR_RPC_URL="https://rpc.example",
    PRIVACY_ANCHOR_CHAIN_ID=97,
    PRIVACY_ANCHOR_VAULT_ADDRESS="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    PRIVACY_ANCHOR_PRIVATE_KEY="11" * 32,
)


@pytest.mark.parametrize(
    "override,field",
    [
        ({"PRIVACY_ANCHOR_RPC_URL": "wss://rpc.example"}, "PRIVACY_ANCHOR_RPC_URL"),  # the client is HTTP-only
        ({"PLATFORM_SIGNER_PRIVATE_KEY": "0x" + "11" * 32}, "PRIVACY_ANCHOR_PRIVATE_KEY"),  # R8: separate keys
        ({"PRIVACY_ANCHOR_PRIVATE_KEY": "zz" * 32}, "PRIVACY_ANCHOR_PRIVATE_KEY"),
    ],
)
def test_anchor_boot_guard_refusals(override, field):
    with pytest.raises(ValueError) as exc:
        Settings(**{**_PROD_ANCHOR, **override})
    assert field in str(exc.value)


# ---------------------------------------------------------------------------
# C2 & C3. Anchor Service & Leaf Record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anchor_step_empty_and_no_new_leaves(db_engine):
    """C2: Empty log returns empty status; same size returns no_new_leaves without tx."""
    sm = get_sessionmaker()
    dummy_key = "1" * 64
    service = LogAnchorService(
        rpc_url="http://fake-rpc.test",
        chain_id=1337,
        vault_address="0x" + "2" * 40,
        private_key=dummy_key,
    )

    async with sm() as db_session:
        # 1. Empty log
        res = await service.anchor_step(db_session)
        assert res["status"] == "empty"
        assert res["anchored"] is False

        # 2. Append leaves
        await append_transparency_log_entry(db_session, "IMAGE_HASH", {"image": "abc"})
        await append_transparency_log_entry(db_session, "ATTESTATION_VERDICT", {"v": "1"})
        await db_session.flush()

        # Simulate anchored state
        q = await db_session.execute(select(TransparencyLogLeaf).order_by(TransparencyLogLeaf.leaf_index.desc()).limit(1))
        latest = q.scalar_one()
        latest.anchored_tx_hash = "0x" + "a" * 64
        latest.anchored_block_number = 100
        await db_session.flush()

        # With latest leaf already anchored, anchor_step is an idempotent no-op
        res2 = await service.anchor_step(db_session)
        assert res2["status"] == "no_new_leaves"
        assert res2["anchored"] is False
        assert res2["size"] == 2


class _FakeChain:
    """A JSON-RPC chain double: tracks nonces, records every sent transaction, and can be told to
    sit on another chain id, refuse gas estimation, leave a transaction pending or revert."""

    def __init__(self, *, chain_id=1337, status="0x1", estimate_error=None, pending=False, receipt=True):
        self.chain_id, self.status, self.estimate_error = chain_id, status, estimate_error
        self.mined_nonce = 0
        self.pending = pending
        self.receipt = receipt
        self.calls: list[str] = []
        self.sent: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        method, params = data["method"], data.get("params") or []
        self.calls.append(method)

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

        if method == "eth_chainId":
            return ok(hex(self.chain_id))
        if method == "eth_getTransactionCount":
            return ok(hex(self.mined_nonce + (1 if self.pending and params[1] == "pending" else 0)))
        if method == "eth_gasPrice":
            return ok("0x3b9aca00")
        if method == "eth_estimateGas":
            if self.estimate_error:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": self.estimate_error}})
            return ok("0x186a0")
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0])
            return ok("0x" + "f" * 64)
        if method == "eth_getTransactionReceipt":
            if not self.receipt:
                return ok(None)
            self.mined_nonce += 1
            return ok({"status": self.status, "blockNumber": "0x1a4", "transactionHash": "0x" + "f" * 64})
        return httpx.Response(400, json={"error": "unhandled"})


async def _anchor_with(db_session, chain: _FakeChain, key: str, vault: str = "0x" + "3" * 40):
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handle)) as fake_client:
        service = LogAnchorService(
            rpc_url="http://fake-rpc.test", chain_id=1337, vault_address=vault, private_key=key, http_client=fake_client,
        )
        return await service.anchor_step(db_session)


@pytest.mark.asyncio
async def test_anchor_step_success_and_tx_storage(db_engine):
    """C2 & C3: Sends commitMerkleRoot(root, size) signed by the anchor key; stores tx hash & block."""
    import rlp
    from eth_account._utils.legacy_transactions import Transaction as LegacyTx

    sm = get_sessionmaker()
    account = Account.create()
    vault_addr = "0x" + "3" * 40

    async with sm() as db_session:
        await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "hash1"})
        leaf = await append_transparency_log_entry(db_session, "ATTESTATION_VERDICT", {"v": "v1"})
        await db_session.flush()

        chain = _FakeChain()
        res = await _anchor_with(db_session, chain, account.key.hex(), vault_addr)

        assert res["status"] == "anchored" and res["anchored"] is True
        assert res["size"] == 2 and res["root"] == leaf.merkle_root
        assert res["tx_hash"] == "0x" + "f" * 64 and res["block_number"] == 420

        # The one transaction sent: to the vault, from the anchor key, on the configured chain,
        # calling commitMerkleRoot(current root, current size).
        assert len(chain.sent) == 1
        tx = rlp.decode(bytes.fromhex(chain.sent[0].removeprefix("0x")), LegacyTx)
        assert "0x" + tx.to.hex() == vault_addr
        assert Account.recover_transaction(chain.sent[0]) == account.address
        assert tx.v in (1337 * 2 + 35, 1337 * 2 + 36)
        assert tx.data.hex() == COMMIT_MERKLE_ROOT_SELECTOR + leaf.merkle_root + format(2, "064x")

        stored = (
            await db_session.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.leaf_index == 1))
        ).scalar_one()
        assert stored.anchored_tx_hash == "0x" + "f" * 64
        assert stored.anchored_block_number == 420

        # Same size again: nothing new to anchor, so no second transaction.
        again = await _anchor_with(db_session, chain, account.key.hex(), vault_addr)
        assert again["status"] == "no_new_leaves" and len(chain.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain,expected",
    [
        (dict(status="0x0"), "reverted"),
        (dict(chain_id=56), "configured chain"),
        (dict(estimate_error="execution reverted: not anchor"), "eth_estimateGas"),
        (dict(pending=True), "still pending"),
        (dict(receipt=False), "timeout"),
    ],
)
async def test_anchor_step_refusals_leave_the_log_unanchored(db_engine, monkeypatch, chain, expected):
    """A revert, a mis-pointed RPC, a call known to fail, a transaction still in flight or a missing
    receipt: AnchorError, nothing recorded, and (except revert/timeout) nothing sent at all."""
    import app.services.log_anchor as log_anchor

    async def no_sleep(_):
        return None

    monkeypatch.setattr(log_anchor.asyncio, "sleep", no_sleep)
    sm = get_sessionmaker()
    async with sm() as db_session:
        await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "hash1"})
        await db_session.flush()
        fake = _FakeChain(**chain)
        with pytest.raises(AnchorError) as exc:
            await _anchor_with(db_session, fake, Account.create().key.hex())
        assert expected in str(exc.value)
        if expected not in ("reverted", "timeout"):
            assert fake.sent == []
        leaf = (await db_session.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.leaf_index == 0))).scalar_one()
        assert leaf.anchored_tx_hash is None and leaf.anchored_block_number is None


# ---------------------------------------------------------------------------
# C4. Worker Execution & Failure Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_run_anchor_cycle_disabled_and_error_handling(db_engine, monkeypatch):
    """C4: run_anchor_cycle respects settings and surfaces chain errors to the worker."""
    sm = get_sessionmaker()
    from app.core.config import settings

    async with sm() as db_session:
        monkeypatch.setattr(settings, "PRIVACY_ANCHOR_ENABLED", False)
        res = await run_anchor_cycle(db_session)
        assert res is None

        monkeypatch.setattr(settings, "PRIVACY_ANCHOR_ENABLED", True)
        monkeypatch.setattr(settings, "PRIVACY_ANCHOR_RPC_URL", "http://failing-node.test")
        monkeypatch.setattr(settings, "PRIVACY_ANCHOR_VAULT_ADDRESS", "0x" + "4" * 40)
        monkeypatch.setattr(settings, "PRIVACY_ANCHOR_PRIVATE_KEY", "2" * 64)

        await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "1"})
        await db_session.flush()

        # A chain/RPC network failure raises AnchorError from anchor_step
        with pytest.raises(AnchorError):
            await run_anchor_cycle(db_session)


@pytest.mark.asyncio
async def test_worker_chain_outage_never_stops_lease_recovery(db_engine, monkeypatch):
    """C4: with the chain down, every sweep still runs lease recovery, and the failing anchor is
    retried only after a full interval (not on every sweep)."""
    from app import worker
    from app.core.config import settings

    recoveries, anchors = [], []

    async def recovery(db):
        recoveries.append(1)
        return {}

    async def anchor(db):
        anchors.append(1)
        raise AnchorError("RPC down")

    monkeypatch.setattr(worker, "run_recovery_cycle", recovery)
    monkeypatch.setattr(worker, "run_anchor_cycle", anchor)
    monkeypatch.setattr(settings, "PRIVACY_ANCHOR_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_ANCHOR_INTERVAL_SECONDS", 600)
    monkeypatch.setattr(settings, "PRIVACY_WITNESS_THRESHOLD", 0)
    timers = {"anchor": float("-inf"), "witness": float("-inf")}
    sm = get_sessionmaker()
    for now in (0.0, 30.0, 60.0, 599.0):
        await worker.sweep_once(sm, timers, now)
    assert len(recoveries) == 4 and len(anchors) == 1
    await worker.sweep_once(sm, timers, 600.0)
    assert len(recoveries) == 5 and len(anchors) == 2


# ---------------------------------------------------------------------------
# C5. Receipt Read Anchor Section
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_read_anchor_section(db_engine):
    """C5: build_anchor_section_for_receipt returns the smallest anchored checkpoint

    with size >= receipt tree size, its tx hash, and a valid consistency proof.
    """
    sm = get_sessionmaker()
    async with sm() as db_session:
        # 1. Create leaves 0, 1, 2
        l0 = await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "0"})
        l1 = await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "1"})
        l2 = await append_transparency_log_entry(db_session, "IMAGE_HASH", {"img": "2"})
        await db_session.flush()

        # Receipt was created at tree size 2
        receipt_tree_size = 2
        receipt_root = l1.merkle_root

        # Before anchoring, anchor section is None
        anchor_before = await build_anchor_section_for_receipt(db_session, receipt_tree_size, receipt_root)
        assert anchor_before is None

        # Now leaf 2 is anchored on-chain at tree size 3
        l2.anchored_tx_hash = "0x" + "9" * 64
        l2.anchored_block_number = 12345
        await db_session.flush()

        # After anchoring, anchor section returns tree size 3 and consistency proof from size 2 to 3
        anchor_after = await build_anchor_section_for_receipt(db_session, receipt_tree_size, receipt_root)
        assert anchor_after is not None
        assert anchor_after["tree_size"] == 3
        assert anchor_after["merkle_root"] == l2.merkle_root
        assert anchor_after["tx_hash"] == "0x" + "9" * 64
        assert anchor_after["block_number"] == 12345
        assert isinstance(anchor_after["consistency_proof"], list)
        assert len(anchor_after["consistency_proof"]) > 0
