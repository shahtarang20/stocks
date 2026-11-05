"""A service replica (Kit job with no declared outputs) completes and settles without verification sampling (written, NOT run)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import kit_verification_run as kvr
from app.services import verification as v


def _job(service: bool = True, **over):
    plan = {"kit": {"slug": "llm-serve"}, "outputs": []}
    if service:
        plan["service"] = {"service_id": "s", "replica_id": "r", "port": 8000}
    base = dict(workload_type="kit", is_challenge=False, shadow_of_job_id=None, data_policy={"v2_plan": plan},
                kit_id="llm-serve", kit_version="1.0.0")
    base.update(over)
    return SimpleNamespace(**base)


def test_kit_result_without_files_is_valid():
    res = v.verify_output("kit", {}, {"service": True, "requests_served": 3})
    assert res["valid"] is True and res["reason"] is None
    assert v.verify_output("kit", {}, {})["valid"] is True
    assert v.verify_output("kit", {}, None)["valid"] is False  # still must be an object


def test_unknown_workload_still_refused():
    assert v.verify_output("nope", {}, {})["reason"] == "unknown_workload"


def test_replica_detection():
    assert v.is_service_replica_job(_job()) is True
    assert v.is_service_replica_job(_job(service=False)) is False
    assert v.is_service_replica_job(_job(data_policy=None)) is False


@pytest.mark.asyncio
async def test_replica_is_never_shadow_sampled(monkeypatch):
    monkeypatch.setattr(v, "should_shadow", lambda *a, **k: True)
    assert await v.maybe_create_shadow_job(None, _job()) is None
    assert await v.create_shadow_job(None, _job()) is None


@pytest.mark.asyncio
async def test_replica_is_never_kit_reference_scheduled(monkeypatch):
    monkeypatch.setattr(v, "should_shadow", lambda *a, **k: True)
    assert await kvr.maybe_schedule_kit_reference(None, _job()) is None


@pytest.mark.asyncio
async def test_kit_without_verification_section_is_ignored(monkeypatch):
    async def manifest(db, job):
        return {"slug": "x"}  # no verification section

    monkeypatch.setattr(kvr, "_manifest_of", manifest)
    monkeypatch.setattr(v, "should_shadow", lambda *a, **k: True)
    assert await kvr.maybe_schedule_kit_reference(None, _job(service=False)) is None
