"""Tests for AMD GPU support: schema, migration, and agent capability ingest (WP4)."""

from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select, text
from alembic.config import Config
from alembic import command

from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.model_registry import Model, ModelVersion, ModelVersionImage, image_for
from app.models.provider import EnrollmentToken, HardwareInventory, ProviderMachine
from app.models.verification import VerificationChallenge
from app.schemas.jobs import JobCreate, JobOut
from app.schemas.marketplace import MarketplaceMachineOut
from app.schemas.provider import GpuReport, HardwareOut
from tests.helpers import auth_headers, register_verify_login


async def become_provider(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    access = tokens["access_token"]
    resp = await client.post(
        "/provider/register",
        json={"display_name": "Test Provider", "contact_email": email},
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


async def test_old_agent_report_ingests_as_nvidia_cuda(client: AsyncClient):
    """An old agent report with no gpu_vendor/gpu_runtime backfills to nvidia/cuda (D8)."""
    access = await become_provider(client, "old_agent@test.com")
    tok_data = await mint_token(client, access)

    # Legacy agent report: only model, vram_mb, cuda_version
    report = {
        "verification_status": "detected",
        "cpu_model": "Intel Xeon",
        "cpu_cores": 16,
        "ram_mb": 65536,
        "disk_gb": 1000,
        "gpus": [{"model": "NVIDIA RTX 4090", "vram_mb": 24576, "cuda_version": "12.4"}],
        "supported_workloads": ["text-generation"],
    }

    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": tok_data["enrollment_token"],
            "agent_version": "0.1.0",
            "hostname": "old-box-1",
            "capability_report": report,
        },
    )
    assert resp.status_code == 201, resp.text
    mach_id = uuid.UUID(resp.json()["machine_id"])

    # Query DB directly to check HardwareInventory fields
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        res = await db.execute(select(HardwareInventory).where(HardwareInventory.machine_id == mach_id))
        hw = res.scalar_one()
        assert hw.gpu_vendor == "nvidia"
        assert hw.gpu_runtime == "cuda"
        assert hw.gpu_runtime_version == "12.4"
        assert hw.cuda_version == "12.4"

    # Query provider machine endpoint to check HardwareOut serialization
    m_resp = await client.get("/provider/machines", headers=auth_headers(access))
    assert m_resp.status_code == 200
    machines = m_resp.json()
    m = next(item for item in machines if item["machine_id"] == str(mach_id))
    hw_out = m["hardware"]
    assert hw_out["gpu_vendor"] == "nvidia"
    assert hw_out["gpu_runtime"] == "cuda"
    assert hw_out["gpu_runtime_version"] == "12.4"


async def test_amd_agent_report_ingests_correctly(client: AsyncClient):
    """An AMD agent report ingests vendor, runtime, and runtime_version (WP4)."""
    access = await become_provider(client, "amd_agent@test.com")
    tok_data = await mint_token(client, access)

    report = {
        "verification_status": "detected",
        "cpu_model": "AMD EPYC 7763",
        "cpu_cores": 64,
        "ram_mb": 262144,
        "disk_gb": 2000,
        "gpus": [
            {
                "model": "AMD Radeon RX 7900 XTX",
                "vram_mb": 24576,
                "gpu_vendor": "amd",
                "gpu_runtime": "rocm",
                "gpu_runtime_version": "6.2.0",
            }
        ],
        "supported_workloads": ["text-generation", "batch-inference"],
    }

    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": tok_data["enrollment_token"],
            "agent_version": "0.2.0",
            "hostname": "amd-box-1",
            "capability_report": report,
        },
    )
    assert resp.status_code == 201, resp.text
    mach_id = uuid.UUID(resp.json()["machine_id"])

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        res = await db.execute(select(HardwareInventory).where(HardwareInventory.machine_id == mach_id))
        hw = res.scalar_one()
        assert hw.gpu_vendor == "amd"
        assert hw.gpu_runtime == "rocm"
        assert hw.gpu_runtime_version == "6.2.0"
        assert hw.gpu_model == "AMD Radeon RX 7900 XTX"


