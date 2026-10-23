"""Phase B2 — result verification (anti-cheating).

Covers the three mechanisms as SERVICES (the job-result path is wired by the
integrator) plus the authorization rules on the verification router:

1. known-answer challenges  — wrong output detected, matching output passes,
   and ``jobs.is_challenge`` never reaches the agent claim payload;
2. redundant (shadow) execution — shadow lands on a DIFFERENT provider, the
   customer is never billed for it, mismatching outputs are recorded failed;
3. model identity — a wrong weight hash is flagged, the right one passes.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.billing import Invoice, LedgerTransaction
from app.models.enums import Role
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.models.verification import (
    RedundancyCheck,
    VerificationChallenge,
    VerificationResult,
)
from app.services import billing, challenge as challenge_service, ledger
from app.services import verification as verification_service
from tests.helpers import (
    auth_headers,
    get_user_id,
    login,
    grant_role,
    register_verify_login,
    seed_hardware_model,
)
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import claim, post_result, seed_echo_model, submit_job

pytestmark = pytest.mark.asyncio

HARDWARE_MODEL = "tinyllama-1.1b-chat-v1"
HARDWARE_DIGEST = "4a382c2b3e83b429d2f2d93e2b243888365671158d3493864a66a1523497c654"


@pytest_asyncio.fixture
async def vclient(db_engine) -> AsyncClient:
    """Client over an app that mounts ONLY the verification router.

    main.py wiring is the integrator's job (strict file ownership), so the
    router is exercised on its own app against the same test engine.
    """
    from app.api.routers import verification as verification_router

    app = FastAPI()
    app.include_router(verification_router.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _customer(client: AsyncClient, email: str) -> str:
    return (await register_verify_login(client, email))["access_token"]


async def _complete_job(client, machine: dict, token: str, output: dict) -> str:
    """Submit -> claim -> report success, returning the completed job id."""
    submitted = await submit_job(client, token)
    assert submitted.status_code == 201, submitted.text
    job_id = submitted.json()["job_id"]
    claimed = await claim(client, machine["machine_id"], machine["secret"])
    assert claimed.status_code == 200, claimed.text
    done = await post_result(
        client,
        machine["machine_id"],
        machine["secret"],
        job_id,
        {"status": "success", "output": output, "metrics": {"duration_seconds": 1}},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    return job_id


# ===========================================================================
# 1. known-answer challenges
# ===========================================================================
async def test_challenge_wrong_output_is_detected_and_recorded_failed(client):
    await seed_echo_model()
    await register_verify_login(client, "audit-a@x.com")
    platform_id = await get_user_id("audit-a@x.com")
    sm = get_sessionmaker()
    async with sm() as s:
        ch = await challenge_service.create_challenge(
            s,
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "2+2?"},
            expected_output={"text": "four"},
            label="arith",
        )
        job = await challenge_service.inject_challenge_job(
            s, challenge=ch, customer_id=platform_id
        )
        await s.commit()
        job_id, challenge_id = job.id, ch.id

    async with sm() as s:
        job = await s.get(Job, job_id)
        assert job.is_challenge is True
        result = await challenge_service.evaluate_challenge_job(
            s, job, {"text": "five"}  # a provider that did not run the model
        )
        await s.commit()
        assert result is not None
        assert result.passed is False
        assert result.kind == "challenge"
        assert result.challenge_id == challenge_id
        assert result.actual_hash != result.expected_hash

    # The failure is persisted as append-only evidence.
    async with sm() as s:
        rows = (
            await s.execute(
                select(VerificationResult).where(VerificationResult.job_id == job_id)
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0].passed is False


async def test_challenge_matching_output_passes_and_is_idempotent(client):
    await seed_echo_model()
    await register_verify_login(client, "audit-b@x.com")
    platform_id = await get_user_id("audit-b@x.com")
    sm = get_sessionmaker()
    async with sm() as s:
        ch = await challenge_service.create_challenge(
            s,
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "2+2?"},
            expected_output={"text": "four"},
        )
        job = await challenge_service.inject_challenge_job(
            s, challenge=ch, customer_id=platform_id
        )
        await s.commit()
        job_id = job.id

    async with sm() as s:
        job = await s.get(Job, job_id)
        first = await challenge_service.evaluate_challenge_job(s, job, {"text": "four"})
        await s.commit()
        assert first.passed is True
        # Key ordering must not matter, and a repeat delivery must not double-write.
        again = await challenge_service.evaluate_challenge_job(s, job, {"text": "four"})
        await s.commit()
        assert again.id == first.id

    async with sm() as s:
        rows = (
            await s.execute(
                select(VerificationResult).where(VerificationResult.job_id == job_id)
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_challenge_evaluates_after_result_path_overwrote_job_verification(client):
    """The shape-verification record overwrites jobs.verification; the challenge
    linkage must still resolve (fallback on model+workload+input)."""
    await seed_echo_model()
    await register_verify_login(client, "audit-c@x.com")
    platform_id = await get_user_id("audit-c@x.com")
    sm = get_sessionmaker()
    async with sm() as s:
        ch = await challenge_service.create_challenge(
            s,
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "capital of France?"},
            expected_output={"text": "Paris"},
        )
        job = await challenge_service.inject_challenge_job(
            s, challenge=ch, customer_id=platform_id
        )
        job.verification = {"valid": True, "workload_type": "text-generation"}
        await s.commit()
        job_id = job.id

    async with sm() as s:
        job = await s.get(Job, job_id)
        result = await challenge_service.evaluate_challenge_job(s, job, {"text": "Paris"})
        await s.commit()
        assert result is not None and result.passed is True


async def test_is_challenge_never_leaks_to_the_agent_claim_payload(client):
    await seed_echo_model()
    machine = await create_listable_machine(client, "p-chal@x.com", hostname="chal")
    await register_verify_login(client, "audit-d@x.com")
    platform_id = await get_user_id("audit-d@x.com")

    sm = get_sessionmaker()
    async with sm() as s:
        ch = await challenge_service.create_challenge(
            s,
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "Hello"},
            expected_output={"text": "Hello"},
        )
        await challenge_service.inject_challenge_job(
            s,
            challenge=ch,
            customer_id=platform_id,
            machine_id=uuid.UUID(machine["machine_id"]),
        )
        await s.commit()

    claimed = await claim(client, machine["machine_id"], machine["secret"])
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    # Indistinguishable from paid work: no flag, no challenge id, anywhere.
    assert "is_challenge" not in body
    assert "challenge_id" not in body
    assert "verification" not in body
    assert "challenge" not in str(body).lower()


async def test_challenge_sampling_honors_rate_bounds():
    assert challenge_service.should_inject_challenge(0.0) is False
    assert challenge_service.should_inject_challenge(1.0) is True
    assert verification_service.should_shadow(0.0) is False
    assert verification_service.should_shadow(1.0) is True


# ===========================================================================
# 2. redundant (shadow) execution
# ===========================================================================
async def _primary_with_two_providers(client, tag: str) -> tuple[dict, dict, str, str]:
    await seed_echo_model()
    m1 = await create_listable_machine(client, f"p1-{tag}@x.com", hostname=f"m1{tag}")
    m2 = await create_listable_machine(client, f"p2-{tag}@x.com", hostname=f"m2{tag}")
    token = await _customer(client, f"c-{tag}@x.com")
    job_id = await _complete_job(client, m1, token, {"text": "Hello"})
    return m1, m2, token, job_id


async def test_shadow_job_runs_on_a_different_provider(client):
    m1, m2, _token, job_id = await _primary_with_two_providers(client, "diff")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, uuid.UUID(job_id))
        pair = await verification_service.create_shadow_job(s, primary)
        await s.commit()
        assert pair is not None
        shadow, check = pair
        assert shadow.shadow_of_job_id == primary.id
        assert str(shadow.machine_id) != m1["machine_id"]
        assert str(shadow.machine_id) == m2["machine_id"]
        assert check.status == "pending"

        primary_machine = await s.get(ProviderMachine, primary.machine_id)
        shadow_machine = await s.get(ProviderMachine, shadow.machine_id)
        # Different PROVIDER, not merely a different box.
        assert primary_machine.provider_id != shadow_machine.provider_id


async def test_shadow_job_is_never_billed_to_the_customer(client):
    m1, m2, token, job_id = await _primary_with_two_providers(client, "bill")
    customer_id = await get_user_id("c-bill@x.com")

    sm = get_sessionmaker()
    async with sm() as s:
        account = await ledger.get_or_create_customer_account(s, customer_id)
        balance_before = int(account.balance_minor)
        primary = await s.get(Job, uuid.UUID(job_id))
        shadow, _check = await verification_service.create_shadow_job(s, primary)
        shadow_id = shadow.id
        await s.commit()

    async with sm() as s:
        shadow = await s.get(Job, shadow_id)
        # The guard the result path uses before calling billing.settle_job.
        assert verification_service.is_unbilled_job(shadow) is True
        assert verification_service.is_unbilled_job(await s.get(Job, uuid.UUID(job_id))) is False
        if not verification_service.is_unbilled_job(shadow):  # pragma: no cover
            await billing.settle_job(s, shadow)
        await verification_service.resolve_redundancy(s, shadow, {"text": "Hello"})
        await s.commit()

    async with sm() as s:
        # No hold, no invoice, no ledger movement references the shadow job.
        assert await billing.get_hold_minor(s, shadow_id) == 0
        assert (
            await s.execute(select(Invoice).where(Invoice.job_id == shadow_id))
        ).scalars().first() is None
        txns = (
            await s.execute(
                select(LedgerTransaction).where(
                    LedgerTransaction.reference_id == str(shadow_id)
                )
            )
        ).scalars().all()
        assert txns == []
        account = await ledger.get_or_create_customer_account(s, customer_id)
        assert int(account.balance_minor) == balance_before


async def test_shadow_mismatch_is_recorded_failed_against_the_primary(client):
    m1, m2, _token, job_id = await _primary_with_two_providers(client, "mismatch")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, uuid.UUID(job_id))
        shadow, _check = await verification_service.create_shadow_job(s, primary)
        shadow_id = shadow.id
        await s.commit()

    async with sm() as s:
        shadow = await s.get(Job, shadow_id)
        check = await verification_service.resolve_redundancy(
            s, shadow, {"text": "something completely different"}
        )
        await s.commit()
        assert check.status == "mismatched"
        assert check.matched is False

    async with sm() as s:
        rows = (
            await s.execute(
                select(VerificationResult).where(
                    VerificationResult.job_id == uuid.UUID(job_id),
                    VerificationResult.kind == "redundant",
                )
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0].passed is False
        assert rows[0].detail["shadow_job_id"] == str(shadow_id)


async def test_shadow_match_is_recorded_passed_and_resolution_is_idempotent(client):
    m1, m2, _token, job_id = await _primary_with_two_providers(client, "match")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, uuid.UUID(job_id))
        shadow, _check = await verification_service.create_shadow_job(s, primary)
        shadow_id = shadow.id
        await s.commit()

    async with sm() as s:
        shadow = await s.get(Job, shadow_id)
        check = await verification_service.resolve_redundancy(s, shadow, {"text": "Hello"})
        await s.commit()
        assert check.status == "matched" and check.matched is True
        again = await verification_service.resolve_redundancy(s, shadow, {"text": "NOPE"})
        await s.commit()
        assert again.status == "matched"  # already resolved; not re-judged

    async with sm() as s:
        rows = (
            await s.execute(
                select(VerificationResult).where(
                    VerificationResult.kind == "redundant",
                    VerificationResult.job_id == uuid.UUID(job_id),
                )
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0].passed is True
        checks = (
            await s.execute(
                select(RedundancyCheck).where(
                    RedundancyCheck.primary_job_id == uuid.UUID(job_id)
                )
            )
        ).scalars().all()
        assert len(checks) == 1


async def test_shadow_is_not_created_without_an_independent_provider(client):
    """One provider only -> no honest cross-check is possible, so no shadow."""
    await seed_echo_model()
    m1 = await create_listable_machine(client, "p-solo@x.com", hostname="solo")
    token = await _customer(client, "c-solo@x.com")
    job_id = await _complete_job(client, m1, token, {"text": "Hello"})
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, uuid.UUID(job_id))
        assert await verification_service.create_shadow_job(s, primary) is None
        await s.commit()


# ===========================================================================
# 3. model identity
# ===========================================================================
async def _hardware_job(customer_id: uuid.UUID) -> uuid.UUID:
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(
            customer_id=customer_id,
            workload_type="text-generation",
            model=HARDWARE_MODEL,
            input={"prompt": "hi"},
            execution_mode="hardware",
        )
        s.add(job)
        await s.commit()
        return job.id


async def test_model_hash_mismatch_is_flagged(client):
    await seed_hardware_model()
    await register_verify_login(client, "c-mid@x.com")
    customer_id = await get_user_id("c-mid@x.com")
    job_id = await _hardware_job(customer_id)

    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        result = await verification_service.check_model_identity(s, job, "ff" * 32)
        await s.commit()
        assert result is not None
        assert result.kind == "model_identity"
        assert result.passed is False
        assert result.expected_hash == HARDWARE_DIGEST
        assert result.actual_hash == "ff" * 32


async def test_matching_model_hash_passes_and_absent_hash_records_nothing(client):
    await seed_hardware_model()
    await register_verify_login(client, "c-mid2@x.com")
    customer_id = await get_user_id("c-mid2@x.com")
    job_id = await _hardware_job(customer_id)
    other_job_id = await _hardware_job(customer_id)

    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        # The agent may report the digest with a sha256: prefix.
        result = await verification_service.check_model_identity(
            s, job, f"sha256:{HARDWARE_DIGEST}"
        )
        await s.commit()
        assert result.passed is True

        other = await s.get(Job, other_job_id)
        # Optional field: nothing reported -> no evidence invented.
        assert await verification_service.check_model_identity(s, other, None) is None
        await s.commit()

    async with sm() as s:
        rows = (
            await s.execute(
                select(VerificationResult).where(
                    VerificationResult.job_id == other_job_id
                )
            )
        ).scalars().all()
        assert rows == []


# ===========================================================================
# 4. authorization
# ===========================================================================
async def test_admin_can_create_and_list_challenges_non_admin_gets_403(client, vclient):
    await register_verify_login(client, "v-admin@x.com")
    await grant_role("v-admin@x.com", Role.administrator)
    # Re-login so the fresh token carries the administrator role claim.
    admin_token = (await login(client, "v-admin@x.com"))["access_token"]
    cust_token = await _customer(client, "v-cust@x.com")

    payload = {
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": "2+2?"},
        "expected_output": {"text": "four"},
        "label": "arith",
    }
    denied = await vclient.post(
        "/v1/admin/verification/challenges", json=payload, headers=auth_headers(cust_token)
    )
    assert denied.status_code == 403, denied.text

    created = await vclient.post(
        "/v1/admin/verification/challenges", json=payload, headers=auth_headers(admin_token)
    )
    assert created.status_code == 201, created.text
    assert created.json()["expected_output_hash"] == challenge_service.canonical_hash(
        {"text": "four"}
    )

    listed = await vclient.get(
        "/v1/admin/verification/challenges", headers=auth_headers(admin_token)
    )
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert (
        await vclient.get(
            "/v1/admin/verification/challenges", headers=auth_headers(cust_token)
        )
    ).status_code == 403
    assert (
        await vclient.get(
            "/v1/admin/verification/results", headers=auth_headers(cust_token)
        )
    ).status_code == 403

    sm = get_sessionmaker()
    async with sm() as s:
        assert len(
            (await s.execute(select(VerificationChallenge))).scalars().all()
        ) == 1


async def test_customer_reads_own_job_results_and_non_owner_gets_404(client, vclient):
    await seed_hardware_model()
    owner_token = await _customer(client, "v-owner@x.com")
    other_token = await _customer(client, "v-other@x.com")
    owner_id = await get_user_id("v-owner@x.com")
    job_id = await _hardware_job(owner_id)

    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        await verification_service.check_model_identity(s, job, "ab" * 32)
        await s.commit()

    mine = await vclient.get(
        f"/v1/jobs/{job_id}/verification-results", headers=auth_headers(owner_token)
    )
    assert mine.status_code == 200, mine.text
    assert len(mine.json()) == 1
    assert mine.json()[0]["kind"] == "model_identity"
    assert mine.json()[0]["passed"] is False

    # A non-owner must not be able to confirm the job even exists.
    theirs = await vclient.get(
        f"/v1/jobs/{job_id}/verification-results", headers=auth_headers(other_token)
    )
    assert theirs.status_code == 404, theirs.text

    anon = await vclient.get(f"/v1/jobs/{job_id}/verification-results")
    assert anon.status_code == 401
