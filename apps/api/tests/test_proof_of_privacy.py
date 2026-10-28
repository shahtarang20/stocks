"""Proof-of-Privacy (ADR-0005), API side.

Each test pins a property that an earlier version of this code got wrong:
- privacy jobs carry ciphertext only; plaintext or raw key shares are refused;
- the Sealed tier is granted only by evidence (trusted image, security locks, stake), never by a
  host's self-report, a stake it gave itself, or software attestation outside dev/test;
- verdicts are Ed25519-signed and a self-signed or edited verdict is refused;
- shares are released only to the host that attested FOR THAT JOB (verdict v2 carries a signed
  job_id), re-sealed so only its private key opens them, and logged before release; the platform
  alone cannot release when it holds < k guardian keys;
- receipts get an id on the job, verify offline, and fail when anything is edited;
- canaries look like real private jobs; only security staff can report a leak;
- stake can only be recorded by an administrator and grants no tier.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import hmac
import json
import secrets
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, settings
from app.core.security import create_access_token, hash_password, hash_token
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.jobs import Job
from app.models.model_registry import Model, ModelVersion
from app.models.privacy import CanaryJob, PrivacyReceipt, TransparencyLogLeaf, TrustedImage
from app.models.provider import HardwareInventory, Provider, ProviderCapability, ProviderMachine
from app.models.reputation import SlashEvent
from app.models.user import User, UserRole
from app.services import privacy_crypto as pc
from app.services.attestation_verifier import sign_verdict, verify_machine_evidence, verify_verdict_signature
from app.services.canary import create_canary_job
from app.services.key_guardians import GuardianReleaseError, release_guardian_shares
from app.services.privacy_keys import guardian_public_keys, verifier_public_key
from app.services.privacy_receipt import create_privacy_receipt_for_job, verify_privacy_receipt_offline, wipe_receipt_signing_bytes
from app.services.transparency_log import append_transparency_log_entry, get_inclusion_proof_for_leaf, verify_inclusion_proof

MODEL = "meta-llama/Llama-3-8b-instruct"
PCRS = {"pcr0": "a" * 64, "pcr4": "b" * 64, "pcr7": "c" * 64, "pcr11": "d" * 64}
IMAGE = "e" * 64
LOCKS = {"bare_metal": True, "iommu_enabled": True, "kernel_lockdown": True}


# --------------------------------------------------------------------------- helpers

def _sm(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _user(db_engine, email: str, role: Role = Role.customer) -> tuple[User, str]:
    async with _sm(db_engine)() as db:
        user = User(email=email, password_hash=hash_password("Password123!"), email_verified=True, status=UserStatus.active)
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=role))
        await db.commit()
    token, _, _ = create_access_token(subject=str(user.id), roles=[role.value])
    return user, token


async def _machine(db_engine, user: User, hostname: str, *, tiers: list[str], status: str = "detected",
                   stake: str = "0", signing_key: str = "machine-key", region: str | None = "us-east") -> ProviderMachine:
    async with _sm(db_engine)() as db:
        provider = (await db.execute(select(Provider).where(Provider.user_id == user.id))).scalar_one_or_none()
        if provider is None:
            provider = Provider(user_id=user.id, display_name="Host", status="active")
            db.add(provider)
            await db.flush()
        m = ProviderMachine(
            provider_id=provider.id, hostname=hostname, agent_version="0.2.0", status="registered", availability="online",
            asking_price_per_hour=decimal.Decimal("3.00"), signing_key=signing_key, secret_hash=hash_token("s"),
            stake_bonded=decimal.Decimal(stake), last_heartbeat_at=utcnow(), region=region,
        )
        db.add(m)
        await db.flush()
        db.add(HardwareInventory(machine_id=m.id, gpu_count=4, gpu_model="NVIDIA RTX 4090", vram_mb=24576, verification_status=status))
        db.add(ProviderCapability(machine_id=m.id, supported_workloads=["text-generation"], supported_isolation_tiers=tiers, verification_status=status))
        await db.commit()
        return m


async def _seed_model(db_engine):
    async with _sm(db_engine)() as db:
        model = Model(name=f"m-{uuid.uuid4().hex[:6]}", trust_status="approved")
        db.add(model)
        await db.flush()
        db.add(ModelVersion(model_id=model.id, version_tag=MODEL, runtime="simulated", supported_workloads=["text-generation"], trust_status="approved"))
        await db.commit()


async def _trusted_image(db_engine, tier: str = "sealed"):
    async with _sm(db_engine)() as db:
        db.add(TrustedImage(image_hash=IMAGE, version_tag="sealed-os-test", tier=tier, expected_pcrs=PCRS, status="active"))
        await db.commit()


def _private_job_body(prompt: str = "TOP SECRET", **extra):
    """What the SDK sends: ciphertext + guardian-sealed shares + result key. Returns (body, dk, result_priv)."""
    dk = secrets.token_bytes(32)
    guardians = guardian_public_keys()
    shares = pc.split_secret(dk, n=len(guardians), k=2)
    result_priv, result_pub = pc.generate_x25519_keypair()
    body = {
        "workload_type": "text-generation",
        "model": MODEL,
        "input": {},
        "isolation_tier": "sealed",
        "encrypted_input": pc.encrypt_data(dk, json.dumps({"prompt": prompt}).encode(), pc.INPUT_AAD),
        "dk_shares": {n: {"x": x, "envelope": pc.seal(pub, s, pc.share_aad(n))} for (n, pub), (x, s) in zip(guardians.items(), shares)},
        "data_policy": {"result_public_key": result_pub},
    }
    body.update(extra)
    return body, dk, result_priv


def _signed_quote(machine: ProviderMachine, signing_key: str, tier: str, nonce: str):
    ts = utcnow().isoformat()
    q = hashlib.sha384(nonce.encode())
    for k in sorted(PCRS):
        q.update(f"{k}:{PCRS[k]}".encode())
    quote = q.hexdigest()
    canonical = f"{machine.id}\n{tier}\n{nonce}\n{ts}\n{json.dumps(PCRS, sort_keys=True)}\n{quote}"
    return ts, quote, hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


async def _attest(client, token, machine, signing_key, tier="sealed", security_state=LOCKS):
    nonce = (await client.post(f"/provider/machines/{machine.id}/attestation/challenge", headers={"Authorization": f"Bearer {token}"})).json()["nonce"]
    ts, quote, sig = _signed_quote(machine, signing_key, tier, nonce)
    host_priv, host_pub = pc.generate_x25519_keypair()
    r_priv, r_pub = pc.generate_ed25519_keypair()
    resp = await client.post(
        f"/provider/machines/{machine.id}/attestation/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"nonce": nonce, "isolation_tier": tier, "timestamp": ts, "pcr_measurements": PCRS, "platform_quote": quote,
              "signature": sig, "ephemeral_public_key": host_pub, "receipt_signing_public_key": r_pub, "security_state": security_state},
    )
    return resp, host_priv, r_priv


@pytest.fixture
def software_attestation(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", True)


# --------------------------------------------------------------------------- job submission

@pytest.mark.asyncio
async def test_private_job_accepts_ciphertext_and_refuses_plaintext(client: AsyncClient, db_engine):
    await _seed_model(db_engine)
    _, token = await _user(db_engine, "c1@test.io")
    h = {"Authorization": f"Bearer {token}"}

    body, _, _ = _private_job_body(value_at_risk="500.00", data_policy=None)
    body["data_policy"] = {"classification": "confidential", "allowed_regions": ["us-east"], "result_public_key": _private_job_body()[0]["data_policy"]["result_public_key"]}
    ok = await client.post("/v1/jobs", headers=h, json=body)
    assert ok.status_code == 201, ok.text
    assert ok.json()["input"] == {} and ok.json()["value_at_risk"] == "500.00"
    async with _sm(db_engine)() as db:
        stored = (await db.execute(select(Job).where(Job.id == uuid.UUID(ok.json()["job_id"])))).scalar_one()
        assert "TOP SECRET" not in json.dumps([stored.input, stored.encrypted_input, stored.dk_shares])

    plain, _, _ = _private_job_body()
    plain["input"] = {"prompt": "TOP SECRET"}
    assert (await client.post("/v1/jobs", headers=h, json=plain)).status_code == 422

    raw_shares, dk, _ = _private_job_body()
    raw_shares["dk_shares"] = {n: {"x": i + 1, "share_hex": "00" * 32} for i, n in enumerate(guardian_public_keys())}
    assert (await client.post("/v1/jobs", headers=h, json=raw_shares)).status_code == 422

    no_key, _, _ = _private_job_body()
    no_key["data_policy"] = {}
    assert (await client.post("/v1/jobs", headers=h, json=no_key)).status_code == 422

    neg, _, _ = _private_job_body(value_at_risk="-1")
    assert (await client.post("/v1/jobs", headers=h, json=neg)).status_code == 422

    typo, _, _ = _private_job_body()
    typo["data_policy"]["alowed_regions"] = ["us-east"]  # unknown keys are refused, not ignored
    assert (await client.post("/v1/jobs", headers=h, json=typo)).status_code == 422

    open_with_cipher = {"workload_type": "text-generation", "model": MODEL, "input": {"prompt": "x"}, "isolation_tier": "open",
                        "encrypted_input": _private_job_body()[0]["encrypted_input"]}
    assert (await client.post("/v1/jobs", headers=h, json=open_with_cipher)).status_code == 422


@pytest.mark.asyncio
async def test_private_tiers_refused_when_privacy_is_off(client: AsyncClient, db_engine, monkeypatch):
    await _seed_model(db_engine)
    _, token = await _user(db_engine, "c-off@test.io")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "PRIVACY_ENABLED", False)
    body, _, _ = _private_job_body()
    resp = await client.post("/v1/jobs", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert resp.status_code == 409 and resp.json()["detail"] == "privacy_tiers_not_enabled"


@pytest.mark.asyncio
async def test_scheduler_enforces_stake_value_at_risk_and_region(client: AsyncClient, db_engine):
    await _seed_model(db_engine)
    host, _ = await _user(db_engine, "h1@test.io", Role.provider)
    low = await _machine(db_engine, host, "low", tiers=["container", "sealed"], status="VERIFIED", stake="500")
    good = await _machine(db_engine, host, "good", tiers=["container", "sealed"], status="VERIFIED", stake="2000")
    nowhere = await _machine(db_engine, host, "nowhere", tiers=["container", "sealed"], status="VERIFIED", stake="9000", region=None)
    _, token = await _user(db_engine, "c2@test.io")
    h = {"Authorization": f"Bearer {token}"}

    body, _, _ = _private_job_body(machine_id=str(low.id))
    r = await client.post("/v1/jobs", headers=h, json=body)
    assert r.status_code == 409 and r.json()["detail"] == "insufficient_stake_for_sealed"

    body, _, _ = _private_job_body(machine_id=str(good.id), value_at_risk="2500.00")
    r = await client.post("/v1/jobs", headers=h, json=body)
    assert r.status_code == 409 and r.json()["detail"] == "value_at_risk_exceeds_stake"

    body, _, _ = _private_job_body(machine_id=str(nowhere.id))
    body["data_policy"]["allowed_regions"] = ["eu-central"]
    r = await client.post("/v1/jobs", headers=h, json=body)
    assert r.status_code == 409 and r.json()["detail"] == "region_policy_mismatch"  # no region = cannot prove it

    body, _, _ = _private_job_body(value_at_risk="1500.00")
    r = await client.post("/v1/jobs", headers=h, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["machine_id"] in (str(good.id), str(nowhere.id))


@pytest.mark.asyncio
async def test_enrollment_cannot_self_report_privacy_tiers(client: AsyncClient, db_engine):
    from tests.test_provider_phase2 import _capability_report, become_provider, mint_token

    access = await become_provider(client, "h-enroll@test.io")
    token = (await mint_token(client, access))["enrollment_token"]
    report = {**_capability_report(), "isolation_tiers": ["container", "gvisor", "sealed", "sealed_pro", "confidential"]}
    r = await client.post("/v1/agents/enroll", json={"enrollment_token": token, "agent_version": "0.2.0", "hostname": "liar", "capability_report": report})
    assert r.status_code == 201, r.text
    async with _sm(db_engine)() as db:
        cap = (await db.execute(select(ProviderCapability).where(ProviderCapability.machine_id == uuid.UUID(r.json()["machine_id"])))).scalar_one()
    assert cap.supported_isolation_tiers == ["container", "gvisor"]


# --------------------------------------------------------------------------- attestation

@pytest.mark.asyncio
async def test_sealed_attestation_refused_without_hardware_attestation(client: AsyncClient, db_engine):
    host, token = await _user(db_engine, "h-hw@test.io", Role.provider)
    m = await _machine(db_engine, host, "hw", tiers=["container"], stake="1500", signing_key="k-hw")
    await _trusted_image(db_engine)
    resp, _, _ = await _attest(client, token, m, "k-hw")
    assert resp.status_code == 409 and resp.json()["detail"] == "hardware_attestation_not_available"
    async with _sm(db_engine)() as db:
        cap = (await db.execute(select(ProviderCapability).where(ProviderCapability.machine_id == m.id))).scalar_one()
        assert "sealed" not in cap.supported_isolation_tiers


@pytest.mark.asyncio
async def test_sealed_attestation_requires_trusted_image_locks_and_stake(client: AsyncClient, db_engine, software_attestation):
    host, token = await _user(db_engine, "h-att@test.io", Role.provider)
    m = await _machine(db_engine, host, "att", tiers=["container"], stake="1500", signing_key="k-att")

    resp, _, _ = await _attest(client, token, m, "k-att")
    assert resp.json()["detail"] == "unknown_os_image"  # nothing registered yet
    await _trusted_image(db_engine)

    resp, _, _ = await _attest(client, token, m, "k-att", security_state={"bare_metal": True})
    assert resp.json()["detail"] == "security_state_violation"  # missing locks are not assumed "on"

    resp, _, _ = await _attest(client, token, m, "k-att")
    assert resp.status_code == 200, resp.text
    verdict = resp.json()["verdict"]
    assert verdict["image_hash"] == IMAGE and verdict["tier_reached"] == "sealed"
    assert verify_verdict_signature(verdict)
    forged = dict(verdict, tier_reached="confidential")
    assert not verify_verdict_signature(forged)
    fpriv, fpub = pc.generate_ed25519_keypair()
    self_signed = dict(verdict, verifier_public_key=fpub)
    from app.services.attestation_verifier import verdict_signing_bytes

    self_signed["signature"] = pc.sign(fpriv, verdict_signing_bytes(self_signed))
    assert not verify_verdict_signature(self_signed)


@pytest.mark.asyncio
async def test_understaked_machine_gets_no_sealed_verdict(client: AsyncClient, db_engine, software_attestation):
    host, token = await _user(db_engine, "h-low@test.io", Role.provider)
    m = await _machine(db_engine, host, "lowatt", tiers=["container"], stake="10", signing_key="k-low")
    await _trusted_image(db_engine)
    resp, _, _ = await _attest(client, token, m, "k-low")
    assert resp.status_code == 409 and resp.json()["detail"] == "insufficient_stake_for_sealed"


# --------------------------------------------------------------------------- guardians, end to end

async def _assigned_private_job(db_engine, machine, customer_id, prompt="TOP SECRET"):
    body, dk, result_priv = _private_job_body(prompt)
    async with _sm(db_engine)() as db:
        job = Job(customer_id=customer_id, workload_type="text-generation", model=MODEL, input={}, isolation_tier="sealed",
                  machine_id=machine.id, encrypted_input=body["encrypted_input"], dk_shares=body["dk_shares"],
                  data_policy=body["data_policy"], value_at_risk=decimal.Decimal("100.00"))
        db.add(job)
        await db.commit()
        return job, dk, result_priv


async def _job_verdict(db_engine, machine_id, job_id, tier="sealed"):
    """A verdict attested for ONE job with fresh host keys: what the agent gets after attesting with
    the claim's key-attestation nonce. Returns (verdict, verdict_leaf_index, host_priv, receipt_priv)."""
    host_priv, host_pub = pc.generate_x25519_keypair()
    r_priv, r_pub = pc.generate_ed25519_keypair()
    async with _sm(db_engine)() as db:
        verdict, leaf = await verify_machine_evidence(
            db, machine=await db.get(ProviderMachine, machine_id), requested_tier=tier, nonce=secrets.token_hex(24),
            pcr_measurements=PCRS, ephemeral_public_key=host_pub, receipt_signing_public_key=r_pub,
            security_state=LOCKS, job_id=str(job_id),
        )
        await db.commit()
    return verdict, leaf.leaf_index, host_priv, r_priv


