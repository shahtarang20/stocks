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


# --------------------------------------------------------------------------- #
# Review fix: output.json is job-controlled (symlink / size)
# --------------------------------------------------------------------------- #
def _run_with_output_planter(tmp_path, plant):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake_runner(argv, output_dir):
        plant(out_dir)
        return DockerRunResult(returncode=0, stdout="", stderr="")

    ex = DockerExecutor(runner=fake_runner, docker_available=lambda: True, nvidia_available=lambda: False,
                        output_root=str(out_dir))
    return ex.run(_hw_spec())


def test_docker_output_json_symlink_to_host_file_is_not_followed(tmp_path):
    secret = tmp_path / "host-secret.json"
    secret.write_text(json.dumps({"text": "HOST SECRET", "machine_secret": "s3cr3t"}))

    def plant(out_dir):
        (out_dir / "output.json").symlink_to(secret)

    r = _run_with_output_planter(tmp_path, plant)
    assert r.status == "failed"
    assert r.error["code"] == "missing_output"
    assert r.artifacts == []
    assert "HOST SECRET" not in json.dumps(r.output)


def test_docker_output_json_over_size_cap_is_refused(tmp_path, monkeypatch):
    import computecoin_agent.executor as ex_mod

    monkeypatch.setattr(ex_mod, "MAX_OUTPUT_JSON_BYTES", 64)

    def plant(out_dir):
        (out_dir / "output.json").write_text(json.dumps({"text": "x" * 500}))

    r = _run_with_output_planter(tmp_path, plant)
    assert r.status == "failed"
    assert r.error["code"] == "missing_output"


def test_docker_output_json_fifo_does_not_hang(tmp_path):
    import os

    def plant(out_dir):
        os.mkfifo(out_dir / "output.json")

    r = _run_with_output_planter(tmp_path, plant)
    assert r.status == "failed"
    assert r.error["code"] == "missing_output"


def test_docker_run_is_named_and_container_removed_after_timeout_kill(tmp_path):
    """`timeout --signal=KILL N docker run` only kills the docker CLI (exit 137); the container itself keeps running
    unless the agent stops it by name."""
    calls = []

    def fake_runner(argv, output_dir):
        calls.append(list(argv))
        return DockerRunResult(returncode=137, stdout="", stderr="")

    ex = DockerExecutor(runner=fake_runner, docker_available=lambda: True, nvidia_available=lambda: False,
                        output_root=str(tmp_path))
    r = ex.run(_hw_spec(job_id="job-to"))
    assert r.status == "failed"
    assert _flag_value(calls[0], "--name") == "ccoin-job-job-to"
    assert ["docker", "rm", "-f", "ccoin-job-job-to"] in calls


def test_docker_container_removed_when_runner_raises(tmp_path):
    calls = []

    def fake_runner(argv, output_dir):
        calls.append(list(argv))
        if argv[0] == "timeout":
            raise OSError("docker client died")
        return DockerRunResult(returncode=0)

    ex = DockerExecutor(runner=fake_runner, docker_available=lambda: True, nvidia_available=lambda: False,
                        output_root=str(tmp_path))
    with pytest.raises(OSError):
        ex.run(_hw_spec(job_id="job-x"))
    assert ["docker", "rm", "-f", "ccoin-job-job-x"] in calls


# --------------------------------------------------------------------------- #
# Review fix: sealed-tier private chat streaming needs the monitor's env pointers
# --------------------------------------------------------------------------- #
def _sealed_spec(**kw):
    spec = _hw_spec(job_id="job-sealed")
    spec.isolation_tier = "sealed"
    for k, v in kw.items():
        setattr(spec, k, v)
    return spec


def test_sealed_tier_accepts_only_the_stream_env_pointers():
    argv = _argv_for(_sealed_spec(extra_env={"COMPUTECOIN_STREAM": "/out/stream.ndjson",
                                              "COMPUTECOIN_PROGRESS": "/out/progress.json"}))
    assert "COMPUTECOIN_STREAM=/out/stream.ndjson" in argv
    assert "--network" in argv and _flag_value(argv, "--network") == "none"
    assert not any(a.startswith("type=bind,source=") and "target=/out" not in a for a in argv)


def test_sealed_tier_still_refuses_staging_mounts_and_other_env():
    from computecoin_agent.executor import ExecutorError

    with pytest.raises(ExecutorError):
        _argv_for(_sealed_spec(extra_env={"LD_PRELOAD": "/x"}))
    with pytest.raises(ExecutorError):
        _argv_for(_sealed_spec(mounts=[{"source": "/data", "target": "/in", "readonly": True}]))


# --------------------------------------------------------------------------- #
# Audit: the executor-created output dir (customer input.json + outputs) must not be left on the provider's disk
# --------------------------------------------------------------------------- #
def _ok_runner(seen):
    def fake_runner(argv, output_dir):
        if argv[0] == "timeout":
            seen.append(output_dir)
            with open(f"{output_dir}/output.json", "w", encoding="utf-8") as fh:
                json.dump({"text": "ok"}, fh)
        return DockerRunResult(returncode=0, stdout="", stderr="")

    return fake_runner


def test_executor_created_output_dir_is_removed_when_nobody_tails_it():
    import os

    seen = []
    ex = DockerExecutor(runner=_ok_runner(seen), docker_available=lambda: True, nvidia_available=lambda: False)
    r = ex.run(_hw_spec(job_id="job-leak"))
    assert r.status == "success" and r.output == {"text": "ok"} and r.artifacts[0].data
    assert seen and not os.path.exists(seen[0])  # input.json / output.json are gone


def test_executor_created_output_dir_is_removed_on_failed_run():
    import os

    seen = []

    def fake_runner(argv, output_dir):
        if argv[0] == "timeout":
            seen.append(output_dir)
            return DockerRunResult(returncode=1, stdout="", stderr="boom")
        return DockerRunResult(returncode=0)

    ex = DockerExecutor(runner=fake_runner, docker_available=lambda: True, nvidia_available=lambda: False)
    assert ex.run(_hw_spec(job_id="job-leak2")).status == "failed"
    assert seen and not os.path.exists(seen[0])


def test_run_open_removes_the_executor_created_dir_after_the_monitor_stopped():
    import os

    from computecoin_agent.jobrunner import _run_open

    seen, events = [], []

    class Mon:
        env = {}

        def set_output_dir(self, path):
            events.append(("dir_exists_at_set", os.path.isdir(path)))

        def start(self):
            pass

        def stop(self):
            events.append(("dir_exists_at_stop", os.path.isdir(seen[0])))  # the monitor's last tick still reads it

    ex = DockerExecutor(runner=_ok_runner(seen), docker_available=lambda: True, nvidia_available=lambda: False)
    spec = _hw_spec(job_id="job-leak3")
    result = _run_open(spec, None, lambda s, c: ex, Mon())
    assert result.status == "success"
    assert ("dir_exists_at_stop", True) in events
    assert not os.path.exists(seen[0]) and spec.owned_output_dir is None


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_max_runtime_never_disables_the_wall_clock_timeout(bad):
    spec = _hw_spec()
    spec.limits["max_runtime_seconds"] = bad
    argv = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: False).build_argv(spec, "/tmp/out")
    assert argv[:3] == ["timeout", "--signal=KILL", str(DockerExecutor.DEFAULT_TIMEOUT_SECONDS)]
