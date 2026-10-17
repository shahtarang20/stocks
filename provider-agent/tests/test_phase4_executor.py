"""Phase 4 executor tests: deterministic simulation + hardened Docker argv.

No real Docker is ever invoked -- the container runner is injected.
"""

import json

import pytest

from computecoin_agent.executor import (
    DockerExecutor,
    HardwareUnavailableError,
    DockerRunResult,
    JobSpec,
    SimulatedExecutor,
    select_executor,
    validate_output,
)


# --------------------------------------------------------------------------- #
# SimulatedExecutor
# --------------------------------------------------------------------------- #
def _textgen_spec(prompt="Hello world", job_id="job-tg"):
    return JobSpec(
        job_id=job_id,
        workload_type="text-generation",
        model="echo-sim-v1",
        input={"prompt": prompt, "max_tokens": 64},
        execution_mode="simulated",
        image_digest=None,
    )


def test_simulated_textgen_is_deterministic_and_stamped():
    spec = _textgen_spec()
    r1 = SimulatedExecutor().run(spec)
    r2 = SimulatedExecutor().run(spec)

    assert r1.status == "success"
    assert r1.execution_mode == "simulated"
    # Deterministic: identical output for identical input.
    assert r1.output == r2.output

    out = r1.output
    assert isinstance(out["text"], str)             # schema {text: str}
    assert out["model"] == "echo-sim-v1"
    assert out["execution_mode"] == "simulated"     # honesty stamp
    assert out["simulated"] is True
    assert isinstance(out["tokens"], int)
    # Never dressed up as real inference.
    assert "[simulated]" in out["text"]
    # Server-side schema check agrees.
    assert validate_output("text-generation", out) is True
    assert r1.output_schema_valid is True


def test_simulated_textgen_emits_output_artifact_matching_output():
    r = SimulatedExecutor().run(_textgen_spec())
    assert len(r.artifacts) == 1
    art = r.artifacts[0]
    assert art.kind == "output"
    assert art.content_type == "application/json"
    # Artifact bytes decode to the same output object.
    assert json.loads(art.data) == r.output
    # sha256/size are computed over the exact bytes.
    assert len(art.sha256) == 64
    assert art.size == len(art.data)


def test_simulated_metrics_are_real_not_fabricated():
    # Inject a fake clock to assert the duration is the measured wall-clock delta.
    ticks = iter([10.0, 10.5])
    r = SimulatedExecutor(clock=lambda: next(ticks)).run(_textgen_spec())
    assert r.metrics["duration_seconds"] == pytest.approx(0.5)
    # No GPU used in simulation -> honestly null, never a fabricated number.
    assert r.metrics["gpu_util_pct"] is None


def test_simulated_batch_manifest_count_equals_items():
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    spec = JobSpec(
        job_id="job-batch",
        workload_type="batch-inference",
        model="echo-sim-v1",
        input={"items": items},
        execution_mode="simulated",
        image_digest=None,
    )
    r = SimulatedExecutor().run(spec)
    assert r.status == "success"
    out = r.output
    assert out["count"] == len(out["items"]) == len(items)
    assert out["simulated"] is True
    for i, item in enumerate(out["items"]):
        assert item["index"] == i
        assert item["status"] == "ok"
    assert validate_output("batch-inference", out) is True


def test_simulated_executor_does_no_network_or_fs_escape(monkeypatch):
    """The simulated path must not open sockets or touch the filesystem."""
    import socket

    def _boom(*a, **k):  # any socket use is a failure
        raise AssertionError("SimulatedExecutor attempted network I/O")

    monkeypatch.setattr(socket, "socket", _boom)

    import builtins

    real_open = builtins.open

    def _guarded_open(file, *a, **k):
        raise AssertionError(f"SimulatedExecutor attempted filesystem access: {file}")

    monkeypatch.setattr(builtins, "open", _guarded_open)
    try:
        r = SimulatedExecutor().run(_textgen_spec())
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert r.status == "success"


# --------------------------------------------------------------------------- #
# DockerExecutor -- hardened argv
# --------------------------------------------------------------------------- #
DIGEST = "ghcr.io/acme/model@sha256:" + "a" * 64


def _hw_spec(gpu_count=0, job_id="job-hw"):
    return JobSpec(
        job_id=job_id,
        workload_type="text-generation",
        model="real-model",
        input={"prompt": "hi"},
        execution_mode="hardware",
        image_digest=DIGEST,
        limits={
            "memory_mb": 4096,
            "cpus": "2.0",
            "gpu_count": gpu_count,
            "max_runtime_seconds": 120,
            "pids_limit": 128,
        },
    )


def _argv_for(spec, output_dir="/var/lib/ccoin/out/job-hw", nvidia=True):
    ex = DockerExecutor(
        docker_available=lambda: True,
        nvidia_available=lambda: nvidia,
    )
    return ex.build_argv(spec, output_dir)


