"""Integration tests for Phase 8: Workload isolation tiers and remote attestation."""

from __future__ import annotations

import decimal
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import hash_token
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.model_registry import Model, ModelVersion
from app.models.provider import (
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.user import User, UserRole


async def _create_test_user(
    db_engine,
    email: str,
    role: Role = Role.customer,
) -> tuple[User, str]:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.core.security import create_access_token, hash_password

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        user = User(
            email=email,
            password_hash=hash_password("Password123!"),
            email_verified=True,
            status=UserStatus.active,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)

    token, _, _ = create_access_token(
        subject=str(user.id),
        roles=[role.value],
    )
    return user, token


async def _setup_provider_machine(
    db_engine,
    user: User,
    hostname: str,
    isolation_tiers: list[str],
    verification_status: str = "detected",
    signing_key: str = "test-machine-hw-key",
) -> tuple[Provider, ProviderMachine]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        from sqlalchemy import select
        res = await db.execute(select(Provider).where(Provider.user_id == user.id))
        provider = res.scalar_one_or_none()
        if provider is None:
            provider = Provider(
                user_id=user.id,
                display_name="Test Provider",
                status="active",
            )
            db.add(provider)
            await db.flush()

        machine = ProviderMachine(
            provider_id=provider.id,
            hostname=hostname,
            agent_version="0.1.0",
            status="registered",
            availability="online",
            asking_price_per_hour=decimal.Decimal("2.50"),
            signing_key=signing_key,
            secret_hash=hash_token("test-secret"),
            last_heartbeat_at=utcnow(),
        )
        db.add(machine)
        await db.flush()

        db.add(
            HardwareInventory(
                machine_id=machine.id,
                gpu_count=4,
                gpu_model="NVIDIA H100 80GB SXM5",
                vram_mb=81920,
                verification_status=verification_status,
            )
        )
        db.add(
            ProviderCapability(
                machine_id=machine.id,
                supported_workloads=["text-generation", "batch-inference"],
                supported_isolation_tiers=isolation_tiers,
                verification_status=verification_status,
            )
        )
        await db.commit()
        await db.refresh(provider)
        await db.refresh(machine)
        return provider, machine


async def _seed_approved_model(db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        model = Model(name=f"llama-3-{uuid.uuid4().hex[:6]}", trust_status="approved")
        db.add(model)
        await db.flush()
        mv = ModelVersion(
            model_id=model.id,
            version_tag="meta-llama/Llama-3-8b-instruct",
            runtime="simulated",
            supported_workloads=["text-generation", "batch-inference"],
            trust_status="approved",
        )
        db.add(mv)
        await db.commit()


# --------------------------------------------------------------------------- #
# 1. Job Submission with Isolation Tiers
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_job_submission_isolation_tiers(client: AsyncClient, db_engine):
    await _seed_approved_model(db_engine)
    user, token = await _create_test_user(db_engine, "cust1@test.io", Role.customer)

    # 1. Default isolation tier is container
    resp = await client.post(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workload_type": "text-generation",
            "model": "meta-llama/Llama-3-8b-instruct",
            "input": {"prompt": "test prompt"},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["isolation_tier"] == "container"

    # 2. Specific isolation tier (gVisor)
    resp_gvisor = await client.post(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workload_type": "text-generation",
            "model": "meta-llama/Llama-3-8b-instruct",
            "input": {"prompt": "test prompt"},
            "isolation_tier": "gvisor",
        },
    )
    assert resp_gvisor.status_code == 201
    assert resp_gvisor.json()["isolation_tier"] == "gvisor"

    # 3. Invalid isolation tier rejected by schema
    resp_invalid = await client.post(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workload_type": "text-generation",
            "model": "meta-llama/Llama-3-8b-instruct",
            "input": {"prompt": "test prompt"},
            "isolation_tier": "untrusted_root",
        },
    )
    assert resp_invalid.status_code == 422


# --------------------------------------------------------------------------- #
# 2. Targeted Validation Rejection on Isolation Mismatch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_job_targeted_machine_isolation_mismatch(client: AsyncClient, db_engine):
    await _seed_approved_model(db_engine)
    prov_user, _ = await _create_test_user(db_engine, "prov1@test.io", Role.provider)
    _, machine = await _setup_provider_machine(
        db_engine, prov_user, "node-container-only", isolation_tiers=["container"]
    )

    cust_user, token = await _create_test_user(db_engine, "cust2@test.io", Role.customer)

    # Submit job demanding gVisor targeting a machine that only supports container
    resp = await client.post(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workload_type": "text-generation",
            "model": "meta-llama/Llama-3-8b-instruct",
            "input": {"prompt": "test prompt"},
            "machine_id": str(machine.id),
            "isolation_tier": "gvisor",
        },
    )
    # Refuses silent downgrade: validation fails with 409
    assert resp.status_code == 409
    assert resp.json()["detail"] == "isolation_tier_not_supported_by_machine"


