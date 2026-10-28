"""Live anchoring on a real EVM (WP-C / C7). Opt-in; needs Node and the compiled contracts:

    cd contracts && npx hardhat compile
    RUN_ANCHOR_INTEGRATION=1 apps/api/.venv/bin/pytest apps/api/tests/test_log_anchor_live.py -v

Starts a LOCAL Hardhat node (chain 31337, its own port), deploys the real ComputeCoinToken and
ComputeStakeVault from contracts/artifacts with a fresh random key funded by hardhat_setBalance,
anchors the transparency log twice with LogAnchorService, and checks with ccverify --rpc that:
- a receipt made at an earlier size verifies through the anchor section (consistency proof + the
  anchored root on chain);
- after the platform rewrites an anchored entry (and recomputes every stored root), the receipt's
  anchor points at a root the chain never saw, and ccverify fails.
Nothing here touches a public network.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from eth_abi import encode
from eth_account import Account
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.privacy import TransparencyLogLeaf
from app.services.attestation_verifier import sign_verdict
from app.services.log_anchor import LogAnchorService, build_anchor_section_for_receipt
from app.services.privacy_keys import verifier_public_key
from app.services.transparency_log import (
    _all_leaf_hashes,
    append_transparency_log_entry,
    compute_merkle_root,
    generate_inclusion_proof,
    hash_leaf,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
if os.path.join(REPO_ROOT, "sdk", "python") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "sdk", "python"))

from computecoin.ccverify import verify_receipt  # noqa: E402

_OPT_IN_VAR = "RUN_ANCHOR_INTEGRATION"
pytestmark = pytest.mark.skipif(
    os.environ.get(_OPT_IN_VAR, "").strip().lower() in ("", "0", "false", "no"),
    reason=f"{_OPT_IN_VAR} is not set. Run: {_OPT_IN_VAR}=1 pytest tests/test_log_anchor_live.py",
)
CHAIN_ID = 31337


def _rpc(url, method, params=()):
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, timeout=10.0)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _artifact(name: str) -> str:
    path = os.path.join(CONTRACTS_DIR, "artifacts", "src", f"{name}.sol", f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["bytecode"]


def _send(url, key, *, to=None, data="0x"):
    acct = Account.from_key(key)
    tx = {
        "nonce": int(_rpc(url, "eth_getTransactionCount", [acct.address, "pending"]), 16),
        "gasPrice": int(_rpc(url, "eth_gasPrice"), 16), "gas": 8_000_000, "chainId": CHAIN_ID, "value": 0, "data": data,
    }
    if to:
        tx["to"] = to
    raw = acct.sign_transaction(tx)
    tx_hash = _rpc(url, "eth_sendRawTransaction", ["0x" + bytes(raw.raw_transaction).hex()])
    for _ in range(50):
        receipt = _rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            assert receipt["status"] == "0x1", receipt
            return receipt
        time.sleep(0.1)
    raise RuntimeError("no receipt")


@pytest.fixture(scope="module")
def chain():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["npx", "hardhat", "node", "--port", str(port)], cwd=CONTRACTS_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        for _ in range(120):
            try:
                if int(_rpc(url, "eth_chainId"), 16) == CHAIN_ID:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.fail("local hardhat node did not start")
        owner = Account.create()
        _rpc(url, "hardhat_setBalance", [owner.address, hex(10**21)])
        key = owner.key.hex()
        token = _send(url, key, data=_artifact("ComputeCoinToken") + encode(["address"], [owner.address]).hex())["contractAddress"]
        args = encode(["address"] * 4, [token, owner.address, owner.address, owner.address]).hex()
        vault = _send(url, key, data=_artifact("ComputeStakeVault") + args)["contractAddress"]
        yield {"url": url, "vault": vault, "key": key}
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)


@pytest.mark.asyncio
async def test_live_anchoring_and_ccverify(chain, db_engine):
    service = LogAnchorService(rpc_url=chain["url"], chain_id=CHAIN_ID, vault_address=chain["vault"], private_key=chain["key"])
    sm = get_sessionmaker()
    job_id, machine_id = str(uuid.uuid4()), str(uuid.uuid4())
    verdict = {
        "verdict_id": str(uuid.uuid4()), "job_id": job_id, "machine_id": machine_id, "image_hash": "ee" * 32,
        "tier_reached": "sealed", "ephemeral_public_key": "11" * 32, "receipt_signing_public_key": "22" * 32,
        "nonce": "n" * 16, "issued_at": "2026-10-28T00:00:00+00:00", "expires_at": "2026-10-28T00:10:00+00:00",
        "verifier_public_key": verifier_public_key(),
    }
    verdict["signature"] = sign_verdict(verdict)

    async with sm() as db:
        verdict_leaf = await append_transparency_log_entry(db, "ATTESTATION_VERDICT", verdict)
        await db.commit()
        # The receipt is made now, at size 1.
        hashes = await _all_leaf_hashes(db)
        proof = generate_inclusion_proof(hashes, 0)
        proof.update(event_type="ATTESTATION_VERDICT", payload=verdict)
        receipt = {
            "job_id": job_id, "isolation_tier": "sealed", "machine_id": machine_id, "image_hash": "ee" * 32,
            "attestation_verdict": verdict, "merkle_root": compute_merkle_root(hashes),
            "inclusion_proofs": {"attestation_verdict": proof},
        }

        # Anchor 1 at size 2, anchor 2 at size 3; a second call at the same size sends nothing.
        await append_transparency_log_entry(db, "IMAGE_HASH", {"img": "a"})
        await db.commit()
        first = await service.anchor_step(db)
        await db.commit()
        assert first["anchored"] is True and first["size"] == 2
        assert (await service.anchor_step(db))["status"] == "no_new_leaves"
        await append_transparency_log_entry(db, "IMAGE_HASH", {"img": "b"})
        await db.commit()
        second = await service.anchor_step(db)
        await db.commit()
        assert second["anchored"] is True and second["size"] == 3

        # Read time: the receipt (size 1) gets the smallest anchor >= 1, i.e. size 2.
        receipt["anchor"] = await build_anchor_section_for_receipt(db, 1, receipt["merkle_root"])
        assert receipt["anchor"]["tree_size"] == 2 and receipt["anchor"]["consistency_proof"]
        good = verify_receipt(receipt, verifier_public_key(), rpc_url=chain["url"], vault_address=chain["vault"])
        assert good["checks"]["anchor_consistency"] is True and good["checks"]["anchored_on_chain"] is True
        assert good["verified"] is True, good

        # The platform rewrites leaf 1 and recomputes every stored root (a full, quiet rewrite).
        leaves = (await db.execute(select(TransparencyLogLeaf).order_by(TransparencyLogLeaf.leaf_index))).scalars().all()
        leaves[1].payload = {"img": "rewritten"}
        leaves[1].leaf_hash = hash_leaf("IMAGE_HASH", leaves[1].payload)
        for i, leaf in enumerate(leaves):
            leaf.merkle_root = compute_merkle_root([x.leaf_hash for x in leaves[: i + 1]])
        await db.commit()
        rewritten = dict(receipt, anchor=await build_anchor_section_for_receipt(db, 1, receipt["merkle_root"]))
    assert rewritten["anchor"]["merkle_root"] != receipt["anchor"]["merkle_root"]
    bad = verify_receipt(rewritten, verifier_public_key(), rpc_url=chain["url"], vault_address=chain["vault"])
    assert bad["checks"]["anchored_on_chain"] is False and bad["verified"] is False
    assert verdict_leaf.leaf_index == 0
