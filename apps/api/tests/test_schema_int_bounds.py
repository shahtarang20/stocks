"""Agent-supplied integers that land in 32-bit Integer columns must be bounded (else Postgres raises -> HTTP 500)."""
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.observability import JobProgressItem
from app.schemas.telemetry import JobTelemetryItem, MachineTelemetryItem


@pytest.mark.parametrize("field", ["mem_used_mb", "mem_total_mb", "gpu_index"])
def test_machine_telemetry_int_overflow_rejected(field):
    with pytest.raises(ValidationError):
        MachineTelemetryItem(**{field: 2**40})


@pytest.mark.parametrize("field", ["mem_used_mb", "step_count"])
def test_job_telemetry_int_overflow_rejected(field):
    with pytest.raises(ValidationError):
        JobTelemetryItem(job_id=uuid.uuid4(), **{field: 2**40})


def test_progress_step_count_overflow_rejected_and_max_accepted():
    with pytest.raises(ValidationError):
        JobProgressItem(job_id=uuid.uuid4(), progress_pct=1.0, step_count=2**40)
    assert JobProgressItem(job_id=uuid.uuid4(), progress_pct=1.0, step_count=2**31 - 1).step_count == 2**31 - 1


def test_provider_capability_int_overflow_rejected():
    from app.schemas.provider import CapabilityReport, GpuReport, HeartbeatCapabilitySummary, MachineUpdate

    for bad in (
        lambda: CapabilityReport(ram_mb=2**40),
        lambda: CapabilityReport(cpu_cores=2**40),
        lambda: CapabilityReport(disk_gb=2**40),
        lambda: GpuReport(vram_mb=2**40),
        lambda: HeartbeatCapabilitySummary(gpu_count=2**40),
        lambda: HeartbeatCapabilitySummary(vram_mb=2**40),
        lambda: MachineUpdate(max_concurrent_jobs=2**40),
    ):
        with pytest.raises(ValidationError):
            bad()
    assert CapabilityReport(ram_mb=262144, disk_gb=4000, cpu_cores=128).ram_mb == 262144


def test_enroll_string_fields_match_column_widths():
    from app.schemas.provider import AgentEnrollRequest, CapabilityReport, GpuReport

    for bad in (
        lambda: GpuReport(cuda_version="9" * 65),
        lambda: GpuReport(gpu_vendor="x" * 33),
        lambda: GpuReport(gpu_runtime_version="1" * 65),
        lambda: GpuReport(model="m" * 256),
        lambda: CapabilityReport(cpu_model="c" * 256),
        lambda: AgentEnrollRequest(enrollment_token="t", hostname="h", agent_version="v" * 65, capability_report={}),
    ):
        with pytest.raises(ValidationError):
            bad()