async def test_enroll_rejects_unknown_vendor_with_422(client: AsyncClient):
    access = await become_provider(client, "bad_vendor@test.com")
    tok_data = await mint_token(client, access)

    report = {
        "gpus": [{"model": "Unknown GPU", "vram_mb": 16384, "gpu_vendor": "intel", "gpu_runtime": "cuda"}],
    }
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": tok_data["enrollment_token"],
            "hostname": "intel-box",
            "capability_report": report,
        },
    )
    assert resp.status_code == 422
    assert "Unknown gpu_vendor" in resp.json()["detail"]


async def test_enroll_rejects_unknown_runtime_with_422(client: AsyncClient):
    access = await become_provider(client, "bad_runtime@test.com")
    tok_data = await mint_token(client, access)

    report = {
        "gpus": [{"model": "RTX 4090", "vram_mb": 24576, "gpu_vendor": "nvidia", "gpu_runtime": "opencl"}],
    }
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": tok_data["enrollment_token"],
            "hostname": "opencl-box",
            "capability_report": report,
        },
    )
    assert resp.status_code == 422
    assert "Unknown gpu_runtime" in resp.json()["detail"]


async def test_enroll_rejects_mixed_vendors_with_422(client: AsyncClient):
    """D2: One vendor per machine. Mixed vendor reports return 422."""
    access = await become_provider(client, "mixed_vendor@test.com")
    tok_data = await mint_token(client, access)

    report = {
        "gpus": [
            {"model": "RTX 4090", "vram_mb": 24576, "gpu_vendor": "nvidia", "gpu_runtime": "cuda"},
            {"model": "RX 7900 XTX", "vram_mb": 24576, "gpu_vendor": "amd", "gpu_runtime": "rocm"},
        ],
    }
    resp = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": tok_data["enrollment_token"],
            "hostname": "mixed-box",
            "capability_report": report,
        },
    )
    assert resp.status_code == 422
    assert "Mixed-vendor" in resp.json()["detail"]


def test_job_create_schema_required_gpu_runtime():
    # Default is cuda
    jc = JobCreate(workload_type="text-generation", model="m1", input={"prompt": "hi"})
    assert jc.required_gpu_runtime == "cuda"

    # Explicit rocm and any are accepted
    jc_rocm = JobCreate(workload_type="text-generation", model="m1", input={"prompt": "hi"}, required_gpu_runtime="rocm")
    assert jc_rocm.required_gpu_runtime == "rocm"

    jc_any = JobCreate(workload_type="text-generation", model="m1", input={"prompt": "hi"}, required_gpu_runtime="any")
    assert jc_any.required_gpu_runtime == "any"

    # Invalid pattern fails
    with pytest.raises(Exception):
        JobCreate(workload_type="text-generation", model="m1", input={"prompt": "hi"}, required_gpu_runtime="metal")


def test_model_version_image_and_image_for_helper():
    mv = ModelVersion(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        version_tag="llama-test",
        runtime="hardware",
        image_digest="cuda_digest_default",
    )
    # Without child rows, cuda returns default image_digest, rocm returns None
    assert image_for(mv, "cuda") == "cuda_digest_default"
    assert image_for(mv, "rocm") is None

    # Add child rows
    img_cuda = ModelVersionImage(id=uuid.uuid4(), model_version_id=mv.id, gpu_runtime="cuda", image_digest="cuda_digest_v2")
    img_rocm = ModelVersionImage(id=uuid.uuid4(), model_version_id=mv.id, gpu_runtime="rocm", image_digest="rocm_digest_v2")
    mv.images = [img_cuda, img_rocm]

    assert image_for(mv, "cuda") == "cuda_digest_v2"
    assert image_for(mv, "rocm") == "rocm_digest_v2"
    assert image_for(mv, "metal") is None


