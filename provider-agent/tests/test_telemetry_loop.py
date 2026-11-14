"""The agent sends REAL GPU readings (and only those) so the host's usage page has something true to show."""

from __future__ import annotations

import json

import httpx

from computecoin_agent import telemetry
from computecoin_agent.config import AgentConfig
from computecoin_agent.telemetry import TelemetryCollector, TelemetrySample
from computecoin_agent.telemetry_loop import TelemetryLoop, build_telemetry_loop, real_gpu_samples

NOW = 1_794_000_000.0  # a unix timestamp, as the agent's own samples carry


def _sample(util=42.0, mode=None, idx=0):
    return TelemetrySample(
        gpu_index=idx, gpu_util_pct=util, mem_util_pct=20.0, mem_used_mb=2048, mem_total_mb=8192, temp_c=61.0, power_w=120.0,
        fan_speed_pct=35.0, timestamp=NOW, extra_metrics={"mode": mode} if mode else None,
    )


# ---- never a made-up reading -------------------------------------------------------------------------------------
def test_the_placeholder_sample_of_a_machine_with_no_gpu_is_never_sent(monkeypatch):
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda: [_sample(25.0, mode="simulated_probe")])
    assert real_gpu_samples() == []


def test_real_samples_pass_through_and_the_placeholder_is_dropped_from_a_mixed_list(monkeypatch):
    monkeypatch.setattr(
        telemetry, "sample_gpu_metrics", lambda: [_sample(10.0, idx=0), _sample(99.0, mode="simulated_probe", idx=1), _sample(30.0, idx=2)]
    )
    assert [s.gpu_index for s in real_gpu_samples()] == [0, 2]


def test_a_host_without_a_gpu_sends_nothing_at_all(monkeypatch):
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda: [_sample(25.0, mode="simulated_probe")])
    posted = []
    collector = TelemetryCollector("m1", "secret", base_url="http://api.invalid", sampler=real_gpu_samples)
    loop = TelemetryLoop(collector, clock=lambda: 0.0)
    loop.tick()
    assert collector.buffer_size == 0  # nothing buffered, so nothing to flush: no request is made
    assert posted == []


# ---- the loop ---------------------------------------------------------------------------------------------------
class _Collector:
    def __init__(self, fail_sample=False, fail_flush=False):
        self.samples, self.flushes, self.fail_sample, self.fail_flush = 0, 0, fail_sample, fail_flush

    def sample_now(self):
        self.samples += 1
        if self.fail_sample:
            raise RuntimeError("nvml went away")
        return []

    def flush(self):
        self.flushes += 1
        if self.fail_flush:
            raise RuntimeError("network down")
        return {}


def test_every_tick_reads_and_the_batch_goes_out_at_most_once_a_minute():
    now = [0.0]
    c = _Collector()
    loop = TelemetryLoop(c, flush_interval=60.0, clock=lambda: now[0])
    loop.tick()
    assert (c.samples, c.flushes) == (1, 1)  # the first tick sends straight away
    for t in (10.0, 30.0, 59.0):
        now[0] = t
        loop.tick()
    assert (c.samples, c.flushes) == (4, 1)  # read every time, sent once
    now[0] = 61.0
    loop.tick()
    assert c.flushes == 2


def test_a_failing_read_or_upload_never_raises_into_the_agent():
    TelemetryLoop(_Collector(fail_sample=True), clock=lambda: 0.0).tick()
    TelemetryLoop(_Collector(fail_flush=True), clock=lambda: 0.0).tick()


def test_readings_that_could_not_be_sent_stay_buffered_for_the_next_batch(monkeypatch):
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda: [_sample(55.0)])
    collector = TelemetryCollector("m1", "secret", base_url="http://api.invalid", sampler=real_gpu_samples)
    down = httpx.MockTransport(lambda request: httpx.Response(503))

    real_flush = collector.flush
    collector.flush = lambda **kw: real_flush(transport=down)  # the network is down
    loop = TelemetryLoop(collector, clock=lambda: 0.0)
    loop.tick()  # must not raise
    assert collector.buffer_size == 1  # still there

    sent = []

    def ok(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": len(sent[-1]["points"]), "alerts_triggered": 0})

    collector.flush = lambda **kw: real_flush(transport=httpx.MockTransport(ok))
    loop._last_flush = float("-inf")
    loop.tick()
    assert collector.buffer_size == 0
    assert len(sent[0]["points"]) == 2  # the held-back reading and the new one
    assert set(sent[0]["points"][0]) >= {"gpu_util_pct", "mem_used_mb", "mem_total_mb", "temp_c", "power_w"}


def test_nothing_about_jobs_or_programs_is_in_what_is_sent(monkeypatch):
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda: [_sample(55.0)])
    collector = TelemetryCollector("m1", "secret", base_url="http://api.invalid", sampler=real_gpu_samples)
    collector.sample_now()
    body = json.dumps([p.to_dict() for p in list(collector._buffer)])
    for forbidden in ("prompt", "job", "process", "pid", "command"):
        assert forbidden not in body.lower()


# ---- wiring ----------------------------------------------------------------------------------------------------
def test_the_built_loop_reads_real_gpus_only():
    loop = build_telemetry_loop("http://api.invalid", "m1", "secret")
    assert loop._collector.sampler is real_gpu_samples


def test_it_is_on_by_default_and_can_be_switched_off():
    assert AgentConfig().telemetry_enabled is True
    assert AgentConfig(telemetry_enabled=False).telemetry_enabled is False


def test_stop_ends_the_background_thread():
    loop = TelemetryLoop(_Collector(), sample_interval=1.0).start()
    loop.stop()
    assert loop._thread is not None and not loop._thread.is_alive()
