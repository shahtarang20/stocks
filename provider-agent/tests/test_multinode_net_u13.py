"""U13 agent: multi-node group network and cancel (written, not run)."""

import threading

import pytest

from computecoin_agent.executor import DockerExecutor, ExecutorError, JobSpec

DIGEST = "ghcr.io/x/y@sha256:" + "a" * 64
RV = {"master_addr": "10.0.0.1", "master_port": 29500, "node_rank": 1, "nnodes": 2, "gpus_per_node": 1, "world_size": 2}


def _spec(**kw):
    return JobSpec(job_id="j1", workload_type="kit", model="m", input={}, execution_mode="hardware", image_digest=DIGEST,
                   limits={"gpu_count": 0}, rendezvous=RV, **kw)


def _ex(**kw):
    if kw.get("multinode_network"):
        kw.setdefault("multinode_network_firewalled", True)
        kw.setdefault("multinode_firewall_check", lambda net: None)
    return DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True, **kw)


def test_rendezvous_job_joins_only_the_group_network_and_keeps_hardening():
    argv = _ex(multinode_network="grp1").build_argv(_spec(), "/tmp/out")
    assert argv[argv.index("--network") + 1] == "grp1"
    for flag in ("--read-only", "--cap-drop", "no-new-privileges"):
        assert flag in argv
    for bad in ("--privileged", "--cap-add", "host", "--publish", "-p"):
        assert bad not in argv
    assert "MASTER_ADDR=10.0.0.1" in argv


def test_rendezvous_job_refused_without_network_or_with_egress_or_private_tier():
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(), "/tmp/out")
    with pytest.raises(ExecutorError):
        _ex(multinode_network="grp1", egress_proxy_image="p@sha256:" + "b" * 64).build_argv(
            _spec(network_policy={"egress": ["a.example:443"]}), "/tmp/out")
    with pytest.raises(ExecutorError):
        _ex(multinode_network="grp1").build_argv(_spec(isolation_tier="sealed"), "/tmp/out")


def test_single_node_job_still_has_no_network():
    s = JobSpec(job_id="j2", workload_type="kit", model="m", input={}, execution_mode="hardware", image_digest=DIGEST,
                limits={"gpu_count": 0})
    argv = _ex(multinode_network="grp1").build_argv(s, "/tmp/out")
    assert argv[argv.index("--network") + 1] == "none"


def test_cancel_event_kills_the_named_container():
    calls = []
    ev = threading.Event()
    ex = _ex()
    cancelled: list[bool] = []
    stop = threading.Event()
    ex._runner = lambda argv, out: calls.append(argv)
    ev.set()
    ex._cancel_watch("ccoin-job-j1", "/tmp/out", stop, ev, cancelled)
    assert cancelled == [True] and calls == [["docker", "kill", "ccoin-job-j1"]]