def test_migration_0022_upgrade_downgrade_cycle(tmp_path, monkeypatch):
    """Test Alembic migration 0022 upgrade -> downgrade -> upgrade on SQLite."""
    import os
    from app.core.config import settings

    db_file = tmp_path / "test_migration.db"
    async_db_url = f"sqlite+aiosqlite:///{db_file}"
    sync_db_url = f"sqlite:///{db_file}"

    monkeypatch.setattr(settings, "DATABASE_URL", async_db_url)

    alembic_cfg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg = Config(alembic_cfg_path)
    cfg.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic")))

    # 1. Upgrade to 0021_tpm_enrolment
    command.upgrade(cfg, "0021_tpm_enrolment")

    # Populate some pre-migration data to test backfill
    engine = create_engine(sync_db_url)
    with engine.begin() as conn:
        u_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, email_verified, status, created_at, updated_at) "
                "VALUES (:id, 'migr@test.com', 'hash', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": u_id},
        )
        prov_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO providers (id, user_id, display_name, status, created_at, updated_at) "
                "VALUES (:id, :u_id, 'Prov', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": prov_id, "u_id": u_id},
        )
        m_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO provider_machines (id, provider_id, hostname, status, availability, max_concurrent_jobs, signing_key, secret_hash, created_at, updated_at) "
                "VALUES (:id, :prov_id, 'host-1', 'registered', 'online', 1, 'key', 'hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": m_id, "prov_id": prov_id},
        )
        # Pre-existing machine with GPU
        conn.execute(
            text(
                "INSERT INTO hardware_inventory (id, machine_id, verification_status, cpu_cores, ram_mb, disk_gb, gpu_count, vram_mb, created_at, updated_at) "
                "VALUES (:id, :m_id, 'detected', 16, 32768, 500, 1, 24576, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": str(uuid.uuid4()), "m_id": m_id},
        )
        # Pre-existing job
        j_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO jobs (id, customer_id, workload_type, model, input, status, gpu_count, estimated_minutes, max_runtime_seconds, retry_policy, attempts_made, priority, execution_mode, isolation_tier, cancel_requested, is_challenge, created_at, updated_at) "
                "VALUES (:id, :u_id, 'text-generation', 'llama', '{}', 'created', 1, 1, 300, '{}', 0, 0, 'simulated', 'container', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": j_id, "u_id": u_id},
        )
        # Pre-existing model version with image_digest
        mod_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO models (id, name, trust_status, created_at, updated_at) "
                "VALUES (:id, 'llama-mod', 'approved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": mod_id},
        )
        mv_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO model_versions (id, model_id, version_tag, runtime, image_digest, supported_workloads, trust_status, created_at, updated_at) "
                "VALUES (:id, :mod_id, 'llama-v1', 'hardware', 'sha256:legacy_cuda_image', '[]', 'approved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": mv_id, "mod_id": mod_id},
        )

    # 2. Upgrade to 0022_amd_gpu_support
    command.upgrade(cfg, "0022_amd_gpu_support")

    # Verify backfill in populated DB
    with engine.begin() as conn:
        hw = conn.execute(text("SELECT gpu_vendor, gpu_runtime FROM hardware_inventory")).fetchone()
        assert hw[0] == "nvidia"
        assert hw[1] == "cuda"

        job = conn.execute(text("SELECT required_gpu_runtime FROM jobs")).fetchone()
        assert job[0] == "cuda"

        all_mvi = conn.execute(text("SELECT model_version_id, gpu_runtime, image_digest FROM model_version_images")).fetchall()
        assert len(all_mvi) >= 1
        mvi = next(r for r in all_mvi if r[2] == "sha256:legacy_cuda_image")
        assert mvi is not None
        assert mvi[1] == "cuda"

    # 3. Downgrade back to 0021_tpm_enrolment
    command.downgrade(cfg, "0021_tpm_enrolment")

    # 4. Upgrade back to 0022_amd_gpu_support
    command.upgrade(cfg, "0022_amd_gpu_support")