@pytest.mark.asyncio
async def test_guardians_release_only_to_the_attested_host_and_log_first(client: AsyncClient, db_engine, software_attestation):
    host, token = await _user(db_engine, "h-g@test.io", Role.provider)
    cust, _ = await _user(db_engine, "c-g@test.io")
    m = await _machine(db_engine, host, "g", tiers=["container"], stake="2000", signing_key="k-g")
    await _trusted_image(db_engine)
    job, dk, _ = await _assigned_private_job(db_engine, m, cust.id)
    verdict, _, host_priv, _ = await _job_verdict(db_engine, m.id, job.id)

    async with _sm(db_engine)() as db:
        machine = await db.get(ProviderMachine, m.id)
        job = await db.get(Job, job.id)
        released = await release_guardian_shares(db, job=job, machine=machine, attestation_verdict=verdict)
        await db.commit()
        logs = (await db.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.event_type == "KEY_RELEASE"))).scalars().all()
    assert len(released) == 2 and len(logs) == 2

    # The host's private key opens the shares and rebuilds the Data Key ...
    shares = [(r["share_index"], pc.open_sealed(host_priv, r["wrapped_share"], pc.release_aad(str(job.id), r["guardian"]))) for r in released]
    assert pc.reconstruct_secret(shares, 2) == dk
    assert json.loads(pc.decrypt_data(dk, job.encrypted_input, pc.INPUT_AAD)) == {"prompt": "TOP SECRET"}
    # ... while the PUBLIC key (published in the verdict and the log) is useless to anyone else.
    other_priv, _ = pc.generate_x25519_keypair()
    with pytest.raises(pc.PrivacyCryptoError):
        pc.open_sealed(other_priv, released[0]["wrapped_share"], pc.release_aad(str(job.id), released[0]["guardian"]))
    # ... and a share released for this job is useless for another.
    with pytest.raises(pc.PrivacyCryptoError):
        pc.open_sealed(host_priv, released[0]["wrapped_share"], pc.release_aad(str(uuid.uuid4()), released[0]["guardian"]))


