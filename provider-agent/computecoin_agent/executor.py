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
        output_root: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._docker_available = docker_available
        self._nvidia_available = nvidia_available
        self._output_root = output_root
        self._clock = clock

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
        ]

        # GPU only when requested AND the toolkit is present.
        if gpu_count > 0:
            if not self._nvidia_available():
                raise HardwareUnavailableError(
                    "GPU job requires the NVIDIA container toolkit, which is "
                    "unavailable on this host"
                )
            argv += ["--gpus", str(gpu_count)]

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
        return ExecutionResult(
            status="success",
            output=output,
            execution_mode=self.execution_mode,
            metrics={
                "duration_seconds": round(duration, 6),
                "max_memory_mb": None,
                "gpu_util_pct": None,
            },
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
