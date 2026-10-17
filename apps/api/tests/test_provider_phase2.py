"""Phase 2 — provider onboarding, machine enrollment, HMAC heartbeat tests."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.provider import EnrollmentToken, HardwareInventory, ProviderMachine
from app.models.user import UserRole
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def become_provider(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    access = tokens["access_token"]
    resp = await client.post(
        "/provider/register",
        json={"display_name": "Acme GPUs", "contact_email": email},
        headers=auth_headers(access),
    )
    assert resp.status_code == 200, resp.text
    return access


async def mint_token(client: AsyncClient, access: str, label: str = "lab") -> dict:
    resp = await client.post(
        "/provider/enrollment-tokens", json={"label": label}, headers=auth_headers(access)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _capability_report() -> dict:
    return {
        "verification_status": "detected",
        "cpu_model": "AMD EPYC 7542",
        "cpu_cores": 32,
        "ram_mb": 262144,
        "disk_gb": 2000,
        "gpus": [{"model": "NVIDIA A100", "vram_mb": 81920, "cuda_version": "12.2"}],
        "supported_workloads": ["text-generation", "batch-inference"],
    }


async def enroll_machine(
    client: AsyncClient, enrollment_token: str, hostname: str = "gpu-box-1"
) -> dict:
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": enrollment_token,
            "agent_version": "0.1.0",
            "hostname": hostname,
            "capability_report": _capability_report(),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def heartbeat_headers(machine_id: str, machine_secret: str, body: dict, timestamp: int | None = None) -> tuple[dict, bytes]:
    """Compute valid HMAC headers for a heartbeat (signs exactly like the agent)."""
    raw = json.dumps(body).encode("utf-8")
    ts = str(timestamp if timestamp is not None else int(utcnow().timestamp()))
    signing_key = machine_secret.removeprefix("msk_")
    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = f"{machine_id}\n{ts}\n{body_hash}"
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": sig,
        "Content-Type": "application/json",
    }
    return headers, raw


def _hb_body(availability: str = "online") -> dict:
    return {
        "availability": availability,
        "capability_summary": {"gpu_count": 1, "vram_mb": 81920},
        "health": {"gpu_temp_c": 55.0, "gpu_util_pct": 10.0, "mem_used_mb": 1024, "uptime_s": 42},
    }


# ---------------------------------------------------------------------------
# 1. provider register
# ---------------------------------------------------------------------------
async def test_provider_register_idempotent_and_grants_role(client: AsyncClient):
    tokens = await register_verify_login(client, "prov1@example.com")
    access = tokens["access_token"]

    r1 = await client.post("/provider/register", json={}, headers=auth_headers(access))
    assert r1.status_code == 200, r1.text
    pid1 = r1.json()["provider_id"]
    assert r1.json()["status"] == "active"

    r2 = await client.post(
        "/provider/register", json={"display_name": "X"}, headers=auth_headers(access)
    )
    assert r2.status_code == 200
    assert r2.json()["provider_id"] == pid1  # idempotent — same provider

    user_id = await get_user_id("prov1@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        roles = (
            await s.execute(select(UserRole.role).where(UserRole.user_id == user_id))
        ).scalars().all()
    assert Role.provider in roles


async def test_non_provider_cannot_mint_token(client: AsyncClient):
    tokens = await register_verify_login(client, "notprov@example.com")
    resp = await client.post(
        "/provider/enrollment-tokens", json={}, headers=auth_headers(tokens["access_token"])
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. enrollment tokens
# ---------------------------------------------------------------------------
async def test_enrollment_token_raw_shown_once_and_never_listed(client: AsyncClient):
    access = await become_provider(client, "prov2@example.com")
    created = await mint_token(client, access)
    raw = created["enrollment_token"]
    assert raw.startswith("enr_")

    listed = await client.get("/provider/enrollment-tokens", headers=auth_headers(access))
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    # Raw secret never appears in the list representation.
    assert "enrollment_token" not in rows[0]
    assert raw not in json.dumps(rows)


async def test_expired_token_rejected_at_enroll(client: AsyncClient):
    access = await become_provider(client, "prov3@example.com")
    created = await mint_token(client, access)
    raw = created["enrollment_token"]

    # Force expiry in the DB.
    sm = get_sessionmaker()
    async with sm() as s:
        tok = (
            await s.execute(
                select(EnrollmentToken).where(
                    EnrollmentToken.id == uuid.UUID(created["enrollment_token_id"])
                )
            )
        ).scalar_one()
        tok.expires_at = utcnow() - dt.timedelta(seconds=10)
        await s.commit()

    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": raw,
            "agent_version": "0.1.0",
            "hostname": "h",
            "capability_report": _capability_report(),
        },
    )
    assert resp.status_code == 401


async def test_used_token_is_single_use(client: AsyncClient):
    access = await become_provider(client, "prov4@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    await enroll_machine(client, raw, hostname="box-a")
    # second enroll with same token -> rejected
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": raw,
            "agent_version": "0.1.0",
            "hostname": "box-b",
            "capability_report": _capability_report(),
        },
    )
    assert resp.status_code == 401


async def test_revoked_token_rejected(client: AsyncClient):
    access = await become_provider(client, "prov5@example.com")
    created = await mint_token(client, access)
    raw = created["enrollment_token"]
    rev = await client.delete(
        f"/provider/enrollment-tokens/{created['enrollment_token_id']}",
        headers=auth_headers(access),
    )
    assert rev.status_code == 204
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": raw,
            "agent_version": "0.1.0",
            "hostname": "h",
            "capability_report": _capability_report(),
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3. enroll happy path
# ---------------------------------------------------------------------------
async def test_enroll_happy_path(client: AsyncClient):
    access = await become_provider(client, "prov6@example.com")
    created = await mint_token(client, access)
    raw = created["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-6")

    assert out["machine_secret"].startswith("msk_")
    assert out["heartbeat_interval_seconds"] == 30
    assert out["online_window_seconds"] == 90
    machine_id = out["machine_id"]

    # Machine listed, availability offline, status offline (no heartbeat), detected.
    machines = (await client.get("/provider/machines", headers=auth_headers(access))).json()
    assert len(machines) == 1
    m = machines[0]
    assert m["availability"] == "offline"
    assert m["status"] == "offline"
    assert m["verification_status"] == "detected"
    assert m["hardware"]["gpu_model"] == "NVIDIA A100"
    assert m["hardware"]["gpu_count"] == 1
    assert "text-generation" in m["supported_workloads"]

    # DB: hardware stored DETECTED, never verified. Token used + linked.
    sm = get_sessionmaker()
    async with sm() as s:
        hw = (
            await s.execute(
                select(HardwareInventory).where(
                    HardwareInventory.machine_id == uuid.UUID(machine_id)
                )
            )
        ).scalar_one()
        assert hw.verification_status == "detected"

        tok = (
            await s.execute(
                select(EnrollmentToken).where(
                    EnrollmentToken.id == uuid.UUID(created["enrollment_token_id"])
                )
            )
        ).scalar_one()
        assert tok.used_at is not None
        assert tok.machine_id == uuid.UUID(machine_id)

        machine = (
            await s.execute(
                select(ProviderMachine).where(ProviderMachine.id == uuid.UUID(machine_id))
            )
        ).scalar_one()
        # The returned msk_ plaintext is NOT persisted verbatim.
        assert machine.signing_key != out["machine_secret"]
        assert machine.secret_hash == hashlib.sha256(
            out["machine_secret"].encode()
        ).hexdigest()
        # The signing key is the random component (what the agent signs with).
        assert machine.signing_key == out["machine_secret"].removeprefix("msk_")


# ---------------------------------------------------------------------------
# 4. heartbeat security
# ---------------------------------------------------------------------------
async def test_heartbeat_valid_updates_and_online(client: AsyncClient):
    access = await become_provider(client, "prov7@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-7")
    mid, secret = out["machine_id"], out["machine_secret"]

    headers, body = heartbeat_headers(mid, secret, _hb_body("online"))
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    detail = (
        await client.get(f"/provider/machines/{mid}", headers=auth_headers(access))
    ).json()
    assert detail["status"] == "online"
    assert detail["availability"] == "online"
    assert detail["last_heartbeat_at"] is not None
    assert detail["last_health"]["uptime_s"] == 42


async def test_heartbeat_tampered_body_rejected(client: AsyncClient):
    access = await become_provider(client, "prov8@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-8")
    mid, secret = out["machine_id"], out["machine_secret"]

    headers, _ = heartbeat_headers(mid, secret, _hb_body("online"))
    # Send a different body than what was signed.
    tampered = json.dumps(_hb_body("maintenance")).encode()
    resp = await client.post("/v1/agents/heartbeat", content=tampered, headers=headers)
    assert resp.status_code == 401


async def test_heartbeat_bad_signature_rejected(client: AsyncClient):
    access = await become_provider(client, "prov9@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-9")
    mid, secret = out["machine_id"], out["machine_secret"]

    headers, body = heartbeat_headers(mid, secret, _hb_body("online"))
    headers["X-ComputeCoin-Signature"] = "deadbeef" * 8
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=headers)
    assert resp.status_code == 401


async def test_heartbeat_stale_timestamp_rejected(client: AsyncClient):
    access = await become_provider(client, "prov10@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-10")
    mid, secret = out["machine_id"], out["machine_secret"]

    old_ts = int(utcnow().timestamp()) - 1000  # > 300s skew
    headers, body = heartbeat_headers(mid, secret, _hb_body("online"), timestamp=old_ts)
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=headers)
    assert resp.status_code == 401


async def test_heartbeat_unknown_machine_rejected(client: AsyncClient):
    access = await become_provider(client, "prov11@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-11")
    secret = out["machine_secret"]
    unknown = str(uuid.uuid4())
    headers, body = heartbeat_headers(unknown, secret, _hb_body("online"))
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. online/offline derivation
# ---------------------------------------------------------------------------
async def test_stale_heartbeat_shows_offline(client: AsyncClient):
    access = await become_provider(client, "prov12@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-12")
    mid, secret = out["machine_id"], out["machine_secret"]

    headers, body = heartbeat_headers(mid, secret, _hb_body("online"))
    assert (await client.post("/v1/agents/heartbeat", content=body, headers=headers)).status_code == 200

    # Age the heartbeat beyond the online window.
    sm = get_sessionmaker()
    async with sm() as s:
        machine = (
            await s.execute(
                select(ProviderMachine).where(ProviderMachine.id == uuid.UUID(mid))
            )
        ).scalar_one()
        machine.last_heartbeat_at = utcnow() - dt.timedelta(seconds=200)
        await s.commit()

    detail = (
        await client.get(f"/provider/machines/{mid}", headers=auth_headers(access))
    ).json()
    assert detail["status"] == "offline"
    assert detail["availability"] == "online"  # operator setting unchanged


# ---------------------------------------------------------------------------
# 6. isolation
# ---------------------------------------------------------------------------
async def test_provider_isolation(client: AsyncClient):
    access_a = await become_provider(client, "a@example.com")
    access_b = await become_provider(client, "b@example.com")

    raw_a = (await mint_token(client, access_a))["enrollment_token"]
    out_a = await enroll_machine(client, raw_a, hostname="a-box")
    mid_a = out_a["machine_id"]

    # B cannot GET/PATCH/DELETE A's machine.
    assert (await client.get(f"/provider/machines/{mid_a}", headers=auth_headers(access_b))).status_code == 404
    assert (
        await client.patch(
            f"/provider/machines/{mid_a}",
            json={"availability": "maintenance"},
            headers=auth_headers(access_b),
        )
    ).status_code == 404
    assert (await client.delete(f"/provider/machines/{mid_a}", headers=auth_headers(access_b))).status_code == 404

    # B cannot see A's tokens.
    b_tokens = (await client.get("/provider/enrollment-tokens", headers=auth_headers(access_b))).json()
    assert b_tokens == []
    # A still owns its machine.
    assert (await client.get(f"/provider/machines/{mid_a}", headers=auth_headers(access_a))).status_code == 200


# ---------------------------------------------------------------------------
# 7. PATCH + DELETE lifecycle
# ---------------------------------------------------------------------------
async def test_patch_updates_and_delete_deregisters(client: AsyncClient):
    access = await become_provider(client, "prov13@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="gpu-13")
    mid, secret = out["machine_id"], out["machine_secret"]

    patched = await client.patch(
        f"/provider/machines/{mid}",
        json={
            "availability": "maintenance",
            "asking_price_per_hour": "1.50",
            "max_concurrent_jobs": 4,
            "region": "us-east",
            "supported_workloads": ["batch-inference"],
        },
        headers=auth_headers(access),
    )
    assert patched.status_code == 200, patched.text
    pj = patched.json()
    assert pj["availability"] == "maintenance"
    assert pj["status"] == "maintenance"
    assert pj["asking_price_per_hour"] == "1.50"
    assert pj["max_concurrent_jobs"] == 4
    assert pj["region"] == "us-east"
    assert pj["supported_workloads"] == ["batch-inference"]

    # Deregister.
    assert (await client.delete(f"/provider/machines/{mid}", headers=auth_headers(access))).status_code == 204
    # Gone from management.
    assert (await client.get(f"/provider/machines/{mid}", headers=auth_headers(access))).status_code == 404
    assert (await client.get("/provider/machines", headers=auth_headers(access))).json() == []

    # Subsequent heartbeat is 401 (credential revoked).
    headers, body = heartbeat_headers(mid, secret, _hb_body("online"))
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=headers)
    assert resp.status_code == 401
