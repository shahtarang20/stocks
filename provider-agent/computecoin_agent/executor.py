"""Job executors for the ComputeCoin provider agent (Phase 4).

HARD ISOLATION RULES (enforced here + documented in ADR-0003 and
docs/api/phase4-execution-protocol.md). Customer workloads run ONLY through one
of the approved Executors defined in this module. There is NO arbitrary-shell
path, and the API process never executes customer code.

SimulatedExecutor
-----------------
* CPU-only dev executor. The DEFAULT whenever ``image_digest`` is null (no pinned
  image) or no Docker+GPU hardware is available.
* Runs a DETERMINISTIC, pure in-process transform. It performs NO network I/O and
  NO filesystem escape: any bytes it materializes are written under a caller-owned
  temp dir and nowhere else.
* Every output it produces is stamped ``execution_mode="simulated"`` and
  ``simulated=true``. It is NEVER presented as real model inference.

DockerExecutor
--------------
* Used ONLY when a pinned ``image_digest`` is supplied AND docker (and, for GPU
  jobs, the NVIDIA toolkit) is actually available on the host.
* Builds a HARDENED ``docker run`` argv:
    - image pinned by ``@sha256:`` digest (never a mutable tag);
    - ``--network none`` by default. A job that declares a validated
      ``network_policy`` ({"egress": ["host:port", ...]}, B5) instead runs on a
      per-job ``--internal`` docker network (no route out) next to a pinned egress
      proxy (``egress_proxy_image``, @sha256: digest, configurable) that is the only
      thing with a route out and allows ONLY the listed host:port (HTTPS via CONNECT);
      the job gets HTTPS_PROXY/HTTP_PROXY. Proxy and network are removed when the job
      ends and denied attempts (proxy log lines containing "DENIED") go into the job
      log. Privacy-tier jobs never get network. If the proxy image is not configured
      the job is refused, never run with wider network;
    - ``--read-only`` root filesystem PLUS a dedicated writable ``--tmpfs`` and a
      per-job output dir that is NOT a bind of any host-sensitive path;
    - ``--cap-drop ALL``;
    - ``--user`` non-root;
    - ``--pids-limit`` to bound fork bombs;
    - ``--memory`` and ``--cpus`` from the job limits;
    - ``--gpus`` ONLY when ``gpu_count > 0`` and the NVIDIA toolkit is present;
    - a hard wall-clock ``timeout``;
    - NO docker socket, NO host filesystem mount, NO host credentials, NO
      ``--privileged``.
* The subprocess runner is INJECTABLE so tests never need real docker.
* If docker (or the GPU toolkit for a GPU job) is unavailable, ``run`` raises
  :class:`HardwareUnavailableError` -- it does NOT silently simulate. A hardware
  job is never downgraded to simulation; only a job the control plane already
  marked simulated runs on the SimulatedExecutor.

Ordinary containers are NOT claimed to be a complete isolation guarantee (see
ADR-0003): the hardening above is defence-in-depth, not a sandbox escape proof.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def check_multinode_firewall(network: str, runner: Optional[Callable[[list[str]], str]] = None) -> Optional[str]:
    """U13 runtime self-check, the agent-side equivalent of ``scripts/setup-multinode-net.sh --verify``: the group network
    must exist with Internal=false AND the firewall rule marker ``computecoin-multinode:<network>`` must be present in
    nftables or the iptables DOCKER-USER chain. Returns None when fine, else a short reason. Any failure to inspect is a
    failure (fail closed). ``runner(argv) -> stdout`` is injectable for tests; it raises on a non-zero exit."""
    def _run(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True).stdout

    run = runner or _run
    try:
        internal = run(["docker", "network", "inspect", "-f", "{{.Internal}}", network]).strip().lower()
    except Exception as exc:  # noqa: BLE001
        return f"cannot inspect docker network {network}: {type(exc).__name__}"
    if internal != "false":
        return f"docker network {network} is internal (or unknown state {internal!r})"
    marker = f"computecoin-multinode:{network}"
    for argv in (["nft", "list", "ruleset"], ["iptables", "-w", "-S", "DOCKER-USER"]):
        try:
            # whole-marker match: the marker of network "a" must not be satisfied by the rules of network "ab"
            if re.search(re.escape(marker) + r"(?![A-Za-z0-9_.-])", run(argv)):
                return None
        except Exception:  # noqa: BLE001 - try the next backend
            continue
    return f"firewall rule marker {marker} not found in nftables or DOCKER-USER"


class ExecutorError(RuntimeError):
    """Base class for executor failures."""


class HardwareUnavailableError(ExecutorError):
    """Raised when a hardware (Docker) job is requested but docker / the GPU
    toolkit is unavailable. The agent must NOT silently simulate a hardware job;
    it surfaces this instead so the job is reported ``failed`` (or requeued)."""


class IsolationUnavailableError(HardwareUnavailableError):
    """Raised when a specific isolation tier (gVisor, microVM, confidential)
    is requested by a job but the required runtime/hardware is unavailable on the host.
    Per ADR-0003, workloads must NOT be silently downgraded to weaker isolation."""


# --------------------------------------------------------------------------- #
# Result / artifact value objects
# --------------------------------------------------------------------------- #
@dataclass
class Artifact:
    """A produced artifact, ready for upload via the signed grant."""

    kind: str
    data: bytes
    content_type: str = "application/json"
    filename: str = "output.json"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)

    def descriptor(self, artifact_id: Optional[str] = None) -> dict[str, Any]:
        d = {
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
        }
        if artifact_id is not None:
            d["artifact_id"] = artifact_id
        return d


@dataclass
class ExecutionResult:
    status: str  # "success" | "failed"
    output: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    execution_mode: str = "simulated"
    output_schema_valid: bool = False
    error: Optional[dict[str, str]] = None
    encrypted_output: Optional[dict[str, Any]] = None
    output_commitment: Optional[str] = None
    wipe_receipt: Optional[dict[str, Any]] = None


# --------------------------------------------------------------------------- #
# Job spec helper
# --------------------------------------------------------------------------- #
@dataclass
class JobSpec:
    """Normalized view of a claimed job (from the claim response)."""

    job_id: str
    workload_type: str
    model: str
    input: dict[str, Any]
    execution_mode: str = "simulated"
    image_digest: Optional[str] = None
    limits: dict[str, Any] = field(default_factory=dict)
    isolation_tier: str = "container"
    encrypted_input: Optional[dict[str, Any]] = None
    result_public_key: Optional[str] = None
    stream_sealed: bool = False  # U18: seal each streamed chunk to result_public_key (private chat stream)
    # U18: a private fan-out part run by private_kit.py. Only this flag lets a privacy tier use the read-only input
    # mounts, and they are RAM-backed; the network is still refused for every privacy tier.
    private_kit: bool = False
    # Set by the private-job flow after the keys are fetched for a job-bound verdict (never from the claim).
    wrapped_shares: Optional[list[dict[str, Any]]] = None
    verdict_id: Optional[str] = None
    # B5: {"egress": ["host:port", ...]} or None (= no network).
    network_policy: Optional[dict[str, Any]] = None
    # U3 (agent runtime v2). All empty/None for a v1 job, which then runs exactly as before.
    # v2: the claim's plan; mounts: [{"source","target","readonly"}] staged by runtime_v2;
    # extra_env: environment the runtime adds (input/cache/checkpoint dirs, resume flag).
    v2: Optional[dict[str, Any]] = None
    mounts: list[dict[str, Any]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    output_dir: Optional[str] = None  # v2: the host dir bound at /out (runtime_v2 collects outputs from it)
    # U13: the claim's multi-node rendezvous block (None = single node); gpu_device_ids is set by the executor's allocator.
    rendezvous: Optional[dict[str, Any]] = None
    gpu_device_ids: Optional[list[int]] = None
    # Called by the executor with the host output dir once it exists (JobMonitor tails stream/progress files there).
    on_output_dir: Optional[Callable[[str], None]] = None
    # U13: set by the job monitor when the API reports the job cancelled; the executor then stops the container.
    cancel_event: Optional[threading.Event] = None

    @classmethod
    def from_claim(cls, data: dict[str, Any]) -> "JobSpec":
        return cls(
            job_id=data["job_id"],
            workload_type=data["workload_type"],
            model=data["model"],
            input=data.get("input") or {},
            execution_mode=data.get("execution_mode", "simulated"),
            image_digest=data.get("image_digest"),
            limits=data.get("limits") or {},
            isolation_tier=data.get("isolation_tier") or "container",
            encrypted_input=data.get("encrypted_input"),
            result_public_key=data.get("result_public_key"),
            stream_sealed=bool(data.get("stream_sealed")),
            network_policy=data.get("network_policy"),
            v2=data.get("v2") if isinstance(data.get("v2"), dict) else None,
            rendezvous=data.get("rendezvous") if isinstance(data.get("rendezvous"), dict) else None,
        )


# --------------------------------------------------------------------------- #
# Abstract interface
# --------------------------------------------------------------------------- #
class Executor(abc.ABC):
    """Abstract executor: ``run(job_spec) -> ExecutionResult``."""

    @abc.abstractmethod
    def run(self, job_spec: JobSpec) -> ExecutionResult:  # pragma: no cover - abstract
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Output-schema validation (what the server also re-checks in VERIFYING)
# --------------------------------------------------------------------------- #
def validate_output(workload_type: str, output: dict[str, Any]) -> bool:
    if workload_type == "text-generation":
        return isinstance(output.get("text"), str)
    if workload_type == "embeddings":
        vectors = output.get("embeddings")
        return (
            isinstance(vectors, list)
            and len(vectors) > 0
            and all(
                isinstance(v, list)
                and len(v) > 0
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
                for v in vectors
            )
        )
    if workload_type == "batch-inference":
        items = output.get("items")
        if not isinstance(items, list):
            return False
        if output.get("count") != len(items):
            return False
        return all(
            isinstance(it, dict) and "index" in it and "status" in it for it in items
        )
    return False


# --------------------------------------------------------------------------- #
# SimulatedExecutor
# --------------------------------------------------------------------------- #
def _deterministic_text_transform(prompt: str) -> str:
    """A clearly-fake, DETERMINISTIC transform. NOT real inference.

    The transform is a pure function of the prompt: it echoes the prompt and
    appends its reversal plus an unambiguous ``[simulated]`` annotation, so the
    output can never be mistaken for a real model completion.
    """
    reversed_prompt = prompt[::-1]
    return f"[simulated] echo: {prompt} | reversed: {reversed_prompt}"


class SimulatedExecutor(Executor):
    """In-process, deterministic, no-network, no-FS-escape executor (dev).

    Produces outputs stamped ``simulated=true`` / ``execution_mode="simulated"``.
    A small output-JSON artifact is produced for upload. Wall-clock duration is
    measured for real; no metrics are fabricated.
    """

    execution_mode = "simulated"

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    def run(self, job_spec: JobSpec) -> ExecutionResult:
        start = self._clock()
        logs = [f"[simulated] starting {job_spec.workload_type} job {job_spec.job_id}"]
        try:
            if job_spec.workload_type == "text-generation":
                output = self._text_generation(job_spec)
            elif job_spec.workload_type == "batch-inference":
                output = self._batch_inference(job_spec)
            else:
                raise ExecutorError(
                    f"unsupported workload_type: {job_spec.workload_type!r}"
                )
        except ExecutorError as exc:
            duration = max(0.0, self._clock() - start)
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={
                    "duration_seconds": round(duration, 6),
                    "max_memory_mb": None,
                    "gpu_util_pct": None,
                },
                logs=logs + [f"[simulated] error: {exc}"],
                output_schema_valid=False,
                error={"code": "unsupported_workload", "message": str(exc)},
            )

        duration = max(0.0, self._clock() - start)
        logs.append(f"[simulated] completed in {duration:.6f}s")

        artifact = Artifact(
            kind="output",
            data=json.dumps(output, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            filename="output.json",
        )
        schema_ok = validate_output(job_spec.workload_type, output)
        return ExecutionResult(
            status="success",
            output=output,
            execution_mode=self.execution_mode,
            metrics={
                "duration_seconds": round(duration, 6),
                "max_memory_mb": None,
                "gpu_util_pct": None,  # simulated: no GPU used, honestly null
            },
            logs=logs,
            artifacts=[artifact],
            output_schema_valid=schema_ok,
        )

    def _text_generation(self, job_spec: JobSpec) -> dict[str, Any]:
        prompt = job_spec.input.get("prompt", "")
        if not isinstance(prompt, str):
            raise ExecutorError("text-generation input.prompt must be a string")
        text = _deterministic_text_transform(prompt)
        # A deterministic, non-fabricated "token" count (whitespace words).
        tokens = len(text.split())
        return {
            "text": text,
            "model": job_spec.model,
            "execution_mode": "simulated",
            "simulated": True,
            "tokens": tokens,
            "isolation_tier": job_spec.isolation_tier,
        }

    def _batch_inference(self, job_spec: JobSpec) -> dict[str, Any]:
        items_in = job_spec.input.get("items")
        if not isinstance(items_in, list):
            raise ExecutorError("batch-inference input.items must be a list")
        items_out = []
        for i, item in enumerate(items_in):
            # Deterministic per-item transform (pure function of the item).
            digest = hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            items_out.append(
                {
                    "index": i,
                    "status": "ok",
                    "result": f"[simulated] sha256={digest[:16]}",
                }
            )
        return {
            "items": items_out,
            "count": len(items_out),
            "model": job_spec.model,
            "execution_mode": "simulated",
            "simulated": True,
            "isolation_tier": job_spec.isolation_tier,
        }


# --------------------------------------------------------------------------- #
# DockerExecutor
# --------------------------------------------------------------------------- #
def _default_docker_available() -> bool:
    """Best-effort probe: is a usable docker CLI present?"""
    from shutil import which

    return which("docker") is not None


def _default_nvidia_available() -> bool:
    from shutil import which

    return which("nvidia-smi") is not None or which("nvidia-container-runtime") is not None


def _default_amd_available() -> bool:
    """Best-effort probe: is AMD ROCm compute available (/dev/kfd and docker)?"""
    from shutil import which

    return os.path.exists("/dev/kfd") and which("docker") is not None


def _device_gids(paths: list[str]) -> list[int]:
    """Groups that own the device files: exactly what the container needs to open them
    (render for renderD*, render or video for /dev/kfd, depending on the distribution)."""
    gids: list[int] = []
    for path in paths:
        try:
            gid = os.stat(path).st_gid
        except OSError as exc:
            raise HardwareUnavailableError(f"AMD device {path} is not accessible ({exc})") from exc
        if gid not in gids:
            gids.append(gid)
    return gids


def build_gpu_argv(
    gpu_vendor: str,
    gpu_count: int,
    *,
    render_nodes: Optional[list[str]] = None,
    render_gid: Optional[int] = None,
    video_gid: Optional[int] = None,
) -> list[str]:
    """Docker GPU argv for the vendor.

    - NVIDIA: byte-identical ``['--gpus', str(gpu_count)]``.
    - AMD (D6): ``/dev/kfd`` plus ONLY the render nodes of the GPUs given (from detection, which
      resolves each GPU's own node), and the numeric GIDs owning those devices. Never the whole
      /dev/dri, never a guessed node, never --privileged / --ipc=host / seccomp=unconfined /
      --cap-add. Fewer known nodes than GPUs requested is refused, not padded.
    """
    vendor = (gpu_vendor or "nvidia").lower().strip()
    if vendor == "nvidia":
        return ["--gpus", str(gpu_count)]
    if vendor != "amd":
        raise ExecutorError(f"Unsupported gpu_vendor: {gpu_vendor!r}")

    nodes = list((render_nodes or [])[:gpu_count])
    if len(nodes) < gpu_count:
        raise HardwareUnavailableError(
            f"AMD GPU job needs {gpu_count} GPU(s) but only {len(nodes)} render node(s) are known"
        )
    argv = ["--device", "/dev/kfd"]
    for node in nodes:
        argv.extend(["--device", node])
    if render_gid is not None or video_gid is not None:
        gids = [g for g in (render_gid, video_gid) if g is not None]
    else:
        gids = _device_gids(["/dev/kfd", *nodes])
    for gid in gids:
        argv.extend(["--group-add", str(gid)])
    return argv


@dataclass
class DockerRunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# A runner takes the argv and the output directory and returns a DockerRunResult.
DockerRunner = Callable[[list[str], str], DockerRunResult]


def _subprocess_runner(argv: list[str], output_dir: str) -> DockerRunResult:  # pragma: no cover - needs docker
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    return DockerRunResult(
        returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


_FORBIDDEN_TARGET_PREFIXES = ("/proc", "/sys", "/dev", "/etc", "/out", "/tmp", "/run", "/var/run", "/root", "/boot")


def check_mount_target(target: str) -> None:
    """A container path a v2 mount may use: absolute, normalised, no dots, never a system or output path."""
    parts = target.split("/")
    if (
        not target.startswith("/")
        or target == "/"
        or any(p in ("..", ".") for p in parts)
        or "//" in target
        or any(c in target for c in (",", "\n", "\x00", "="))
        or any(target == f or target.startswith(f + "/") for f in _FORBIDDEN_TARGET_PREFIXES)
    ):
        raise ExecutorError(f"unsafe container mount target: {target!r}")


class DockerExecutor(Executor):
    """Hardened container executor (only with a pinned image digest + docker).

    The ``runner`` is injectable; the default shells out to ``docker`` via
    subprocess. ``output_root`` is the base dir under which a per-job writable
    output dir is created (default: a fresh temp dir). The container writes its
    result JSON to ``/out/output.json`` inside the dedicated output dir, which the
    executor reads back after the run.
    """

    execution_mode = "hardware"

    # Defaults; limits from the job override memory/cpus/pids/timeout.
    DEFAULT_PIDS_LIMIT = 256
    DEFAULT_TIMEOUT_SECONDS = 300
    DEFAULT_TMPFS_SIZE = "64m"
    CONTAINER_OUT = "/out"
    CONTAINER_TMP = "/tmp"
    NONROOT_USER = "65534:65534"  # nobody:nogroup

    def __init__(
        self,
        *,
        runner: DockerRunner = _subprocess_runner,
        docker_available: Callable[[], bool] = _default_docker_available,
        nvidia_available: Callable[[], bool] = _default_nvidia_available,
        amd_available: Optional[Callable[[], bool]] = None,
        kfd_available: Optional[Callable[[], bool]] = None,
        gvisor_available: Optional[Callable[[], bool]] = None,
        microvm_available: Optional[Callable[[], bool]] = None,
        confidential_available: Optional[Callable[[], bool]] = None,
        gpu_vendor: Optional[str] = None,
        render_nodes: Optional[list[str]] = None,
        render_gid: Optional[int] = None,
        video_gid: Optional[int] = None,
        amdsmi_module: Any = None,
        nvml_module: Any = None,
        output_root: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        egress_proxy_image: Optional[str] = None,
        thermal_guard: Any = None,
        thermal_poll_seconds: float = 2.0,
        gpu_allocator: Any = None,  # U13: GpuAllocator for this host's NVIDIA GPUs (None = whole-count --gpus as before)
        multinode_network: Optional[str] = None,  # U13: operator-created internal docker network for multi-node jobs
        cluster_addr: Optional[str] = None,  # U13: this host's private cluster address (rank 0 publishes on it ONLY)
        multinode_network_firewalled: bool = False,  # U13: operator attestation (agent config), default refuse
        multinode_firewall_check: Optional[Callable[[str], Optional[str]]] = None,  # runtime self-check, None = ok
    ) -> None:
        from .isolation import (
            probe_confidential_computing,
            probe_gvisor,
            probe_microvm,
        )

        self._runner = runner
        self._docker_available = docker_available
        self._nvidia_available = nvidia_available
        self._amd_available = amd_available or _default_amd_available
        self._kfd_available = kfd_available or (lambda: os.path.exists("/dev/kfd"))
        self._gvisor_available = gvisor_available or probe_gvisor
        self._microvm_available = microvm_available or probe_microvm
        self._confidential_available = confidential_available or probe_confidential_computing
        self._render_nodes = render_nodes
        self._render_gid = render_gid
        self._video_gid = video_gid
        self._amdsmi_module = amdsmi_module
        self._nvml_module = nvml_module
        self._output_root = output_root
        self._clock = clock
        self._egress_proxy_image = egress_proxy_image
        self._thermal_guard = thermal_guard
        self._thermal_poll_seconds = thermal_poll_seconds
        self._gpu_allocator = gpu_allocator
        self._multinode_network = (multinode_network or "").strip() or None
        self._cluster_addr = (cluster_addr or "").strip() or None
        self._multinode_firewalled = bool(multinode_network_firewalled)
        self._multinode_firewall_check = multinode_firewall_check or check_multinode_firewall

        # Resolved on the first GPU job (_resolve_gpu), not per construction: a CPU job never
        # pays for GPU detection.
        self._gpu_vendor = gpu_vendor.lower().strip() if gpu_vendor else None

    def _rank_network_args(self, rdv: dict[str, Any], job_id: str) -> list[str]:
        """U13 (ADR-0003 addendum, rank-0 reachability). Rank 0's container must LISTEN where the other ranks dial:
        ``master_addr`` (the API sets it to rank 0's host ``cluster_addr``). So rank 0 joins the group network with a
        stable ``--network-alias`` and publishes the rendezvous port ONLY on this host's cluster_addr
        (``--publish <cluster_addr>:<port>:<port>``), never 0.0.0.0 and never a public address. Fail closed on any
        mismatch: a rank whose master_addr is not a literal private address, or a rank 0 whose host has no matching
        cluster_addr, is refused instead of started unreachable. Other ranks publish nothing."""
        import ipaddress

        def _private(v: Any) -> Optional[str]:
            try:
                ip = ipaddress.ip_address(str(v))
            except ValueError:
                return None
            if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                return None
            return str(ip)

        master = _private(rdv.get("master_addr"))
        if master is None:
            raise ExecutorError("multi-node master_addr must be a literal private address; refusing")
        if int(rdv.get("node_rank", -1)) != 0:
            return []
        own = _private(self._cluster_addr) if self._cluster_addr else None
        if own is None:
            raise ExecutorError("multi-node rank 0 but no valid cluster_addr is configured on this host; refusing")
        if own != master:
            raise ExecutorError("multi-node master_addr does not match this host's cluster_addr; refusing")
        port = int(rdv.get("master_port", 0))
        if not (1024 <= port <= 65535):
            raise ExecutorError("multi-node master_port out of range; refusing")
        alias = "cc-rank0-" + re.sub(r"[^a-zA-Z0-9_.-]", "", str(job_id))[:40]
        return ["--network-alias", alias, "--publish", f"{own}:{port}:{port}"]

    def _resolve_gpu(self) -> str:
        """The vendor of this host's GPUs, and for AMD each GPU's own render node, from detection
        (an explicit constructor value wins). Unknown vendor = NVIDIA, as before AMD support."""
        need_nodes = self._render_nodes is None and self._gpu_vendor in (None, "amd")
        if self._gpu_vendor is None or need_nodes:
            from .config import AgentConfig
            from .hardware import detect_gpus

            try:
                configured = AgentConfig.load().gpu_vendor
            except Exception:
                configured = None
            gpus, _ = detect_gpus(config_gpu_vendor=configured, amdsmi_module=self._amdsmi_module)
            if self._gpu_vendor is None:
                self._gpu_vendor = gpus[0].gpu_vendor if gpus else (configured or "nvidia")
            if self._gpu_vendor == "amd" and self._render_nodes is None:
                self._render_nodes = [g.render_node for g in gpus if g.gpu_vendor == "amd" and g.render_node]
        return self._gpu_vendor

    # -- B5 egress allowlist helpers ----------------------------------------- #
    PROXY_PORT = 3128

    @staticmethod
    def _egress_list(job_spec: JobSpec) -> list[str]:
        policy = job_spec.network_policy
        if not policy:
            return []
        egress = policy.get("egress") if isinstance(policy, dict) else None
        if not isinstance(egress, list):
            raise ExecutorError("network_policy must be {'egress': [...]}")
        return [str(e) for e in egress]

    @staticmethod
    def _net_name(job_spec: JobSpec) -> str:
        return f"ccoin-net-{job_spec.job_id}"

    @staticmethod
    def _proxy_name(job_spec: JobSpec) -> str:
        return f"ccoin-proxy-{job_spec.job_id}"

    def _proxy_image(self) -> str:
        image = self._egress_proxy_image
        if image is None:
            try:
                from .config import AgentConfig

                image = AgentConfig.load().egress_proxy_image
            except Exception:
                image = None
        if not image or "@sha256:" not in image:
            raise HardwareUnavailableError(
                "job needs network egress but no egress proxy image pinned by @sha256: digest is "
                "configured (egress_proxy_image); refusing rather than widening the network"
            )
        return image

    def _proxy_url(self, job_spec: JobSpec) -> str:
        return f"http://{self._proxy_name(job_spec)}:{self.PROXY_PORT}"

    def _setup_egress(self, job_spec: JobSpec, egress: list[str], output_dir: str) -> None:
        """Create the internal network and start the proxy (the only member with a route out)."""
        net, proxy = self._net_name(job_spec), self._proxy_name(job_spec)
        image = self._proxy_image()
        steps = [
            ["docker", "network", "create", "--internal", net],
            [
                "docker", "run", "-d", "--rm", "--name", proxy, "--network", net,
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", self.NONROOT_USER, "--pids-limit", "64", "--memory", "128m",
                "-e", f"COMPUTECOIN_EGRESS_ALLOW={','.join(egress)}",
                "-e", f"COMPUTECOIN_PROXY_PORT={self.PROXY_PORT}",
                image,
            ],
            # The proxy joins the default bridge as well: it alone can reach out.
            ["docker", "network", "connect", "bridge", proxy],
        ]
        for step in steps:
            res = self._runner(step, output_dir)
            if res.returncode != 0:
                raise ExecutorError(f"egress setup failed at '{' '.join(step[:3])}': {(res.stderr or '').strip()[:200]}")

    def _teardown_egress(self, job_spec: JobSpec, output_dir: str) -> list[str]:
        """Collect denied-attempt log lines, then remove proxy and network (best effort, never raises)."""
        denied: list[str] = []
        proxy, net = self._proxy_name(job_spec), self._net_name(job_spec)
        try:
            res = self._runner(["docker", "logs", proxy], output_dir)
            for line in _split_lines((res.stdout or "") + "\n" + (res.stderr or "")):
                if "DENIED" in line:
                    denied.append(f"[egress] {line.strip()[:300]}")
        except Exception:
            pass
        for step in (["docker", "rm", "-f", proxy], ["docker", "network", "rm", net]):
            try:
                self._runner(step, output_dir)
            except Exception:
                pass
        return denied[:200]

    # -- argv construction (pure; the core of the hardening contract) -------- #
    def build_argv(self, job_spec: JobSpec, output_dir: str) -> list[str]:
        prefix, image, suffix = self.build_argv_parts(job_spec, output_dir)
        return [*prefix, image, *suffix]

    def build_argv_parts(self, job_spec: JobSpec, output_dir: str) -> tuple[list[str], str, list[str]]:
        """Same hardened argv as build_argv, as (prefix_args, image, suffix_args): the image position is explicit, so
        callers (sessions) can insert flags or a command override without assuming the image is the last element."""
        if not job_spec.image_digest or "@sha256:" not in job_spec.image_digest:
            raise ExecutorError(
                "DockerExecutor requires an image pinned by @sha256: digest"
            )

        limits = job_spec.limits or {}
        byoc = None
        if job_spec.v2 and job_spec.v2.get("byoc") is not None:
            # U8 BYOC: the byoc block is re-validated here; its limits replace the claim's, its tier is forced.
            from .byoc import ByocError, parse_byoc

            try:
                byoc = parse_byoc(job_spec.v2)
            except ByocError as exc:
                raise ExecutorError(f"invalid BYOC block: {exc}")
            if byoc.image != job_spec.image_digest:
                raise ExecutorError("BYOC image does not match the claim image")
            limits = {
                **limits, "memory_mb": byoc.memory_mb, "cpus": byoc.cpus, "pids_limit": byoc.pids_limit,
                "gpu_count": byoc.gpu_count,
                "max_runtime_seconds": min(int(limits.get("max_runtime_seconds", byoc.max_runtime_seconds or self.DEFAULT_TIMEOUT_SECONDS)),
                                           byoc.max_runtime_seconds or self.DEFAULT_TIMEOUT_SECONDS),
            }
            if (job_spec.isolation_tier or "container").lower() in ("sealed", "sealed_pro", "confidential"):
                raise ExecutorError("BYOC is not offered on privacy tiers")
        memory_mb = int(limits.get("memory_mb", 2048))
        cpus = str(limits.get("cpus", "1.0"))
        gpu_count = int(limits.get("gpu_count", 0) or 0)
        pids_limit = int(limits.get("pids_limit", self.DEFAULT_PIDS_LIMIT))
        timeout_s = int(
            limits.get("max_runtime_seconds", self.DEFAULT_TIMEOUT_SECONDS)
        )

        egress = self._egress_list(job_spec)
        if egress and (job_spec.isolation_tier or "container").lower() in ("sealed", "sealed_pro", "confidential"):
            raise ExecutorError("privacy-tier jobs never get network access")
        # U13 / ADR-0003 addendum: a multi-node rank (claim has `rendezvous`) joins ONLY the operator-created internal
        # group network (no route out). Fail closed: no configured network, a privacy tier or an egress policy = refuse.
        if job_spec.rendezvous is not None:
            if (job_spec.isolation_tier or "container").lower() in ("sealed", "sealed_pro", "confidential"):
                raise ExecutorError("privacy-tier jobs never get network access (multi-node refused)")
            if egress:
                raise ExecutorError("multi-node jobs cannot declare network egress")
            if not self._multinode_network:
                raise ExecutorError("multi-node job but no multinode_network is configured on this host; refusing")
            # ADR-0003 U13: the group network is NOT --internal (needed for --publish), so its confinement is the host
            # firewall. Refuse unless the operator attested it AND the runtime self-check finds it in place.
            if not self._multinode_firewalled:
                raise ExecutorError(
                    "multinode_firewall_not_attested: set multinode_network_firewalled = true only after "
                    "scripts/setup-multinode-net.sh --verify printed OK; refusing")
            problem = self._multinode_firewall_check(self._multinode_network)
            if problem:
                raise ExecutorError(f"multinode_firewall_check_failed: {problem}; refusing")
            job_network = self._multinode_network
            net_extra = self._rank_network_args(job_spec.rendezvous, job_spec.job_id)
        else:
            net_extra = []
            job_network = self._net_name(job_spec) if egress else "none"  # no egress by default (B5: internal net + proxy)

        argv: list[str] = [
            "timeout",
            "--signal=KILL",
            str(timeout_s),
            "docker",
            "run",
            "--rm",
            # --- isolation / hardening ---
            "--network",
            job_network,
            *net_extra,  # U13: rank 0 only: --network-alias and --publish restricted to cluster_addr
            "--read-only",     # read-only rootfs
            "--cap-drop",
            "ALL",             # drop all Linux capabilities
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.NONROOT_USER,  # non-root
            "--pids-limit",
            str(pids_limit),
            "--memory",
            f"{memory_mb}m",
            "--cpus",
            cpus,
            # dedicated writable scratch (NOT a host-sensitive bind):
            "--tmpfs",
            f"{self.CONTAINER_TMP}:rw,noexec,nosuid,size={self.DEFAULT_TMPFS_SIZE}",
            # per-job writable output dir (an isolated, agent-owned dir):
            "--mount",
            f"type=bind,source={output_dir},target={self.CONTAINER_OUT}",
            # Standardized container environment variables
            "-e",
            f"COMPUTECOIN_INPUT={self.CONTAINER_OUT}/input.json",
            "-e",
            f"COMPUTECOIN_OUTPUT={self.CONTAINER_OUT}/output.json",
            "-e",
            f"COMPUTECOIN_JOB_ID={job_spec.job_id}",
            "-e",
            f"COMPUTECOIN_MODEL={job_spec.model}",
            "-e",
            f"COMPUTECOIN_WORKLOAD_TYPE={job_spec.workload_type}",
        ]
        if job_spec.mounts or job_spec.extra_env:
            if (job_spec.isolation_tier or "container").lower() in ("sealed", "sealed_pro", "confidential") and not job_spec.private_kit:
                # No staging mounts on a privacy tier. The only environment allowed is the job monitor's sealed-stream
                # pointers (paths inside /out, a RAM-backed dir): without them a private chat could never stream.
                if job_spec.mounts or not set(job_spec.extra_env) <= {"COMPUTECOIN_STREAM", "COMPUTECOIN_PROGRESS"}:
                    raise ExecutorError("privacy-tier jobs do not use the v2 staging mounts")
            for m in job_spec.mounts:
                source, target = str(m["source"]), str(m["target"])
                check_mount_target(target)
                if "," in source or "\n" in source or not os.path.isabs(source):
                    raise ExecutorError("invalid mount source")
                spec = f"type=bind,source={source},target={target}"
                argv += ["--mount", spec + (",readonly" if m.get("readonly", True) else "")]
            for key, value in sorted(job_spec.extra_env.items()):
                if "=" in key or "\n" in key or "\n" in value:
                    raise ExecutorError("invalid environment entry")
                argv += ["-e", f"{key}={value}"]
        if egress:
            proxy_url = self._proxy_url(job_spec)
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                argv += ["-e", f"{key}={proxy_url}"]
            argv += ["-e", f"NO_PROXY="]

        # --- Phase 8: Isolation Tier Enforcement ---
        from .isolation import (
            TIER_CONFIDENTIAL,
            TIER_CONTAINER,
            TIER_GVISOR,
            TIER_MICROVM,
            TIER_SEALED,
            TIER_SEALED_PRO,
        )

        tier = byoc.tier if byoc is not None else (job_spec.isolation_tier or TIER_CONTAINER).lower()

        vendor = self._resolve_gpu() if gpu_count > 0 else (self._gpu_vendor or "nvidia")
        # D5: AMD GPU jobs support container isolation only in v1
        if gpu_count > 0 and vendor == "amd" and tier != TIER_CONTAINER:
            raise IsolationUnavailableError(
                f"AMD GPU jobs support container isolation only in v1 (requested {tier!r}). "
                "Workload cannot be silently downgraded."
            )

        nccl_applied = False
        # GPU only when requested AND the driver/toolkit is present.
        if gpu_count > 0:
            if vendor == "amd":
                if not self._kfd_available():
                    raise HardwareUnavailableError(
                        "AMD GPU job requires /dev/kfd, which is unavailable on this host"
                    )
                argv += build_gpu_argv(
                    "amd",
                    gpu_count,
                    render_nodes=self._render_nodes,
                    render_gid=self._render_gid,
                    video_gid=self._video_gid,
                )
            else:
                if not self._nvidia_available():
                    raise HardwareUnavailableError(
                        "GPU job requires the NVIDIA container toolkit, which is "
                        "unavailable on this host"
                    )
                if gpu_count > 1 and job_spec.gpu_device_ids:
                    # U13: explicit device set chosen by the agent's allocator, private IPC with a bounded --shm-size
                    # (ADR-0003 forbids --ipc=host), NCCL env for one host or the multi-node rendezvous.
                    from .gpu_alloc import GpuAllocationError, nccl_env, nvidia_gpus_args, shm_size_mb

                    if len(job_spec.gpu_device_ids) != gpu_count:
                        raise ExecutorError("gpu device set does not match gpu_count")
                    try:
                        argv += nvidia_gpus_args(job_spec.gpu_device_ids)
                        argv += ["--shm-size", f"{shm_size_mb(memory_mb, limits)}m"]
                        for key, value in sorted(nccl_env(gpu_count, job_spec.rendezvous).items()):
                            argv += ["-e", f"{key}={value}"]
                        nccl_applied = True
                    except GpuAllocationError as exc:
                        raise ExecutorError(str(exc)) from exc
                else:
                    argv += build_gpu_argv("nvidia", gpu_count)

        if job_spec.rendezvous is not None and not nccl_applied:
            # U13: the rendezvous variables are needed for any multi-node rank, also with one GPU per node.
            from .gpu_alloc import GpuAllocationError, nccl_env

            try:
                for key, value in sorted(nccl_env(max(gpu_count, 1), job_spec.rendezvous).items()):
                    argv += ["-e", f"{key}={value}"]
            except GpuAllocationError as exc:
                raise ExecutorError(str(exc)) from exc

        if tier == TIER_GVISOR:
            if not self._gvisor_available():
                raise IsolationUnavailableError(
                    "gVisor isolation requested but runsc runtime is not available on this host. "
                    "Workload cannot be silently downgraded to ordinary container isolation."
                )
            argv += ["--runtime", "runsc"]
        elif tier == TIER_MICROVM:
            if not self._microvm_available():
                raise IsolationUnavailableError(
                    "microVM isolation requested but Kata Containers / microVM runtime is not available on this host. "
                    "Workload cannot be silently downgraded to ordinary container isolation."
                )
            argv += ["--runtime", "kata-qemu"]
        elif tier == TIER_CONFIDENTIAL:
            if not self._confidential_available():
                raise IsolationUnavailableError(
                    "Confidential computing isolation requested but hardware enclave / attestation is not available on this host. "
                    "Workload cannot be silently downgraded."
                )
            argv += ["--runtime", "kata-qemu-sev"]
        elif tier not in (TIER_CONTAINER, TIER_SEALED, TIER_SEALED_PRO):
            # sealed / sealed_pro: the host itself is the sealed VM (verified by attestation); the job runs in the
            # same hardened container. Everything else is refused, never silently downgraded.
            raise ExecutorError(f"Unsupported isolation_tier: {job_spec.isolation_tier!r}")

        # The pinned image is the LAST positional before the (implicit) entrypoint.
        if byoc is not None:
            # no swap, no shared IPC; never privileged / host namespaces / devices (none of these flags exist here)
            argv += ["--memory-swap", f"{byoc.memory_mb}m", "--ipc", "none"]
            for key, value in sorted(byoc.env.items()):
                argv += ["-e", f"{key}={value}"]
            if byoc.command:
                argv += ["--entrypoint", byoc.command[0]]
            return argv, job_spec.image_digest, [*byoc.command[1:], *byoc.args]
        return argv, job_spec.image_digest, []

    def _thermal_watch(self, container: str, output_dir: str, stop: "threading.Event", tripped: list[float]) -> None:
        """Poll the GPU temperature while the job runs; at the hard stop, kill the container."""
        from .thermal import HARD_STOP

        guard = self._thermal_guard
        while not stop.wait(self._thermal_poll_seconds):
            try:
                temp = guard.read_max_temp()
                if guard.evaluate(temp) == HARD_STOP and temp is not None:
                    tripped.append(temp)
                    self._runner(["docker", "kill", container], output_dir)
                    return
            except Exception:
                continue  # a broken sensor never stops a job; None never triggers

    def _cancel_watch(self, container: str, output_dir: str, stop: "threading.Event", cancel_event: "threading.Event",
                      cancelled: list[bool]) -> None:
        """U13: when the job monitor sees the API cancelled this job, kill exactly this container."""
        while not stop.is_set():
            if cancel_event.wait(1.0):
                if stop.is_set():
                    return
                cancelled.append(True)
                try:
                    self._runner(["docker", "kill", container], output_dir)
                except Exception:
                    pass
                return

    def run(self, job_spec: JobSpec) -> ExecutionResult:
        if job_spec.execution_mode != "hardware":
            # A simulated job must never be routed to the DockerExecutor.
            raise ExecutorError(
                "DockerExecutor refuses a non-hardware job (would misrepresent "
                "execution_mode)"
            )
        if not self._docker_available():
            raise HardwareUnavailableError(
                "hardware execution unavailable: docker is not installed / not "
                "usable on this host (the agent will NOT silently simulate a "
                "hardware job)"
            )

        start = self._clock()
        created_tmp = False
        output_dir = job_spec.output_dir or self._output_root
        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix="ccoin-job-")
            created_tmp = True
        os.makedirs(output_dir, exist_ok=True)
        try:
            os.chmod(output_dir, 0o777)
        except OSError:
            pass
        if job_spec.on_output_dir is not None:
            try:
                job_spec.on_output_dir(output_dir)
            except Exception:
                pass  # monitoring is best-effort

        # Materialize input.json inside output_dir for container reading
        input_path = os.path.join(output_dir, "input.json")
        try:
            with open(input_path, "w", encoding="utf-8") as fh:
                json.dump(job_spec.input, fh)
            try:
                os.chmod(input_path, 0o666)
            except OSError:
                pass
        except OSError as exc:
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={
                    "duration_seconds": 0.0,
                    "max_memory_mb": None,
                    "gpu_util_pct": None,
                },
                logs=[f"[hardware] failed to write input.json: {exc}"],
                output_schema_valid=False,
                error={
                    "code": "input_write_error",
                    "message": f"failed to materialize input.json: {exc}",
                },
            )

        allocated: list[int] = []
        gpus_wanted = int((job_spec.limits or {}).get("gpu_count", 0) or 0)
        if self._gpu_allocator is not None and gpus_wanted > 1 and not job_spec.gpu_device_ids:
            try:
                allocated = self._gpu_allocator.acquire(gpus_wanted)
            except Exception as exc:  # no free device set: fail the attempt, never share cards
                return ExecutionResult(
                    status="failed", execution_mode=self.execution_mode,
                    metrics={"duration_seconds": 0.0, "max_memory_mb": None, "gpu_util_pct": None},
                    logs=[f"[hardware] {exc}"], output_schema_valid=False,
                    error={"code": "gpu_unavailable", "message": str(exc)},
                )
            job_spec.gpu_device_ids = allocated
        try:
            return self._run_allocated(job_spec, output_dir, start, created_tmp)
        finally:
            if allocated:
                self._gpu_allocator.release(allocated)

    def _run_allocated(self, job_spec: JobSpec, output_dir: str, start: float, created_tmp: bool) -> ExecutionResult:
        argv = self.build_argv(job_spec, output_dir)
        logs = [f"[hardware] docker run for job {job_spec.job_id}"]
        egress = self._egress_list(job_spec)
        container_name = f"ccoin-job-{job_spec.job_id}"
        # Always named: the wall-clock `timeout --signal=KILL` only kills the docker CLI, never the container, so the
        # name is needed to stop it afterwards (and by the thermal / cancel watchdogs).
        argv = [*argv[: argv.index("--rm") + 1], "--name", container_name, *argv[argv.index("--rm") + 1 :]]
        run_result: Optional[DockerRunResult] = None
        stop_event = threading.Event()
        thermal_tripped: list[float] = []
        cancelled: list[bool] = []
        cancel_watchdog = None
        watchdog = None
        try:
            if egress:
                try:
                    self._setup_egress(job_spec, egress, output_dir)
                except ExecutorError as exc:
                    self._teardown_egress(job_spec, output_dir)
                    return ExecutionResult(
                        status="failed",
                        execution_mode=self.execution_mode,
                        metrics={"duration_seconds": 0.0, "max_memory_mb": None, "gpu_util_pct": None},
                        logs=logs + [f"[egress] {exc}"],
                        output_schema_valid=False,
                        error={"code": "egress_setup_failed", "message": str(exc)},
                    )
                logs.append(f"[egress] allowlist: {', '.join(egress)} (everything else denied)")
            if self._thermal_guard is not None:
                watchdog = threading.Thread(
                    target=self._thermal_watch,
                    args=(container_name, output_dir, stop_event, thermal_tripped),
                    daemon=True,
                )
                watchdog.start()
            if job_spec.cancel_event is not None:
                cancel_watchdog = threading.Thread(
                    target=self._cancel_watch,
                    args=(container_name, output_dir, stop_event, job_spec.cancel_event, cancelled),
                    daemon=True,
                )
                cancel_watchdog.start()
            run_result = self._runner(argv, output_dir)
        finally:
            if run_result is None or run_result.returncode != 0:
                # timeout (137) / cancelled / crashed client: the container may still be running; stop and remove it.
                try:
                    self._runner(["docker", "rm", "-f", container_name], output_dir)
                except Exception:
                    pass
            stop_event.set()
            if watchdog is not None:
                watchdog.join(timeout=5)
            if cancel_watchdog is not None:
                cancel_watchdog.join(timeout=5)
            if egress:
                logs.extend(self._teardown_egress(job_spec, output_dir))
        duration = max(0.0, self._clock() - start)

        if thermal_tripped:
            # B6: a thermal stop is reported failed so the customer is not charged and the job can fail over.
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={"duration_seconds": round(duration, 6), "max_memory_mb": None, "gpu_util_pct": None},
                logs=logs + [f"[thermal] GPU reached {thermal_tripped[0]:.0f} C (hard stop); container stopped"],
                output_schema_valid=False,
                error={"code": "thermal_shutdown", "message": f"GPU temperature {thermal_tripped[0]:.0f} C reached the hard stop"},
            )

        if cancelled:
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={"duration_seconds": round(duration, 6), "max_memory_mb": None, "gpu_util_pct": None},
                logs=logs + ["[cancel] the job was cancelled by the API; container stopped"],
                output_schema_valid=False,
                error={"code": "cancelled", "message": "job cancelled; container stopped"},
            )

        if run_result.returncode != 0:
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={
                    "duration_seconds": round(duration, 6),
                    "max_memory_mb": None,
                    "gpu_util_pct": None,
                },
                logs=logs + _split_lines(run_result.stderr),
                output_schema_valid=False,
                error={
                    "code": "container_nonzero_exit",
                    "message": f"docker run exited {run_result.returncode}",
                },
            )

        # Read the output the container wrote to the dedicated output dir. Exit 0
        # alone is NOT proof of useful work -- we require a parseable output.
        output_path = os.path.join(output_dir, "output.json")
        try:
            # The job controls everything in this dir: never follow a planted symlink (host-file exfiltration) and
            # never read an unbounded file into the agent's memory.
            raw = _read_job_file(output_path, MAX_OUTPUT_JSON_BYTES)
            output = json.loads(raw)
        except (OSError, ValueError) as exc:
            if job_spec.v2 is not None:
                # v2: outputs are the Kit's declared files, collected and hashed by runtime_v2;
                # output.json is optional. (A declared required output that is missing fails there.)
                return ExecutionResult(
                    status="success",
                    output={},
                    execution_mode=self.execution_mode,
                    metrics={"duration_seconds": round(duration, 6), "max_memory_mb": None, "gpu_util_pct": None},
                    logs=logs + _split_lines(run_result.stdout),
                    output_schema_valid=False,
                )
            return ExecutionResult(
                status="failed",
                execution_mode=self.execution_mode,
                metrics={
                    "duration_seconds": round(duration, 6),
                    "max_memory_mb": None,
                    "gpu_util_pct": None,
                },
                logs=logs + [f"[hardware] could not read output.json: {exc}"],
                output_schema_valid=False,
                error={
                    "code": "missing_output",
                    "message": "container exited 0 but produced no valid output",
                },
            )

        artifact = Artifact(
            kind="output",
            data=raw,
            content_type="application/json",
            filename="output.json",
        )
        schema_ok = validate_output(job_spec.workload_type, output)

        # Collect hardware metrics if available (honest readings; never fabricated)
        gpu_util_pct = None
        max_mem_mb = None
        if self._gpu_vendor == "amd" and (job_spec.limits or {}).get("gpu_count", 0):
            try:
                from .telemetry import sample_gpu_metrics

                samples = sample_gpu_metrics(
                    simulated=False,
                    amdsmi_module=self._amdsmi_module,
                )
                if samples and samples[0].extra_metrics is None:
                    gpu_util_pct = samples[0].gpu_util_pct
                    max_mem_mb = samples[0].mem_used_mb
            except Exception:
                pass
        elif self._nvidia_available():
            try:
                from .telemetry import sample_gpu_metrics

                samples = sample_gpu_metrics(
                    simulated=False,
                    nvml_module=self._nvml_module,
                )
                if samples and samples[0].extra_metrics is None:
                    gpu_util_pct = samples[0].gpu_util_pct
                    max_mem_mb = samples[0].mem_used_mb
            except Exception:
                pass

        metrics = {
            "duration_seconds": round(duration, 6),
            "max_memory_mb": max_mem_mb,
            "gpu_util_pct": gpu_util_pct,
        }
        if isinstance(output.get("tokens"), (int, float)):
            metrics["tokens"] = int(output["tokens"])
        if isinstance(output.get("metrics"), dict):
            metrics.update(output["metrics"])

        return ExecutionResult(
            status="success",
            output=output,
            execution_mode=self.execution_mode,
            metrics=metrics,
            logs=logs + _split_lines(run_result.stdout),
            artifacts=[artifact],
            output_schema_valid=schema_ok,
        )


MAX_OUTPUT_JSON_BYTES = 128 * 1024 * 1024


def _read_job_file(path: str, max_bytes: int) -> bytes:
    """Read a file the (untrusted) job wrote: regular files only, symlinks refused (O_NOFOLLOW), size capped.
    Raises OSError for a symlink, a non-regular file or an oversized file."""
    import stat as _stat

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)  # ELOOP on a symlink; O_NONBLOCK so a planted FIFO cannot hang the agent
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not _stat.S_ISREG(st.st_mode):
            raise OSError(f"{os.path.basename(path)} is not a regular file")
        if st.st_size > max_bytes:
            raise OSError(f"{os.path.basename(path)} exceeds {max_bytes} bytes")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"{os.path.basename(path)} exceeds {max_bytes} bytes")
    return data


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    return [ln for ln in text.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def select_executor(
    job_spec: JobSpec,
    capabilities: Optional[dict[str, Any]] = None,
    *,
    docker_executor_factory: Callable[[], DockerExecutor] = DockerExecutor,
    simulated_executor_factory: Callable[[], SimulatedExecutor] = SimulatedExecutor,
) -> Executor:
    """Pick the executor: image_digest null -> Simulated; else Docker.

    ``capabilities`` is reserved for richer routing (e.g. refusing a GPU job on a
    GPU-less host); the digest rule is the primary switch per the contract.
    """
    if not job_spec.image_digest:
        return simulated_executor_factory()
    return docker_executor_factory()
