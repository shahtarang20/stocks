#!/usr/bin/env python3
"""Kit smoke runner (U5.1). Runs a Kit image the way the agent does and writes a JSON report for CI.

    python kits/tools/smoke_runner.py --smoke kits/ai-image/smoke/smoke.json --image ghcr.io/org/kit@sha256:... \
        [--input-file scene=/path/scene.blend] --report report.json

Flags mirror the agent executor (ADR-0003): no network, read-only rootfs, non-root, caps dropped, tmpfs /tmp,
writable /out, read-only /in. It checks the generic contract (exit 0, output.json ok, declared outputs exist,
PNG outputs not blank). Kit-specific checks listed in smoke.json ("cross_gpu", "resume", ...) are compared across
runs by the CI report job and by the verify_plugins, not here.
Docker flags to verify against current docs on the runner (L4). Not executed by the author of this file.

Steps (kits/README.md, "Smoke test spec"): a smoke.json with a ``steps`` list does not run the batch contract. Instead:
  * ``{"type": "service", ...}``  starts the image like service_agent.DockerServiceContainer (internal docker network, no
    published port, read-only, non-root, caps dropped), waits for the health path, sends each request to the declared
    port by container IP, checks the status/body predicates and removes the container and network.
  * ``{"type": "session", ...}``  starts the image in tcp mode like session_agent.build_session_argv (no network, the
    kit's session command as the main process) and talks to ``tcp_port`` with ``docker exec -i <c> nc 127.0.0.1 <port>``.
CPU-only Kits (kit.json resource_defaults.gpu_count 0) never get a --gpus flag; the report names the CPU host class.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

SERVICE_USER = "65532:65532"  # service_agent.DockerServiceContainer
SESSION_USER = "10001:10001"
MAX_BODY_BYTES = 1_048_576
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")


def gpu_model() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30, check=False).stdout.strip().splitlines()
        return out[0] if out else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def kit_manifest_for(smoke_path: str, kit_json: Optional[str] = None) -> dict:
    """kit.json next to the smoke dir (kits/<slug>/smoke/smoke.json -> kits/<slug>/kit.json), or {} when absent."""
    p = Path(kit_json) if kit_json else Path(smoke_path).resolve().parent.parent / "kit.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def kit_gpu_count(manifest: dict) -> Optional[int]:
    """resource_defaults.gpu_count, or None when unknown (unknown is treated as a GPU Kit, as before)."""
    v = ((manifest or {}).get("resource_defaults") or {}).get("gpu_count")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def host_label(gpu_count: Optional[int], override: str = "") -> str:
    if override:
        return override
    if gpu_count == 0:
        return f"cpu ({platform.machine() or 'unknown'}, {os.cpu_count() or '?'} cores)"
    return gpu_model()


def _gpu_args(gpu_count: Optional[int], no_gpu: bool) -> list[str]:
    """``--gpus all`` only for a Kit that needs a GPU. gpu_count 0 (CPU-only Kit) or --no-gpu: no flag at all."""
    return [] if no_gpu or gpu_count == 0 else ["--gpus", "all"]


# ---- predicates -----------------------------------------------------------------------------------------------

def _json_path(doc: Any, path: str) -> tuple[bool, Any]:
    cur = doc
    for part in [p for p in path.split(".") if p != ""]:
        if isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def check_response(expect: dict, status: Optional[int], body: bytes) -> list[str]:
    """Failures of one HTTP response against a step's ``expect`` block ([] = all predicates hold).

    Predicates: ``status`` (int or list of ints; default: any 2xx), ``body_contains`` (str), ``body_regex`` (str),
    ``json`` (list of {path, equals | non_empty | type}; path is dot separated, list indexes are digits).
    """
    fails: list[str] = []
    want = (expect or {}).get("status")
    if want is None:
        if status is None or not 200 <= status < 300:
            fails.append(f"status {status} is not 2xx")
    else:
        allowed = want if isinstance(want, list) else [want]
        if status not in allowed:
            fails.append(f"status {status} not in {allowed}")
    text = body.decode("utf-8", "replace")
    needle = (expect or {}).get("body_contains")
    if needle is not None and needle not in text:
        fails.append(f"body does not contain {needle!r}")
    rx = (expect or {}).get("body_regex")
    if rx is not None and not re.search(rx, text, re.S):
        fails.append(f"body does not match {rx!r}")
    rules = (expect or {}).get("json") or []
    if rules:
        try:
            doc = json.loads(text)
        except ValueError:
            return fails + ["body is not JSON"]
        for r in rules:
            found, val = _json_path(doc, str(r.get("path", "")))
            if not found:
                fails.append(f"json path {r.get('path')!r} missing")
            elif "equals" in r and val != r["equals"]:
                fails.append(f"json {r.get('path')!r} != {r['equals']!r}")
            elif r.get("non_empty") and (val is None or val == "" or val == [] or val == {}):
                fails.append(f"json {r.get('path')!r} is empty")
            elif "type" in r and type(val).__name__ != {"string": "str", "number": "float", "integer": "int", "array": "list",
                                                        "object": "dict", "boolean": "bool"}.get(r["type"], r["type"]):
                if not (r["type"] == "number" and isinstance(val, int) and not isinstance(val, bool)):
                    fails.append(f"json {r.get('path')!r} is not {r['type']}")
    return fails


def check_text(expect: dict, text: str) -> list[str]:
    """Session response predicates: ``contains`` (str) and ``regex`` (str). At least one is required."""
    fails: list[str] = []
    if not (expect or {}).get("contains") and not (expect or {}).get("regex"):
        return ["session step needs expect.contains or expect.regex"]
    c = (expect or {}).get("contains")
    if c is not None and c not in text:
        fails.append(f"response does not contain {c!r}")
    rx = (expect or {}).get("regex")
    if rx is not None and not re.search(rx, text, re.S):
        fails.append(f"response does not match {rx!r}")
    return fails


# ---- service step ---------------------------------------------------------------------------------------------

def build_service_argv(image: str, name: str, network: str, gpu_count: Optional[int], no_gpu: bool = False) -> list[str]:
    """The same flags as service_agent.DockerServiceContainer.start (detached, internal network, nothing published)."""
    argv = ["docker", "run", "-d", "--name", name, "--network", network, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp", "--pids-limit", "512",
            "--user", SERVICE_USER, *_gpu_args(gpu_count, no_gpu)]
    argv.append(image)
    return argv


def _http(method: str, url: str, body: bytes, content_type: str, timeout: float) -> tuple[Optional[int], bytes]:
    """One request. Returns (status or None when unreachable, body capped at MAX_BODY_BYTES). Never follows redirects."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    req = urllib.request.Request(url, data=body or None, method=method, headers={"Content-Type": content_type})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return None, b""