@pytest.mark.asyncio
async def test_guardians_refuse_bad_verdicts_and_bad_states(client: AsyncClient, db_engine, software_attestation, monkeypatch):
    host, token = await _user(db_engine, "h-r@test.io", Role.provider)
    cust, _ = await _user(db_engine, "c-r@test.io")
    m = await _machine(db_engine, host, "r", tiers=["container"], stake="2000", signing_key="k-r")
    other = await _machine(db_engine, host, "r2", tiers=["container"], stake="2000", signing_key="k-r2")
    await _trusted_image(db_engine)
    machine_level = (await _attest(client, token, m, "k-r"))[0].json()["verdict"]
    job, _, _ = await _assigned_private_job(db_engine, m, cust.id)
    other_job, _, _ = await _assigned_private_job(db_engine, m, cust.id)
    verdict, _, _, _ = await _job_verdict(db_engine, m.id, job.id)

    async def attempt(v=verdict, machine_id=m.id):
        async with _sm(db_engine)() as db:
            with pytest.raises(GuardianReleaseError) as exc:
                await release_guardian_shares(db, job=await db.get(Job, job.id), machine=await db.get(ProviderMachine, machine_id), attestation_verdict=v)
            return exc.value.code

    assert await attempt(v=dict(verdict, ephemeral_public_key=pc.generate_x25519_keypair()[1])) == "verdict_signature_invalid"
    # R4/R5: a machine-level verdict (job_id null), a verdict for another job, or a v1 verdict with
    # no job_id at all never releases this job's keys.
    assert machine_level["job_id"] is None
    assert await attempt(v=machine_level) == "verdict_not_for_this_job"
    assert await attempt(v=(await _job_verdict(db_engine, m.id, other_job.id))[0]) == "verdict_not_for_this_job"
    v1 = {k: val for k, val in verdict.items() if k not in ("job_id", "signature")}
    v1["signature"] = pc.sign(settings.PRIVACY_VERIFIER_SIGNING_KEY, pc.canonical_json(v1))  # correctly signed v1
    assert await attempt(v=v1) == "verdict_signature_invalid"
    assert await attempt(machine_id=other.id) == "machine_mismatch"
    expired = dict(verdict, issued_at="2020-01-01T00:00:00+00:00", expires_at="2020-01-01T00:30:00+00:00")
    expired["signature"] = sign_verdict(expired)
    assert await attempt(v=expired) == "verdict_expired"

    async with _sm(db_engine)() as db:
        img = (await db.execute(select(TrustedImage))).scalar_one()
        img.status = "revoked"
        await db.commit()
    assert await attempt() == "image_not_trusted"
    async with _sm(db_engine)() as db:
        (await db.execute(select(TrustedImage))).scalar_one().status = "active"
        machine = await db.get(ProviderMachine, m.id)
        machine.is_banned = True
        await db.commit()
    assert await attempt() == "machine_is_banned"
    async with _sm(db_engine)() as db:
        (await db.get(ProviderMachine, m.id)).is_banned = False
        await db.commit()

    # A production-shaped process holds fewer guardian keys than the threshold: it cannot release alone.
    only_one = json.dumps({"computecoin": json.loads(settings.PRIVACY_GUARDIAN_PRIVATE_KEYS)["computecoin"]})
    monkeypatch.setattr(settings, "PRIVACY_GUARDIAN_PUBLIC_KEYS", json.dumps(guardian_public_keys()))
    monkeypatch.setattr(settings, "PRIVACY_GUARDIAN_PRIVATE_KEYS", only_one)
    assert await attempt() == "insufficient_guardians_available"


