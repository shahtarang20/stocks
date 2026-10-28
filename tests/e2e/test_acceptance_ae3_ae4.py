"""End-to-end acceptance AE-3 and AE-4 (docs/operations/proof-of-privacy-remaining-work.md, section 5).

AE-3  Website private job: prepared by the website's own browser code (apps/web/src/lib/ccpp.ts and
      customer-keys.ts, run through vitest), submitted to the real API, run on the host with a
      job-bound verdict and guardian key release, decrypted by the browser code, and its receipt
      verified by the browser code. Every request is recorded: the customer's private key and the
      plaintext never appear in one, and the plaintext is in no database row.

AE-4  Split view: the platform (which holds the log key) shows two DIFFERENT logs of the same size to
      two guardians. The independent witness cosigns the first and refuses the second, so the
      guardian shown the second log refuses to release; the first log's cosignature cannot be
      reused for the second. Uses the real apps/witness and two real apps/guardian instances.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

import apps.guardian.main as guardian_main
import apps.witness.main as witness_main
from apps.guardian.config import GuardianConfig
from apps.witness.config import WitnessConfig
from app.core.config import settings
from app.db.base import Base
from app.models.jobs import Job
from app.services import job_fsm as fsm
from app.services import privacy_crypto as pc
from app.services.attestation_verifier import sign_verdict
from app.services.privacy_keys import log_public_key, verifier_public_key
from app.services.transparency_log import (
    compute_merkle_root,
    generate_inclusion_proof,
    hash_leaf,
    sign_checkpoint,
)
from computecoin_agent.attestation import generate_attestation_report, generate_ephemeral_keypair, generate_receipt_signing_keypair
from computecoin_agent.privacy import execute_sealed_job

from .test_proof_of_privacy_e2e import (
    TEST_IMAGE_HASH,
    TEST_LOCKS,
    TEST_MODEL,
    TEST_PCRS,
    GuardianAsyncClientMock,
    _agent_headers,
    _configure_privacy_environment,
    _fetch_keys,
    _seed_infrastructure,
    _sm,
)

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
HAVE_WEB = (WEB / "node_modules" / ".bin" / "vitest").exists() and shutil.which("node") is not None


# --------------------------------------------------------------------------- AE-3 helpers

def _browser(step: dict) -> dict:
    """Run one step of apps/web/src/lib/browser-bridge.test.ts (the website's own code)."""
    work = Path(tempfile.mkdtemp(prefix="ae3-", dir=REPO / ".tools")) if (REPO / ".tools").exists() else Path(tempfile.mkdtemp(prefix="ae3-"))
    try:
        (work / "in.json").write_text(json.dumps(step))
        env = dict(os.environ, CCPP_BRIDGE_INPUT=str(work / "in.json"), CCPP_BRIDGE_OUTPUT=str(work / "out.json"))
        proc = subprocess.run([str(WEB / "node_modules/.bin/vitest"), "run", "src/lib/browser-bridge.test.ts"],
                              cwd=WEB, env=env, capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        return json.loads((work / "out.json").read_text())
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _attest(client, machine, key: str, nonce: str) -> tuple[dict, str, str]:
    """Attest fresh in-RAM keys. Returns (verdict, host X25519 private key, receipt Ed25519 private key)."""
    eph_priv, eph_pub = generate_ephemeral_keypair()
    sign_priv, sign_pub = generate_receipt_signing_keypair()
    report = generate_attestation_report(machine_id=str(machine.id), signing_key=key, nonce=nonce, isolation_tier="sealed",
                                         custom_measurements=TEST_PCRS)
    raw = json.dumps({
        "nonce": nonce, "isolation_tier": "sealed", "timestamp": report.timestamp, "pcr_measurements": report.pcr_measurements,
        "platform_quote": report.platform_quote, "signature": report.signature, "ephemeral_public_key": eph_pub,
        "receipt_signing_public_key": sign_pub, "security_state": TEST_LOCKS,
    }, sort_keys=True).encode()
    r = await client.post("/v1/agents/attestation/verify", content=raw, headers=_agent_headers(machine.id, key, raw))
    assert r.status_code == 200, r.text
    return r.json()["verdict"], eph_priv, sign_priv


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_WEB, reason="apps/web node_modules (vitest) not installed")
async def test_ae3_website_private_job_round_trip(client: AsyncClient, db_engine, monkeypatch):
    _configure_privacy_environment(monkeypatch)
    _, c_token, _, _, machine, key = await _seed_infrastructure(db_engine)
    marker = f"AE3-SECRET-{uuid.uuid4().hex}"

    sent: list[str] = []

    async def record(request: httpx.Request):
        sent.append(f"{request.method} {request.url}\n{dict(request.headers)}\n{request.content.decode('utf-8', 'replace')}")

    client.event_hooks["request"].append(record)

    # 1. The browser: fetch the guardian keys, make the customer key, encrypt and seal.
    keys = (await client.get("/v1/privacy/verifier-key")).json()
    prepared = _browser({
        "step": "prepare", "guardians": keys["guardians"], "threshold": keys["threshold"],
        "input": {"prompt": f"Summarise: {marker}"}, "workload_type": "text-generation", "model": TEST_MODEL,
        "isolation_tier": "sealed",
    })
    storage = prepared["browser_storage"]
    r = await client.post("/v1/jobs", json=prepared["submission"], headers={"Authorization": f"Bearer {c_token}"})
    assert r.status_code in (200, 201), r.text
    job_id = str(r.json().get("job_id") or r.json().get("id"))

    # 2. The host: eligible machine verdict, keyless claim, job-bound verdict, keys, sealed run.
    async with _sm(db_engine)() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        job.machine_id = machine.id
        for nxt in (fsm.MATCHED, fsm.RESERVED):
            if job.status != fsm.RESERVED:
                await fsm.transition(db, job, nxt, reason="ae3", actor="scheduler")
        await db.commit()
    raw = b"{}"
    nonce = (await client.post("/v1/agents/attestation/challenge", content=raw, headers=_agent_headers(machine.id, key, raw))).json()["nonce"]
    await _attest(client, machine, key, nonce)
    claim_body = b'{"accept_private": true}'
    claim = (await client.post("/v1/agents/jobs/claim", content=claim_body, headers=_agent_headers(machine.id, key, claim_body))).json()
    assert claim["job_id"] == job_id and claim["input"] == {} and claim["wrapped_shares"] is None
    verdict, host_priv, sign_priv = await _attest(client, machine, key, claim["key_attestation_nonce"])
    with patch("app.services.key_guardians.httpx.AsyncClient", GuardianAsyncClientMock):
        keys_resp = await _fetch_keys(client, machine, key, job_id, verdict["verdict_id"])
    assert keys_resp.status_code == 200, keys_resp.text
    sealed = execute_sealed_job(
        job_id=job_id, machine_id=str(machine.id), ephemeral_private_key=host_priv, receipt_signing_private_key=sign_priv,
        result_public_key=claim["result_public_key"], encrypted_input=claim["encrypted_input"],
        wrapped_shares=keys_resp.json()["wrapped_shares"], workload_runner_fn=lambda inp: {"answer": inp["prompt"].upper()},
    )
    result = json.dumps({"status": "success", "encrypted_output": sealed.encrypted_output,
                         "output_commitment": sealed.output_commitment, "wipe_receipt": sealed.wipe_receipt}, sort_keys=True).encode()
    r = await client.post(f"/v1/agents/jobs/{job_id}/result", content=result, headers=_agent_headers(machine.id, key, result))
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text

    # 3. The browser: decrypt the output and verify the receipt locally.
    job_view = (await client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {c_token}"})).json()
    receipt = (await client.get(f"/v1/privacy/receipts/{job_id}", headers={"Authorization": f"Bearer {c_token}"})).json()
    opened = _browser({
        "step": "open", "browser_storage": storage, "encrypted_output": job_view["encrypted_output"], "job_id": job_id,
        "receipt": receipt, "verifier_public_key": verifier_public_key(), "expected_image_hash": TEST_IMAGE_HASH,
    })
    assert opened["output"] == {"answer": f"SUMMARISE: {marker}".upper()}
    assert opened["receipt_verified"] is True, opened["receipt_checks"]

    # 4. Nothing secret left the browser, and the plaintext is stored nowhere.
    assert sent, "requests were recorded"
    for request in sent:
        assert storage["privateKeyHex"] not in request
        assert marker not in request and marker.upper() not in request
    async with _sm(db_engine)() as db:
        for table in Base.metadata.sorted_tables:
            for row in (await db.execute(select(table))).all():
                dumped = json.dumps([str(v) for v in row])
                assert marker not in dumped and marker.upper() not in dumped, table.name
                assert storage["privateKeyHex"] not in dumped, table.name


# --------------------------------------------------------------------------- AE-4

def _signed_log(entries: list[dict]) -> tuple[list[str], dict]:
    hashes = [hash_leaf("KEY_RELEASE" if "guardian" in e else "IMAGE_HASH", e) for e in entries]
    return hashes, sign_checkpoint(len(hashes), compute_merkle_root(hashes), "2026-10-28T00:00:00+00:00")


@pytest.mark.asyncio
async def test_ae4_split_view_is_caught_by_the_witness_and_the_guardian_refuses(monkeypatch, tmp_path):
    state = Path(tempfile.mkdtemp(prefix="ae4-", dir=REPO / ".tools")) if (REPO / ".tools").exists() else tmp_path
    try:
        w_priv, w_pub = pc.generate_ed25519_keypair()
        witness_main.config = WitnessConfig(name="witness-1", private_key=w_priv, log_public_key=log_public_key(),
                                            state_path=str(state / "witness.json"))
        witness = httpx.AsyncClient(transport=httpx.ASGITransport(app=witness_main.app), base_url="http://w")
        guardians = {}
        for name in ("guardian-a", "guardian-b"):
            g_priv, g_pub = pc.generate_x25519_keypair()
            guardians[name] = (g_pub, GuardianConfig(
                name=name, private_key=g_priv, verifier_public_key=verifier_public_key(), log_public_key=log_public_key(),
                trusted_images={TEST_IMAGE_HASH}, state_path=str(state / f"{name}.json"),
                witness_keys={"witness-1": w_pub}, witness_threshold=1,
            ))

        host_priv, host_pub = pc.generate_x25519_keypair()
        verdict = {
            "verdict_id": "v-ae4", "job_id": "job-ae4", "machine_id": "m-ae4", "image_hash": TEST_IMAGE_HASH,
            "tier_reached": "sealed", "ephemeral_public_key": host_pub, "receipt_signing_public_key": pc.generate_ed25519_keypair()[1],
            "nonce": "n" * 16, "issued_at": pc_now(), "expires_at": pc_now(minutes=10), "verifier_public_key": verifier_public_key(),
        }
        verdict["signature"] = sign_verdict(verdict)

        # Both logs start the same; at size 3 they differ (the platform shows log A to guardian-a and
        # a rewritten log B to guardian-b: same size, different history).
        common = [{"img": "boot"}]
        entry = {name: {"guardian": name, "job_id": "job-ae4", "machine_id": "m-ae4", "verdict_id": "v-ae4",
                        "share_index": i + 1, "released_at": "t"} for i, name in enumerate(guardians)}
        log_a = common + [entry["guardian-a"], entry["guardian-b"]]
        log_b = common + [entry["guardian-b"], {"img": "rewritten-history"}]
        hashes_a, cp_a = _signed_log(log_a)
        hashes_b, cp_b = _signed_log(log_b)
        assert cp_a["tree_size"] == cp_b["tree_size"] and cp_a["root_hash"] != cp_b["root_hash"]

        # The witness cosigns log A, then refuses log B at the same size.
        r = await witness.post("/cosign", json={"checkpoint": cp_a, "consistency_proof": []})
        assert r.status_code == 200
        cosig_a = {"witness": "witness-1", "signature": r.json()["signature"]}
        r = await witness.post("/cosign", json={"checkpoint": cp_b, "consistency_proof": []})
        assert r.status_code == 409 and r.json()["detail"] == "split_view_detected"

        async def release(name, hashes, cp, cosigs, leaf_index):
            g_pub, cfg = guardians[name]
            guardian_main.config = cfg
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guardian_main.app), base_url="http://g") as g:
                return await g.post("/release", json={
                    "job_id": "job-ae4", "machine_id": "m-ae4", "attestation_verdict": verdict,
                    "sealed_share": {"x": entry[name]["share_index"], "envelope": pc.seal(g_pub, b"S" * 32, pc.share_aad(name))},
                    "log_entry": entry[name], "inclusion_proof": generate_inclusion_proof(hashes, leaf_index),
                    "checkpoint": cp, "consistency_proof": [], "witness_cosignatures": cosigs,
                })

        # guardian-a sees the witnessed log A and releases.
        ok = await release("guardian-a", hashes_a, cp_a, [cosig_a], 1)
        assert ok.status_code == 200, ok.text
        # guardian-b is shown log B: no witness cosigned it, and A's cosignature does not cover B.
        for cosigs in ([], [cosig_a]):
            refused = await release("guardian-b", hashes_b, cp_b, cosigs, 1)
            assert refused.status_code == 400 and refused.json()["detail"] == "witness_cosignatures_insufficient"
        # The honest view still works for guardian-b.
        assert (await release("guardian-b", hashes_a, cp_a, [cosig_a], 2)).status_code == 200
        await witness.aclose()
    finally:
        if state != tmp_path:
            shutil.rmtree(state, ignore_errors=True)


def pc_now(minutes: int = 0) -> str:
    import datetime as dt

    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()
