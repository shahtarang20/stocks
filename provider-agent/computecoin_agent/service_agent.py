"""Service replica runtime (U11, docs/architecture/U11-services-design.md).

A replica is an ordinary claimed job whose claim carries ``v2.service``. Instead of a batch run this module keeps the
Kit's container up and PULLS customer requests for this replica from the API (outbound only, machine-signed), forwards
each to the container on a local address, and posts the response back. The host accepts no inbound connection from the
internet; the container has no egress (internal docker network, reached by its container IP).

Fail closed: only the declared port and a validated path are ever contacted; request and response sizes are capped;
when the API says stop (draining, window end, replica removed) the container is stopped and the uptime is reported in
the normal job result (``metrics.duration_seconds``), which the existing job billing meters.
"""

from __future__ import annotations

import base64
import datetime as _dt
import re
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, Protocol

from .executor import ExecutionResult, JobSpec

_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")
_METHODS = ("GET", "POST")
HEARTBEAT_EVERY = 15.0  # seconds between job-lease heartbeats
POLL_WAIT = 10  # long-poll seconds
MAX_POLL_SILENCE = 120.0  # stop when the API has been unreachable this long
MAX_CONCURRENCY_CAP = 64  # hard ceiling of the service's max_concurrency setting
DEFAULT_REQUEST_TIMEOUT = 30.0
DRAIN_WAIT_SECONDS = 30.0  # on stop, wait this long for in-flight requests before the container is removed


def _request_timeout(req: dict[str, Any]) -> float:
    """Each request has its own timeout: the time left until its API-side deadline (1..300 s); 30 s when unknown."""
    raw = req.get("deadline_at")
    if not raw:
        return DEFAULT_REQUEST_TIMEOUT
    try:
        deadline = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=_dt.timezone.utc)
        left = (deadline - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    except ValueError:
        return DEFAULT_REQUEST_TIMEOUT
    return float(min(300.0, max(1.0, left)))


class ServiceAgentError(RuntimeError):
    pass


def safe_path(path: str) -> bool:
    return bool(_PATH_RE.fullmatch(path or "")) and ".." not in path and "//" not in path  # ``$`` would accept a trailing newline


class ServiceContainer(Protocol):
    def start(self) -> None: ...
    def request(self, method: str, path: str, content_type: str, body: bytes, timeout: float, max_bytes: int) -> tuple[int, str, bytes]: ...
    def healthy(self, path: str) -> bool: ...
    def stop(self) -> None: ...


class DockerServiceContainer:
    """Hardened container on a docker ``--internal`` network (no egress). Needs a real host to verify."""

    def __init__(self, image: str, port: int, *, network: str = "computecoin-svc-internal", gpus: int = 0, name: str = "",
                 memory_mb: int = 2048, cpus: str = "1", runner: Optional[Callable[..., Any]] = None) -> None:
        self.image, self.port, self.network, self.gpus = image, port, network, gpus
        self.name = name or f"cc-svc-{time.time_ns()}"
        self.memory_mb, self.cpus = int(memory_mb), str(cpus)
        self._run = runner or subprocess.run
        self._ip: Optional[str] = None

    def _ensure_internal_network(self) -> None:
        """The replica must have NO route out: the network is created --internal when missing, and a pre-existing
        network of that name that is not internal is refused (fail closed, never widened)."""
        probe = self._run(["docker", "network", "inspect", "-f", "{{.Internal}}", self.network],
                          capture_output=True, timeout=30, text=True)
        if probe.returncode != 0:
            self._run(["docker", "network", "create", "--internal", self.network], check=True, capture_output=True, timeout=30)
        elif (probe.stdout or "").strip().lower() != "true":
            raise ServiceAgentError(f"docker network {self.network} is not internal; refusing")

    def start(self) -> None:
        if "@sha256:" not in self.image:
            raise ServiceAgentError("service image must be pinned by digest")
        self._ensure_internal_network()
        cmd = ["docker", "run", "-d", "--name", self.name, "--network", self.network, "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--read-only",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--pids-limit", "512",
               "--memory", f"{self.memory_mb}m", "--memory-swap", f"{self.memory_mb}m", "--cpus", self.cpus,
               "--user", "65532:65532"]
        if self.gpus:
            cmd += ["--gpus", str(self.gpus)]
        cmd.append(self.image)
        self._run(cmd, check=True, capture_output=True, timeout=600)
        out = self._run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", self.name],
                        check=True, capture_output=True, timeout=30, text=True)
        self._ip = out.stdout.strip() or None
        if not self._ip:
            raise ServiceAgentError("container has no address on the internal network")

    def _client(self, timeout: float):
        import httpx

        return httpx.Client(base_url=f"http://{self._ip}:{self.port}", timeout=timeout, follow_redirects=False)

    def request(self, method: str, path: str, content_type: str, body: bytes, timeout: float, max_bytes: int) -> tuple[int, str, bytes]:
        with self._client(timeout) as c, c.stream(method, path, content=body or None,
                                                 headers={"Content-Type": content_type}) as r:
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise ServiceAgentError("response_too_large")
            return r.status_code, r.headers.get("content-type", "application/octet-stream"), bytes(data)

    def healthy(self, path: str) -> bool:
        try:
            with self._client(5.0) as c:
                return 200 <= c.get(path).status_code < 300
        except Exception:
            return False

    def stop(self) -> None:
        self._run(["docker", "rm", "-f", self.name], capture_output=True, timeout=60)