# --------------------------------------------------------------------------- log and receipts

@pytest.mark.asyncio
async def test_transparency_log_proofs_and_public_endpoints(client: AsyncClient, db_engine):
    async with _sm(db_engine)() as db:
        leaves = [await append_transparency_log_entry(db, "IMAGE_HASH", {"i": i}) for i in range(7)]
        await db.commit()
        p = await get_inclusion_proof_for_leaf(db, 4)
    assert verify_inclusion_proof(p["leaf_hash"], 4, p["tree_size"], p["root_hash"], p["audit_path"])
    assert not verify_inclusion_proof(p["leaf_hash"], 5, p["tree_size"], p["root_hash"], p["audit_path"])
    assert leaves[-1].merkle_root == p["root_hash"]

    api = (await client.get("/v1/privacy/transparency-log/proof/4")).json()
    assert api["audit_path"] == p["audit_path"] and api["payload"] == {"i": 4}
    assert (await client.get("/v1/privacy/transparency-log/anchor")).json()["total_leaves"] == 7
    keys = (await client.get("/v1/privacy/verifier-key")).json()
    assert keys["verifier_public_key"] == verifier_public_key() and keys["threshold"] == 2 and len(keys["guardians"]) == 2
    # The published fingerprint is what clients pin (spec 6e); the shared vector keeps API, SDK and web equal.
    from app.services.privacy_keys import guardian_manifest_fingerprint

    assert keys["guardian_fingerprint"] == guardian_manifest_fingerprint(keys["guardians"], keys["threshold"])
    assert guardian_manifest_fingerprint({"computecoin": "AA" * 32, "partner": "bb" * 32}, 2) == (
        "90047f24b306726940afab8f77365e85bf3b89f88bd916a9c5e60da4890a31f1"
    )