# --------------------------------------------------------------------------- #
# 3. Scheduler Isolation Tier Matching
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_scheduler_matches_only_eligible_isolation_tier(client: AsyncClient, db_engine):
    await _seed_approved_model(db_engine)
    prov_user, _ = await _create_test_user(db_engine, "prov2@test.io", Role.provider)

    # Machine A: Container only
    _, mach_container = await _setup_provider_machine(
        db_engine, prov_user, "node-container", isolation_tiers=["container"]
    )
    # Machine B: gVisor supported
    _, mach_gvisor = await _setup_provider_machine(
        db_engine, prov_user, "node-gvisor", isolation_tiers=["container", "gvisor"]
    )

    cust_user, token = await _create_test_user(db_engine, "cust3@test.io", Role.customer)

    # Job requiring gVisor
    resp = await client.post(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workload_type": "text-generation",
            "model": "meta-llama/Llama-3-8b-instruct",
            "input": {"prompt": "secure prompt"},
            "isolation_tier": "gvisor",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    job_id = data["job_id"]

    # Verify that the job was matched and reserved to mach_gvisor (NOT mach_container)
    assert data["status"] == "reserved"
    assert data["machine_id"] == str(mach_gvisor.id)
    assert data["isolation_tier"] == "gvisor"

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.jobs import Job
    from sqlalchemy import select

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        j = (await db.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
        assert j.status == "reserved"
        assert j.machine_id == mach_gvisor.id
        assert j.isolation_tier == "gvisor"
        assert j.reservation_id is not None


# --------------------------------------------------------------------------- #
# 4. Remote Hardware Attestation Lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_remote_hardware_attestation(client: AsyncClient, db_engine):
    prov_user, token = await _create_test_user(db_engine, "prov3@test.io", Role.provider)
    signing_key = "hw-signing-key-secret-42"
    _, machine = await _setup_provider_machine(
        db_engine,
        prov_user,
        "node-h100-cc",
        isolation_tiers=["container"],
        verification_status="detected",
        signing_key=signing_key,
    )

    # 1. Request fresh challenge nonce
    resp_chal = await client.post(
        f"/provider/machines/{machine.id}/attestation/challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_chal.status_code == 200
    nonce = resp_chal.json()["nonce"]
    assert len(nonce) > 16

    # 2. Replay with wrong nonce -> rejected
    resp_wrong_nonce = await client.post(
        f"/provider/machines/{machine.id}/attestation/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "nonce": "replayed-wrong-nonce-123456",
            "isolation_tier": "confidential",
            "timestamp": utcnow().isoformat(),
            "pcr_measurements": {"pcr0": "hash0", "pcr1": "hash1"},
            "platform_quote": "0" * 96,
            "signature": "1" * 64,
        },
    )
    assert resp_wrong_nonce.status_code == 400
    assert resp_wrong_nonce.json()["detail"] == "invalid_or_expired_nonce"

    # 3. Construct genuine signed quote
    ts = utcnow().isoformat()
    measurements = {
        "pcr0": "a" * 64,
        "pcr1": "b" * 64,
        "pcr2": "c" * 64,
    }
    quote_hasher = hashlib.sha384()
    quote_hasher.update(nonce.encode("utf-8"))
    for k in sorted(measurements.keys()):
        quote_hasher.update(f"{k}:{measurements[k]}".encode("utf-8"))
    platform_quote = quote_hasher.hexdigest()

    measurements_json = json.dumps(measurements, sort_keys=True)
    canonical_str = (
        f"{machine.id}\n"
        f"confidential\n"
        f"{nonce}\n"
        f"{ts}\n"
        f"{measurements_json}\n"
        f"{platform_quote}"
    )
    sig = hmac.new(
        signing_key.encode("utf-8"),
        canonical_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # 4. Tampered quote rejected
    resp_tampered = await client.post(
        f"/provider/machines/{machine.id}/attestation/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "nonce": nonce,
            "isolation_tier": "confidential",
            "timestamp": ts,
            "pcr_measurements": measurements,
            "platform_quote": "f" * 96,
            "signature": sig,
        },
    )
    assert resp_tampered.status_code == 400
    assert resp_tampered.json()["detail"] == "platform_quote_tampered"

    # 5. Genuine verification succeeds
    resp_ok = await client.post(
        f"/provider/machines/{machine.id}/attestation/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "nonce": nonce,
            "isolation_tier": "confidential",
            "timestamp": ts,
            "pcr_measurements": measurements,
            "platform_quote": platform_quote,
            "signature": sig,
        },
    )
    assert resp_ok.status_code == 200, resp_ok.text
    body = resp_ok.json()
    assert body["verified"] is True
    assert body["verification_status"] == "VERIFIED"
    assert body["isolation_tier"] == "confidential"

    # 6. Check machine promoted and audit history recorded
    resp_hist = await client.get(
        f"/provider/machines/{machine.id}/attestation/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_hist.status_code == 200
    history = resp_hist.json()
    assert len(history) == 1
    assert history[0]["isolation_tier"] == "confidential"
    assert history[0]["verification_status"] == "verified"