def _signed(base_url: str, machine_id: str, secret: str, path: str, body: dict[str, Any], transport: Any, timeout: float) -> Any:
    from .jobrunner import _signed_post

    return _signed_post(base_url.rstrip("/") + path, machine_id, secret, body, transport=transport, timeout=timeout)


def run_service(
    base_url: str, machine_id: str, machine_secret: str, spec: JobSpec, *,
    container: Optional[ServiceContainer] = None, transport: Any = None, timeout: float = 40.0,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> ExecutionResult:
    """Serve until the API says stop. Returns the job result carrying the metered uptime."""
    from .jobrunner import post_heartbeat

    svc = (spec.v2 or {}).get("service") or {}
    replica_id = svc.get("replica_id")
    if not replica_id or spec.isolation_tier in ("sealed", "sealed_pro", "confidential"):
        return ExecutionResult(status="failed", error={"code": "service_not_supported", "message": "invalid service claim"},
                               execution_mode="hardware")
    port = int(svc.get("port") or 8000)
    health_path = svc.get("health_path") or "/health"
    max_resp = int(svc.get("max_response_bytes") or 4_194_304)
    max_req = int(svc.get("max_request_bytes") or 1_048_576)
    try:
        max_conc = max(1, min(MAX_CONCURRENCY_CAP, int(svc.get("max_concurrency") or 1)))
    except (TypeError, ValueError):
        max_conc = 1
    if not safe_path(health_path):
        return ExecutionResult(status="failed", error={"code": "invalid_health_path", "message": "bad health path"},
                               execution_mode="hardware")
    limits = spec.limits or {}
    try:
        mem_mb, cpus = int(limits.get("memory_mb") or 2048), float(limits.get("cpus") or 1)
        if mem_mb < 64 or not 0.1 <= cpus <= 256:
            raise ValueError
    except (TypeError, ValueError):
        return ExecutionResult(status="failed", error={"code": "invalid_limits", "message": "bad service limits"},
                               execution_mode="hardware")
    box = container or DockerServiceContainer(
        spec.image_digest or "", port, gpus=int(limits.get("gpu_count") or 0),
        name=f"cc-svc-{re.sub(r'[^a-zA-Z0-9_.-]', '', str(spec.job_id))[:48]}", memory_mb=mem_mb, cpus=f"{cpus:g}")
    started = clock()
    served = 0
    served_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=max_conc, thread_name_prefix="cc-svc-req")
    inflight: set[Future] = set()
    error: Optional[dict[str, str]] = None

    def _work(req: dict[str, Any]) -> None:
        nonlocal served
        try:
            _handle(base_url, machine_id, machine_secret, box, req, max_req, max_resp, transport, timeout,
                    _request_timeout(req))
        finally:
            with served_lock:
                served += 1

    try:
        box.start()
        last_hb = last_ok = clock()
        while True:
            now = clock()
            if now - last_hb >= HEARTBEAT_EVERY:
                try:
                    post_heartbeat(base_url, machine_id, machine_secret, spec.job_id, transport=transport, timeout=timeout)
                except Exception:
                    pass
                last_hb = now
            inflight = {f for f in inflight if not f.done()}
            full = len(inflight) >= max_conc
            try:
                # While every slot is busy the poll only keeps the replica heartbeat alive (no work, no long wait).
                resp = _signed(base_url, machine_id, machine_secret, f"/v1/agents/services/replicas/{replica_id}/poll",
                               {"healthy": box.healthy(health_path), "accepting": not full,
                                "wait_seconds": 0 if inflight else POLL_WAIT}, transport, timeout)
            except Exception:
                resp = None
            if resp is None or resp.status_code >= 500:
                if clock() - last_ok > MAX_POLL_SILENCE:
                    error = {"code": "api_unreachable", "message": "control plane unreachable"}
                    break
                sleep(2.0)
                continue
            if resp.status_code in (404, 409, 401):
                error = {"code": "replica_removed", "message": "replica no longer valid"}
                break
            last_ok = clock()
            data = resp.json()
            if data.get("stop"):
                break
            req = data.get("request")
            if not req:
                if inflight:
                    sleep(0.25)  # nothing new; give running requests a moment without a busy loop
                continue
            inflight.add(pool.submit(_work, req))
    except Exception as exc:  # container start failure etc.; never leaks internals
        error = {"code": "service_error", "message": type(exc).__name__}
    finally:
        # Let in-flight requests finish (bounded) so their responses are posted before the container goes away.
        pool.shutdown(wait=False)
        drain_until = clock() + DRAIN_WAIT_SECONDS
        while any(not f.done() for f in inflight) and clock() < drain_until:
            sleep(0.2)
        try:
            box.stop()
        except Exception:
            pass
    uptime = max(0.0, clock() - started)
    metrics = {"duration_seconds": round(uptime, 3), "requests_served": served}
    if error:
        return ExecutionResult(status="failed", metrics=metrics, error=error, execution_mode="hardware")
    return ExecutionResult(status="success", output={"service": True, "requests_served": served}, metrics=metrics,
                           execution_mode="hardware", output_schema_valid=True)


def _handle(base_url, machine_id, secret, box, req, max_req, max_resp, transport, timeout, req_timeout=DEFAULT_REQUEST_TIMEOUT) -> None:
    rid = req.get("request_id")
    method, path = str(req.get("method", "")), str(req.get("path", ""))
    status, ctype, body, err = 502, None, b"", None
    try:
        raw = base64.b64decode(req.get("body_b64") or "", validate=True)
        if method not in _METHODS or not safe_path(path) or len(raw) > max_req:
            err = "invalid_request"
        else:
            status, ctype, body = box.request(method, path, str(req.get("content_type") or "application/json"), raw,
                                              req_timeout, max_resp)
    except ServiceAgentError as exc:
        err = str(exc)[:48]
    except Exception:
        err = "upstream_error"
    payload = {"status": status, "content_type": ctype, "body_b64": base64.b64encode(body).decode("ascii")}
    if err:
        payload["error_code"] = err
    try:
        _signed(base_url, machine_id, secret, f"/v1/agents/services/requests/{rid}/response", payload, transport, timeout)
    except Exception:
        pass  # the API retries or expires the request; posting twice is safe (first accepted response wins)
