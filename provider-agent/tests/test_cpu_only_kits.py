"""CPU-only Kits on the agent: no --gpus for a gpu_count 0 claim, and the opt-in heartbeat flag. WRITTEN, NOT RUN.

Bug re-insertion list: make the executor add ``--gpus`` for gpu_count 0 (argv test fails); always emit ``cpu_capable`` in
``default_health`` (omitted-by-default test fails).
"""

from computecoin_agent import heartbeat
from computecoin_agent.executor import DockerExecutor, JobSpec

DIGEST = "ghcr.io/acme/kit-scientific-compute@sha256:" + "b" * 64


def _spec(gpu_count):
    return JobSpec(
        job_id="job-cpu", workload_type="kit", model="scientific-compute@1.0.0", input={},
        execution_mode="hardware", image_digest=DIGEST,
        limits={"memory_mb": 4096, "cpus": "2.0", "gpu_count": gpu_count, "max_runtime_seconds": 120, "pids_limit": 128},
    )


def _argv(gpu_count, nvidia=True):
    ex = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: nvidia)
    return ex.build_argv(_spec(gpu_count), "/var/lib/ccoin/out/job-cpu")


def test_cpu_only_claim_runs_without_any_gpu_flag_even_on_a_gpu_host():
    argv = _argv(0, nvidia=True)
    assert "--gpus" not in argv and not any(a.startswith("--gpus") for a in argv)
    assert "--device" not in argv  # no AMD render nodes either
    assert argv[-1] == DIGEST


def test_cpu_only_claim_runs_on_a_host_without_the_nvidia_toolkit():
    argv = _argv(0, nvidia=False)  # must not raise: a CPU host has no GPU runtime
    assert "--gpus" not in argv


def test_gpu_claim_still_gets_gpus():
    argv = _argv(1, nvidia=True)
    assert argv[argv.index("--gpus") + 1] == "1"


def test_heartbeat_flag_is_omitted_unless_the_operator_opted_in(monkeypatch):
    monkeypatch.setattr(heartbeat, "_cpu_capable_flag", lambda: None)
    assert "cpu_capable" not in heartbeat.default_health()
    monkeypatch.setattr(heartbeat, "_cpu_capable_flag", lambda: True)
    assert heartbeat.default_health()["cpu_capable"] is True


def test_cpu_capable_flag_reads_the_agent_config(monkeypatch):
    from computecoin_agent.config import AgentConfig

    class _Cfg:
        cpu_only_kits_enabled = True

    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, *a, **k: _Cfg()))
    assert heartbeat._cpu_capable_flag() is True
    _Cfg.cpu_only_kits_enabled = False
    assert heartbeat._cpu_capable_flag() is None
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(OSError("x"))))
    assert heartbeat._cpu_capable_flag() is None  # never raises into the heartbeat