@pytest.mark.asyncio
async def test_privacy_receipt_end_to_end(client: AsyncClient, db_engine, software_attestation):
    host, token = await _user(db_engine, "h-rc@test.io", Role.provider)
    cust, cust_token = await _user(db_engine, "c-rc@test.io")
    stranger, stranger_token = await _user(db_engine, "x-rc@test.io")
    m = await _machine(db_engine, host, "rc", tiers=["container"], stake="2000", signing_key="k-rc")
    await _trusted_image(db_engine)
    job, _, _ = await _assigned_private_job(db_engine, m, cust.id)
    verdict, verdict_leaf_index, _, r_priv = await _job_verdict(db_engine, m.id, job.id)
    foreign, _, _, foreign_r_priv = await _job_verdict(db_engine, m.id, uuid.uuid4())

    async with _sm(db_engine)() as db:
        machine, j = await db.get(ProviderMachine, m.id), await db.get(Job, job.id)
        released = await release_guardian_shares(db, job=j, machine=machine, attestation_verdict=verdict)
        wipe = {"machine_id": str(m.id), "job_id": str(job.id), "wiped_items": ["key_material"], "wiped_at": utcnow().isoformat()}
        wipe["signature"] = pc.sign(r_priv, wipe_receipt_signing_bytes(wipe))

        # B9: a verdict attested for another job never backs this receipt, even with a wipe receipt
        # correctly signed by that verdict's own key (so only the job binding can refuse it).
        foreign_wipe = dict(wipe)
        foreign_wipe["signature"] = pc.sign(foreign_r_priv, wipe_receipt_signing_bytes(wipe))
        with pytest.raises(ValueError, match="not attested for this job"):
            await create_privacy_receipt_for_job(db, job=j, machine=machine, attestation_verdict=foreign,
                                                 verdict_leaf_index=verdict_leaf_index, guardian_releases=released, wipe_receipt=foreign_wipe)

        bad_wipe = dict(wipe, signature=pc.sign(pc.generate_ed25519_keypair()[0], wipe_receipt_signing_bytes(wipe)))
        with pytest.raises(ValueError):
            await create_privacy_receipt_for_job(db, job=j, machine=machine, attestation_verdict=verdict,
                                                 verdict_leaf_index=verdict_leaf_index, guardian_releases=released, wipe_receipt=bad_wipe)

        receipt = await create_privacy_receipt_for_job(db, job=j, machine=machine, attestation_verdict=verdict,
                                                       verdict_leaf_index=verdict_leaf_index, guardian_releases=released, wipe_receipt=wipe)
        await db.commit()
        assert receipt.verified is True
        assert (await db.get(Job, job.id)).privacy_receipt_id == receipt.id  # was always None before

    got = await client.get(f"/v1/privacy/receipts/{job.id}", headers={"Authorization": f"Bearer {cust_token}"})
    assert got.status_code == 200, got.text
    data = got.json()
    assert verify_privacy_receipt_offline(data)["valid"] is True
    assert (await client.post("/v1/privacy/receipts/verify", json=data)).json()["valid"] is True
    assert (await client.get(f"/v1/privacy/receipts/{job.id}", headers={"Authorization": f"Bearer {stranger_token}"})).status_code == 404
    assert data["anchor"] is None and data["witness"] is None  # nothing anchored or witnessed yet

    # Once the worker has anchored and witnessed a LATER checkpoint, the same receipt read carries
    # both sections (built from the database; reading a receipt never calls a chain or a witness).
    async with _sm(db_engine)() as db:
        await append_transparency_log_entry(db, "IMAGE_HASH", {"later": True})
        last = (await db.execute(select(TransparencyLogLeaf).order_by(TransparencyLogLeaf.leaf_index.desc()))).scalars().first()
        last.anchored_tx_hash, last.anchored_block_number = "0x" + "ab" * 32, 7
        last.witness_cosignatures = {"checkpoint": {"tree_size": last.leaf_index + 1, "root_hash": last.merkle_root}, "cosignatures": []}
        await db.commit()
    later = (await client.get(f"/v1/privacy/receipts/{job.id}", headers={"Authorization": f"Bearer {cust_token}"})).json()
    receipt_size = next(iter(data["inclusion_proofs"].values()))["tree_size"]
    for section in (later["anchor"], later["witness"]):
        assert section is not None and section["consistency_proof"]
    assert later["anchor"]["tree_size"] == last.leaf_index + 1 > receipt_size
    assert later["anchor"]["merkle_root"] == last.merkle_root

    tampered = json.loads(json.dumps(data))
    tampered["wipe_receipt"]["wiped_items"] = ["gpu_memory", "key_material"]
    assert verify_privacy_receipt_offline(tampered)["valid"] is False
    # B9: a receipt whose verdict was attested for a different job does not verify.
    tampered = json.loads(json.dumps(data))
    tampered["job_id"] = str(uuid.uuid4())
    report = verify_privacy_receipt_offline(tampered)
    assert report["valid"] is False and report["checks"]["verdict_for_this_job"] is False
    tampered = json.loads(json.dumps(data))
    first = next(iter(tampered["inclusion_proofs"]))
    tampered["inclusion_proofs"][first]["payload"]["nonce"] = "edited"
    assert verify_privacy_receipt_offline(tampered)["valid"] is False


