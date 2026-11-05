"""U13 agent: firewall attestation gate, self-check helper, benchmark responder (loopback only). Written, not run.

Bug re-insertion list: drop the attestation check in build_argv (refusal test fails); accept Internal=true in
check_multinode_firewall (internal test fails); bind the responder to 0.0.0.0 (bind test fails).
"""

import asyncio
import json
import time

import pytest

from computecoin_agent import netbench
from computecoin_agent.executor import DockerExecutor, ExecutorError, JobSpec, check_multinode_firewall

DIGEST = "ghcr.io/x/y@sha256:" + "a" * 64
RV = {"master_addr": "10.0.0.1", "master_port": 29500, "node_rank": 1, "nnodes": 2, "gpus_per_node": 1, "world_size": 2}


def _spec(**kw):
    return JobSpec(job_id="j1", workload_type="kit", model="m", input={}, execution_mode="hardware", image_digest=DIGEST,
                   limits={"gpu_count": 0}, rendezvous=RV, **kw)


def _ex(**kw):
    return DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True, multinode_network="grp1", **kw)


def test_refused_without_attestation_even_if_check_would_pass():
    with pytest.raises(ExecutorError, match="multinode_firewall_not_attested"):
        _ex(multinode_firewall_check=lambda n: None).build_argv(_spec(), "/tmp/out")


def test_refused_when_self_check_fails():
    with pytest.raises(ExecutorError, match="multinode_firewall_check_failed"):
        _ex(multinode_network_firewalled=True, multinode_firewall_check=lambda n: "no marker").build_argv(_spec(), "/tmp/out")


def test_attested_and_checked_runs_with_hardening_kept():
    argv = _ex(multinode_network_firewalled=True, multinode_firewall_check=lambda n: None).build_argv(_spec(), "/tmp/out")
    assert argv[argv.index("--network") + 1] == "grp1"
    for flag in ("--read-only", "--cap-drop", "no-new-privileges"):
        assert flag in argv
    for bad in ("--privileged", "--cap-add", "--net=host"):
        assert bad not in argv


def test_private_tier_still_refused_with_attestation():
    with pytest.raises(ExecutorError):
        _ex(multinode_network_firewalled=True, multinode_firewall_check=lambda n: None).build_argv(
            _spec(isolation_tier="sealed"), "/tmp/out")


def _runner(internal="false", ruleset="", fail=()):
    def run(argv):
        if argv[0] in fail:
            raise RuntimeError("missing tool")
        if argv[0] == "docker":
            return internal + "\n"
        return ruleset
    return run


def test_check_ok_with_nft_marker():
    r = _runner(ruleset='ip saddr 10.20.0.0/24 drop comment "computecoin-multinode:grp1"')
    assert check_multinode_firewall("grp1", r) is None


def test_check_ok_with_iptables_fallback():
    r = _runner(ruleset="-A DOCKER-USER -m comment --comment computecoin-multinode:grp1 -j DROP", fail=("nft",))
    assert check_multinode_firewall("grp1", r) is None


def test_check_fails_internal_missing_marker_or_other_network_marker():
    assert check_multinode_firewall("grp1", _runner(internal="true", ruleset="computecoin-multinode:grp1"))
    assert check_multinode_firewall("grp1", _runner(ruleset="nothing here"))
    assert check_multinode_firewall("grp1", _runner(ruleset="computecoin-multinode:other"))
    assert check_multinode_firewall("grp1", _runner(fail=("docker",)))
    assert check_multinode_firewall("grp1", _runner(fail=("nft", "iptables")))


# ---- benchmark responder: asyncio, loopback only. The responder itself refuses loopback, so the test patches the guard.

def test_responder_refuses_non_private_bind():
    for bad in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "not-an-ip"):
        with pytest.raises(ValueError):
            asyncio.run(netbench.serve_responder(bad, "k"))


def test_responder_proof_roundtrip_on_loopback(monkeypatch):
    async def go():
        monkeypatch.setattr(netbench, "_private_literal", lambda a: "127.0.0.1")
        monkeypatch.setattr(netbench, "PING_ROUNDS", 3)
        monkeypatch.setattr(netbench, "BW_BYTES", 4096)
        server = await netbench.serve_responder("10.0.0.1", "secret", port=0)
        host, port = server.sockets[0].getsockname()[:2]
        assert host == "127.0.0.1"
        nonce = f"{int(time.time()) + 60}.mac"
        r, w = await asyncio.open_connection(host, port)
        w.write((json.dumps({"nonce": nonce, "prober_id": "p1"}) + "\n").encode())
        await w.drain()
        got = json.loads(await asyncio.wait_for(r.readline(), 5))
        assert got["proof"] == netbench.proof("secret", nonce, "p1")
        for _ in range(3):
            w.write(b"x")
            await w.drain()
            assert await asyncio.wait_for(r.readexactly(1), 5) == b"x"
        w.write(b"z" * 4096)
        await w.drain()
        assert await asyncio.wait_for(r.readexactly(1), 5) == b"k"
        w.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())


def test_responder_ignores_expired_nonce(monkeypatch):
    async def go():
        monkeypatch.setattr(netbench, "_private_literal", lambda a: "127.0.0.1")
        server = await netbench.serve_responder("10.0.0.1", "secret", port=0)
        host, port = server.sockets[0].getsockname()[:2]
        r, w = await asyncio.open_connection(host, port)
        w.write((json.dumps({"nonce": f"{int(time.time()) - 10}.mac", "prober_id": "p1"}) + "\n").encode())
        await w.drain()
        assert await asyncio.wait_for(r.read(), 5) == b""  # closed without a proof
        w.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())


def test_check_marker_of_a_longer_network_name_does_not_satisfy_a_shorter_one():
    # network "grp1" must not be considered firewalled because the rules of "grp10" exist
    assert check_multinode_firewall("grp1", _runner(ruleset='drop comment "computecoin-multinode:grp10"'))
    assert check_multinode_firewall("grp1", _runner(ruleset="-A DOCKER-USER --comment computecoin-multinode:grp1.b -j DROP", fail=("nft",)))
    assert check_multinode_firewall("grp1", _runner(ruleset='drop comment "computecoin-multinode:grp1"')) is None
