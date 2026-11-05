"""U8 BYOC ROUTES through the ASGI client (written, NOT run): validate, submit gate, denylist admin, claim-time recheck."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.byoc import ByocImageDenylist, ByocImageScan
from app.models.enums import Role
from app.models.jobs import Job
from app.services import byoc as byoc_svc
from app.services import image_scan
from tests.helpers import auth_headers, grant_role, login, register_verify_login

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "a" * 64
IMG = "ghcr.io/acme/tool@" + DIGEST


def _spec(**kw):
    s = {"version": 2, "mode": "byoc", "image": IMG, "command": ["python", "x.py"]}
    s.update(kw)
    return s


class _FakeScanner(image_scan.ImageScanner):
    name = "fake"

    def __init__(self, status=image_scan.CLEAN):
        self.status, self.calls = status, 0

    async def scan(self, image_ref):
        self.calls += 1
        findings = [{"code": "miner_package"}] if self.status == image_scan.DENIED else []
        return image_scan.ScanResult(status=self.status, scanner=self.name, findings=findings)


@pytest.fixture(autouse=True)
def _byoc_env(monkeypatch):
    monkeypatch.setattr(settings, "BYOC_REGISTRY_ALLOWLIST", "ghcr.io/acme", raising=False)
    image_scan.set_scanner(None)
    yield
    image_scan.set_scanner(None)


async def _user(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def _admin(client, email="byoc-admin@example.com"):
    await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return auth_headers((await login(client, email))["access_token"])


async def test_validate_ok_and_per_field_errors(client):
    h = await _user(client, "byoc-val@example.com")
    r = await client.post("/v1/jobs/byoc/validate", headers=h, json={"spec": _spec()})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["image_digest"] == DIGEST and r.json()["isolation"] == "gvisor"
    assert r.json()["effective_resources"]["gpu_count"] == 1

    cases = [
        (_spec(image="ghcr.io/acme/tool:latest"), "image"),
        (_spec(image="evil.io/x/y@" + DIGEST), "image"),
        (_spec(env={"COMPUTECOIN_X": "1"}), "env.COMPUTECOIN_X"),
        (_spec(network={"egress": ["a.example:443"]}), "network.egress"),
        (_spec(privileged=True), "privileged"),
        (_spec(resources={"gpu": {"count": 8}}), "resources.gpu.count"),
        (_spec(max_runtime_seconds=settings.BYOC_MAX_RUNTIME_SECONDS + 1), "max_runtime_seconds"),
    ]
    for spec, field in cases:
        r = await client.post("/v1/jobs/byoc/validate", headers=h, json={"spec": spec})
        assert r.status_code == 422, (spec, r.text)
        assert field in {e["field"] for e in r.json()["detail"]["errors"]}, (field, r.json())
    assert (await client.post("/v1/jobs/byoc/validate", json={"spec": _spec()})).status_code in (401, 403)


async def test_validate_refused_when_allowlist_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "BYOC_REGISTRY_ALLOWLIST", "", raising=False)
    h = await _user(client, "byoc-empty@example.com")
    r = await client.post("/v1/jobs/byoc/validate", headers=h, json={"spec": _spec()})
    assert r.status_code == 403
    assert r.json()["detail"]["errors"][0]["code"] == "byoc_registry_allowlist_empty"


async def test_submit_refused_with_unconfigured_scanner_and_no_job_created(client):
    h = await _user(client, "byoc-uncfg@example.com")
    assert image_scan.get_scanner().name == image_scan.UnconfiguredScanner.name
    r = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["errors"][0]["code"] == "scanner_unconfigured"
    async with get_sessionmaker()() as db:
        assert (await db.execute(select(Job).where(Job.workload_type == "byoc"))).first() is None
        assert (await db.execute(select(ByocImageScan))).first() is None  # an unconfigured scan is never cached as clean


async def test_submit_refused_when_scan_denies_and_result_is_cached(client):
    h = await _user(client, "byoc-denied@example.com")
    image_scan.set_scanner(_FakeScanner(image_scan.DENIED))
    r = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert r.status_code == 422
    assert "image_refused_by_scan" in {e["code"] for e in r.json()["detail"]["errors"]}
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ByocImageScan).where(ByocImageScan.digest == DIGEST))).scalar_one()
        assert row.status == image_scan.DENIED
        assert (await db.execute(select(Job).where(Job.workload_type == "byoc"))).first() is None


async def test_scanner_error_fails_closed(client):
    h = await _user(client, "byoc-err@example.com")
    image_scan.set_scanner(_FakeScanner(image_scan.ERROR))
    r = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert r.status_code == 503 and r.json()["detail"]["errors"][0]["code"] == "scan_failed"


async def test_clean_scan_submits_then_denylist_blocks_submit_and_claim_recheck(client):
    h = await _user(client, "byoc-ok@example.com")
    admin = await _admin(client)
    scanner = _FakeScanner(image_scan.CLEAN)
    image_scan.set_scanner(scanner)

    r = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert r.status_code == 201, r.text
    job_id = uuid.UUID(r.json().get("job_id") or r.json()["id"])
    async with get_sessionmaker()() as db:
        job = (await db.execute(select(Job).where(Job.workload_type == "byoc"))).scalar_one()
        assert job.spec_v2["byoc"]["isolation"] == "gvisor" and job.isolation_tier == "container"
        assert await byoc_svc.claim_denied_reason(db, job) is None  # cleared, not denylisted

    # A fresh scan is cached: a second submit does not scan again.
    again = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert again.status_code == 201 and scanner.calls == 1

    # Admin denylists the digest after the job was queued.
    added = await client.post("/v1/admin/byoc/denylist", headers=admin, json={"digest": DIGEST, "reason": "known miner"})
    assert added.status_code == 201 and added.json()["digest"] == DIGEST
    async with get_sessionmaker()() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert await byoc_svc.claim_denied_reason(db, job) == "byoc_image_denied"  # claim-time recheck
    refused = await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})
    assert refused.status_code == 422 and refused.json()["detail"]["errors"][0]["code"] == "image_denied"

    # Removing it restores clearance (the cached clean scan is still fresh).
    assert (await client.delete(f"/v1/admin/byoc/denylist/{DIGEST}", headers=admin)).json() == {"removed": DIGEST}
    async with get_sessionmaker()() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert await byoc_svc.claim_denied_reason(db, job) is None


async def test_claim_recheck_fails_closed_without_clean_scan_or_with_tampered_image(client):
    h = await _user(client, "byoc-recheck@example.com")
    image_scan.set_scanner(_FakeScanner(image_scan.CLEAN))
    assert (await client.post("/v1/jobs/byoc", headers=h, json={"spec": _spec()})).status_code == 201
    async with get_sessionmaker()() as db:
        job = (await db.execute(select(Job).where(Job.workload_type == "byoc"))).scalar_one()
        scan = (await db.execute(select(ByocImageScan).where(ByocImageScan.digest == DIGEST))).scalar_one()
        scan.status = image_scan.DENIED
        await db.flush()
        assert await byoc_svc.claim_denied_reason(db, job) == "byoc_image_not_cleared"
        scan.status = image_scan.CLEAN
        spec = dict(job.spec_v2)
        spec["kit_image"] = "ghcr.io/acme/other@sha256:" + "c" * 64  # image differs from the byoc block
        job.spec_v2 = spec
        await db.flush()
        assert await byoc_svc.claim_denied_reason(db, job) == "byoc_integrity_failed"


async def test_denylist_admin_routes(client):
    admin = await _admin(client, "byoc-admin2@example.com")
    user = await _user(client, "byoc-plain@example.com")
    body = {"digest": DIGEST, "reason": "malware"}
    assert (await client.post("/v1/admin/byoc/denylist", headers=user, json=body)).status_code == 403
    assert (await client.get("/v1/admin/byoc/denylist", headers=user)).status_code == 403
    assert (await client.get("/v1/admin/byoc/status", headers=user)).status_code == 403

    st = await client.get("/v1/admin/byoc/status", headers=admin)
    assert st.status_code == 200 and st.json()["scanner"] == "unconfigured" and st.json()["scanner_configured"] is False
    assert st.json()["registry_allowlist"] == ["ghcr.io/acme"]

    assert (await client.post("/v1/admin/byoc/denylist", headers=admin, json={"digest": "sha256:" + "Z" * 64, "reason": "bad"})).status_code == 422
    assert (await client.post("/v1/admin/byoc/denylist", headers=admin, json={"digest": "short", "reason": "bad"})).status_code == 422
    assert (await client.post("/v1/admin/byoc/denylist", headers=admin, json=body)).status_code == 201
    assert (await client.post("/v1/admin/byoc/denylist", headers=admin, json=body)).status_code == 409
    listed = await client.get("/v1/admin/byoc/denylist", headers=admin)
    assert [x["digest"] for x in listed.json()["items"]] == [DIGEST]
    assert (await client.delete(f"/v1/admin/byoc/denylist/{'sha256:' + 'b' * 64}", headers=admin)).status_code == 404
    assert (await client.delete("/v1/admin/byoc/denylist/not-a-digest", headers=admin)).status_code == 422
    assert (await client.delete(f"/v1/admin/byoc/denylist/{DIGEST}", headers=admin)).status_code == 200
    async with get_sessionmaker()() as db:
        assert (await db.execute(select(ByocImageDenylist))).first() is None


async def test_idempotent_submit_is_rechecked_after_the_scan_commit(client, monkeypatch):
    """The scan result is committed (releasing the per-customer lock) before the job is built. A concurrent request
    with the same Idempotency-Key can create the job in that gap; the second request must return it, not add a
    duplicate (or hit the unique index and 500)."""
    from tests.helpers import get_user_id

    h = await _user(client, "byoc-idem@example.com")
    uid = await get_user_id("byoc-idem@example.com")
    image_scan.set_scanner(_FakeScanner(image_scan.CLEAN))
    real = byoc_svc.ensure_image_cleared
    created: list[uuid.UUID] = []

    async def ensure_then_race(db, image, digest):
        await real(db, image, digest)
        async with get_sessionmaker()() as other:  # the concurrent request wins the race in the gap
            job = Job(customer_id=uid, workload_type="byoc", model="byoc", input={}, status="queued", gpu_count=1,
                      estimated_minutes=1, max_runtime_seconds=60, retry_policy={}, execution_mode="hardware",
                      idempotency_key="idem-byoc-1", spec_v2={"byoc": {}})
            other.add(job)
            await other.commit()
            created.append(job.id)

    monkeypatch.setattr(byoc_svc, "ensure_image_cleared", ensure_then_race)
    r = await client.post("/v1/jobs/byoc", headers={**h, "Idempotency-Key": "idem-byoc-1"}, json={"spec": _spec()})
    assert r.status_code == 201, r.text
    assert uuid.UUID(r.json().get("job_id") or r.json()["id"]) == created[0]
    async with get_sessionmaker()() as db:
        assert len((await db.execute(select(Job).where(Job.idempotency_key == "idem-byoc-1"))).scalars().all()) == 1
