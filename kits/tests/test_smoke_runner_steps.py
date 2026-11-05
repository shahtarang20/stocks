"""Smoke runner service/session steps, CPU-only support and smoke-spec validation (WRITTEN, NOT RUN).
Run from repo root: pytest kits/tests. Pure Python: docker, GPUs and the network are replaced by fakes.

Bug re-insertion list: add ``--gpus all`` when gpu_count is 0 (cpu tests fail); publish a port in build_service_argv (flag
test fails); skip the ``docker rm`` in the service ``finally`` (cleanup test fails); drop the session-step requirement for a
session Kit in kit_ci (session-required test fails); count 'cpu' labels for a GPU Kit (host class test fails).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
KITS = ROOT / "kits"
sys.path.insert(0, str(ROOT / "scripts"))

import kit_ci  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load(KITS / "tools" / "smoke_runner.py", "smoke_runner_under_test")
IMAGE = "ghcr.io/computecoin/kit-x@sha256:" + "c" * 64


def _smoke(slug):
    return json.loads((KITS / slug / "smoke" / "smoke.json").read_text(encoding="utf-8"))


def _manifest(slug):
    return json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))


class FakeDocker:
    """Records every docker call; ``script`` maps a command prefix to a (returncode, stdout, stderr) result."""

    def __init__(self, script=None):
        self.calls: list[list[str]] = []
        self.inputs: list[object] = []
        self.script = script or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.inputs.append(kw.get("input"))
        for prefix, res in self.script.items():
            if " ".join(argv).startswith(prefix):
                if isinstance(res, list):
                    res = res.pop(0) if len(res) > 1 else res[0]
                if isinstance(res, BaseException):
                    raise res
                rc, out, err = res
                return NS(returncode=rc, stdout=out, stderr=err)
        return NS(returncode=0, stdout=b"", stderr=b"")

    def starts(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _service_docker():
    return FakeDocker({"docker inspect": (0, b"172.20.0.2\n", b""), "docker logs": (0, b"boot log", b"")})


SERVICE_STEP = {
    "type": "service", "id": "serve", "port": 8000, "health_path": "/health", "start_timeout_seconds": 30,
    "requests": [{"id": "chat", "method": "POST", "path": "/v1/chat/completions", "body": {"max_tokens": 16},
                  "expect": {"status": 200, "json": [{"path": "choices.0.message.content", "non_empty": True}]}}],
}
GOOD_BODY = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()


# ---------------------------------------------------------------------------
# service step
# ---------------------------------------------------------------------------
def test_service_step_happy_path_uses_internal_network_and_cleans_up():
    docker, clk = _service_docker(), Clock()
    seen = []

    def http(method, url, body, ctype, timeout):
        seen.append((method, url, body))
        return (200, b"ok") if url.endswith("/health") else (200, GOOD_BODY)

    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="t", run=docker, http=http, sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"serve:health": True, "serve:chat": True}
    assert seen[0][:2] == ("GET", "http://172.20.0.2:8000/health")
    assert seen[1][:2] == ("POST", "http://172.20.0.2:8000/v1/chat/completions")
    assert json.loads(seen[1][2]) == {"max_tokens": 16}
    net = docker.starts("docker", "network", "create")[0]
    assert "--internal" in net
    run = docker.starts("docker", "run")[0]
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit", "-d"):
        assert flag in run
    assert run[run.index("--user") + 1] == "65532:65532"
    assert run[run.index("--network") + 1] == "cc-smoke-net-t"
    assert run[run.index("--gpus") + 1] == "all"
    assert not any(a in ("-p", "--publish", "-P", "--privileged", "--network=host") for a in run)
    assert run[-1] == IMAGE
    assert docker.starts("docker", "rm", "-f", "cc-smoke-t") and docker.starts("docker", "network", "rm", "cc-smoke-net-t")


def test_service_step_cpu_kit_gets_no_gpus_flag():
    docker, clk = _service_docker(), Clock()
    sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=0, tag="c", run=docker, http=lambda *a: (200, GOOD_BODY),
                        sleep=clk.sleep, clock=clk)
    assert "--gpus" not in docker.starts("docker", "run")[0]
    assert "--gpus" not in sr.build_service_argv(IMAGE, "n", "net", 0)
    assert "--gpus" not in sr.build_service_argv(IMAGE, "n", "net", 1, no_gpu=True)
    assert "--gpus" in sr.build_service_argv(IMAGE, "n", "net", None)  # unknown manifest = a GPU Kit, as before


def test_service_step_times_out_when_never_healthy_and_still_cleans_up():
    docker, clk = _service_docker(), Clock()
    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="h", run=docker, http=lambda *a: (503, b""),
                              sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"serve:health": False}  # no request is sent to an unhealthy server
    assert "boot log" in res["detail"]["logs_tail"]
    assert docker.starts("docker", "rm", "-f", "cc-smoke-h") and docker.starts("docker", "network", "rm", "cc-smoke-net-h")
    assert clk.t >= 30


def test_service_step_unreachable_server_counts_as_unhealthy():
    docker, clk = _service_docker(), Clock()
    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="u", run=docker, http=lambda *a: (None, b""),
                              sleep=clk.sleep, clock=clk)
    assert res["checks"]["serve:health"] is False


def test_service_step_predicate_failure_is_reported_per_request():
    docker, clk = _service_docker(), Clock()

    def http(method, url, body, ctype, timeout):
        return (200, b"x") if url.endswith("/health") else (200, json.dumps({"choices": [{"message": {"content": ""}}]}).encode())

    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="p", run=docker, http=http, sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"serve:health": True, "serve:chat": False}
    assert "empty" in res["detail"]["failures"]["serve:chat"][0]


def test_service_step_start_failure_and_network_failure():
    docker = FakeDocker({"docker run": (125, b"", b"no such image")})
    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="s", run=docker)
    assert res["checks"] == {"serve:start": False} and "no such image" in res["detail"]["error"]
    assert docker.starts("docker", "rm", "-f", "cc-smoke-s")  # a failed run can leave a created container
    docker = FakeDocker({"docker network create": (1, b"", b"denied")})
    res = sr.run_service_step(SERVICE_STEP, IMAGE, gpu_count=1, tag="n", run=docker)
    assert res["checks"] == {"serve:network": False} and not docker.starts("docker", "run")


def test_service_step_refuses_unsafe_paths_before_touching_docker():
    for bad in ({**SERVICE_STEP, "health_path": "/../etc"},
                {**SERVICE_STEP, "requests": [{"id": "r", "method": "GET", "path": "http://evil/", "expect": {"status": 200}}]},
                {**SERVICE_STEP, "requests": [{"id": "r", "method": "DELETE", "path": "/x", "expect": {"status": 200}}]},
                {**SERVICE_STEP, "port": 0}):
        docker = FakeDocker()
        res = sr.run_service_step(bad, IMAGE, gpu_count=1, tag="b", run=docker)
        assert any(k.endswith(":spec") and v is False for k, v in res["checks"].items())
        assert docker.calls == []


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------
def test_check_response_predicates():
    ok = sr.check_response
    assert ok({}, 204, b"") == []  # default: any 2xx
    assert ok({}, 500, b"") != []
    assert ok({"status": [200, 201]}, 201, b"") == [] and ok({"status": 200}, 201, b"") != []
    assert ok({"body_contains": "abc"}, 200, b"xxabcxx") == [] and ok({"body_contains": "abc"}, 200, b"x") != []
    assert ok({"body_regex": r"^\d+$"}, 200, b"123") == [] and ok({"body_regex": r"^\d+$"}, 200, b"12a") != []
    j = json.dumps({"a": {"b": [{"c": "v"}]}, "n": 3, "e": ""}).encode()
    assert ok({"json": [{"path": "a.b.0.c", "equals": "v"}, {"path": "n", "type": "number"}]}, 200, j) == []
    assert ok({"json": [{"path": "a.b.1.c", "non_empty": True}]}, 200, j) != []  # index out of range
    assert ok({"json": [{"path": "e", "non_empty": True}]}, 200, j) != []
    assert ok({"json": [{"path": "n", "type": "string"}]}, 200, j) != []
    assert ok({"json": [{"path": "n", "equals": 4}]}, 200, j) != []
    assert ok({"json": [{"path": "n", "equals": 3}]}, 200, b"not json") == ["body is not JSON"]
    assert sr.check_text({"contains": "200"}, "HTTP/1.1 200 OK") == []
    assert sr.check_text({"regex": r"^HTTP/1\.[01] 200"}, "HTTP/1.0 200 OK\r\n") == []
    assert sr.check_text({"regex": r"^HTTP/1\.[01] 200"}, "HTTP/1.1 502") != []
    assert sr.check_text({}, "anything") != []  # a session step needs a predicate


# ---------------------------------------------------------------------------
# session step
# ---------------------------------------------------------------------------
SESSION = _manifest("notebooks")["session"]
SESSION_STEP = _smoke("notebooks")["steps"][0]


def test_session_argv_mirrors_session_agent():
    argv = sr.build_session_argv(IMAGE, "cc-smoke-x", SESSION, 1)
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert argv[argv.index("--gpus") + 1] == "all"
    i = argv.index("--entrypoint")
    assert argv[i + 1:i + 3] == ["jupyter", IMAGE] and argv[i + 3] == "lab"  # kit command, image before its arguments
    assert any(a.startswith("COMPUTECOIN_SESSION_BASE_PATH=") for a in argv)
    assert not any(a in ("-p", "--publish", "-P", "--privileged", "--network=host") for a in argv)
    assert "--gpus" not in sr.build_session_argv(IMAGE, "n", SESSION, 0)


def test_session_step_retries_until_ready_then_checks_response_and_cleans_up():
    clk = Clock()
    docker = FakeDocker({"docker exec": [(1, b"", b"connection refused"), (0, b"HTTP/1.0 200 OK\r\n\r\n{}", b"")]})
    res = sr.run_session_step(SESSION_STEP, IMAGE, SESSION, gpu_count=1, tag="a", run=docker, sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"jupyter:response": True}
    execs = docker.starts("docker", "exec")
    assert len(execs) == 2 and execs[0] == ["docker", "exec", "-i", "cc-smoke-a", "nc", "127.0.0.1", "8888"]
    assert docker.inputs[docker.calls.index(execs[0])] == SESSION_STEP["request"].encode()
    assert docker.starts("docker", "rm", "-f", "cc-smoke-a")


def test_session_step_fails_after_the_ready_timeout():
    clk = Clock()
    docker = FakeDocker({"docker exec": (1, b"", b"refused")})
    step = {**SESSION_STEP, "ready_timeout_seconds": 10}
    res = sr.run_session_step(step, IMAGE, SESSION, gpu_count=1, tag="f", run=docker, sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"jupyter:response": False} and res["detail"]["failures"]
    assert docker.starts("docker", "rm", "-f", "cc-smoke-f")


def test_session_step_accepts_partial_output_when_nc_keeps_the_socket_open():
    clk = Clock()
    exc = subprocess.TimeoutExpired(["nc"], 15, output=b"HTTP/1.1 200 OK\r\n")
    docker = FakeDocker({"docker exec": exc})
    res = sr.run_session_step(SESSION_STEP, IMAGE, SESSION, gpu_count=1, tag="t", run=docker, sleep=clk.sleep, clock=clk)
    assert res["checks"] == {"jupyter:response": True}


def test_session_step_needs_a_tcp_session_kit():
    docker = FakeDocker()
    res = sr.run_session_step(SESSION_STEP, IMAGE, {"mode": "pty"}, gpu_count=1, tag="x", run=docker)
    assert res["checks"] == {"jupyter:spec": False} and docker.calls == []


# ---------------------------------------------------------------------------
# CPU-only support and the shipped smoke specs
# ---------------------------------------------------------------------------
def test_cpu_kit_detection_and_gpu_args():
    assert sr.kit_gpu_count(_manifest("scientific-compute")) == 0
    assert sr.kit_gpu_count(_manifest("llm-serve")) == 1
    assert sr.kit_gpu_count({}) is None and sr.kit_gpu_count({"resource_defaults": {"gpu_count": True}}) is None
    assert sr._gpu_args(0, False) == [] and sr._gpu_args(None, False) == ["--gpus", "all"]
    assert sr._gpu_args(2, True) == []
    smoke_path = str(KITS / "scientific-compute" / "smoke" / "smoke.json")
    assert sr.kit_gpu_count(sr.kit_manifest_for(smoke_path)) == 0  # kit.json found next to the smoke directory
    assert sr.host_label(0, "cpu-a") == "cpu-a" and sr.host_label(0).startswith("cpu (")


def test_run_steps_dispatches_by_type_and_passes_the_kit_gpu_count():
    clk = Clock()
    docker = FakeDocker({"docker inspect": (0, b"10.0.0.5\n", b""), "docker exec": (0, b"HTTP/1.1 200 OK", b"")})
    spec = {"steps": [SERVICE_STEP, {**SESSION_STEP, "id": "sess"}, {"type": "bogus"}]}
    out = sr.run_steps(spec, IMAGE, {"resource_defaults": {"gpu_count": 0}, "session": SESSION},
                       run=docker, http=lambda *a: (200, GOOD_BODY), sleep=clk.sleep, clock=clk)
    assert out["checks"]["serve:health"] and out["checks"]["serve:chat"] and out["checks"]["sess:response"]
    assert out["checks"]["step2:type"] is False
    assert all("--gpus" not in c for c in docker.calls if c[:2] == ["docker", "run"])


@pytest.mark.parametrize("slug,kind", [("llm-serve", "service"), ("notebooks", "session")])
def test_shipped_specs_use_the_step_schema_and_pass_the_offline_gate(slug, kind):
    smoke = _smoke(slug)
    assert [s["type"] for s in smoke["steps"]] == [kind]
    assert kit_ci.check_kit(KITS / slug) == []
    if kind == "service":  # the same port and health path the llm-serve entrypoint and service_agent use
        st = smoke["steps"][0]
        assert (st["port"], st["health_path"]) == (8000, "/health")
        assert st["requests"][0]["body"]["temperature"] == 0
    else:
        assert smoke["steps"][0]["tcp_port"] == _manifest("notebooks")["session"]["tcp_port"]


def test_scientific_compute_is_checked_as_a_cpu_kit():
    smoke = _smoke("scientific-compute")
    assert "gpu_models_required" not in smoke and len(set(smoke["host_classes_required"])) >= 3
    assert all(kit_ci._is_cpu_label(x) for x in smoke["host_classes_required"])
    assert kit_ci.check_kit(KITS / "scientific-compute") == []


def test_kits_workflow_lists_the_three_kits():
    wf = (ROOT / ".github" / "workflows" / "kits.yml").read_text(encoding="utf-8")
    smoke_part = wf[wf.index("  smoke:"):wf.index("  sign-publish:")]
    sign_part = wf[wf.index("  sign-publish:"):]
    for slug in ("llm-serve", "notebooks"):
        assert slug in smoke_part and slug in sign_part
    assert "slug: scientific-compute, gpu: cpu-a" in smoke_part and "scientific-compute" in sign_part
    # scientific-compute must not also run on the GPU labels.
    slug_line = [ln for ln in smoke_part.splitlines() if ln.strip().startswith("slug: [")][0]
    assert "scientific-compute" not in slug_line


# ---------------------------------------------------------------------------
# kit_ci validation of smoke specs
# ---------------------------------------------------------------------------
def _copy_kit(tmp_path, slug, mutate):
    dst = tmp_path / slug
    shutil.copytree(KITS / slug, dst)
    path = dst / "smoke" / "smoke.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    mutate(d)
    path.write_text(json.dumps(d), encoding="utf-8")
    return kit_ci.check_kit(dst)


def _has(errs, text):
    return any(text in e for e in errs)


def test_ci_requires_a_session_step_for_a_session_kit(tmp_path):
    assert _has(_copy_kit(tmp_path, "notebooks", lambda d: d.pop("steps")), "needs a 'session' step")


def test_ci_session_step_must_match_the_manifest(tmp_path):
    def wrong_port(d):
        d["steps"][0]["tcp_port"] = 9999

    def no_expect(d):
        d["steps"][0]["expect"] = {}

    def bad_regex(d):
        d["steps"][0]["expect"] = {"regex": "("}

    assert _has(_copy_kit(tmp_path / "a", "notebooks", wrong_port), "tcp_port must equal")
    assert _has(_copy_kit(tmp_path / "b", "notebooks", no_expect), "expect needs contains")
    assert _has(_copy_kit(tmp_path / "c", "notebooks", bad_regex), "does not compile")


def test_ci_service_step_rules(tmp_path):
    def mut(fn):
        def inner(d):
            fn(d["steps"][0])
        return inner

    cases = {
        "port must be": mut(lambda s: s.__setitem__("port", 70000)),
        "health_path": mut(lambda s: s.__setitem__("health_path", "health")),
        "requests must be": mut(lambda s: s.__setitem__("requests", [])),
        "path must be a safe": mut(lambda s: s["requests"][0].__setitem__("path", "/../x")),
        "method must be": mut(lambda s: s["requests"][0].__setitem__("method", "DELETE")),
        "needs expect": mut(lambda s: s["requests"][0].__setitem__("expect", {})),
        "unknown expect key": mut(lambda s: s["requests"][0]["expect"].__setitem__("bogus", 1)),
        "timeout must be": mut(lambda s: s.__setitem__("start_timeout_seconds", 10_000)),
        "type must be one of": mut(lambda s: s.__setitem__("type", "batch")),
        "unique lowercase id": mut(lambda s: s.__setitem__("id", "Bad Id")),
    }
    for i, (needle, fn) in enumerate(cases.items()):
        assert _has(_copy_kit(tmp_path / str(i), "llm-serve", fn), needle), needle


def test_ci_steps_must_be_a_non_empty_list(tmp_path):
    assert _has(_copy_kit(tmp_path, "llm-serve", lambda d: d.__setitem__("steps", [])), "non-empty list")


def test_ci_host_classes_cpu_vs_gpu(tmp_path):
    # A CPU-only Kit cannot satisfy the gate with GPU model names, and needs three distinct cpu labels.
    def gpu_names(d):
        d.pop("host_classes_required")
        d["gpu_models_required"] = ["A", "B", "C"]

    def two_cpu(d):
        d["host_classes_required"] = ["cpu-a", "cpu-b", "cpu-a"]

    assert _has(_copy_kit(tmp_path / "a", "scientific-compute", gpu_names), "CPU host classes")
    assert _has(_copy_kit(tmp_path / "b", "scientific-compute", two_cpu), "CPU host classes")

    # A GPU Kit cannot satisfy it with cpu labels.
    def cpu_labels(d):
        d["gpu_models_required"] = ["cpu", "cpu-a", "cpu-b"]

    assert _has(_copy_kit(tmp_path / "c", "llm-serve", cpu_labels), "GPU models")


def test_cpu_label_shapes():
    for ok in ("cpu", "cpu-a", "CPU-x86-big", "cpu_b", "cpu.c"):
        assert kit_ci._is_cpu_label(ok), ok
    for bad in ("gpu-3090", "cpux", "NVIDIA A100 80GB", "xcpu", ""):
        assert not kit_ci._is_cpu_label(bad), bad


def test_ci_leaves_batch_specs_alone():
    for slug in ("ai-image", "blender-render", "ffmpeg-transcode", "upscale"):
        assert "steps" not in _smoke(slug)
        assert kit_ci.check_kit(KITS / slug) == []
    spec = copy.deepcopy(_smoke("ai-image"))
    assert kit_ci.check_smoke_steps(spec, _manifest("ai-image"), "ai-image") == []


def test_kit_ci_manifest_keys_match_the_api_validator():
    """scripts/kit_ci.py says its key list is identical to app/services/kits.py (validate_manifest): a drift makes CI
    reject manifests the API accepts (split, egress, assets, checkpoint were missing)."""
    import importlib.util
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("kit_ci", root / "scripts" / "kit_ci.py")
    kit_ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit_ci)
    src = (root / "apps/api/app/services/kits.py").read_text()
    block = re.search(r"allowed = \{([^}]*)\}", src).group(1)
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(kit_ci.ALLOWED)


def test_publish_refuses_to_send_the_admin_token_over_plain_http(monkeypatch, tmp_path):
    """The bearer token went to whatever --api-url said, http:// included, and urllib forwards it on redirects."""
    assert kit_ci._publish_url_ok("https://api.example/v1/admin/kits")
    assert kit_ci._publish_url_ok("http://localhost:8000/v1/admin/kits")
    assert not kit_ci._publish_url_ok("http://api.example/v1/admin/kits")
    assert not kit_ci._publish_url_ok("file:///etc/passwd")
    signed = tmp_path / "k.json"
    signed.write_text("{}")
    monkeypatch.setenv("KIT_ADMIN_TOKEN", "t")
    args = type("A", (), {"token_env": "KIT_ADMIN_TOKEN", "signed": str(signed), "api_url": "http://evil.example"})()
    import pytest

    with pytest.raises(SystemExit):
        kit_ci.cmd_publish(args)
