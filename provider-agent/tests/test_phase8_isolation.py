"""Phase 8 tests: Workload isolation tiers and remote hardware attestation."""

from __future__ import annotations

import datetime as dt
import pytest

from computecoin_agent.attestation import (
    AttestationReport,
    generate_attestation_report,
    verify_attestation_report,
)
from computecoin_agent.executor import (
    DockerExecutor,
    DockerRunResult,
    IsolationUnavailableError,
    JobSpec,
    SimulatedExecutor,
)
from computecoin_agent.isolation import (
    TIER_CONFIDENTIAL,
    TIER_CONTAINER,
    TIER_GVISOR,
    TIER_MICROVM,
    detect_supported_isolation_tiers,
)


def _dummy_runner(argv: list[str], output_dir: str) -> DockerRunResult:
    return DockerRunResult(returncode=0, stdout="{}", stderr="")


# --------------------------------------------------------------------------- #
# 1. Isolation Tiers Probing
# --------------------------------------------------------------------------- #
def test_detect_supported_isolation_tiers():
    # Baseline: only container
    tiers = detect_supported_isolation_tiers(
        probe_gvisor_fn=lambda: False,
        probe_microvm_fn=lambda: False,
        probe_cc_fn=lambda: False,
    )
    assert tiers == [TIER_CONTAINER]

    # With gVisor
    tiers = detect_supported_isolation_tiers(
        probe_gvisor_fn=lambda: True,
        probe_microvm_fn=lambda: False,
        probe_cc_fn=lambda: False,
    )
    assert tiers == [TIER_CONTAINER, TIER_GVISOR]

    # Full stack
    tiers = detect_supported_isolation_tiers(
        probe_gvisor_fn=lambda: True,
        probe_microvm_fn=lambda: True,
        probe_cc_fn=lambda: True,
    )
    assert tiers == [TIER_CONTAINER, TIER_GVISOR, TIER_MICROVM, TIER_CONFIDENTIAL]


# --------------------------------------------------------------------------- #
# 2. JobSpec Isolation Field
# --------------------------------------------------------------------------- #
def test_job_spec_isolation_tier():
    spec_default = JobSpec.from_claim(
        {"job_id": "j1", "workload_type": "text-generation", "model": "m"}
    )
    assert spec_default.isolation_tier == TIER_CONTAINER

    spec_gvisor = JobSpec.from_claim(
        {
            "job_id": "j2",
            "workload_type": "text-generation",
            "model": "m",
            "isolation_tier": TIER_GVISOR,
        }
    )
    assert spec_gvisor.isolation_tier == TIER_GVISOR


# --------------------------------------------------------------------------- #
# 3. DockerExecutor Argv Construction for Stronger Isolation
# --------------------------------------------------------------------------- #
def test_docker_executor_gvisor_runtime():
    executor = DockerExecutor(
        runner=_dummy_runner,
        docker_available=lambda: True,
        gvisor_available=lambda: True,
    )
    spec = JobSpec(
        job_id="j-gvis",
        workload_type="text-generation",
        model="m",
        input={"prompt": "hi"},
        image_digest="repo@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        execution_mode="hardware",
        isolation_tier=TIER_GVISOR,
    )
    argv = executor.build_argv(spec, "/tmp/out")
    assert "--runtime" in argv
    idx = argv.index("--runtime")
    assert argv[idx + 1] == "runsc"


def test_docker_executor_microvm_runtime():
    executor = DockerExecutor(
        runner=_dummy_runner,
        docker_available=lambda: True,
        microvm_available=lambda: True,
    )
    spec = JobSpec(
        job_id="j-micro",
        workload_type="text-generation",
        model="m",
        input={"prompt": "hi"},
        image_digest="repo@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        execution_mode="hardware",
        isolation_tier=TIER_MICROVM,
    )
    argv = executor.build_argv(spec, "/tmp/out")
    assert "--runtime" in argv
    idx = argv.index("--runtime")
    assert argv[idx + 1] == "kata-qemu"


