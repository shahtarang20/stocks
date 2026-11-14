"""The API's Windows-host alerts read the health keys the agent really sends.

``observability.platform_signals`` used to carry a TODO to confirm its key names against the supervisor and updater.
This test feeds the AGENT'S OWN health output through the API's reader, so a renamed key on either side fails here.
"""

from __future__ import annotations

from app.services.observability import platform_signals
from computecoin_agent.windows.governance.policy import Decision, State
from computecoin_agent.windows.supervisor_wiring import RuntimeHealthCache, static_update_health


def _runtime_block(distro_ok: bool, gpu_smoke_ok: bool) -> dict:
    cache = RuntimeHealthCache(lambda previous, allowed: {"distro_ok": distro_ok, "gpu_smoke_ok": gpu_smoke_ok}, background=False)
    cache.refresh()
    return cache.provider()()


def test_unhealthy_distro_and_failing_gpu_smoke_are_read_from_the_agents_runtime_block():
    health = _runtime_block(distro_ok=False, gpu_smoke_ok=False)
    assert set(health["runtime"]) == {"distro_ok", "gpu_smoke_ok"}
    signals = platform_signals(health)
    assert signals["distro_unhealthy"] is True
    assert signals["gpu_smoke_failing"] is True


def test_a_healthy_runtime_raises_nothing():
    signals = platform_signals(_runtime_block(distro_ok=True, gpu_smoke_ok=True))
    assert signals["distro_unhealthy"] is False and signals["gpu_smoke_failing"] is False


def test_yielding_is_read_from_the_agents_availability_detail():
    decision = Decision(state=State.YIELDING, code="foreground_gpu_app", reason="the owner is using the GPU")
    assert platform_signals(decision.heartbeat_health())["yielding"] is True
    online = Decision(state=State.ONLINE, code="ok", reason="ok")
    assert platform_signals(online.heartbeat_health())["yielding"] is False


def test_a_rolled_back_update_is_read_from_the_agents_update_block():
    block = static_update_health("1.2.3")()
    assert set(block["update"]) == {"rolled_back", "version"}
    assert platform_signals(block)["update_rolled_back"] is False
    assert platform_signals({"update": {**block["update"], "rolled_back": True}})["update_rolled_back"] is True


def test_a_host_that_reports_none_of_this_reads_zero_and_nothing_is_invented():
    for health in (None, {}, {"runtime": {}}, {"platform": {"os": "linux"}}):
        assert not any(platform_signals(health).values())
