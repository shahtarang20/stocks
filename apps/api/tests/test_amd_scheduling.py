"""AMD GPU support: where jobs go and what runs (D1/D4/D5, WP5/WP6 rules).

Before these fixes the agent could enrol an AMD GPU but nothing on the server looked at the
runtime: a default (CUDA) job could be placed on an AMD machine and handed the CUDA image, a
shadow re-run of an AMD job could be compared with an NVIDIA run, and an AMD challenge answer was
checked against the CUDA hash, so honest AMD hosts could be marked as failing.
"""

from __future__ import annotations

import decimal
import hashlib
import hmac
import json
import time
import uuid

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.model_registry import Model, ModelVersion, ModelVersionImage
from app.models.enums import UserStatus
from app.models.provider import HardwareInventory, Provider, ProviderCapability, ProviderMachine
from app.models.user import User
from app.services import job_fsm as fsm
from app.services.scheduler import JobValidationError, _eligible_machines, validate_job
from tests.helpers import get_user_id, register_verify_login

MODEL = "amd-test-model-v1"
CUDA_IMG = "registry.example/model-cuda@sha256:" + "c" * 64
ROCM_IMG = "registry.example/model-rocm@sha256:" + "a" * 64


async def _customer(client) -> uuid.UUID:
    email = f"amd-{uuid.uuid4().hex[:6]}@example.com"
    await register_verify_login(client, email)
    return await get_user_id(email)


async def _model(*, rocm_image: bool) -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        model = Model(name=f"amd-model-{uuid.uuid4().hex[:6]}", trust_status="approved")
        s.add(model)
        await s.flush()
        mv = ModelVersion(model_id=model.id, version_tag=MODEL, runtime="vllm", image_digest=CUDA_IMG,
                          supported_workloads=["text-generation"], trust_status="approved")
        s.add(mv)
        await s.flush()
        if rocm_image:
            s.add(ModelVersionImage(model_version_id=mv.id, gpu_runtime="rocm", image_digest=ROCM_IMG))
        await s.commit()


async def _machine(user_id, name: str, runtime: str | None, *, tiers=("container",), key="k", gpu_model: str | None = None, price: str = "1.00") -> uuid.UUID:
    sm = get_sessionmaker()
    async with sm() as s:
        owner = User(email=f"host-{uuid.uuid4().hex[:8]}@example.com", password_hash="pw", email_verified=True,
                     status=UserStatus.active)  # one provider per user, so each machine gets its own host
        s.add(owner)
        await s.flush()
        provider = Provider(user_id=owner.id, display_name=name)
        s.add(provider)
        await s.flush()
        m = ProviderMachine(provider_id=provider.id, hostname=name, status="registered", availability="online",
                            asking_price_per_hour=decimal.Decimal(price), max_concurrent_jobs=4, signing_key=key,
                            secret_hash="h", last_heartbeat_at=utcnow())
        s.add(m)
        await s.flush()
        s.add(ProviderCapability(machine_id=m.id, supported_workloads=["text-generation"], supported_isolation_tiers=list(tiers)))
        chosen_model = gpu_model or ("RX 7900" if runtime == "rocm" else "RTX 4090")
        s.add(HardwareInventory(machine_id=m.id, gpu_count=1, gpu_model=chosen_model,
                                gpu_vendor={"rocm": "amd", "cuda": "nvidia"}.get(runtime or ""), gpu_runtime=runtime))
        await s.commit()
        return m.id


async def _eligible(customer, **job_kw) -> set:
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                  status=fsm.QUEUED, gpu_count=job_kw.pop("gpu_count", 1), **job_kw)
        s.add(job)
        await s.flush()
        ids = {c.machine.id for c in await _eligible_machines(s, job, utcnow())}
        await s.rollback()
        return ids


@pytest.mark.asyncio
async def test_jobs_only_go_to_machines_with_their_gpu_runtime(client):
    customer = await _customer(client)
    await _model(rocm_image=True)
    nvidia = await _machine(customer, "nv", "cuda")
    legacy = await _machine(customer, "old-nv", None)  # enrolled before AMD support = NVIDIA (D8)
    amd = await _machine(customer, "amd", "rocm")
    hw = dict(execution_mode="hardware")
    assert await _eligible(customer, **hw) == {nvidia, legacy}  # default is cuda
    assert await _eligible(customer, required_gpu_runtime="rocm", **hw) == {amd}
    assert await _eligible(customer, required_gpu_runtime="any", **hw) == {nvidia, legacy, amd}
    assert await _eligible(customer, gpu_count=0, **hw) >= {nvidia, legacy, amd}  # CPU work fits anywhere