def test_docker_executor_confidential_runtime():
    executor = DockerExecutor(
        runner=_dummy_runner,
        docker_available=lambda: True,
        confidential_available=lambda: True,
    )
    spec = JobSpec(
        job_id="j-conf",
        workload_type="text-generation",
        model="m",
        input={"prompt": "hi"},
        image_digest="repo@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        execution_mode="hardware",
        isolation_tier=TIER_CONFIDENTIAL,
    )
    argv = executor.build_argv(spec, "/tmp/out")
    assert "--runtime" in argv
    idx = argv.index("--runtime")
    assert argv[idx + 1] == "kata-qemu-sev"


# --------------------------------------------------------------------------- #
# 4. Strict Rejection — No Silent Downgrade
# --------------------------------------------------------------------------- #
def test_docker_executor_refuses_silent_downgrade():
    executor = DockerExecutor(
        runner=_dummy_runner,
        docker_available=lambda: True,
        gvisor_available=lambda: False,  # gVisor missing on host
    )
    spec = JobSpec(
        job_id="j-fail",
        workload_type="text-generation",
        model="m",
        input={"prompt": "hi"},
        image_digest="repo@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        execution_mode="hardware",
        isolation_tier=TIER_GVISOR,
    )
    with pytest.raises(IsolationUnavailableError) as exc_info:
        executor.build_argv(spec, "/tmp/out")
    assert "gVisor isolation requested but runsc runtime is not available" in str(
        exc_info.value
    )


# --------------------------------------------------------------------------- #
# 5. Simulated Executor Isolation Metadata
# --------------------------------------------------------------------------- #
def test_simulated_executor_records_isolation_tier():
    executor = SimulatedExecutor()
    spec = JobSpec(
        job_id="j-sim",
        workload_type="text-generation",
        model="test-model",
        input={"prompt": "hello compute"},
        execution_mode="simulated",
        isolation_tier=TIER_MICROVM,
    )
    res = executor.run(spec)
    assert res.status == "success"
    assert res.execution_mode == "simulated"
    assert res.output["isolation_tier"] == TIER_MICROVM


# --------------------------------------------------------------------------- #
# 6. Remote Attestation Generation & Verification
# --------------------------------------------------------------------------- #
def test_attestation_report_lifecycle():
    key = "dev-machine-attestation-secret"
    nonce = "challenge-nonce-xyz123"
    report = generate_attestation_report(
        machine_id="m-100",
        signing_key=key,
        nonce=nonce,
        isolation_tier=TIER_CONFIDENTIAL,
    )

    # Valid verification
    ok, err = verify_attestation_report(report, signing_key=key, expected_nonce=nonce)
    assert ok is True
    assert err is None


def test_attestation_replay_rejected():
    key = "dev-machine-attestation-secret"
    report = generate_attestation_report(
        machine_id="m-100",
        signing_key=key,
        nonce="old-nonce",
    )
    ok, err = verify_attestation_report(
        report, signing_key=key, expected_nonce="new-challenge-nonce"
    )
    assert ok is False
    assert err == "nonce_mismatch"


def test_attestation_tampered_signature_rejected():
    key = "dev-machine-attestation-secret"
    nonce = "challenge-nonce-xyz123"
    report = generate_attestation_report(
        machine_id="m-100",
        signing_key=key,
        nonce=nonce,
    )
    # Tamper with signature
    report.signature = "tampered" + report.signature[8:]
    ok, err = verify_attestation_report(report, signing_key=key, expected_nonce=nonce)
    assert ok is False
    assert err == "signature_verification_failed"


def test_attestation_tampered_measurements_rejected():
    key = "dev-machine-attestation-secret"
    nonce = "challenge-nonce-xyz123"
    report = generate_attestation_report(
        machine_id="m-100",
        signing_key=key,
        nonce=nonce,
    )
    # Tamper with kernel measurement
    report.pcr_measurements["pcr2"] = "0000000000000000000000000000000000000000"
    ok, err = verify_attestation_report(report, signing_key=key, expected_nonce=nonce)
    assert ok is False
    assert err == "platform_quote_tampered"


def test_attestation_expired_report_rejected():
    key = "dev-machine-attestation-secret"
    nonce = "challenge-nonce-xyz123"
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
    report = generate_attestation_report(
        machine_id="m-100",
        signing_key=key,
        nonce=nonce,
        timestamp=past,
    )
    ok, err = verify_attestation_report(
        report, signing_key=key, expected_nonce=nonce, max_age_seconds=60
    )
    assert ok is False
    assert err == "attestation_expired"
