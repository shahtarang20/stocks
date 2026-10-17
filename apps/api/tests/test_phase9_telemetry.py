"""Integration tests for Phase 9: Telemetry at Scale & Operational Alerting."""

from __future__ import annotations

import decimal
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token, hash_token
from app.db.base import utcnow
from app.models.enums import Role, UserStatus
from app.models.jobs import Job
from app.models.model_registry import Model, ModelVersion
from app.models.provider import (
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.user import User


def _sign_machine_request(
    machine_id: str,
    signing_key: str,
    raw_body: bytes,
    ts: int,
) -> dict[str, str]:
    body_hash = hashlib.sha256(raw_body).hexdigest()
    canonical = f"{machine_id}\n{ts}\n{body_hash}"
    sig = hmac.new(signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": str(ts),
        "X-ComputeCoin-Signature": sig,
    }


async def _create_test_user(db_engine, email: str, role: Role) -> tuple[User, str]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

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
        from app.models.user import UserRole
        db.add(UserRole(user_id=user.id, role=role))
        await db.commit()

    token, _, _ = create_access_token(
        subject=str(user.id),
        roles=[role.value],
    )
    return user, token


async def _setup_provider_machine(
    db_engine,
    user: User,
    hostname: str,
    signing_key: str = "test-machine-hw-key",
) -> tuple[Provider, ProviderMachine]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        provider = Provider(
            user_id=user.id,
            display_name="Test Telemetry Provider",
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
                gpu_count=2,
                gpu_model="NVIDIA H100 80GB",
                vram_mb=81920,
            )
        )
        db.add(
            ProviderCapability(
                machine_id=machine.id,
                supported_workloads=["text-generation"],
                supported_isolation_tiers=["container", "gvisor"],
            )
        )
        await db.commit()
        return provider, machine


@pytest.mark.asyncio
async def test_machine_telemetry_ingestion_and_query(client: AsyncClient, db_engine):
    prov_user, prov_token = await _create_test_user(db_engine, "prov-telemetry@test.io", Role.provider)
    signing_key = "test-signing-key-telemetry"
    _, machine = await _setup_provider_machine(db_engine, prov_user, "telemetry-node-01", signing_key)

    # Ingest 3 points spaced over 2 minutes
    base_ts = datetime.now(timezone.utc)
    points = [
        {
            "timestamp": (base_ts - timedelta(minutes=2)).isoformat(),
            "gpu_index": 0,
            "gpu_util_pct": 50.0,
            "mem_util_pct": 40.0,
            "mem_used_mb": 32000,
            "mem_total_mb": 80000,
            "temp_c": 55.0,
            "power_w": 250.0,
        },
        {
            "timestamp": (base_ts - timedelta(minutes=1)).isoformat(),
            "gpu_index": 0,
            "gpu_util_pct": 80.0,
            "mem_util_pct": 45.0,
            "mem_used_mb": 36000,
            "mem_total_mb": 80000,
            "temp_c": 62.0,
            "power_w": 310.0,
        },
        {
            "timestamp": base_ts.isoformat(),
            "gpu_index": 0,
            "gpu_util_pct": 90.0,
            "mem_util_pct": 50.0,
            "mem_used_mb": 40000,
            "mem_total_mb": 80000,
            "temp_c": 68.0,
            "power_w": 340.0,
        },
    ]

    payload_bytes = json.dumps({"points": points}, separators=(",", ":")).encode("utf-8")
    now_ts = int(time.time())
    headers = _sign_machine_request(str(machine.id), signing_key, payload_bytes, now_ts)

    # Ingest batch
    resp = await client.post("/v1/telemetry/machine", content=payload_bytes, headers=headers)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["accepted"] == 3
    assert res_data["alerts_triggered"] == 0

    # Query bucketed rollups via provider JWT
    query_resp = await client.get(
        f"/v1/telemetry/machines/{machine.id}",
        params={"bucket_seconds": 60},
        headers={"Authorization": f"Bearer {prov_token}"},
    )
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    assert q_data["machine_id"] == str(machine.id)
    assert len(q_data["points"]) >= 1
    assert q_data["current"] is not None
    assert q_data["current"]["temp_c"] == 68.0


