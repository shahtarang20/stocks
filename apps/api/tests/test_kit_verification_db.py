"""U6/U7 Kit verification against the database (written, NOT run).

``maybe_schedule_kit_reference`` samples a completed, billed Kit job (rate 1.0 creates an unbilled reference run, 0.0
does not). ``sweep_kit_verifications`` judges finished pairs; the data-plane download is replaced by fake store files,
so only the sweep, the comparison plug-in (``exact``) and the evidence rows are exercised.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.data_plane import DataFile, DataObject
from app.models.jobs import Job
from app.models.kits import Kit
from app.models.verification import RedundancyCheck, VerificationResult
from app.services import job_fsm as fsm
from app.services import kit_verification_run as kvr
from app.services import verification as verification_service
from tests.test_cpu_only_kits_and_kit_shadow import _kit_primary

pytestmark = pytest.mark.asyncio

SLUG, VERSION = "blender-render", "4.2.0"


async def _ensure_kit(manifest: dict | None = None) -> None:
    async with get_sessionmaker()() as db:
        if (await db.execute(select(Kit).where(Kit.slug == SLUG, Kit.version == VERSION))).scalar_one_or_none() is None:
            db.add(Kit(slug=SLUG, version=VERSION, name="Blender", category="3d", image_digest="r/x@sha256:" + "a" * 64,
                       manifest=manifest if manifest is not None else {"verification": {"method": "exact"}},
                       manifest_sha256="0" * 64, signature="0" * 128, signing_key_id="test", status="published"))
            await db.commit()


# ---- maybe_schedule_kit_reference -------------------------------------------------------------

async def test_rate_one_creates_an_unbilled_reference_run_and_check(client, monkeypatch):
    _m1, m2, pid, _cid = await _kit_primary(client, "kvs1")
    await _ensure_kit()
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)
    sm = get_sessionmaker()
    async with sm() as s:
        pair = await kvr.maybe_schedule_kit_reference(s, await s.get(Job, pid))
        assert pair is not None
        shadow, check = pair
        await s.commit()
        assert shadow.shadow_of_job_id == pid and verification_service.is_unbilled_job(shadow)
        assert str(shadow.machine_id) == m2["machine_id"] and shadow.status == fsm.RESERVED
        assert check.status == "pending" and check.primary_job_id == pid and check.shadow_job_id == shadow.id
    async with sm() as s:
        assert len((await s.execute(select(RedundancyCheck).where(RedundancyCheck.primary_job_id == pid))).scalars().all()) == 1


async def test_rate_zero_creates_nothing(client, monkeypatch):
    _m1, _m2, pid, _cid = await _kit_primary(client, "kvs0")
    await _ensure_kit()
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 0.0)
    sm = get_sessionmaker()
    async with sm() as s:
        assert await kvr.maybe_schedule_kit_reference(s, await s.get(Job, pid)) is None
        await s.commit()
    async with sm() as s:
        assert (await s.execute(select(RedundancyCheck))).first() is None
        assert (await s.execute(select(Job).where(Job.shadow_of_job_id.is_not(None)))).first() is None


async def test_manifest_can_only_lower_the_rate(client, monkeypatch):
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)
    assert kvr.sample_rate({"verification": {"method": "exact", "sample_fraction": 0}}) == 0.0
    assert kvr.sample_rate({"verification": {"method": "exact", "sample_fraction": 0.5}}) == 0.5
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 0.1)
    assert kvr.sample_rate({"verification": {"method": "exact", "sample_fraction": 1}}) == 0.1  # cannot raise it
    assert kvr.sample_rate({"verification": {"sample_fraction": True}}) == 0.1  # a bool is not a fraction


async def test_no_verification_section_or_unknown_kit_is_not_sampled(client, monkeypatch):
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)
    _m1, _m2, pid, _cid = await _kit_primary(client, "kvs2")
    sm = get_sessionmaker()
    async with sm() as s:  # the Kit row does not exist yet
        assert await kvr.maybe_schedule_kit_reference(s, await s.get(Job, pid)) is None
    await _ensure_kit({"outputs": []})  # a Kit with no verification section
    async with sm() as s:
        assert await kvr.maybe_schedule_kit_reference(s, await s.get(Job, pid)) is None
        assert (await s.execute(select(RedundancyCheck))).first() is None


# ---- sweep_kit_verifications ------------------------------------------------------------------

CONTENT: dict[str, bytes] = {}


def _fake_fetch(monkeypatch, *, fail=False):
    """Write the fake store bytes (keyed by storage key) into the temp dirs the sweep hands over."""
    async def fetch(db, files, dest: Path):
        if fail:
            return None
        out = []
        for f, key in files:
            target = dest / Path(f.name).name
            target.write_bytes(CONTENT[key])
            out.append(target)
        return out

    monkeypatch.setattr(kvr, "_fetch_all", fetch)


async def _add_output(owner_id, job_id, data: bytes, name="frame.png") -> None:
    key = f"store/{uuid.uuid4()}"
    CONTENT[key] = data
    async with get_sessionmaker()() as db:
        obj = DataObject(scope_key=str(owner_id), sha256=uuid.uuid4().hex + uuid.uuid4().hex, size_bytes=len(data), storage_key=key, refcount=1)
        db.add(obj)
        await db.flush()
        db.add(DataFile(owner_id=owner_id, object_id=obj.id, name=f"out/{job_id}/{name}", size_bytes=len(data),
                        sha256=obj.sha256, content_type="image/png"))
        await db.commit()


async def _pair(client, tag, monkeypatch, *, shadow_status=fsm.COMPLETED):
    """A primary with a reference run created by the real sampler, the shadow put into ``shadow_status``."""
    _m1, _m2, pid, cid = await _kit_primary(client, tag)
    await _ensure_kit()
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)
    sm = get_sessionmaker()
    async with sm() as s:
        shadow, check = await kvr.maybe_schedule_kit_reference(s, await s.get(Job, pid))
        shadow.status = shadow_status  # the agent's result path is not under test here
        await s.commit()
        return pid, shadow.id, check.id, cid


async def _check(cid: uuid.UUID) -> RedundancyCheck:
    async with get_sessionmaker()() as db:
        return await db.get(RedundancyCheck, cid)


async def test_sweep_records_matched_with_evidence_for_identical_outputs(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvm", monkeypatch)
    await _add_output(cid, pid, b"same bytes")
    await _add_output(cid, sid, b"same bytes")
    _fake_fetch(monkeypatch)
    counts = await kvr.sweep_kit_verifications(get_sessionmaker())
    assert counts["checked"] >= 1 and counts["resolved"] >= 1 and counts["errors"] == 0
    check = await _check(chk)
    assert check.status == "matched" and check.matched is True and check.resolved_at is not None
    assert check.detail["kit_verification_status"] == "passed"
    async with get_sessionmaker()() as db:
        res = (await db.execute(select(VerificationResult).where(VerificationResult.job_id == pid, VerificationResult.kind == "kit_output"))).scalar_one()
        assert res.passed is True and res.detail["method"] == "exact" and res.detail["reference_job_id"] == str(sid)
    # A second sweep finds nothing pending: resolved checks are not judged again (no duplicate evidence).
    again = await kvr.sweep_kit_verifications(get_sessionmaker())
    assert again["checked"] == 0
    async with get_sessionmaker()() as db:
        assert len((await db.execute(select(VerificationResult).where(VerificationResult.job_id == pid))).scalars().all()) == 1


async def test_sweep_records_mismatched_for_different_outputs(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvx", monkeypatch)
    await _add_output(cid, pid, b"provider answer")
    await _add_output(cid, sid, b"reference answer")
    _fake_fetch(monkeypatch)
    await kvr.sweep_kit_verifications(get_sessionmaker())
    check = await _check(chk)
    assert check.status == "mismatched" and check.matched is False
    async with get_sessionmaker()() as db:
        res = (await db.execute(select(VerificationResult).where(VerificationResult.job_id == pid))).scalar_one()
        assert res.passed is False and res.detail["status"] == "failed"


async def test_inconclusive_when_reference_output_is_missing(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvi", monkeypatch)
    await _add_output(cid, pid, b"only the primary has files")
    _fake_fetch(monkeypatch)
    await kvr.sweep_kit_verifications(get_sessionmaker())
    check = await _check(chk)
    assert check.status == "inconclusive" and check.matched is None
    assert check.detail["reason"] == "output_files_missing"
    async with get_sessionmaker()() as db:  # absence of evidence records no verdict against the provider
        assert (await db.execute(select(VerificationResult).where(VerificationResult.job_id == pid))).first() is None


async def test_inconclusive_when_reference_run_did_not_complete(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvf", monkeypatch, shadow_status=fsm.FAILED)
    await _add_output(cid, pid, b"x")
    _fake_fetch(monkeypatch)
    await kvr.sweep_kit_verifications(get_sessionmaker())
    check = await _check(chk)
    assert check.status == "inconclusive" and check.detail["reason"] == "reference_run_not_completed"


async def test_pending_while_the_reference_run_is_still_running(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvp", monkeypatch, shadow_status=fsm.RUNNING)
    _fake_fetch(monkeypatch)
    counts = await kvr.sweep_kit_verifications(get_sessionmaker())
    assert counts["resolved"] == 0
    assert (await _check(chk)).status == "pending"


async def test_inconclusive_when_download_or_hash_check_fails(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvd", monkeypatch)
    await _add_output(cid, pid, b"a")
    await _add_output(cid, sid, b"a")
    _fake_fetch(monkeypatch, fail=True)
    await kvr.sweep_kit_verifications(get_sessionmaker())
    check = await _check(chk)
    assert check.status == "inconclusive" and check.detail["reason"] == "output_download_or_hash_failed"


async def test_store_outage_keeps_the_check_pending_for_a_retry(client, monkeypatch):
    pid, sid, chk, cid = await _pair(client, "kvo", monkeypatch)
    await _add_output(cid, pid, b"a")
    await _add_output(cid, sid, b"a")

    async def boom(db, files, dest):
        raise RuntimeError("store down")

    monkeypatch.setattr(kvr, "_fetch_all", boom)
    counts = await kvr.sweep_kit_verifications(get_sessionmaker())
    assert counts["resolved"] == 0 and counts["errors"] == 0
    assert (await _check(chk)).status == "pending"