# --------------------------------------------------------------------------- canaries

@pytest.mark.asyncio
async def test_canary_is_indistinguishable_and_leak_reporting_is_restricted(client: AsyncClient, db_engine):
    host, _ = await _user(db_engine, "h-c@test.io", Role.provider)
    _, cust_token = await _user(db_engine, "c-c@test.io")
    sec, sec_token = await _user(db_engine, "sec@test.io", Role.security_operator)
    m = await _machine(db_engine, host, "c", tiers=["container", "sealed"], status="VERIFIED", stake="4000")
    async with _sm(db_engine)() as db:
        job, canary = await create_canary_job(db, machine=await db.get(ProviderMachine, m.id), customer_id=sec.id, model_version=MODEL)
        await db.commit()
        token_value = canary.canary_token
    assert job.input == {} and pc.is_data_envelope(job.encrypted_input)
    assert token_value not in json.dumps([job.input, job.encrypted_input, job.dk_shares, job.data_policy])
    assert set(job.dk_shares) == set(guardian_public_keys())

    body = {"canary_token": token_value, "proof_description": "Found in a public paste"}
    assert (await client.post("/v1/privacy/canary/report-leak", json=body)).status_code == 401
    assert (await client.post("/v1/privacy/canary/report-leak", json=body, headers={"Authorization": f"Bearer {cust_token}"})).status_code == 403
    r = await client.post("/v1/privacy/canary/report-leak", json=body, headers={"Authorization": f"Bearer {sec_token}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "slashed" and r.json()["slashed_amount"] == "4000.00"

    async with _sm(db_engine)() as db:
        machine = await db.get(ProviderMachine, m.id)
        assert machine.is_banned and machine.stake_bonded == 0
        slash = (await db.execute(select(SlashEvent))).scalar_one()
        assert slash.amount_minor == 400000 and slash.ledger_txn_id is None  # recorded, not yet executed on-chain
        log = (await db.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.event_type == "TAMPER_EVIDENCE"))).scalar_one()
        assert token_value[:16] not in json.dumps(log.payload)


