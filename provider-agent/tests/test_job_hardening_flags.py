"""Every customer container is started with the full set of hardening flags, not only some kinds of job."""

from __future__ import annotations

import pytest

from computecoin_agent.executor import DockerExecutor, JobSpec

DIGEST = "ghcr.io/x/y@sha256:" + "a" * 64


def _argv(**kw):
    ex = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True)
    spec = JobSpec(job_id="j1", workload_type="kit", model="m", input={}, execution_mode="hardware",
                   image_digest=DIGEST, limits={"gpu_count": 0, **kw.pop("limits", {})}, **kw)
    return ex.build_argv(spec, "/tmp/out")


def _value(argv, flag, nth=0):
    idxs = [i for i, a in enumerate(argv) if a == flag]
    return argv[idxs[nth] + 1]


def test_an_ordinary_job_has_no_swap_and_no_core_dumps():
    argv = _argv(limits={"memory_mb": 2048})
    assert _value(argv, "--memory") == "2048m"
    assert _value(argv, "--memory-swap") == "2048m"  # equal to the memory limit means no swap at all
    assert _value(argv, "--ulimit") == "core=0"


def test_the_swap_limit_follows_the_jobs_memory_limit():
    assert _value(_argv(limits={"memory_mb": 4096}), "--memory-swap") == "4096m"


def test_no_flag_is_given_twice():
    argv = _argv()
    for flag in ("--memory", "--memory-swap", "--ulimit", "--cap-drop", "--read-only", "--user", "--pids-limit"):
        assert argv.count(flag) == 1, flag


@pytest.mark.parametrize("bad", ["--privileged", "--cap-add", "--pid", "--userns", "--device"])
def test_no_privileged_flag_ever_appears(bad):
    assert bad not in _argv()
    assert "seccomp=unconfined" not in " ".join(_argv())
