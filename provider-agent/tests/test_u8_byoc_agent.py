"""U8 agent side (written, not run): parse_byoc validation and DockerExecutor.build_argv hardening for BYOC."""

import pytest

from computecoin_agent.byoc import ByocError, parse_byoc
from computecoin_agent.executor import DockerExecutor, ExecutorError, IsolationUnavailableError, JobSpec

IMG = "ghcr.io/acme/tool@sha256:" + "a" * 64


def _block(**kw):
    b = {"image": IMG, "isolation": "gvisor", "command": ["python", "/app/run.py"], "args": ["--n", "5"],
         "env": {"APP_MODE": "fast"}, "gpu_count": 1, "memory_mb": 1024, "cpus": "2", "pids_limit": 128,
         "max_runtime_seconds": 600}
    b.update(kw)
    return b


def _spec(block, tier="container"):
    return JobSpec(job_id="j1", workload_type="byoc", model="byoc", input={}, execution_mode="hardware",
                   image_digest=IMG, isolation_tier=tier, v2={"byoc": block})


def _ex(gvisor=True):
    return DockerExecutor(runner=lambda a, o: None, docker_available=lambda: True, nvidia_available=lambda: True,
                          gvisor_available=lambda: gvisor, microvm_available=lambda: False, gpu_vendor="nvidia")


def test_parse_none_without_block():
    assert parse_byoc({"kit": {}}) is None


@pytest.mark.parametrize("bad", [
    {"image": "ghcr.io/acme/tool:latest"}, {"isolation": "container"}, {"env": {"LD_PRELOAD": "x"}},
    {"env": {"lower": "x"}}, {"command": ["a\x00b"]}, {"memory_mb": 10**9}, {"args": ["x"] * 33},
])
def test_parse_refuses(bad):
    with pytest.raises(ByocError):
        parse_byoc({"byoc": _block(**bad)})


def test_argv_is_hardened():
    argv = _ex().build_argv(_spec(_block()), "/tmp/out")
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert "--read-only" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--privileged" not in argv and "--pid" not in argv and "--device" not in argv
    assert not any("docker.sock" in a for a in argv)
    assert argv[argv.index("--entrypoint") + 1] == "python"
    i = argv.index(IMG)
    assert argv[i + 1:] == ["/app/run.py", "--n", "5"]
    assert argv[argv.index("--memory") + 1] == "1024m"


def test_no_runsc_refuses_never_downgrades():
    with pytest.raises(IsolationUnavailableError):
        _ex(gvisor=False).build_argv(_spec(_block()), "/tmp/out")


def test_image_mismatch_and_privacy_tier_refused():
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(_block(image="ghcr.io/acme/other@sha256:" + "b" * 64)), "/tmp/out")
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(_block(), tier="sealed"), "/tmp/out")