def _flag_value(argv, flag):
    """Return the token immediately following ``flag`` (or None)."""
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def test_docker_argv_is_hardened():
    out_dir = "/var/lib/ccoin/out/job-hw"
    argv = _argv_for(_hw_spec(gpu_count=0), output_dir=out_dir)

    # Digest-pinned image, and it is the final positional token.
    assert DIGEST in argv
    assert "@sha256:" in argv[-1]
    assert argv[-1] == DIGEST

    # No egress by default.
    assert _flag_value(argv, "--network") == "none"
    # Read-only rootfs.
    assert "--read-only" in argv
    # Drop every capability.
    assert _flag_value(argv, "--cap-drop") == "ALL"
    # Bound fork bombs + resource quotas from limits.
    assert _flag_value(argv, "--pids-limit") == "128"
    assert _flag_value(argv, "--memory") == "4096m"
    assert _flag_value(argv, "--cpus") == "2.0"
    # Non-root user (never root / uid 0).
    user = _flag_value(argv, "--user")
    assert user is not None
    assert not user.startswith("0:")
    assert user != "root"
    # Hard wall-clock timeout wraps the run.
    assert argv[0] == "timeout"
    assert "120" in argv[:4]
    # no-new-privileges hardening.
    assert "no-new-privileges" in argv

    # --- the forbidden things are ABSENT ---
    joined = " ".join(argv)
    assert "docker.sock" not in joined          # NO docker socket
    assert "--privileged" not in argv           # NOT privileged
    # No GPU for a CPU job.
    assert "--gpus" not in argv
    # No host-sensitive bind: the only bind source is the dedicated output dir.
    for i, tok in enumerate(argv):
        if tok in ("--mount", "-v", "--volume"):
            mountspec = argv[i + 1]
            assert "docker.sock" not in mountspec
            assert "/etc" not in mountspec
            assert out_dir in mountspec          # only the per-job output dir
            assert "source=/\b" not in mountspec


def test_docker_argv_adds_gpus_only_when_requested():
    argv = _argv_for(_hw_spec(gpu_count=2), nvidia=True)
    assert _flag_value(argv, "--gpus") == "2"


def test_docker_gpu_job_without_toolkit_raises():
    spec = _hw_spec(gpu_count=1)
    ex = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: False)
    with pytest.raises(HardwareUnavailableError):
        ex.build_argv(spec, "/tmp/out")


def test_docker_requires_digest_pinned_image():
    spec = _hw_spec()
    spec.image_digest = "ghcr.io/acme/model:latest"  # mutable tag, not a digest
    ex = DockerExecutor(docker_available=lambda: True)
    with pytest.raises(Exception):
        ex.build_argv(spec, "/tmp/out")


def test_docker_unavailable_hardware_job_raises_no_silent_simulate():
    """A hardware job on a host without Docker must RAISE, never simulate."""
    spec = _hw_spec()
    ex = DockerExecutor(docker_available=lambda: False)
    with pytest.raises(HardwareUnavailableError):
        ex.run(spec)


def test_docker_executor_parses_fake_runner_output(tmp_path):
    """The injected runner writes output.json; the executor reads it back."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    produced = {"text": "real-ish output", "model": "real-model"}

    def fake_runner(argv, output_dir):
        # Assert we were handed the hardened argv (digest-pinned, no socket).
        assert argv[-1] == DIGEST
        assert "docker.sock" not in " ".join(argv)
        (out_dir / "output.json").write_text(json.dumps(produced))
        return DockerRunResult(returncode=0, stdout="done\n", stderr="")

    ex = DockerExecutor(
        runner=fake_runner,
        docker_available=lambda: True,
        nvidia_available=lambda: True,
        output_root=str(out_dir),
    )
    r = ex.run(_hw_spec())
    assert r.status == "success"
    assert r.execution_mode == "hardware"
    assert r.output == produced
    assert r.output_schema_valid is True
    assert len(r.artifacts) == 1


def test_docker_executor_nonzero_exit_is_failure():
    def fake_runner(argv, output_dir):
        return DockerRunResult(returncode=137, stdout="", stderr="OOM killed")

    ex = DockerExecutor(
        runner=fake_runner,
        docker_available=lambda: True,
        nvidia_available=lambda: True,
    )
    r = ex.run(_hw_spec())
    assert r.status == "failed"
    assert r.error is not None


# --------------------------------------------------------------------------- #
# select_executor
# --------------------------------------------------------------------------- #
def test_select_executor_switches_on_image_digest():
    sim = select_executor(_textgen_spec(), None)
    assert isinstance(sim, SimulatedExecutor)

    dock = select_executor(_hw_spec(), None)
    assert isinstance(dock, DockerExecutor)