@pytest.mark.asyncio
async def test_job_telemetry_ingestion_and_query(client: AsyncClient, db_engine):
    prov_user, _ = await _create_test_user(db_engine, "prov-telemetry2@test.io", Role.provider)
    signing_key = "test-job-key"
    _, machine = await _setup_provider_machine(db_engine, prov_user, "job-node-01", signing_key)

    cust_user, cust_token = await _create_test_user(db_engine, "cust-telemetry@test.io", Role.customer)

    # Create a job in DB
    from sqlalchemy.ext.asyncio import async_sessionmaker
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        job = Job(
            customer_id=cust_user.id,
            machine_id=machine.id,
            workload_type="text-generation",
            model="test-model",
            input={"prompt": "hi"},
            status="running",
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    # Ingest job execution telemetry
    job_points = [
        {
            "job_id": str(job_id),
            "gpu_util_pct": 75.0,
            "mem_used_mb": 12000,
            "tokens_per_sec": 48.5,
            "step_count": 100,
        },
        {
            "job_id": str(job_id),
            "gpu_util_pct": 85.0,
            "mem_used_mb": 12500,
            "tokens_per_sec": 51.5,
            "step_count": 200,
        },
    ]

    payload_bytes = json.dumps({"points": job_points}, separators=(",", ":")).encode("utf-8")
    headers = _sign_machine_request(str(machine.id), signing_key, payload_bytes, int(time.time()))

    ingest_resp = await client.post("/v1/telemetry/job", content=payload_bytes, headers=headers)
    assert ingest_resp.status_code == 200
    assert ingest_resp.json()["accepted"] == 2

    # Query job execution telemetry via customer JWT
    query_resp = await client.get(
        f"/v1/telemetry/jobs/{job_id}",
        params={"bucket_seconds": 60},
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    assert q_data["job_id"] == str(job_id)
    assert q_data["total_samples"] == 2
    assert q_data["avg_tokens_per_sec"] == 50.0


@pytest.mark.asyncio
async def test_telemetry_threshold_alert_lifecycle(client: AsyncClient, db_engine):
    prov_user, prov_token = await _create_test_user(db_engine, "prov-telemetry3@test.io", Role.provider)
    admin_user, admin_token = await _create_test_user(db_engine, "admin-telemetry@test.io", Role.administrator)
    signing_key = "test-alert-key"
    _, machine = await _setup_provider_machine(db_engine, prov_user, "alert-node-01", signing_key)

    # Ingest a point exceeding the 85°C overheating threshold (e.g. 91.5°C -> critical)
    overheat_points = [
        {
            "gpu_index": 0,
            "gpu_util_pct": 99.0,
            "mem_util_pct": 96.0,  # also triggers memory alert
            "temp_c": 91.5,
            "power_w": 400.0,
        }
    ]
    payload_bytes = json.dumps({"points": overheat_points}, separators=(",", ":")).encode("utf-8")
    headers = _sign_machine_request(str(machine.id), signing_key, payload_bytes, int(time.time()))

    ingest_resp = await client.post("/v1/telemetry/machine", content=payload_bytes, headers=headers)
    assert ingest_resp.status_code == 200
    assert ingest_resp.json()["alerts_triggered"] >= 1

    # Provider lists alerts
    alerts_resp = await client.get(
        "/v1/telemetry/alerts",
        params={"machine_id": str(machine.id), "status": "active"},
        headers={"Authorization": f"Bearer {prov_token}"},
    )
    assert alerts_resp.status_code == 200
    alerts = alerts_resp.json()
    assert len(alerts) >= 1
    alert_id = alerts[0]["id"]
    assert alerts[0]["status"] == "active"

    # Acknowledge alert
    ack_resp = await client.post(
        f"/v1/telemetry/alerts/{alert_id}/acknowledge",
        headers={"Authorization": f"Bearer {prov_token}"},
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["status"] == "acknowledged"

    # Resolve alert as admin
    res_resp = await client.post(
        f"/v1/telemetry/alerts/{alert_id}/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_resp.status_code == 200
    assert res_resp.json()["status"] == "resolved"


@pytest.mark.asyncio
async def test_platform_telemetry_stats(client: AsyncClient, db_engine):
    user, token = await _create_test_user(db_engine, "stats-user@test.io", Role.customer)
    resp = await client.get("/v1/telemetry/stats", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert "active_gpus" in data
    assert "fleet_avg_gpu_util_pct" in data
    assert "fleet_total_power_kw" in data
    assert "active_alerts_count" in data


@pytest.mark.asyncio
async def test_job_telemetry_cross_tenant_injection_dropped(client: AsyncClient, db_engine):
    """A machine cannot inject telemetry for a job assigned to a DIFFERENT machine
    (audit M2). Points for non-owned jobs are dropped (accepted == 0)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    prov_a, _ = await _create_test_user(db_engine, "prov-ta@test.io", Role.provider)
    prov_b, _ = await _create_test_user(db_engine, "prov-tb@test.io", Role.provider)
    _, machine_a = await _setup_provider_machine(db_engine, prov_a, "node-a", "key-a")
    _, machine_b = await _setup_provider_machine(db_engine, prov_b, "node-b", "key-b")
    cust, _ = await _create_test_user(db_engine, "cust-ti@test.io", Role.customer)

    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        job = Job(
            customer_id=cust.id,
            machine_id=machine_a.id,  # job belongs to machine A
            workload_type="text-generation",
            model="test-model",
            input={"prompt": "hi"},
            status="running",
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    # Machine B signs a batch targeting machine A's job.
    points = [{"job_id": str(job_id), "gpu_util_pct": 99.0, "mem_used_mb": 1, "step_count": 1}]
    payload = json.dumps({"points": points}, separators=(",", ":")).encode("utf-8")
    headers = _sign_machine_request(str(machine_b.id), "key-b", payload, int(time.time()))
    resp = await client.post("/v1/telemetry/job", content=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0  # cross-tenant point dropped