# --------------------------------------------------------------------------- freivalds and stake

@pytest.mark.asyncio
async def test_freivalds(client: AsyncClient, db_engine):
    _, token = await _user(db_engine, "f@test.io")
    h = {"Authorization": f"Bearer {token}"}
    A, B = [[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]
    ok = await client.post("/v1/privacy/freivalds/verify", headers=h, json={"matrix_a": A, "matrix_b": B, "matrix_c": [[19.0, 22.0], [43.0, 50.0]]})
    assert ok.json()["verified"] is True
    bad = await client.post("/v1/privacy/freivalds/verify", headers=h, json={"matrix_a": A, "matrix_b": B, "matrix_c": [[19.0, 22.0], [43.0, 51.0]], "rounds": 30})
    assert bad.json()["verified"] is False
    ragged = await client.post("/v1/privacy/freivalds/verify", headers=h, json={"matrix_a": [[1.0, 2.0], [3.0]], "matrix_b": B, "matrix_c": B})
    assert ragged.status_code == 422
    assert (await client.post("/v1/privacy/freivalds/verify", json={"matrix_a": A, "matrix_b": B, "matrix_c": B})).status_code == 401


@pytest.mark.asyncio
async def test_only_an_administrator_records_stake_and_it_grants_no_tier(client: AsyncClient, db_engine):
    host, host_token = await _user(db_engine, "h-s@test.io", Role.provider)
    _, admin_token = await _user(db_engine, "admin-s@test.io", Role.administrator)
    m = await _machine(db_engine, host, "s", tiers=["container"])
    body = {"machine_id": str(m.id), "amount": "1500.00", "vault_tx_hash": "0x" + "ab" * 32}
    assert (await client.post("/v1/privacy/stake/bond", json=body, headers={"Authorization": f"Bearer {host_token}"})).status_code == 403
    r = await client.post("/v1/privacy/stake/bond", json=body, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    assert decimal.Decimal(r.json()["bonded_stake"]) == decimal.Decimal("1500.00")
    async with _sm(db_engine)() as db:
        cap = (await db.execute(select(ProviderCapability).where(ProviderCapability.machine_id == m.id))).scalar_one()
        assert cap.supported_isolation_tiers == ["container"]


# --------------------------------------------------------------------------- config guard

def test_production_privacy_needs_real_keys_and_fewer_local_guardians_than_threshold():
    strong = dict(ENVIRONMENT="production", AUTH_SECRET="a" * 40, SESSION_SECRET="b" * 40, SETTLEMENT_ENABLED=False, _env_file=None)
    assert Settings(**strong).privacy_active is False  # off by default in production
    with pytest.raises(Exception, match="PRIVACY_VERIFIER_SIGNING_KEY"):
        Settings(**strong, PRIVACY_ENABLED=True)
    g1, _ = pc.generate_x25519_keypair()
    g2, _ = pc.generate_x25519_keypair()
    real = dict(PRIVACY_ENABLED=True, PRIVACY_VERIFIER_SIGNING_KEY=pc.generate_ed25519_keypair()[0],
                PRIVACY_LOG_SIGNING_KEY=pc.generate_ed25519_keypair()[0])
    with pytest.raises(Exception, match="fewer keys than the threshold"):
        Settings(**strong, **real, PRIVACY_GUARDIAN_PRIVATE_KEYS=json.dumps({"computecoin": g1, "partner": g2}))
    ok = Settings(**strong, **real, PRIVACY_GUARDIAN_PRIVATE_KEYS=json.dumps({"computecoin": g1}))
    assert ok.privacy_active is True
    with pytest.raises(Exception, match="PRIVACY_SOFTWARE_ATTESTATION"):
        Settings(**strong, **real, PRIVACY_GUARDIAN_PRIVATE_KEYS=json.dumps({"computecoin": g1}), PRIVACY_SOFTWARE_ATTESTATION=True)


def test_crypto_module_is_identical_in_api_agent_and_sdk():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    copies = [root / "apps/api/app/services/privacy_crypto.py", root / "provider-agent/computecoin_agent/privacy_crypto.py", root / "sdk/python/computecoin/privacy_crypto.py"]
    texts = [p.read_text() for p in copies if p.exists()]
    assert len(texts) == 3 and texts[0] == texts[1] == texts[2], "the three ccpp-v1 implementations must stay byte-identical"
