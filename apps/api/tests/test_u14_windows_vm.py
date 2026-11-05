"""U14 Windows VM tier: spec fields and scheduler gate. Written without being run in the authoring session."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.job_spec_v2 import JobSpecV2
from app.services import scheduler_v2

VM = {"image": "dataset:abc", "image_sha256": "a" * 64, "licence_ack": True}


def _spec(**kw):
    return {"version": 2, "kit": "win-render@1.0.0", **kw}


def test_os_and_vm_only_with_mode_vm():
    with pytest.raises(ValidationError):
        JobSpecV2.model_validate(_spec(os="windows"))
    with pytest.raises(ValidationError):
        JobSpecV2.model_validate(_spec(vm=VM))
    with pytest.raises(ValidationError):
        JobSpecV2.model_validate(_spec(mode="vm"))
    with pytest.raises(ValidationError):
        JobSpecV2.model_validate(_spec(mode="vm", os="linux", vm=VM))
    assert JobSpecV2.model_validate(_spec(mode="vm", os="windows", vm=VM)).vm.vcpus == 4


def test_licence_ack_required_true():
    with pytest.raises(ValidationError):
        JobSpecV2.model_validate(_spec(mode="vm", os="windows", vm={**VM, "licence_ack": False}))


def _job(spec):
    return SimpleNamespace(spec_v2={"spec": spec}, gpu_count=1)


def _machine(vfio):
    return SimpleNamespace(hardware=SimpleNamespace(raw={"vfio_gpu": vfio}))


def test_scheduler_gate(monkeypatch):
    job = _job({"mode": "vm", "os": "windows", "vm": {"gpu_passthrough": True}})
    monkeypatch.setattr(settings, "WINDOWS_VM_ENABLED", False)
    assert scheduler_v2.windows_vm_reason(job, _machine(True)) == "windows_vm_disabled"
    monkeypatch.setattr(settings, "WINDOWS_VM_ENABLED", True)
    assert scheduler_v2.windows_vm_reason(job, _machine(False)) == "vfio_gpu_required"
    assert scheduler_v2.windows_vm_reason(job, _machine("yes")) == "vfio_gpu_required"
    assert scheduler_v2.windows_vm_reason(job, _machine(True)) is None


def test_batch_job_unaffected(monkeypatch):
    monkeypatch.setattr(settings, "WINDOWS_VM_ENABLED", False)
    assert scheduler_v2.windows_vm_reason(_job({"mode": "batch"}), _machine(False)) is None
