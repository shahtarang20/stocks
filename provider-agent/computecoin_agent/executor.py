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
    - ``--network none`` by default (an egress allowlist is only added when the
      workload profile explicitly requires it);
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
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
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
    # Set by the private-job flow after the keys are fetched for a job-bound verdict (never from the claim).
    wrapped_shares: Optional[list[dict[str, Any]]] = None
    verdict_id: Optional[str] = None

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

        # Resolved on the first GPU job (_resolve_gpu), not per construction: a CPU job never
        # pays for GPU detection.
        self._gpu_vendor = gpu_vendor.lower().strip() if gpu_vendor else None

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

    # -- argv construction (pure; the core of the hardening contract) -------- #
    def build_argv(self, job_spec: JobSpec, output_dir: str) -> list[str]:
        if not job_spec.image_digest or "@sha256:" not in job_spec.image_digest:
            raise ExecutorError(
                "DockerExecutor requires an image pinned by @sha256: digest"
            )

        limits = job_spec.limits or {}
        memory_mb = int(limits.get("memory_mb", 2048))
        cpus = str(limits.get("cpus", "1.0"))
        gpu_count = int(limits.get("gpu_count", 0) or 0)
        pids_limit = int(limits.get("pids_limit", self.DEFAULT_PIDS_LIMIT))
        timeout_s = int(
            limits.get("max_runtime_seconds", self.DEFAULT_TIMEOUT_SECONDS)
        )

        argv: list[str] = [
            "timeout",
            "--signal=KILL",
            str(timeout_s),
            "docker",
            "run",
            "--rm",
            # --- isolation / hardening ---
            "--network",
            "none",            # no egress by default
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

        # --- Phase 8: Isolation Tier Enforcement ---
        from .isolation import (
            TIER_CONFIDENTIAL,
            TIER_CONTAINER,
            TIER_GVISOR,
            TIER_MICROVM,
        )

        tier = (job_spec.isolation_tier or TIER_CONTAINER).lower()

        vendor = self._resolve_gpu() if gpu_count > 0 else (self._gpu_vendor or "nvidia")
        # D5: AMD GPU jobs support container isolation only in v1
        if gpu_count > 0 and vendor == "amd" and tier != TIER_CONTAINER:
            raise IsolationUnavailableError(
                f"AMD GPU jobs support container isolation only in v1 (requested {tier!r}). "
                "Workload cannot be silently downgraded."
            )

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
                argv += build_gpu_argv("nvidia", gpu_count)

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
        elif tier != TIER_CONTAINER:
            raise ExecutorError(f"Unsupported isolation_tier: {job_spec.isolation_tier!r}")

        # The pinned image is the LAST positional before the (implicit) entrypoint.
        argv.append(job_spec.image_digest)
        return argv

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
        output_dir = self._output_root
        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix="ccoin-job-")
            created_tmp = True
        os.makedirs(output_dir, exist_ok=True)
        try:
            os.chmod(output_dir, 0o777)
        except OSError:
            pass

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

        argv = self.build_argv(job_spec, output_dir)
        logs = [f"[hardware] docker run for job {job_spec.job_id}"]
        run_result = self._runner(argv, output_dir)
        duration = max(0.0, self._clock() - start)

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
            with open(output_path, "rb") as fh:
                raw = fh.read()
            output = json.loads(raw)
        except (OSError, ValueError) as exc:
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
