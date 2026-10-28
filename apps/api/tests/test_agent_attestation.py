"""Integration tests for Agent-Side Attestation (WP2, Checkpoints 2.1 & 2.4)."""

import decimal
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.privacy import TransparencyLogLeaf, TrustedImage
from app.models.provider import (
    MachineAttestation,
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.user import User, UserRole
from app.services import privacy_crypto as pc
from app.services.attestation_verifier import VERDICT_FIELDS

PCRS = {"pcr0": "a" * 64, "pcr4": "b" * 64, "pcr7": "c" * 64, "pcr11": "d" * 64}
IMAGE = "e" * 64
LOCKS = {"bare_metal": True, "iommu_enabled": True, "kernel_lockdown": True}


def _sm(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _setup_machine(db_engine, signing_key: str = "test-agent-key", stake: str = "2000.00", *, image: bool = True) -> ProviderMachine:
    async with _sm(db_engine)() as db:
        user = User(
            email=f"host-{uuid.uuid4().hex[:6]}@example.com",
            password_hash="pw",
            email_verified=True,
            status=UserStatus.active,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=Role.provider))
        provider = Provider(user_id=user.id, display_name="Host Provider", status="active")
        db.add(provider)
        await db.flush()

        machine = ProviderMachine(
            provider_id=provider.id,
            hostname=f"node-{uuid.uuid4().hex[:4]}",
            agent_version="0.2.0",
            status="registered",
            availability="online",
            asking_price_per_hour=decimal.Decimal("3.00"),
            signing_key=signing_key,
            secret_hash="secret",
            stake_bonded=decimal.Decimal(stake),
            last_heartbeat_at=utcnow(),
        )
        db.add(machine)
        await db.flush()
        db.add(HardwareInventory(machine_id=machine.id, gpu_count=1, gpu_model="RTX 4090", vram_mb=24576))
        db.add(ProviderCapability(machine_id=machine.id, supported_workloads=["text-generation"], supported_isolation_tiers=["container"]))

        if image:  # the trusted image is global; a second machine in one test must not re-add it
            db.add(TrustedImage(image_hash=IMAGE, version_tag="sealed-v1", tier="sealed", expected_pcrs=PCRS, status="active"))
        await db.commit()
        return machine


def _agent_headers(machine_id: uuid.UUID, signing_key: str, raw_body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    body_hash = hashlib.sha256(raw_body).hexdigest()
    canonical = f"{machine_id}\n{ts}\n{body_hash}"
    sig = hmac.new(signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": str(machine_id),
        "X-ComputeCoin-Timestamp": str(ts),
        "X-ComputeCoin-Signature": sig,
    }


@pytest.mark.asyncio
async def test_agent_attestation_flow_success(client: AsyncClient, db_engine, monkeypatch):
    """Checkpoint 2.1: Agent obtains a signed verdict using machine HMAC authentication with NO user login."""
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    signing_key = "agent-super-secret-key"
    machine = await _setup_machine(db_engine, signing_key=signing_key, stake="2000.00")

    # 1. Request challenge nonce
    empty_body = b"{}"
    challenge_headers = _agent_headers(machine.id, signing_key, empty_body)
    resp = await client.post("/v1/agents/attestation/challenge", content=empty_body, headers=challenge_headers)
    assert resp.status_code == 200, resp.text
    challenge = resp.json()
    assert challenge["machine_id"] == str(machine.id)
    nonce = challenge["nonce"]
    assert len(nonce) == 48

    # 2. Build quote and evidence
    _, eph_pub = pc.generate_x25519_keypair()
    _, sign_pub = pc.generate_ed25519_keypair()

    quote_hasher = hashlib.sha384()
    quote_hasher.update(nonce.encode("utf-8"))
    for k in sorted(PCRS.keys()):
        quote_hasher.update(f"{k}:{PCRS[k]}".encode("utf-8"))
    platform_quote = quote_hasher.hexdigest()

    now_iso = utcnow().isoformat()
    measurements_json = json.dumps(PCRS, sort_keys=True)
    canonical_report = f"{machine.id}\nsealed\n{nonce}\n{now_iso}\n{measurements_json}\n{platform_quote}"
    report_sig = hmac.new(signing_key.encode("utf-8"), canonical_report.encode("utf-8"), hashlib.sha256).hexdigest()

    verify_payload = {
        "nonce": nonce,
        "isolation_tier": "sealed",
        "timestamp": now_iso,
        "pcr_measurements": PCRS,
        "platform_quote": platform_quote,
        "signature": report_sig,
        "ephemeral_public_key": eph_pub,
        "receipt_signing_public_key": sign_pub,
        "security_state": LOCKS,
    }
    raw_verify_body = json.dumps(verify_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    verify_headers = _agent_headers(machine.id, signing_key, raw_verify_body)

    resp = await client.post("/v1/agents/attestation/verify", content=raw_verify_body, headers=verify_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verified"] is True
    assert data["isolation_tier"] == "sealed"
    verdict = data["verdict"]
    assert verdict is not None
    assert verdict["tier_reached"] == "sealed"
    assert verdict["ephemeral_public_key"] == eph_pub
    assert verdict["receipt_signing_public_key"] == sign_pub
    assert pc.verify(verdict["verifier_public_key"], pc.canonical_json({k: verdict[k] for k in VERDICT_FIELDS}), verdict["signature"])
    # A machine-level challenge gives a machine-level verdict: it can never release a job's keys.
    assert verdict["job_id"] is None

    # The verdict and its log leaf are stored on the attestation record (what the claim reads;
    # last_health is replaced by every heartbeat, so it cannot hold them).
    async with _sm(db_engine)() as db:
        res = await db.execute(
            select(MachineAttestation).where(MachineAttestation.machine_id == machine.id).order_by(MachineAttestation.created_at.desc())
        )
        att = res.scalars().first()
        assert att.details["verdict"]["verdict_id"] == verdict["verdict_id"]
        assert att.details["log_leaf_index"] is not None


@pytest.mark.asyncio
async def test_agent_attestation_security_failures(client: AsyncClient, db_engine, monkeypatch):
    """Checkpoint 2.4: Replayed nonce, wrong machine signature, and banned machine are refused."""
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    signing_key = "agent-secret-key-sec"
    machine = await _setup_machine(db_engine, signing_key=signing_key, stake="2000.00")

    # 1. Challenge
    resp = await client.post("/v1/agents/attestation/challenge", content=b"{}", headers=_agent_headers(machine.id, signing_key, b"{}"))
    assert resp.status_code == 200
    nonce = resp.json()["nonce"]

    _, eph_pub = pc.generate_x25519_keypair()
    _, sign_pub = pc.generate_ed25519_keypair()

    quote_hasher = hashlib.sha384()
    quote_hasher.update(nonce.encode("utf-8"))
    for k in sorted(PCRS.keys()):
        quote_hasher.update(f"{k}:{PCRS[k]}".encode("utf-8"))
    platform_quote = quote_hasher.hexdigest()

    now_iso = utcnow().isoformat()
    measurements_json = json.dumps(PCRS, sort_keys=True)
    canonical_report = f"{machine.id}\nsealed\n{nonce}\n{now_iso}\n{measurements_json}\n{platform_quote}"
    report_sig = hmac.new(signing_key.encode("utf-8"), canonical_report.encode("utf-8"), hashlib.sha256).hexdigest()

    verify_payload = {
        "nonce": nonce,
        "isolation_tier": "sealed",
        "timestamp": now_iso,
        "pcr_measurements": PCRS,
        "platform_quote": platform_quote,
        "signature": report_sig,
        "ephemeral_public_key": eph_pub,
        "receipt_signing_public_key": sign_pub,
        "security_state": LOCKS,
    }
    raw_body = json.dumps(verify_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # A. Wrong machine HMAC header signature -> 401
    bad_headers = _agent_headers(machine.id, "wrong-key", raw_body)
    res_bad_sig = await client.post("/v1/agents/attestation/verify", content=raw_body, headers=bad_headers)
    assert res_bad_sig.status_code == 401

    # B. Submit validly once -> 200
    good_headers = _agent_headers(machine.id, signing_key, raw_body)
    res_valid = await client.post("/v1/agents/attestation/verify", content=raw_body, headers=good_headers)
    assert res_valid.status_code == 200

    # C. Replay the same nonce -> 400 invalid_or_expired_nonce
    replay_headers = _agent_headers(machine.id, signing_key, raw_body)
    res_replay = await client.post("/v1/agents/attestation/verify", content=raw_body, headers=replay_headers)
    assert res_replay.status_code == 400
    assert "invalid_or_expired_nonce" in res_replay.text

    # D. Banned machine -> 409 machine_is_banned
    async with _sm(db_engine)() as db:
        res = await db.execute(select(ProviderMachine).where(ProviderMachine.id == machine.id))
        m = res.scalar_one()
        m.is_banned = True
        await db.commit()

    # Get fresh nonce
    res_chal2 = await client.post("/v1/agents/attestation/challenge", content=b"{}", headers=_agent_headers(machine.id, signing_key, b"{}"))
    nonce2 = res_chal2.json()["nonce"]

    qh2 = hashlib.sha384()
    qh2.update(nonce2.encode("utf-8"))
    for k in sorted(PCRS.keys()):
        qh2.update(f"{k}:{PCRS[k]}".encode("utf-8"))
    pq2 = qh2.hexdigest()

    canon2 = f"{machine.id}\nsealed\n{nonce2}\n{now_iso}\n{measurements_json}\n{pq2}"
    sig2 = hmac.new(signing_key.encode("utf-8"), canon2.encode("utf-8"), hashlib.sha256).hexdigest()

    verify_payload["nonce"] = nonce2
    verify_payload["platform_quote"] = pq2
    verify_payload["signature"] = sig2
    raw_body2 = json.dumps(verify_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    res_banned = await client.post("/v1/agents/attestation/verify", content=raw_body2, headers=_agent_headers(machine.id, signing_key, raw_body2))
    assert res_banned.status_code == 409
    assert "machine_is_banned" in res_banned.text


async def _attest(client, machine, signing_key, *, heartbeat_between: bool = False):
    resp = await client.post("/v1/agents/attestation/challenge", content=b"{}", headers=_agent_headers(machine.id, signing_key, b"{}"))
    nonce = resp.json()["nonce"]
    if heartbeat_between:
        hb = json.dumps({
            "availability": "online",
            "capability_summary": {"gpu_count": 1, "vram_mb": 24576},
            "health": {"gpu_temp_c": 50.0, "gpu_util_pct": 1.0, "mem_used_mb": 100, "uptime_s": 5},
        }).encode()
        r = await client.post("/v1/agents/heartbeat", content=hb, headers=_agent_headers(machine.id, signing_key, hb))
        assert r.status_code == 200, r.text
    return (await _attest_with_nonce(client, machine, signing_key, nonce))[0]


async def _attest_with_nonce(client, machine, signing_key, nonce):
    """Submit evidence for ``nonce`` with fresh host keys. Returns (response, host_private_key)."""
    q = hashlib.sha384(nonce.encode())
    for k in sorted(PCRS):
        q.update(f"{k}:{PCRS[k]}".encode())
    quote = q.hexdigest()
    ts = utcnow().isoformat()
    sig = hmac.new(signing_key.encode(), f"{machine.id}\nsealed\n{nonce}\n{ts}\n{json.dumps(PCRS, sort_keys=True)}\n{quote}".encode(), hashlib.sha256).hexdigest()
    host_priv, host_pub = pc.generate_x25519_keypair()
    body = json.dumps({
        "nonce": nonce, "isolation_tier": "sealed", "timestamp": ts, "pcr_measurements": PCRS, "platform_quote": quote,
        "signature": sig, "ephemeral_public_key": host_pub,
        "receipt_signing_public_key": pc.generate_ed25519_keypair()[1], "security_state": LOCKS,
    }).encode()
    resp = await client.post("/v1/agents/attestation/verify", content=body, headers=_agent_headers(machine.id, signing_key, body))
    return resp, host_priv


@pytest.mark.asyncio
async def test_a_heartbeat_between_challenge_and_verify_does_not_lose_the_nonce(client: AsyncClient, db_engine, monkeypatch):
    """Heartbeats arrive every 30 s and used to replace last_health, erasing the pending nonce."""
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="hb-key")
    resp = await _attest(client, machine, "hb-key", heartbeat_between=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"]["tier_reached"] == "sealed"


async def _private_job(db_engine, machine) -> tuple[uuid.UUID, bytes]:
    """A RESERVED Sealed job for ``machine``: ciphertext input and guardian-sealed key shares."""
    from app.models.jobs import Job
    from app.services import job_fsm as fsm
    from app.services.privacy_keys import guardian_public_keys

    dk = os.urandom(32)
    guardians = guardian_public_keys()
    shares = pc.split_secret(dk, n=len(guardians), k=2)
    async with _sm(db_engine)() as db:
        user = User(email=f"c-{uuid.uuid4().hex[:6]}@example.com", password_hash="pw", email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        job = Job(
            customer_id=user.id, workload_type="text-generation", model="m", input={}, isolation_tier="sealed",
            machine_id=machine.id, status=fsm.RESERVED, value_at_risk=decimal.Decimal("10"),
            encrypted_input=pc.encrypt_data(dk, b'{"prompt":"x"}', pc.INPUT_AAD),
            dk_shares={n: {"x": x, "envelope": pc.seal(p, s, pc.share_aad(n))} for (n, p), (x, s) in zip(guardians.items(), shares)},
            data_policy={"result_public_key": pc.generate_x25519_keypair()[1]},
        )
        db.add(job)
        await db.commit()
        return job.id, dk


async def _claim(client, machine, signing_key, accept_private=True):
    body = json.dumps({"accept_private": accept_private}).encode()
    return await client.post("/v1/agents/jobs/claim", content=body, headers=_agent_headers(machine.id, signing_key, body))


async def _keys(client, machine, signing_key, job_id, verdict_id):
    body = json.dumps({"verdict_id": verdict_id}).encode()
    return await client.post(f"/v1/agents/jobs/{job_id}/keys", content=body, headers=_agent_headers(machine.id, signing_key, body))


async def _key_release_rows(db_engine, job_id=None):
    async with _sm(db_engine)() as db:
        rows = (await db.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.event_type == "KEY_RELEASE"))).scalars().all()
    return [r for r in rows if job_id is None or r.payload.get("job_id") == str(job_id)]


@pytest.mark.asyncio
async def test_private_jobs_go_only_to_agents_that_declare_a_live_session(client: AsyncClient, db_engine, monkeypatch):
    """B3: a private claim needs accept_private, hands out NO keys, and returns only a nonce bound to the job."""
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="acc-key")
    assert (await _attest(client, machine, "acc-key")).status_code == 200
    job_id, _ = await _private_job(db_engine, machine)

    assert (await _claim(client, machine, "acc-key", accept_private=False)).status_code == 204
    async with _sm(db_engine)() as db:
        assert (await db.get(Job, job_id)).status == fsm.RESERVED

    r = await _claim(client, machine, "acc-key")
    assert r.status_code == 200, r.text
    claim = r.json()
    assert claim["input"] == {}
    assert claim["wrapped_shares"] is None and claim["verdict_id"] is None
    assert claim["key_attestation_required"] is True and len(claim["key_attestation_nonce"]) == 48
    assert await _key_release_rows(db_engine) == []  # nothing is released (or logged) at claim time
    async with _sm(db_engine)() as db:
        health = (await db.get(ProviderMachine, machine.id)).last_health
    assert health["latest_attestation_nonce_job_id"] == str(job_id)


@pytest.mark.asyncio
async def test_job_keys_go_once_to_the_host_that_attested_for_that_job(client: AsyncClient, db_engine, monkeypatch):
    """B4 golden path: claim -> attest with the job nonce -> fetch keys once; a replay is refused."""
    import datetime as dt

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-ok")
    assert (await _attest(client, machine, "k-ok")).status_code == 200
    job_id, dk = await _private_job(db_engine, machine)
    claim = (await _claim(client, machine, "k-ok")).json()

    resp, host_priv = await _attest_with_nonce(client, machine, "k-ok", claim["key_attestation_nonce"])
    assert resp.status_code == 200, resp.text
    verdict = resp.json()["verdict"]
    assert verdict["job_id"] == str(job_id)
    lifetime = dt.datetime.fromisoformat(verdict["expires_at"]) - dt.datetime.fromisoformat(verdict["issued_at"])
    assert lifetime == dt.timedelta(minutes=10)

    r = await _keys(client, machine, "k-ok", job_id, verdict["verdict_id"])
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["job_id"] == str(job_id) and got["verdict_id"] == verdict["verdict_id"]
    shares = [(s["share_index"], pc.open_sealed(host_priv, s["wrapped_share"], pc.release_aad(str(job_id), s["guardian"])))
              for s in got["wrapped_shares"]]
    assert pc.reconstruct_secret(shares, 2) == dk
    assert len(await _key_release_rows(db_engine, job_id)) == 2

    again = await _keys(client, machine, "k-ok", job_id, verdict["verdict_id"])
    assert again.status_code == 409 and again.json()["detail"] == "keys_already_released"
    assert len(await _key_release_rows(db_engine, job_id)) == 2  # the refused replay logged nothing


@pytest.mark.asyncio
async def test_job_keys_refuse_a_machine_level_or_other_job_verdict(client: AsyncClient, db_engine, monkeypatch):
    """B2/B4: only a verdict attested for THIS job releases its keys; the job stays runnable after a wrong ask."""
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-wj")
    assert (await _attest(client, machine, "k-wj")).status_code == 200
    job_a, _ = await _private_job(db_engine, machine)
    claim_a = (await _claim(client, machine, "k-wj")).json()
    verdict_a = (await _attest_with_nonce(client, machine, "k-wj", claim_a["key_attestation_nonce"]))[0].json()["verdict"]
    job_b, _ = await _private_job(db_engine, machine)
    claim_b = (await _claim(client, machine, "k-wj")).json()
    assert {claim_a["job_id"], claim_b["job_id"]} == {str(job_a), str(job_b)}
    job_a, job_b = uuid.UUID(claim_a["job_id"]), uuid.UUID(claim_b["job_id"])

    # A verdict for job A cannot fetch job B's keys.
    r = await _keys(client, machine, "k-wj", job_b, verdict_a["verdict_id"])
    assert r.status_code == 409 and r.json()["detail"] == "verdict_not_for_this_job"
    # A machine-level verdict (job_id null) cannot fetch any job's keys.
    machine_level = (await _attest(client, machine, "k-wj")).json()["verdict"]
    assert machine_level["job_id"] is None
    r = await _keys(client, machine, "k-wj", job_b, machine_level["verdict_id"])
    assert r.status_code == 409 and r.json()["detail"] == "verdict_not_for_this_job"
    # An unknown verdict id is refused too.
    r = await _keys(client, machine, "k-wj", job_b, "no-such-verdict")
    assert r.status_code == 409 and r.json()["detail"] == "verdict_not_found"

    assert await _key_release_rows(db_engine, job_b) == []
    async with _sm(db_engine)() as db:
        assert (await db.get(Job, job_b)).status == fsm.RUNNING  # a wrong ask does not punish the job
    # Job A's own verdict still works.
    assert (await _keys(client, machine, "k-wj", job_a, verdict_a["verdict_id"])).status_code == 200


@pytest.mark.asyncio
async def test_job_keys_refuse_another_machine(client: AsyncClient, db_engine, monkeypatch):
    """B4: another machine can neither fetch the job's keys nor use its own verdict for them."""
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-m1")
    thief = await _setup_machine(db_engine, signing_key="k-m2", image=False)
    assert (await _attest(client, machine, "k-m1")).status_code == 200
    job_id, _ = await _private_job(db_engine, machine)
    claim = (await _claim(client, machine, "k-m1")).json()
    verdict = (await _attest_with_nonce(client, machine, "k-m1", claim["key_attestation_nonce"]))[0].json()["verdict"]

    r = await _keys(client, thief, "k-m2", job_id, verdict["verdict_id"])
    assert r.status_code == 404 and r.json()["detail"] == "job_not_found"
    thief_verdict = (await _attest(client, thief, "k-m2")).json()["verdict"]
    r = await _keys(client, machine, "k-m1", job_id, thief_verdict["verdict_id"])
    assert r.status_code == 409 and r.json()["detail"] == "verdict_not_found"
    assert await _key_release_rows(db_engine, job_id) == []


@pytest.mark.asyncio
async def test_job_keys_refuse_an_expired_verdict_and_fail_the_job(client: AsyncClient, db_engine, monkeypatch):
    """B4: a job-bound verdict lives 10 minutes; after that the job fails cleanly (no keys, hold refunded)."""
    import datetime as dt

    from app.api.routers import agents as agents_router
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-ex")
    assert (await _attest(client, machine, "k-ex")).status_code == 200
    job_id, _ = await _private_job(db_engine, machine)
    claim = (await _claim(client, machine, "k-ex")).json()
    verdict = (await _attest_with_nonce(client, machine, "k-ex", claim["key_attestation_nonce"]))[0].json()["verdict"]

    refunded = []
    real_release = agents_router.billing.release_hold
    async def spy(db, job):
        refunded.append(job.id)
        await real_release(db, job)
    monkeypatch.setattr(agents_router.billing, "release_hold", spy)
    later = utcnow() + dt.timedelta(minutes=11)
    monkeypatch.setattr(agents_router, "utcnow", lambda: later)

    body = json.dumps({"verdict_id": verdict["verdict_id"]}).encode()
    headers = _agent_headers(machine.id, "k-ex", body, ts=int(later.timestamp()))  # the request is sent "later" too
    r = await client.post(f"/v1/agents/jobs/{job_id}/keys", content=body, headers=headers)
    assert r.status_code == 409 and r.json() == {"detail": "key_release_failed", "reason": "verdict_expired"}
    assert await _key_release_rows(db_engine, job_id) == []
    assert refunded == [job_id]
    async with _sm(db_engine)() as db:
        job = await db.get(Job, job_id)
        assert job.status == fsm.FAILED and job.error_code == "key_release_failed:verdict_expired"


@pytest.mark.asyncio
async def test_key_release_logs_and_commits_before_releasing(client: AsyncClient, db_engine, monkeypatch):
    """B5 (R6): if releasing fails AFTER logging, the KEY_RELEASE rows stay committed and the job fails
    with its hold refunded; the release step only ever runs after the commit."""
    from app.api.routers import agents as agents_router
    from app.models.jobs import Job
    from app.services import job_fsm as fsm
    from app.services.key_guardians import GuardianReleaseError

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-r6")
    assert (await _attest(client, machine, "k-r6")).status_code == 200
    job_id, _ = await _private_job(db_engine, machine)
    claim = (await _claim(client, machine, "k-r6")).json()
    verdict = (await _attest_with_nonce(client, machine, "k-r6", claim["key_attestation_nonce"]))[0].json()["verdict"]

    seen_by_release = []
    async def failing_execute(db, *, job, machine, attestation_verdict, plan, **kw):
        # Read through a SEPARATE connection: the entries must already be committed.
        seen_by_release.append(len(await _key_release_rows(db_engine, job.id)))
        raise GuardianReleaseError("external_guardian_unavailable", "down")
    refunded = []
    real_release = agents_router.billing.release_hold
    async def spy(db, job):
        refunded.append(job.id)
        await real_release(db, job)
    monkeypatch.setattr(agents_router, "execute_guardian_release", failing_execute)
    monkeypatch.setattr(agents_router.billing, "release_hold", spy)

    r = await _keys(client, machine, "k-r6", job_id, verdict["verdict_id"])
    assert r.status_code == 409 and r.json() == {"detail": "key_release_failed", "reason": "external_guardian_unavailable"}
    assert seen_by_release == [2]
    assert len(await _key_release_rows(db_engine, job_id)) == 2
    assert refunded == [job_id]
    async with _sm(db_engine)() as db:
        assert (await db.get(Job, job_id)).status == fsm.FAILED


@pytest.mark.asyncio
async def test_a_job_nonce_attests_only_while_the_job_is_running_here(client: AsyncClient, db_engine, monkeypatch):
    """The job-bound challenge cannot mint a verdict for a job that is no longer running on this machine."""
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)
    machine = await _setup_machine(db_engine, signing_key="k-na")
    assert (await _attest(client, machine, "k-na")).status_code == 200
    job_id, _ = await _private_job(db_engine, machine)
    claim = (await _claim(client, machine, "k-na")).json()
    async with _sm(db_engine)() as db:
        job = await db.get(Job, job_id)
        job.status = fsm.CANCELLED
        await db.commit()

    resp, _ = await _attest_with_nonce(client, machine, "k-na", claim["key_attestation_nonce"])
    assert resp.status_code == 409 and "job_not_attestable" in resp.text