@pytest.mark.asyncio
async def test_amd_machines_need_a_rocm_image_and_the_container_tier(client):
    customer = await _customer(client)
    await _model(rocm_image=False)
    amd = await _machine(customer, "amd", "rocm", tiers=("container", "gvisor"))
    assert await _eligible(customer, required_gpu_runtime="rocm", execution_mode="hardware") == set()  # no ROCm build
    assert await _eligible(customer, required_gpu_runtime="rocm", execution_mode="simulated") == {amd}
    # D5: even when the machine lists gVisor, an AMD GPU job is not offered anything but container.
    assert await _eligible(customer, required_gpu_runtime="rocm", execution_mode="simulated", isolation_tier="gvisor") == set()


@pytest.mark.asyncio
async def test_pinning_a_job_to_a_machine_of_the_other_runtime_is_refused(client):
    customer = await _customer(client)
    await _model(rocm_image=True)
    amd = await _machine(customer, "amd", "rocm")
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                  status=fsm.CREATED, gpu_count=1, machine_id=amd)
        with pytest.raises(JobValidationError, match="^gpu_runtime$"):
            await validate_job(s, job)


def _headers(machine_id, key: str, raw: bytes) -> dict:
    ts = int(time.time())
    sig = hmac.new(key.encode(), f"{machine_id}\n{ts}\n{hashlib.sha256(raw).hexdigest()}".encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-ComputeCoin-Machine": str(machine_id),
            "X-ComputeCoin-Timestamp": str(ts), "X-ComputeCoin-Signature": sig}


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime,image", [("rocm", ROCM_IMG), ("cuda", CUDA_IMG)])
async def test_claim_hands_out_the_image_built_for_the_machines_runtime(client, runtime, image):
    customer = await _customer(client)
    await _model(rocm_image=True)
    machine = await _machine(customer, f"m-{runtime}", runtime, key="claim-key")
    sm = get_sessionmaker()
    async with sm() as s:
        s.add(Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                  status=fsm.RESERVED, gpu_count=1, machine_id=machine, execution_mode="hardware",
                  required_gpu_runtime="any"))
        await s.commit()
    body = json.dumps({"accept_private": False}).encode()
    r = await client.post("/v1/agents/jobs/claim", content=body, headers=_headers(machine, "claim-key", body))
    assert r.status_code == 200, r.text
    assert r.json()["image_digest"] == image


@pytest.mark.asyncio
async def test_challenge_answers_are_per_runtime_and_missing_means_inconclusive(client):
    from app.services.challenge import create_challenge, evaluate_challenge_job, hash_for_policy, inject_challenge_job

    customer = await _customer(client)
    await _model(rocm_image=True)
    amd = await _machine(customer, "amd", "rocm")
    output = {"text": "rocm answer"}
    sm = get_sessionmaker()
    async with sm() as s:
        challenge = await create_challenge(s, workload_type="text-generation", model=MODEL, input={"prompt": "q"},
                                           expected_output={"text": "cuda answer"})
        job = await inject_challenge_job(s, challenge=challenge, customer_id=customer, machine_id=amd)
        job.gpu_count = 1
        # The CUDA answer must not be used to judge an AMD machine: no ROCm answer = inconclusive.
        assert await evaluate_challenge_job(s, job, output) is None
        challenge.expected_hashes_by_runtime = {"rocm": hash_for_policy(output, challenge.config)}
        result = await evaluate_challenge_job(s, job, output)
        assert result is not None and result.passed is True


@pytest.mark.asyncio
async def test_a_shadow_re_run_uses_the_same_runtime_as_the_primary(client):
    from app.services.verification import create_shadow_job

    customer = await _customer(client)
    await _model(rocm_image=True)
    primary_machine = await _machine(customer, "amd-a", "rocm")
    nvidia = await _machine(customer, "nv", "cuda")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                      status=fsm.COMPLETED, gpu_count=1, machine_id=primary_machine, execution_mode="simulated",
                      required_gpu_runtime="any")
        s.add(primary)
        await s.flush()
        # Only an NVIDIA machine is free: comparing its bytes with an AMD run could wrongly accuse
        # the AMD host, so no shadow is made (inconclusive) rather than a cross-vendor one.
        assert await create_shadow_job(s, primary) is None
        await s.commit()
    other_amd = await _machine(customer, "amd-b", "rocm")
    async with sm() as s:
        primary = await s.get(Job, primary.id)
        shadow, _ = await create_shadow_job(s, primary)
        assert shadow.required_gpu_runtime == "rocm" and shadow.machine_id == other_amd
        assert shadow.machine_id != nvidia