def _request_body(req: dict) -> tuple[bytes, str]:
    ctype = str(req.get("content_type") or "application/json")
    if "body" not in req or req["body"] is None:
        return b"", ctype
    if isinstance(req["body"], str):
        return req["body"].encode("utf-8"), ctype
    return json.dumps(req["body"]).encode("utf-8"), ctype


def run_service_step(
    step: dict, image: str, *, gpu_count: Optional[int], no_gpu: bool = False, tag: str = "svc",
    run: Callable[..., Any] = subprocess.run, http: Callable[..., tuple[Optional[int], bytes]] = _http,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Start, wait for health, send the requests, stop. Returns {"checks": {id: bool}, "detail": {...}}.

    Always removes the container and the network (even on failure). ``run``/``http``/``sleep``/``clock`` are injectable
    so the step can be unit tested without docker.
    """
    sid = str(step.get("id") or "service")
    port = 8000 if step.get("port") is None else int(step["port"])  # an explicit 0 is invalid, not "default"
    health = str(step.get("health_path") or "/health")
    name, network = f"cc-smoke-{tag}", f"cc-smoke-net-{tag}"
    checks: dict[str, Any] = {}
    detail: dict[str, Any] = {"step": sid}
    if not 1 <= port <= 65535 or not _PATH_RE.match(health) or ".." in health:
        return {"checks": {f"{sid}:spec": False}, "detail": {**detail, "error": "invalid port or health_path"}}
    for req in step.get("requests") or []:
        if not _PATH_RE.match(str(req.get("path", ""))) or str(req.get("method", "GET")).upper() not in ("GET", "POST"):
            return {"checks": {f"{sid}:spec": False}, "detail": {**detail, "error": f"invalid request {req.get('id')}"}}
    created_net = created_ctr = False
    try:
        net = run(["docker", "network", "create", "--internal", network], capture_output=True, timeout=60, check=False)
        if net.returncode != 0:
            return {"checks": {f"{sid}:network": False}, "detail": {**detail, "error": "network create failed"}}
        created_net = True
        t_start = clock()
        started = run(build_service_argv(image, name, network, gpu_count, no_gpu), capture_output=True, timeout=600, check=False)
        created_ctr = True  # a failed run can still leave a created container; removal below is unconditional
        if started.returncode != 0:
            checks[f"{sid}:start"] = False
            detail["error"] = (started.stderr or b"")[-500:].decode("utf-8", "replace") if isinstance(started.stderr, bytes) else str(started.stderr)[-500:]
            return {"checks": checks, "detail": detail}
        insp = run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                   capture_output=True, timeout=30, check=False)
        ip = (insp.stdout.decode() if isinstance(insp.stdout, bytes) else str(insp.stdout or "")).strip()
        if insp.returncode != 0 or not ip:
            checks[f"{sid}:address"] = False
            return {"checks": checks, "detail": {**detail, "error": "container has no address on the internal network"}}
        base = f"http://{ip}:{port}"
        deadline = t_start + float(step.get("start_timeout_seconds") or 600)
        healthy = False
        while clock() < deadline:
            status, _ = http("GET", base + health, b"", "application/json", 5.0)
            if status is not None and 200 <= status < 300:
                healthy = True
                break
            sleep(2.0)
        checks[f"{sid}:health"] = healthy
        detail["seconds_to_healthy"] = round(clock() - t_start, 1)
        if not healthy:
            logs = run(["docker", "logs", "--tail", "40", name], capture_output=True, timeout=30, check=False)
            detail["logs_tail"] = (logs.stdout or b"")[-800:].decode("utf-8", "replace") if isinstance(logs.stdout, bytes) else ""
            return {"checks": checks, "detail": detail}
        for req in step.get("requests") or []:
            rid = f"{sid}:{req.get('id') or req.get('path')}"
            body, ctype = _request_body(req)
            status, data = http(str(req.get("method", "GET")).upper(), base + str(req["path"]), body, ctype,
                                float(req.get("timeout_seconds") or 60))
            fails = check_response(req.get("expect") or {}, status, data)
            checks[rid] = not fails
            if fails:
                detail.setdefault("failures", {})[rid] = fails
        return {"checks": checks, "detail": detail}
    finally:
        if created_ctr:
            run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
        if created_net:
            run(["docker", "network", "rm", network], capture_output=True, timeout=60, check=False)


# ---- session step ---------------------------------------------------------------------------------------------

def build_session_argv(image: str, name: str, session: dict, gpu_count: Optional[int], no_gpu: bool = False) -> list[str]:
    """session_agent.build_session_argv for mode tcp: detached, NO network, read-only, non-root, the kit's session
    command as the main process (``--entrypoint cmd[0] image cmd[1:]``), base path env set."""
    argv = ["docker", "run", "-d", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", SESSION_USER, "--pids-limit", "512",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=2g", *_gpu_args(gpu_count, no_gpu),
            "-e", "COMPUTECOIN_SESSION_BASE_PATH=/dashboard/sessions/smoke/proxy/"]  # session_agent.SESSION_BASE_PATH shape
    cmd = session.get("command")
    if cmd:
        argv += ["--entrypoint", str(cmd[0]), image, *[str(c) for c in cmd[1:]]]
    else:
        argv.append(image)
    return argv


def run_session_step(
    step: dict, image: str, session: dict, *, gpu_count: Optional[int], no_gpu: bool = False, tag: str = "sess",
    run: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Start in tcp mode, connect with ``docker exec -i <c> nc 127.0.0.1 <tcp_port>``, send ``request`` and check the
    response. Retries until ``ready_timeout_seconds``. Always removes the container."""
    sid = str(step.get("id") or "session")
    port = int(step.get("tcp_port") or session.get("tcp_port") or 0)
    detail: dict[str, Any] = {"step": sid}
    if session.get("mode") != "tcp" or not 1 <= port <= 65535 or not isinstance(step.get("request"), str) or not step["request"]:
        return {"checks": {f"{sid}:spec": False}, "detail": {**detail, "error": "needs a tcp session kit, tcp_port and request"}}
    name = f"cc-smoke-{tag}"
    created = False
    try:
        t_start = clock()
        started = run(build_session_argv(image, name, session, gpu_count, no_gpu), capture_output=True, timeout=600, check=False)
        created = True
        if started.returncode != 0:
            return {"checks": {f"{sid}:start": False},
                    "detail": {**detail, "error": ((started.stderr or b"")[-500:].decode("utf-8", "replace")
                                                   if isinstance(started.stderr, bytes) else "start failed")}}
        deadline = t_start + float(step.get("ready_timeout_seconds") or 120)
        fails: list[str] = ["not attempted"]
        while clock() < deadline:
            try:
                proc = run(["docker", "exec", "-i", name, "nc", "127.0.0.1", str(port)],
                           input=step["request"].encode("utf-8"), capture_output=True, timeout=15, check=False)
                out = proc.stdout if isinstance(proc.stdout, bytes) else b""
            except subprocess.TimeoutExpired as exc:  # nc keeps the socket open: what arrived so far is the response
                out = exc.stdout if isinstance(exc.stdout, bytes) else b""
            text = out[:MAX_BODY_BYTES].decode("utf-8", "replace")
            fails = check_text(step.get("expect") or {}, text)
            if not fails:
                break
            sleep(2.0)
        detail["seconds_to_ready"] = round(clock() - t_start, 1)
        if fails:
            detail["failures"] = fails
        return {"checks": {f"{sid}:response": not fails}, "detail": detail}
    finally:
        if created:
            run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


def run_steps(spec: dict, image: str, manifest: dict, *, no_gpu: bool = False, **inject: Any) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    details: list[dict] = []
    gpus = kit_gpu_count(manifest)
    for i, step in enumerate(spec.get("steps") or []):
        kind = step.get("type")
        tag = f"{os.getpid()}-{i}"
        if kind == "service":
            res = run_service_step(step, image, gpu_count=gpus, no_gpu=no_gpu, tag=tag, **inject)
        elif kind == "session":
            inj = {k: v for k, v in inject.items() if k != "http"}
            res = run_session_step(step, image, (manifest or {}).get("session") or {}, gpu_count=gpus, no_gpu=no_gpu, tag=tag, **inj)
        else:
            res = {"checks": {f"step{i}:type": False}, "detail": {"error": f"unknown step type {kind!r}"}}
        checks.update(res["checks"])
        details.append(res["detail"])
    return {"checks": checks, "details": details}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--input-file", action="append", default=[], help="name=path, mounted read-only at /in/<name>")
    ap.add_argument("--report", required=True)
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--kit-json", default=None, help="manifest (default: kit.json next to the smoke directory)")
    ap.add_argument("--host-class", default="", help="label recorded in the report, e.g. cpu-a (default: detected)")
    a = ap.parse_args()
    if "@sha256:" not in a.image or a.image.endswith(":PENDING"):
        sys.exit("image must be pinned by a real digest")
    spec = json.loads(Path(a.smoke).read_text(encoding="utf-8"))
    manifest = kit_manifest_for(a.smoke, a.kit_json)
    gpus = kit_gpu_count(manifest)
    report: dict = {"kit": spec["kit"], "image": a.image, "gpu": host_label(gpus, a.host_class), "checks": {}}
    if spec.get("steps"):  # service / session steps: the image is long-running, there is no batch contract to check
        t0 = time.time()
        res = run_steps(spec, a.image, manifest, no_gpu=a.no_gpu)
        report["seconds"] = round(time.time() - t0, 1)
        report["checks"], report["steps"] = res["checks"], res["details"]
        ok = bool(report["checks"]) and all(v is True for v in report["checks"].values())
        report["passed"] = ok
        Path(a.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"kit": report["kit"], "gpu": report["gpu"], "passed": ok}))
        return 0 if ok else 1
    with tempfile.TemporaryDirectory(prefix="kit-smoke-") as tmp:
        out, ckpt = Path(tmp, "out"), Path(tmp, "ckpt")
        out.mkdir(), ckpt.mkdir()
        os.chmod(out, 0o777), os.chmod(ckpt, 0o777)
        (out / "input.json").write_text(json.dumps(spec["input"]), encoding="utf-8")
        argv = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "10001:10001", "--pids-limit", "512",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=2g",
                "--mount", f"type=bind,source={out},target=/out", "--mount", f"type=bind,source={ckpt},target=/ckpt",
                "-e", "COMPUTECOIN_INPUT=/out/input.json", "-e", "COMPUTECOIN_OUTPUT=/out/output.json",
                "-e", "COMPUTECOIN_CHECKPOINT_DIR=/ckpt", "-e", "COMPUTECOIN_JOB_ID=smoke"]
        for item in a.input_file:
            name, path = item.split("=", 1)
            argv += ["--mount", f"type=bind,source={os.path.abspath(path)},target=/in/{name},readonly"]
        argv += _gpu_args(gpus, a.no_gpu)  # no --gpus for a CPU-only Kit (gpu_count 0)
        argv.append(a.image)
        t0 = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=spec["limits"]["max_seconds"], check=False)
            code, tail = proc.returncode, (proc.stderr or b"")[-1500:].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            code, tail = -1, "timeout"
        report["seconds"] = round(time.time() - t0, 1)
        report["checks"]["exit_zero"] = code == 0
        result = {}
        try:
            result = json.loads((out / "output.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        report["checks"]["output_ok"] = bool(result.get("ok"))
        files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and p.name not in ("input.json", "output.json"))
        report["checks"]["has_outputs"] = bool(files)
        report["outputs"] = files[:200]
        blank = []
        try:
            from PIL import Image, ImageStat
            for f in files:
                if f.lower().endswith(".png"):
                    with Image.open(out / f) as im:
                        if max(ImageStat.Stat(im.convert("RGB")).stddev) < 3.0:
                            blank.append(f)
        except ImportError:
            report["checks"]["png_not_blank"] = None
        else:
            report["checks"]["png_not_blank"] = not blank
        report["stderr_tail"] = tail if code != 0 else ""
        report["result"] = {k: v for k, v in result.items() if k not in ("images", "frames")}
    ok = all(v is not False for v in report["checks"].values()) and report["checks"]["exit_zero"]
    report["passed"] = ok
    Path(a.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"kit": report["kit"], "gpu": report["gpu"], "passed": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
