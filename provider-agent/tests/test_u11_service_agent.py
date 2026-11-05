"""U11 service agent concurrency pool (written, NOT executed): run_service with a fake API and a fake container."""

import base64
import datetime as dt
import threading
import time
import types

import pytest

from computecoin_agent import jobrunner
from computecoin_agent import service_agent as sa
from computecoin_agent.executor import JobSpec


def _spec(max_concurrency=2):
    return JobSpec(job_id="j1", workload_type="kit", model="k@1", input={}, execution_mode="hardware",
                   v2={"service": {"replica_id": "r1", "port": 8000, "health_path": "/health",
                                   "max_concurrency": max_concurrency}})


def _req(i, deadline_in=None):
    r = {"request_id": f"q{i}", "method": "POST", "path": "/infer", "content_type": "application/json",
         "body_b64": base64.b64encode(b"{}").decode(), "attempt": 1}
    if deadline_in is not None:
        r["deadline_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=deadline_in)).isoformat()
    return r


class FakeContainer:
    def __init__(self, delay=0.1, events=None):
        self.delay, self.events = delay, events if events is not None else []
        self.active = self.max_active = 0
        self.timeouts: list[float] = []
        self.lock = threading.Lock()

    def start(self):
        self.events.append("start")

    def healthy(self, path):
        return True

    def request(self, method, path, content_type, body, timeout, max_bytes):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.timeouts.append(timeout)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return 200, "application/json", b'{"ok":true}'

    def stop(self):
        self.events.append("stop")


class FakeApi:
    """Hands out queued requests on poll (ignoring ``accepting`` so the agent's own pool is what bounds concurrency) and
    answers stop once ``stop_after`` responses were posted (or immediately after the last request was handed out)."""

    def __init__(self, requests, stop_after_handout=False, events=None):
        self.todo, self.responses = list(requests), {}
        self.total = len(requests)
        self.stop_after_handout = stop_after_handout
        self.events = events if events is not None else []
        self.lock = threading.Lock()
        self.handed = 0

    def __call__(self, base_url, machine_id, secret, path, body, transport, timeout):
        time.sleep(0.01)
        with self.lock:
            if path.endswith("/poll"):
                if self.todo:
                    self.handed += 1
                    return types.SimpleNamespace(status_code=200, json=lambda r=self.todo.pop(0): {"stop": False, "request": r})
                done = len(self.responses) >= self.total
                if done or (self.stop_after_handout and self.handed >= self.total):
                    return types.SimpleNamespace(status_code=200, json=lambda: {"stop": True, "reason": "draining"})
                return types.SimpleNamespace(status_code=200, json=lambda: {"stop": False, "request": None})
            rid = path.split("/requests/")[1].split("/")[0]
            self.responses[rid] = body
            self.events.append("response")
            return types.SimpleNamespace(status_code=200, json=lambda: {})


@pytest.fixture
def heartbeats(monkeypatch):
    stamps: list[float] = []
    monkeypatch.setattr(jobrunner, "post_heartbeat", lambda *a, **k: stamps.append(time.monotonic()))
    return stamps


def _run(api, box, monkeypatch, spec=None):
    monkeypatch.setattr(sa, "_signed", api)
    return sa.run_service("http://api", "m1", "secret", spec or _spec(), container=box, timeout=5.0,
                          sleep=lambda s: time.sleep(min(s, 0.02)))


def test_max_concurrency_is_respected(monkeypatch, heartbeats):
    box = FakeContainer(delay=0.15)
    api = FakeApi([_req(i) for i in range(6)])
    res = _run(api, box, monkeypatch, _spec(max_concurrency=2))
    assert res.status == "success" and res.metrics["requests_served"] == 6
    assert box.max_active == 2  # parallel up to the limit, never above it
    assert len(api.responses) == 6 and all(b["status"] == 200 for b in api.responses.values())


def test_concurrency_one_serialises_requests(monkeypatch, heartbeats):
    box = FakeContainer(delay=0.05)
    res = _run(FakeApi([_req(i) for i in range(3)]), box, monkeypatch, _spec(max_concurrency=1))
    assert res.status == "success" and box.max_active == 1


def test_concurrency_is_clamped_to_the_hard_cap(monkeypatch, heartbeats):
    box = FakeContainer(delay=0.01)
    res = _run(FakeApi([_req(0)]), box, monkeypatch, _spec(max_concurrency=10_000))
    assert res.status == "success" and box.max_active == 1  # (clamp to MAX_CONCURRENCY_CAP, here one request only)


def test_each_request_gets_its_own_deadline_timeout(monkeypatch, heartbeats):
    box = FakeContainer(delay=0.01)
    _run(FakeApi([_req(0, deadline_in=8), _req(1, deadline_in=-5), _req(2)]), box, monkeypatch)
    short, expired, unknown = sorted(box.timeouts)[0], sorted(box.timeouts)[1], sorted(box.timeouts)[2]
    assert short == 1.0  # past deadline: floor of 1 s, never negative
    assert 5.0 < expired <= 8.0  # time left until the API-side deadline
    assert unknown == sa.DEFAULT_REQUEST_TIMEOUT


def test_request_timeout_bounds():
    far = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    assert sa._request_timeout({"deadline_at": far}) == 300.0
    assert sa._request_timeout({"deadline_at": "garbage"}) == sa.DEFAULT_REQUEST_TIMEOUT
    assert sa._request_timeout({}) == sa.DEFAULT_REQUEST_TIMEOUT


def test_heartbeat_is_not_blocked_by_a_slow_request(monkeypatch, heartbeats):
    monkeypatch.setattr(sa, "HEARTBEAT_EVERY", 0.05)
    box = FakeContainer(delay=0.8)
    started = time.monotonic()
    res = _run(FakeApi([_req(0)]), box, monkeypatch, _spec(max_concurrency=1))
    assert res.status == "success"
    assert time.monotonic() - started >= 0.8
    assert len(heartbeats) >= 3  # the main loop kept heartbeating while the request was in the worker thread


def test_drain_on_stop_posts_inflight_responses_before_the_container_is_removed(monkeypatch, heartbeats):
    events: list[str] = []
    box = FakeContainer(delay=0.4, events=events)
    api = FakeApi([_req(0), _req(1)], stop_after_handout=True, events=events)
    res = _run(api, box, monkeypatch, _spec(max_concurrency=2))
    assert res.status == "success" and res.metrics["requests_served"] == 2
    assert events.index("stop") > max(i for i, e in enumerate(events) if e == "response")  # drained first
    assert len(api.responses) == 2
    assert box.active == 0


def test_uptime_is_reported_in_the_result_metrics(monkeypatch, heartbeats):
    box = FakeContainer(delay=0.01)
    res = _run(FakeApi([_req(0)]), box, monkeypatch)
    assert res.metrics["duration_seconds"] > 0 and "requests_served" in res.metrics


# --- review fix: the real container wrapper (never exercised before) ---------------------------------------------
class _FakeRun:
    def __init__(self, internal=None):
        self.calls, self.internal = [], internal

    def __call__(self, argv, **kw):
        import types

        self.calls.append(list(argv))
        if argv[:3] == ["docker", "network", "inspect"]:
            if self.internal is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="no such network")
            return types.SimpleNamespace(returncode=0, stdout=f"{str(self.internal).lower()}\n", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="172.20.0.5\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


_IMG = "ghcr.io/x/svc@sha256:" + "a" * 64


def test_docker_service_container_is_bounded_internal_and_uniquely_named():
    import pytest as _pytest
    from computecoin_agent.service_agent import DockerServiceContainer, ServiceAgentError

    run = _FakeRun(internal=None)
    box = DockerServiceContainer(_IMG, 8000, name="cc-svc-job1", memory_mb=1024, cpus="2", runner=run)
    box.start()
    assert ["docker", "network", "create", "--internal", "computecoin-svc-internal"] in run.calls
    cmd = next(c for c in run.calls if c[:2] == ["docker", "run"])
    assert cmd[cmd.index("--memory") + 1] == "1024m" and cmd[cmd.index("--cpus") + 1] == "2"
    assert cmd[cmd.index("--memory-swap") + 1] == "1024m"
    assert "noexec" in cmd[cmd.index("--tmpfs") + 1] and "size=" in cmd[cmd.index("--tmpfs") + 1]
    assert "--privileged" not in cmd and cmd[cmd.index("--name") + 1] == "cc-svc-job1"
    # two replicas created in the same instant must not collide on a container name
    assert DockerServiceContainer(_IMG, 8000).name != DockerServiceContainer(_IMG, 8000).name

    with _pytest.raises(ServiceAgentError):  # an existing network that is NOT internal is never used
        DockerServiceContainer(_IMG, 8000, runner=_FakeRun(internal=False)).start()