@pytest.mark.asyncio
async def test_a_rocm_job_with_a_tier_amd_cannot_offer_is_refused_at_submit(client):
    customer = await _customer(client)
    await _model(rocm_image=True)
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                  status=fsm.CREATED, gpu_count=1, required_gpu_runtime="rocm", isolation_tier="gvisor")
        with pytest.raises(JobValidationError, match="rocm_tier_gvisor_unsupported"):
            await validate_job(s, job)


@pytest.mark.asyncio
async def test_model_identity_is_checked_against_the_image_for_the_machines_runtime(client):
    """An AMD host reporting its (correct) ROCm image digest used to be compared with the CUDA one."""
    from app.services.verification import check_model_identity

    customer = await _customer(client)
    await _model(rocm_image=True)
    amd = await _machine(customer, "amd", "rocm")
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(customer_id=customer, workload_type="text-generation", model=MODEL, input={"prompt": "hi"},
                  status=fsm.RUNNING, gpu_count=1, machine_id=amd, execution_mode="hardware")
        s.add(job)
        await s.flush()
        honest = await check_model_identity(s, job, ROCM_IMG.split("@", 1)[1])
        assert honest is not None and honest.passed is True


@pytest.mark.asyncio
async def test_scheduler_logs_clear_skip_reasons(client, caplog):
    """Scheduler must log clear reasons: (gpu_runtime), (amd_isolation), (no_image)."""
    import logging
    caplog.set_level(logging.DEBUG)

    customer = await _customer(client)
    # Model without ROCm image
    await _model(rocm_image=False)
    amd_m = await _machine(customer, "amd-box", "rocm", tiers=("container", "gvisor"))
    nv_m = await _machine(customer, "nv-box", "cuda")

    # 1) CUDA job on AMD machine -> logs "scheduler skip (gpu_runtime)"
    caplog.clear()
    await _eligible(customer, required_gpu_runtime="cuda", execution_mode="hardware")
    assert any("scheduler skip (gpu_runtime)" in record.message and str(amd_m) in record.message for record in caplog.records)

    # 2) ROCm job with gVisor tier -> logs "scheduler skip (amd_isolation)"
    caplog.clear()
    await _eligible(customer, required_gpu_runtime="rocm", isolation_tier="gvisor", execution_mode="simulated")
    assert any("scheduler skip (amd_isolation)" in record.message and str(amd_m) in record.message for record in caplog.records)

    # 3) ROCm hardware job on AMD machine without ROCm image -> logs "scheduler skip (no_image)"
    caplog.clear()
    await _eligible(customer, required_gpu_runtime="rocm", execution_mode="hardware")
    assert any("scheduler skip (no_image)" in record.message and str(amd_m) in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_inference_routing_amd_image_and_skip_logging(client, caplog):
    """Inference on AMD-only marketplace uses ROCm image; with no ROCm image, AMD machine is skipped with logged reason."""
    import logging
    from tests.helpers import auth_headers

    caplog.set_level(logging.DEBUG)
    customer = await _customer(client)

    # Register customer and get auth token
    user_data = await register_verify_login(client, f"inf-{uuid.uuid4().hex[:6]}@example.com")
    token = user_data["access_token"]
    headers = auth_headers(token)

    # Create model tag
    inf_model = f"amd-inf-{uuid.uuid4().hex[:6]}"
    sm = get_sessionmaker()
    async with sm() as s:
        m_obj = Model(name=f"model-{inf_model}", trust_status="approved")
        s.add(m_obj)
        await s.flush()
        mv = ModelVersion(
            model_id=m_obj.id,
            version_tag=inf_model,
            runtime="vllm",  # a hardware model: image routing applies (a simulated model runs no image)
            image_digest=CUDA_IMG,
            supported_workloads=["text-generation"],
            trust_status="approved",
        )
        s.add(mv)
        await s.commit()

    # Create only an AMD machine
    amd_m = await _machine(customer, f"amd-inf-{uuid.uuid4().hex[:6]}", "rocm")

    # 1) Model has CUDA_IMG but no ROCm image: AMD machine skipped with logged reason "no_image"
    caplog.clear()
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": inf_model, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    # The AMD machine was skipped because no ROCm image exists for this model
    assert any("inference skip (no_image)" in record.message and str(amd_m) in record.message for record in caplog.records)
    # Since no machine was eligible, response proceeded as simulated fallback without machine
    assert resp.json()["computecoin"]["machine_id"] is None
    assert resp.json()["computecoin"]["image_digest"] is None

    # 2) Now add ROCm image to model
    async with sm() as s:
        mv_row = (await s.execute(select(ModelVersion).where(ModelVersion.version_tag == inf_model))).scalar_one()
        s.add(ModelVersionImage(model_version_id=mv_row.id, gpu_runtime="rocm", image_digest=ROCM_IMG))
        await s.commit()

    # Now inference routes to the AMD machine and uses the ROCm image!
    caplog.clear()
    resp2 = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": inf_model, "messages": [{"role": "user", "content": "hello again"}]},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["computecoin"]["machine_id"] == str(amd_m)
    assert data2["computecoin"]["image_digest"] == ROCM_IMG


@pytest.mark.asyncio
async def test_marketplace_http_route_filters_by_vendor_and_runtime(client):
    """GET /v1/marketplace/machines filters by gpu_vendor and gpu_runtime through HTTP route."""
    customer = await _customer(client)
    amd_id = await _machine(customer, "market-amd", "rocm")
    nv_id = await _machine(customer, "market-nv", "cuda")

    # Filter by gpu_vendor=amd
    r_amd_vendor = await client.get("/v1/marketplace/machines?gpu_vendor=amd")
    assert r_amd_vendor.status_code == 200
    ids_amd_vendor = {m["machine_id"] for m in r_amd_vendor.json()["items"]}
    assert str(amd_id) in ids_amd_vendor
    assert str(nv_id) not in ids_amd_vendor

    # Filter by gpu_vendor=nvidia
    r_nv_vendor = await client.get("/v1/marketplace/machines?gpu_vendor=nvidia")
    assert r_nv_vendor.status_code == 200
    ids_nv_vendor = {m["machine_id"] for m in r_nv_vendor.json()["items"]}
    assert str(nv_id) in ids_nv_vendor
    assert str(amd_id) not in ids_nv_vendor

    # Filter by gpu_runtime=rocm
    r_rocm = await client.get("/v1/marketplace/machines?gpu_runtime=rocm")
    assert r_rocm.status_code == 200
    ids_rocm = {m["machine_id"] for m in r_rocm.json()["items"]}
    assert str(amd_id) in ids_rocm
    assert str(nv_id) not in ids_rocm

    # Filter by gpu_runtime=cuda
    r_cuda = await client.get("/v1/marketplace/machines?gpu_runtime=cuda")
    assert r_cuda.status_code == 200
    ids_cuda = {m["machine_id"] for m in r_cuda.json()["items"]}
    assert str(nv_id) in ids_cuda
    assert str(amd_id) not in ids_cuda


@pytest.mark.asyncio
async def test_shadow_machine_prefers_same_gpu_model(client):
    from app.services.verification import create_shadow_job

    customer = await _customer(client)
    await _model(rocm_image=True)
    primary_machine = await _machine(customer, "amd-primary", "rocm", gpu_model="RX 7900 XTX", price="1.00")
    # Candidate A: cheaper ($0.50), but DIFFERENT GPU model ("RX 7600")
    cand_diff_model = await _machine(customer, "amd-cheap", "rocm", gpu_model="RX 7600", price="0.50")
    # Candidate B: more expensive ($2.00), but SAME GPU model ("RX 7900 XTX")
    cand_same_model = await _machine(customer, "amd-match", "rocm", gpu_model="RX 7900 XTX", price="2.00")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(
            customer_id=customer,
            workload_type="text-generation",
            model=MODEL,
            input={"prompt": "hi"},
            status=fsm.COMPLETED,
            gpu_count=1,
            machine_id=primary_machine,
            execution_mode="simulated",
            required_gpu_runtime="any",
        )
        s.add(primary)
        await s.flush()
        shadow, _ = await create_shadow_job(s, primary)
        # Even though cand_diff_model is $0.50 and cand_same_model is $2.00, cand_same_model wins
        # because shadow re-runs prefer the same GPU model!
        assert shadow is not None
        assert shadow.machine_id == cand_same_model


@pytest.mark.asyncio
async def test_honest_amd_provider_never_mismatched_or_slashed(client):
    from app.services.verification import create_shadow_job
    from app.services import reputation as reputation_svc

    customer = await _customer(client)
    await _model(rocm_image=True)
    primary_machine = await _machine(customer, "honest-amd", "rocm")
    # Only NVIDIA machines exist as other providers
    nvidia_machine = await _machine(customer, "other-nv", "cuda")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(
            customer_id=customer,
            workload_type="text-generation",
            model=MODEL,
            input={"prompt": "hi"},
            status=fsm.COMPLETED,
            gpu_count=1,
            machine_id=primary_machine,
            execution_mode="simulated",
            required_gpu_runtime="rocm",
            output={"text": "honest amd output bytes"},
        )
        s.add(primary)
        await s.flush()

        # No shadow is created because cross-vendor shadow is forbidden
        shadow_result = await create_shadow_job(s, primary)
        assert shadow_result is None

        # Confirm reputation signals show 0 verification failures
        mach = await s.get(ProviderMachine, primary_machine)
        signals = await reputation_svc.compute_signals(s, mach.provider_id)
        assert signals.verification_failures == 0


@pytest.mark.asyncio
async def test_cheating_amd_provider_is_caught_by_amd_shadow(client):
    from app.services.verification import create_shadow_job, resolve_redundancy
    from app.models.verification import VerificationResult
    from app.services import reputation as reputation_svc

    customer = await _customer(client)
    await _model(rocm_image=True)
    cheat_machine = await _machine(customer, "cheat-amd", "rocm")
    honest_machine = await _machine(customer, "honest-shadow-amd", "rocm")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(
            customer_id=customer,
            workload_type="text-generation",
            model=MODEL,
            input={"prompt": "hi"},
            status=fsm.COMPLETED,
            gpu_count=1,
            machine_id=cheat_machine,
            execution_mode="simulated",
            required_gpu_runtime="rocm",
            output={"text": "cheat fake output"},
        )
        s.add(primary)
        await s.flush()

        shadow, check = await create_shadow_job(s, primary)
        assert shadow is not None and shadow.machine_id == honest_machine
        assert check.status == "pending"

        # Shadow runs on honest machine and produces real output
        resolved = await resolve_redundancy(s, shadow, {"text": "real honest output"})
        assert resolved is not None
        assert resolved.status == "mismatched"
        assert resolved.matched is False

        # Verification failure is recorded against primary job
        res = (
            await s.execute(
                select(VerificationResult).where(VerificationResult.job_id == primary.id)
            )
        ).scalar_one()
        assert res.passed is False

        # Provider reputation counts the verification failure
        mach = await s.get(ProviderMachine, cheat_machine)
        signals = await reputation_svc.compute_signals(s, mach.provider_id)
        assert signals.verification_failures == 1


@pytest.mark.asyncio
async def test_inference_never_routes_a_hardware_model_without_an_image_or_to_a_banned_machine(client):
    """An empty image set used to mean "no filter", and banned machines were not skipped."""
    from app.services.inference import _find_eligible_machine

    customer = await _customer(client)
    sm = get_sessionmaker()
    async with sm() as s:
        model = Model(name=f"no-image-{uuid.uuid4().hex[:6]}", trust_status="approved")
        s.add(model)
        await s.flush()
        bare = ModelVersion(model_id=model.id, version_tag="no-image-v1", runtime="vllm", image_digest=None,
                            supported_workloads=["text-generation"], trust_status="approved")
        s.add(bare)
        await s.commit()
    await _model(rocm_image=True)
    nvidia = await _machine(customer, "nv", "cuda")
    async with sm() as s:
        bare = (await s.execute(select(ModelVersion).where(ModelVersion.version_tag == "no-image-v1"))).scalar_one()
        machine, image = await _find_eligible_machine(s, "text-generation", model_version=bare)
        assert machine is None and image is None
        mv = (await s.execute(select(ModelVersion).where(ModelVersion.version_tag == MODEL))).scalar_one()
        machine, image = await _find_eligible_machine(s, "text-generation", model_version=mv)
        assert machine.id == nvidia and image == CUDA_IMG
        (await s.get(ProviderMachine, nvidia)).is_banned = True
        await s.flush()
        machine, _ = await _find_eligible_machine(s, "text-generation", model_version=mv)
        assert machine is None
