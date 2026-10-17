"""Workstream B: Hardware GPU execution tests for provider agent.

Tests the hardened container execution contract, input/output serialization,
artifact handling, metrics collection, and gating for real GPU hardware.

GATING:
* Real container hardware tests are strictly gated behind RUN_GPU_TESTS=1
  AND the availability of Docker + NVIDIA GPU toolkit (nvidia-smi).
* CPU-only CI hosts always pass without failure or skipped errors.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any
import pytest

from computecoin_agent.executor import (
    Artifact,
    DockerExecutor,
    DockerRunResult,
    ExecutionResult,
    HardwareUnavailableError,
    JobSpec,
    select_executor,
    validate_output,
)
from computecoin_agent.jobrunner import build_result_body, run_one

PINNED_TINYLLAMA_DIGEST = (
    "ghcr.io/computecoin/model-runners/tinyllama-v1@sha256:"
    "4a382c2b3e83b429d2f2d93e2b243888365671158d3493864a66a1523497c654"
)


def is_real_gpu_available() -> bool:
    """Check if the environment can run real GPU container tests."""
    if os.environ.get("RUN_GPU_TESTS") != "1":
        return False
    return (
        shutil.which("docker") is not None
        and (shutil.which("nvidia-smi") is not None or shutil.which("nvidia-container-runtime") is not None)
    )


def make_hw_job_spec(
    job_id: str = "job-hw-001",
    prompt: str = "Explain quantum computing in one sentence.",
    gpu_count: int = 1,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        workload_type="text-generation",
        model="tinyllama-1.1b-chat-v1",
        input={"prompt": prompt, "max_tokens": 64},
        execution_mode="hardware",
        image_digest=PINNED_TINYLLAMA_DIGEST,
        limits={
            "memory_mb": 4096,
            "cpus": "2.0",
            "gpu_count": gpu_count,
            "max_runtime_seconds": 120,
            "pids_limit": 128,
        },
        isolation_tier="container",
    )


# --------------------------------------------------------------------------- #
# Unit / Mock Hardware Execution Tests (Green in all environments / CI)
# --------------------------------------------------------------------------- #
def test_docker_executor_materializes_input_json_for_container(tmp_path):
    """The executor must write input.json to output_dir with proper permissions
    and pass environment variables pointing to COMPUTECOIN_INPUT and COMPUTECOIN_OUTPUT."""
    out_dir = tmp_path / "job-run"
    out_dir.mkdir()

    captured_argv = []
    captured_input_content = None

    def mock_runner(argv: list[str], output_directory: str) -> DockerRunResult:
        nonlocal captured_input_content
        captured_argv.extend(argv)
        # Read the input.json written by DockerExecutor before container run
        input_file = out_dir / "input.json"
        assert input_file.exists(), "input.json was not created in output directory"
        captured_input_content = json.loads(input_file.read_text(encoding="utf-8"))

        # Produce mock output.json as if model container finished
        output_data = {
            "text": "Quantum computing harnesses superposition and entanglement for rapid computation.",
            "model": "tinyllama-1.1b-chat-v1",
            "execution_mode": "hardware",
            "simulated": False,
            "tokens": 12,
        }
        (out_dir / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        return DockerRunResult(returncode=0, stdout="Inference complete in 1.45s\n", stderr="")

    executor = DockerExecutor(
        runner=mock_runner,
        docker_available=lambda: True,
        nvidia_available=lambda: True,
        output_root=str(out_dir),
    )

    spec = make_hw_job_spec(prompt="What is quantum computing?")
    res = executor.run(spec)

    # 1. Verification of execution result
    assert res.status == "success"
    assert res.execution_mode == "hardware"
    assert res.output_schema_valid is True
    assert "superposition" in res.output["text"]
    assert res.output["simulated"] is False
    assert res.metrics["duration_seconds"] >= 0.0

    # 2. Input was serialized faithfully
    assert captured_input_content == {"prompt": "What is quantum computing?", "max_tokens": 64}

    # 3. Environment variables in argv
    assert "-e" in captured_argv
    assert f"COMPUTECOIN_INPUT=/out/input.json" in captured_argv
    assert f"COMPUTECOIN_OUTPUT=/out/output.json" in captured_argv
    assert f"COMPUTECOIN_JOB_ID={spec.job_id}" in captured_argv
    assert f"COMPUTECOIN_MODEL={spec.model}" in captured_argv
    assert f"COMPUTECOIN_WORKLOAD_TYPE={spec.workload_type}" in captured_argv

    # 4. Artifact created
    assert len(res.artifacts) == 1
    art = res.artifacts[0]
    assert art.kind == "output"
    assert art.content_type == "application/json"
    assert json.loads(art.data.decode("utf-8")) == res.output


def test_docker_executor_handles_container_failure(tmp_path):
    """Non-zero exit code produces status='failed' with container_nonzero_exit."""
    out_dir = tmp_path / "job-fail"
    out_dir.mkdir()

    def failing_runner(argv: list[str], output_directory: str) -> DockerRunResult:
        return DockerRunResult(returncode=137, stdout="", stderr="Killed: out of memory (OOM)\n")

    executor = DockerExecutor(
        runner=failing_runner,
        docker_available=lambda: True,
        nvidia_available=lambda: True,
        output_root=str(out_dir),
    )

    res = executor.run(make_hw_job_spec())
    assert res.status == "failed"
    assert res.execution_mode == "hardware"
    assert res.error["code"] == "container_nonzero_exit"
    assert "137" in res.error["message"]


def test_docker_executor_handles_missing_output(tmp_path):
    """Exit code 0 but no output.json produces status='failed' with missing_output."""
    out_dir = tmp_path / "job-no-out"
    out_dir.mkdir()

    def empty_runner(argv: list[str], output_directory: str) -> DockerRunResult:
        return DockerRunResult(returncode=0, stdout="Finished without output\n", stderr="")

    executor = DockerExecutor(
        runner=empty_runner,
        docker_available=lambda: True,
        nvidia_available=lambda: True,
        output_root=str(out_dir),
    )

    res = executor.run(make_hw_job_spec())
    assert res.status == "failed"
    assert res.error["code"] == "missing_output"


def test_docker_executor_refuses_silent_downgrade():
    """Hardware job on host without Docker or without NVIDIA toolkit must raise
    HardwareUnavailableError and NEVER simulate."""
    # 1. No docker
    ex_no_docker = DockerExecutor(docker_available=lambda: False)
    with pytest.raises(HardwareUnavailableError) as exc_info:
        ex_no_docker.run(make_hw_job_spec())
    assert "docker is not installed" in str(exc_info.value)

    # 2. No nvidia toolkit for GPU job
    ex_no_nvidia = DockerExecutor(
        docker_available=lambda: True,
        nvidia_available=lambda: False,
    )
    with pytest.raises(HardwareUnavailableError) as exc_info:
        ex_no_nvidia.build_argv(make_hw_job_spec(gpu_count=1), "/tmp/out")
    assert "GPU job requires the NVIDIA container toolkit" in str(exc_info.value)


def test_select_executor_routes_hardware_job_to_docker():
    """Presence of pinned image_digest must route to DockerExecutor."""
    spec = make_hw_job_spec()
    executor = select_executor(spec)
    assert isinstance(executor, DockerExecutor)


def test_hardware_result_body_assembly():
    """Result body assembly preserves execution_mode='hardware' and actual metrics."""
    res = ExecutionResult(
        status="success",
        output={"text": "hello from GPU", "model": "tinyllama-1.1b-chat-v1"},
        execution_mode="hardware",
        output_schema_valid=True,
        metrics={
            "duration_seconds": 2.345,
            "max_memory_mb": 1850,
            "gpu_util_pct": 74.5,
        },
    )
    artifacts = [
        {"artifact_id": "art-1", "kind": "output", "sha256": "abc", "size": 42, "content_type": "application/json"}
    ]
    body = build_result_body(res, artifacts)
    assert body["status"] == "success"
    assert body["execution_mode"] == "hardware"
    assert body["metrics"]["duration_seconds"] == 2.345
    assert body["metrics"]["gpu_util_pct"] == 74.5
    assert body["metrics"]["max_memory_mb"] == 1850
    assert body["evidence"]["output_schema_valid"] is True


# --------------------------------------------------------------------------- #
# Real Hardware Integration Test (Gated behind RUN_GPU_TESTS=1 + GPU Host)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not is_real_gpu_available(),
    reason="Real GPU integration test requires RUN_GPU_TESTS=1 and a host with Docker + NVIDIA GPU",
)
def test_real_gpu_container_execution(tmp_path):
    """End-to-end execution on real GPU hardware using the live Docker daemon."""
    out_dir = tmp_path / "real-gpu-job"
    out_dir.mkdir()

    executor = DockerExecutor(output_root=str(out_dir))
    spec = make_hw_job_spec(prompt="ComputeCoin hardware verification prompt.")

    res = executor.run(spec)
    assert res.status == "success"
    assert res.execution_mode == "hardware"
    assert res.output_schema_valid is True
    assert isinstance(res.output.get("text"), str)
    assert len(res.output["text"]) > 0
    assert res.metrics["duration_seconds"] > 0
    assert len(res.artifacts) >= 1
