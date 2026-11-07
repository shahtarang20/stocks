"""The Windows supervisor loop (W2 + W4 client side).

One process, the Windows Service, owns:

* the HEARTBEAT to the API (reusing ``heartbeat.send_contract_heartbeat``), with native GPU telemetry
  (``telemetry.sample_gpu_metrics`` through NVML on Windows) in the health block;
* the THERMAL GUARD (``thermal.ThermalGuard``) fed by that native telemetry;
* the AVAILABILITY state: ``online``, ``throttled``, ``yielding``, ``maintenance``, ``draining``;
* the RUNNER inside the managed WSL2 distro: start it, connect the control channel (we are the CLIENT, the runner
  is the server), push ``start`` / ``set_availability`` / ``set_limits``, ``drain`` and ``stop`` it, restart it with
  backoff when it dies.

Availability and the heartbeat. The API accepts ``online | maintenance | offline | throttled`` and treats
``maintenance`` as a sticky owner hold (apps/api/app/api/routers/agents.py). The supervisor therefore reports every
non-online state as ``throttled`` and carries the real state in the additive health field ``availability_detail``
(same mapping as ``windows/governance/policy.py``). A host is reported ``online`` only when the runner is connected,
started and nothing else blocks claims: it never advertises capacity it cannot serve (X4, L1).

Wiring added on top (all optional, injected by ``build_supervisor``; tests build a bare Supervisor without them):

* GOVERNANCE (W5, X13): ``availability_source`` is ``governance.wiring.SupervisorGovernance.source``; the control channel's
  job messages call ``on_job_event`` so sleep prevention follows the job; the engine's ``stop_job`` calls
  :meth:`Supervisor.abort_running_job`. Any failure of the engine reports ``yielding`` (fail closed).
* UPDATES (W6): ``update_gate`` (a ``supervisor_wiring.SupervisorUpdateGate``) closes claims below the forced-update floor or
  while an update installs; ``update_runner`` is the scheduler's thread body (never installs while a job runs, drains
  through the control channel first: :meth:`begin_update_drain` / :meth:`end_update_drain`).
* HEALTH: ``health_extras`` providers add the additive ``runtime`` and ``update`` keys to the heartbeat health block, next to
  ``availability_detail`` = ``{state, code, reason}``.

Nothing here touches money code. Every Windows / wsl.exe interface is TODO(verify-docs); see the modules it uses.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from ..control_channel import (
    AVAILABILITY_STATES,
    ControlChannelError,
    ControlClient,
    ControlMessage,
    generate_token,
)

logger = logging.getLogger("computecoin_agent.windows.supervisor")

HEARTBEAT_AVAILABILITY = {
    "online": "online", "throttled": "throttled", "yielding": "throttled", "maintenance": "throttled", "draining": "throttled",
}
# Highest precedence first: the first one that applies wins.
PRECEDENCE = ("draining", "maintenance", "yielding", "throttled", "online")
SIMULATED_MODE = "simulated_probe"  # telemetry.sample_gpu_metrics' fake sample: never reported as real


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class SupervisorSettings:
    distro: str = "computecoin"
    runner_user: str = "ccrunner"
    runner_program: str = "/opt/computecoin/venv/bin/computecoin-agent"
    token_file_in_distro: str = "/etc/computecoin/control.token"
    control_host: str = "127.0.0.1"
    control_port: int = 47821
    allow_private_control_address: bool = False
    tick_seconds: float = 2.0
    drain_grace_seconds: float = 20.0
    stop_grace_seconds: float = 10.0
    kill_wait_seconds: float = 5.0
    max_restarts: int = 5
    restart_window_seconds: float = 300.0
    restart_backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0)
    telemetry_max_age_seconds: float = 15.0
    # Governance: True (default) reads the owner's activity from the signed snapshot file written by the user-session
    # helper (the service is in session 0); False reads the Win32 probes directly (supervisor run in the owner's session).
    use_session_helper: bool = True
    snapshot_max_age_seconds: float = 30.0  # an older snapshot is stale, and stale blocks claims

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SupervisorSettings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:  # fail closed on typos
            raise ValueError(f"unknown supervisor setting(s): {sorted(unknown)}")
        values = dict(raw)
        if "restart_backoff_seconds" in values:
            values["restart_backoff_seconds"] = tuple(float(x) for x in values["restart_backoff_seconds"])
        s = cls(**values)
        s.validate()
        return s

    @classmethod
    def load(cls, path: "str | Path") -> "SupervisorSettings":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(tomllib.loads(p.read_text(encoding="utf-8")))

    def validate(self) -> None:
        if not 1 <= int(self.control_port) <= 65535:
            raise ValueError("control_port must be 1..65535")
        for name in ("tick_seconds", "drain_grace_seconds", "stop_grace_seconds", "kill_wait_seconds", "snapshot_max_age_seconds"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if self.max_restarts < 0 or not self.restart_backoff_seconds:
            raise ValueError("max_restarts must be >= 0 and restart_backoff_seconds non-empty")


# --------------------------------------------------------------------------- #
# Runner process abstraction
# --------------------------------------------------------------------------- #
class RunnerProcess(Protocol):
    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: Optional[float] = None) -> Optional[int]: ...


class RunnerLauncher(Protocol):
    def launch(self) -> RunnerProcess: ...


class WslRunnerLauncher:
    """Starts ``computecoin-agent work --control-socket ...`` inside the managed distro via ``wsl.exe --exec``.

    The control token is NOT on the command line (process lists are readable): it is provisioned as a file inside the
    distro with :meth:`provision_token` and the runner reads ``--control-token-file``. argv construction reuses the
    W3 builder (``windows/runtime/wsl.py``), which also refuses a shell as the in-distro program.
    TODO(verify-docs): ``--distribution``, ``--user`` (read 2026-11-06 at
    https://learn.microsoft.com/windows/wsl/basic-commands) and ``--exec`` (not on that page).
    TODO(verify-docs): the supervisor reaches the runner at ``control_host:control_port`` through WSL2 localhost
    forwarding (default NAT mode) or the shared loopback of mirrored mode; neither was checked.
    """

    def __init__(self, settings: SupervisorSettings, *, popen: Callable[..., Any] = subprocess.Popen,
                 run: Optional[Callable[..., Any]] = None) -> None:
        self._s = settings
        self._popen = popen
        self._run = run

    def runner_argv(self) -> list[str]:
        from .runtime.wsl import argv_exec, assert_no_shell

        program = [
            self._s.runner_program, "work",
            "--control-socket", f"{self._s.control_host}:{self._s.control_port}",
            "--control-token-file", self._s.token_file_in_distro,
        ]
        if self._s.allow_private_control_address:
            program.append("--control-allow-private")
        assert_no_shell(program)
        return argv_exec(self._s.distro, program, user=self._s.runner_user)

    def launch(self) -> RunnerProcess:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                                  "shell": False}
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # TODO(verify-docs): Windows-only constant
        if flag:
            kwargs["creationflags"] = flag
        return self._popen(self.runner_argv(), **kwargs)

    def provision_token(self, token: str) -> None:
        """Write the control token into the distro (mode 600, owned by the runner user). Never logs the token."""
        from .runtime.wsl import argv_exec

        if self._run is None:
            raise ControlChannelError("no wsl runner available to provision the control token")
        path = self._s.token_file_in_distro
        steps: list[tuple[list[str], Optional[bytes]]] = [
            (argv_exec(self._s.distro, ["mkdir", "-p", path.rsplit("/", 1)[0]], user="root"), None),
            # create the file EMPTY with the final mode and owner first, so the secret is never in a looser file
            (argv_exec(self._s.distro, ["install", "-m", "600", "-o", self._s.runner_user, "-g", self._s.runner_user,
                                        "/dev/null", path], user="root"), None),
            (argv_exec(self._s.distro, ["tee", path], user="root"), token.encode("utf-8")),
        ]
        for argv, stdin in steps:
            result = self._run(argv, stdin=stdin) if stdin is not None else self._run(argv)
            if getattr(result, "returncode", 1) != 0:
                raise ControlChannelError("could not provision the control token in the runtime distro")


# --------------------------------------------------------------------------- #
# Telemetry (native NVML on Windows)
# --------------------------------------------------------------------------- #
def native_gpu_sampler() -> list[Any]:
    """Real GPU samples only. ``sample_gpu_metrics`` falls back to a SIMULATED sample when nothing is readable; that
    fake must never feed the thermal guard or the heartbeat, so it is dropped here."""
    from ..telemetry import sample_gpu_metrics

    out = []
    for s in sample_gpu_metrics(simulated=False) or []:
        extra = getattr(s, "extra_metrics", None) or {}
        if extra.get("mode") == SIMULATED_MODE:
            continue
        out.append(s)
    return out


def health_from_samples(samples: Sequence[Any]) -> dict[str, Any]:
    """Heartbeat health fields from real samples: hottest GPU, busiest GPU, total used VRAM. Empty -> {}."""
    temps = [s.temp_c for s in samples if getattr(s, "temp_c", None) is not None]
    utils = [s.gpu_util_pct for s in samples if getattr(s, "gpu_util_pct", None) is not None]
    mems = [s.mem_used_mb for s in samples if getattr(s, "mem_used_mb", None) is not None]
    out: dict[str, Any] = {}
    if temps:
        out["gpu_temp_c"] = max(temps)
    if utils:
        out["gpu_util_pct"] = max(utils)
    if mems:
        out["mem_used_mb"] = int(sum(mems))
    return out


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #
@dataclass
class ShutdownResult:
    drained: bool
    forced: bool
    note: str = ""


class Supervisor:
    """All dependencies are injectable; ``tick()`` and ``heartbeat_once()`` are synchronous so tests drive them."""

    def __init__(
        self,
        *,
        base_url: str,
        machine_id: str,
        machine_secret: str,
        token: str,
        settings: SupervisorSettings,
        launcher: RunnerLauncher,
        report: Any = None,
        heartbeat_interval: float = 30.0,
        thermal_guard: Any = None,
        gpu_limits: Optional[dict[str, float]] = None,
        telemetry_sampler: Callable[[], list[Any]] = native_gpu_sampler,
        availability_source: Optional[Callable[[], tuple[str, dict[str, Any]]]] = None,
        heartbeat_send: Optional[Callable[..., Any]] = None,
        health_base: Optional[Callable[[Any], dict[str, Any]]] = None,
        client_factory: Callable[..., Any] = ControlClient,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Optional[Callable[[float], None]] = None,
        token_provisioner: Optional[Callable[[str], None]] = None,
        transport: Any = None,
        on_job_event: Optional[Callable[[], None]] = None,
        health_extras: Sequence[Callable[[], dict[str, Any]]] = (),
        update_gate: Any = None,
        update_runner: Optional[Callable[[threading.Event], None]] = None,
        on_shutdown: Sequence[Callable[[], None]] = (),
    ) -> None:
        self._base_url, self._machine_id, self._secret = base_url, machine_id, machine_secret
        self._token = token
        self._s = settings
        self._launcher = launcher
        self._report = report
        self._hb_interval = heartbeat_interval
        self._guard = thermal_guard
        self._gpu_limits = gpu_limits or {}
        self._sampler = telemetry_sampler
        self._source = availability_source
        self._heartbeat_send = heartbeat_send
        self._health_base = health_base
        self._client_factory = client_factory
        self._clock = clock
        self._sleeper = sleeper
        self._provision = token_provisioner  # writes the control token into the distro before each runner launch
        self._transport = transport
        self._on_job_event = on_job_event  # governance re-evaluation on job_started / job_finished / usage
        self._health_extras = tuple(health_extras)
        self._update_gate = update_gate
        self._update_runner = update_runner
        self._on_shutdown = tuple(on_shutdown)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._proc: Optional[RunnerProcess] = None
        self._client: Any = None
        self._runner_ready = False
        self._runner_active_jobs = 0
        self._runner_draining = False
        self._started_sent = False
        self._restarts: list[float] = []
        self._next_launch_at = 0.0
        self._runner_gave_up = False
        self._drain_requested = False
        self._update_drain = False
        self._hb_ok_at: Optional[float] = None
        self._shutting_down = False
        self._samples: list[Any] = []
        self._samples_at: Optional[float] = None
        self._last_pushed: Optional[tuple[str, Optional[float]]] = None
        self._state = "maintenance"
        self._detail: dict[str, Any] = {"code": "starting"}
        self._fatal: Optional[BaseException] = None
        self.last_error_codes: list[str] = []

    # -- lifecycle ------------------------------------------------------------------------------------------- #
    def set_thermal_guard(self, guard: Any) -> None:
        self._guard = guard

    def start(self) -> None:
        self._apply_power_limit()
        self.tick()
        loops: list[tuple[Callable[[], None], str]] = [(self._main_loop, "supervisor-main"),
                                                       (self._heartbeat_loop, "supervisor-heartbeat")]
        if self._update_runner is not None:
            loops.append((self._update_loop, "supervisor-update"))
        for target, name in loops:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def fatal_error(self) -> Optional[BaseException]:
        return self._fatal

    def request_drain(self) -> None:
        """Owner or operator asks to finish the current job and take no more (reversible with :meth:`resume`)."""
        with self._lock:
            self._drain_requested = True
        self._send_quiet("drain", {"reason": "requested"})

    def resume(self) -> None:
        with self._lock:
            self._drain_requested = False
            self._started_sent = False  # a fresh `start` clears the runner's sticky drain

    def begin_update_drain(self) -> None:
        """The updater wants to install: finish what runs and claim nothing new (separate from the operator's drain)."""
        with self._lock:
            self._update_drain = True
        self._send_quiet("drain", {"reason": "update"})

    def end_update_drain(self) -> None:
        with self._lock:
            self._update_drain = False
            if not self._drain_requested:
                self._started_sent = False  # a fresh `start` clears the runner's sticky drain

    def drain_complete(self) -> bool:
        """No job is running and the runner has acknowledged the drain (or there is no connected runner)."""
        with self._lock:
            return self._runner_active_jobs == 0 and (self._runner_draining or not self._runner_ready)

    def active_jobs(self) -> int:
        return self._active_jobs()

    def update_health_ok(self) -> bool:
        """The updater's health check for a freshly installed version: the runner is connected and started and a heartbeat
        was accepted recently. (It does not need ``online``: the host may be draining for the update.)"""
        with self._lock:
            ok_at = self._hb_ok_at
            runner_ok = self._runner_ready and self._started_sent and not self._runner_gave_up
        window = max(90.0, 3.0 * float(self._hb_interval))
        return bool(runner_ok and ok_at is not None and self._clock() - ok_at < window)

    def abort_running_job(self, reason: str) -> None:
        """Governance said stop the running job now (an explicit "stop now", or a full-screen app with the owner's
        preempt option on). The runner has no safe cancel, so the runner process is terminated and the server-side lease
        expires and re-queues the job; the supervisor then restarts the runner, which claims nothing while the engine says no."""
        if self._active_jobs() <= 0:
            return
        logger.warning("stopping the running job on governance request (%s)", reason)
        self._terminate_process()

    def shutdown(self, timeout: float = 25.0, *, progress: Optional[Callable[[], None]] = None) -> ShutdownResult:
        """Drain, then stop, then terminate the runner, all within ``timeout`` seconds (best effort).

        An in-flight job gets ``drain_grace_seconds`` (never more than half of ``timeout``) to finish. The runner has
        no safe cancel, so a job still running at the deadline is abandoned: the runner process is terminated and the
        server-side lease expires and re-queues it. Returns what happened; never raises.
        """
        deadline = self._clock() + timeout
        with self._lock:
            self._shutting_down = True
            self._drain_requested = True
        self._recompute()
        self._heartbeat_quiet("stopping")  # tell the scheduler to stop sending before we go
        self._send_quiet("drain", {"reason": "service_stop"})
        drain_until = min(deadline, self._clock() + min(self._s.drain_grace_seconds, timeout / 2.0))
        while self._active_jobs() > 0 and self._clock() < drain_until and self._proc_alive():
            self._pulse(progress)
        drained = self._active_jobs() == 0
        self._send_quiet("stop", {"reason": "service_stop", "grace_seconds": float(self._s.stop_grace_seconds)})
        forced = False
        stop_until = min(deadline, self._clock() + self._s.stop_grace_seconds)
        while self._proc_alive() and self._clock() < stop_until:
            self._pulse(progress)
        if self._proc_alive():
            forced = True
            self._terminate_process()
        self._stop.set()
        self._close_client()
        for closer in self._on_shutdown:  # e.g. release the sleep-prevention request
            try:
                closer()
            except Exception as exc:  # noqa: BLE001
                logger.debug("shutdown hook failed: %s", type(exc).__name__)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2.0)
        note = "" if drained else "a job was still running; the runner was stopped and the lease will expire"
        return ShutdownResult(drained=drained, forced=forced, note=note)

    # -- availability ------------------------------------------------------------------------------------------ #
    def availability(self) -> str:
        with self._lock:
            return self._state

    def heartbeat_availability(self) -> str:
        return HEARTBEAT_AVAILABILITY[self.availability()]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"availability": self._state, "detail": dict(self._detail), "runner_ready": self._runner_ready,
                    "active_jobs": self._runner_active_jobs, "runner_gave_up": self._runner_gave_up}

    def _recompute(self) -> None:
        """Pick the highest-precedence applicable state. Online needs a connected, started runner."""
        causes: dict[str, str] = {}
        with self._lock:
            if self._shutting_down or self._drain_requested or self._update_drain:
                causes["draining"] = "service_stop" if self._shutting_down else ("requested" if self._drain_requested else "update")
            runner_ok = self._runner_ready and self._started_sent and not self._runner_gave_up
        if self._source is not None:
            try:
                ext_state, ext_detail = self._source()
                if ext_state in AVAILABILITY_STATES and ext_state != "online":
                    causes.setdefault(ext_state, str(ext_detail.get("code", ext_state)))
            except Exception as exc:  # noqa: BLE001 - an unknown governance answer must block, not allow
                causes.setdefault("yielding", "availability_source_error")
                logger.warning("availability source failed: %s", type(exc).__name__)
        thermal = self._thermal_check()
        if thermal in ("throttled", "hard_stop"):
            causes["throttled"] = "gpu_" + thermal
        if self._update_gate is not None:
            try:
                if not self._update_gate.may_claim():  # below the forced-update floor, updating, or a failed rollback
                    causes.setdefault("throttled", "below_update_floor" if self._update_gate.below_floor else "update_gate_closed")
            except Exception:  # noqa: BLE001 - an unreadable gate must block
                causes.setdefault("throttled", "update_gate_error")
        if not runner_ok:
            causes.setdefault("maintenance" if self._runner_gave_up else "throttled",
                              "runner_failed" if self._runner_gave_up else "runner_not_ready")
        for state in PRECEDENCE:
            if state in causes:
                with self._lock:
                    self._state, self._detail = state, {"code": causes[state], "causes": sorted(causes)}
                return
        with self._lock:
            self._state, self._detail = "online", {"code": "ok"}

    def _thermal_check(self) -> str:
        if self._guard is None:
            return "ok"
        try:
            return self._guard.check()
        except Exception:  # noqa: BLE001
            return "ok"  # ThermalGuard's own rule: an unreadable sensor never blocks

    # -- one pass of the main loop ------------------------------------------------------------------------------ #
    def tick(self) -> None:
        self._sample()
        self._manage_runner()
        self._recompute()
        self._push_availability()

    def _sample(self) -> None:
        try:
            samples = list(self._sampler() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("telemetry sample failed: %s", type(exc).__name__)
            samples = []
        with self._lock:
            self._samples, self._samples_at = samples, self._clock()

    def _fresh_samples(self) -> list[Any]:
        with self._lock:
            if self._samples_at is None or self._clock() - self._samples_at > self._s.telemetry_max_age_seconds:
                return []
            return list(self._samples)

    def _hottest(self) -> Optional[float]:
        temps = [s.temp_c for s in self._fresh_samples() if getattr(s, "temp_c", None) is not None]
        return max(temps) if temps else None

    def thermal_sampler(self) -> list[Any]:
        """Sampler for the ThermalGuard: the supervisor's own cached native samples."""
        return self._fresh_samples()

    # -- runner management ---------------------------------------------------------------------------------------- #
    def _proc_alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def _manage_runner(self) -> None:
        with self._lock:
            if self._shutting_down or self._runner_gave_up:
                return
        if self._proc is not None and not self._proc_alive():
            logger.warning("runner exited (code %s)", self._proc.poll())
            self._on_runner_down()
        if self._proc is None:
            if self._clock() >= self._next_launch_at:
                self._launch()
            return
        if self._client is None or not self._client.is_open():
            self._connect()
        if self._client is not None and self._client.is_open() and not self._started_sent and self._runner_ready:
            self._send_start()

    def _launch(self) -> None:
        now = self._clock()
        self._restarts = [t for t in self._restarts if now - t < self._s.restart_window_seconds]
        if len(self._restarts) > self._s.max_restarts:
            with self._lock:
                self._runner_gave_up = True
            logger.error("runner keeps failing (%d restarts in %.0fs); not restarting", len(self._restarts),
                         self._s.restart_window_seconds)
            return
        try:
            if self._provision is not None:
                self._provision(self._token)
            self._proc = self._launcher.launch()
            self._restarts.append(now)
        except Exception as exc:  # noqa: BLE001
            logger.error("could not start the runner: %s", type(exc).__name__)
            self._proc = None
            self._restarts.append(now)
            self._next_launch_at = now + self._backoff()

    def _backoff(self) -> float:
        steps = self._s.restart_backoff_seconds
        return steps[min(max(len(self._restarts) - 1, 0), len(steps) - 1)]

    def _on_runner_down(self) -> None:
        self._close_client()
        with self._lock:
            self._proc = None
            self._runner_ready = False
            self._started_sent = False
            self._runner_active_jobs = 0
            self._runner_draining = False
            self._last_pushed = None
        self._next_launch_at = self._clock() + self._backoff()

    def _connect(self) -> None:
        self._close_client()
        try:
            client = self._client_factory(
                self._s.control_host, self._s.control_port, self._token, self._on_message,
                allow_private=self._s.allow_private_control_address, on_close=self._on_client_close,
            )
            client.connect()
        except Exception as exc:  # noqa: BLE001 - the runner may not be listening yet
            logger.debug("control connect failed: %s", type(exc).__name__)
            return
        self._client = client

    def _on_client_close(self) -> None:
        with self._lock:
            self._runner_ready = False
            self._started_sent = False
            self._last_pushed = None

    def _close_client(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def _send_start(self) -> None:
        limits = self._limits_payload()
        if not limits or self._send_quiet("set_limits", limits):
            if self._send_quiet("start", {}):
                with self._lock:
                    self._started_sent = True

    def _limits_payload(self) -> dict[str, Any]:
        keys = ("gpu_temp_cap_c", "gpu_temp_resume_c", "gpu_temp_hard_stop_c", "gpu_power_limit_w", "max_concurrent_jobs")
        out: dict[str, Any] = {k: self._gpu_limits[k] for k in keys if k in self._gpu_limits and self._gpu_limits[k] is not None}
        if self._guard is not None:
            out.setdefault("gpu_temp_cap_c", self._guard.cap_c)
            out.setdefault("gpu_temp_resume_c", self._guard.resume_c)
            out.setdefault("gpu_temp_hard_stop_c", self._guard.hard_stop_c)
        return out

    def _push_availability(self) -> None:
        """Tell the runner the current availability and the native temperature (its thermal guard is fed by it)."""
        with self._lock:
            ready = self._runner_ready and self._client is not None
        if not ready:
            return
        data: dict[str, Any] = {"availability": self.availability()}
        temp = self._hottest()
        if temp is not None:
            data["gpu_temp_c"] = float(temp)
        self._send_quiet("set_availability", data)

    def _send_quiet(self, msg_type: str, data: dict[str, Any]) -> bool:
        c = self._client
        if c is None or not c.is_open():
            return False
        try:
            c.send(msg_type, data)
            return True
        except (ControlChannelError, OSError) as exc:
            logger.debug("control send %s failed: %s", msg_type, type(exc).__name__)
            return False

    def _on_message(self, msg: ControlMessage) -> None:
        with self._lock:
            if msg.type == "ready":
                self._runner_ready = True
            elif msg.type == "usage":
                self._runner_active_jobs = int(msg.data.get("active_jobs", 0))
                self._runner_draining = bool(msg.data.get("draining", False))
            elif msg.type == "job_started":
                self._runner_active_jobs = int(msg.data.get("active_jobs", 0))
            elif msg.type == "job_finished":
                self._runner_active_jobs = int(msg.data.get("active_jobs", 0))
            elif msg.type == "error":
                code = str(msg.data.get("code", "unknown"))
                self.last_error_codes = (self.last_error_codes + [code])[-20:]
                logger.warning("runner reported error %s", code)
        if msg.type in ("job_started", "job_finished", "usage") and self._on_job_event is not None:
            try:  # outside the lock: the governance probes must not run while it is held
                self._on_job_event()
            except Exception as exc:  # noqa: BLE001
                logger.debug("job event hook failed: %s", type(exc).__name__)

    def _active_jobs(self) -> int:
        with self._lock:
            return self._runner_active_jobs

    def _terminate_process(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=self._s.kill_wait_seconds)
        except Exception:  # noqa: BLE001
            pass
        if p.poll() is None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _apply_power_limit(self) -> None:
        limit = self._gpu_limits.get("gpu_power_limit_w")
        if limit:
            from ..thermal import apply_power_limit

            apply_power_limit(limit)  # best effort, never raises

    # -- heartbeat ----------------------------------------------------------------------------------------------------- #
    def heartbeat_health(self) -> dict[str, Any]:
        """Health block: the contract defaults, native telemetry, and the availability detail."""
        base: dict[str, Any]
        if self._health_base is not None:
            base = dict(self._health_base(self._report))
        else:
            from ..heartbeat import default_health

            base = default_health(self._report)
        base.update(health_from_samples(self._fresh_samples()))
        for provider in self._health_extras:  # additive keys: health.runtime, health.update
            try:
                extra = provider()
            except Exception as exc:  # noqa: BLE001 - a failing provider drops its keys, never the heartbeat
                logger.debug("health provider failed: %s", type(exc).__name__)
                continue
            if isinstance(extra, dict):
                base.update(extra)
        snap = self.snapshot()
        code = str(snap["detail"].get("code", ""))
        # ``reason`` is the stable cause code, never free text: the human line can name the owner's foreground app.
        base["availability_detail"] = {"state": snap["availability"], "code": code, "reason": code}
        return base

    def heartbeat_once(self, *, override_detail: Optional[str] = None) -> Any:
        from ..heartbeat import send_contract_heartbeat

        send = self._heartbeat_send or send_contract_heartbeat
        self._recompute()
        health = self.heartbeat_health()
        if override_detail:
            health["availability_detail"] = {"state": "draining", "code": override_detail, "reason": override_detail}
        availability = "throttled" if override_detail else self.heartbeat_availability()
        response = send(
            self._base_url, self._machine_id, self._secret,
            report=self._report, availability=availability, health=health, transport=self._transport,
        )
        with self._lock:
            self._hb_ok_at = self._clock()  # an exception above means the heartbeat was NOT accepted
        return response

    def _heartbeat_quiet(self, detail: Optional[str] = None) -> None:
        try:
            self.heartbeat_once(override_detail=detail)
        except Exception as exc:  # noqa: BLE001
            logger.debug("final heartbeat failed: %s", type(exc).__name__)

    # -- threads -------------------------------------------------------------------------------------------------------- #
    def _main_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive any single failure
                logger.error("supervisor tick failed: %s", type(exc).__name__)
            self._stop.wait(self._s.tick_seconds)

    def _update_loop(self) -> None:
        runner = self._update_runner
        if runner is None:
            return
        try:
            runner(self._stop)
        except Exception as exc:  # noqa: BLE001 - the updater must never take the supervisor down
            logger.error("update scheduler stopped: %s", type(exc).__name__)

    def _heartbeat_loop(self) -> None:
        from ..heartbeat import HeartbeatAuthError, HeartbeatStaleError

        while not self._stop.is_set():
            try:
                self.heartbeat_once()
            except HeartbeatAuthError as exc:
                logger.error("heartbeat rejected: the credential is no longer valid; re-enrol")
                self._fatal = exc
                self._stop.set()
                return
            except HeartbeatStaleError:
                logger.warning("heartbeat rejected as stale (clock skew?)")
            except Exception as exc:  # noqa: BLE001 - transient network error
                logger.warning("heartbeat failed: %s", type(exc).__name__)
            self._stop.wait(self._hb_interval)

    def _pulse(self, progress: Optional[Callable[[], None]]) -> None:
        if progress is not None:
            try:
                progress()
            except Exception:  # noqa: BLE001
                pass
        if self._sleeper is not None:
            self._sleeper(0.25)  # tests inject a sleeper that advances a fake clock
        else:
            self._stop.wait(0.25)


# --------------------------------------------------------------------------- #
# Wiring for the service / console entry points
# --------------------------------------------------------------------------- #
CONTROL_TOKEN_NAME = "control_token"


def ensure_control_token(store: Any) -> str:
    """The per-install control token: created once (256 random bits), kept in the secret store, never in a file."""
    existing = store.load(CONTROL_TOKEN_NAME)
    if existing:
        return existing
    token = generate_token()
    store.store(CONTROL_TOKEN_NAME, token)
    return token


def build_supervisor(
    layout: Any = None,
    *,
    store: Any = None,
    launcher_factory: Optional[Callable[..., Any]] = None,
    governance_factory: Optional[Callable[..., Any]] = None,
    updater_factory: Optional[Callable[..., Any]] = None,
    runtime_measure: Optional[Callable[..., Any]] = None,
) -> Supervisor:
    """Assemble a production Supervisor from the ProgramData layout and the secret store. Raises when not enrolled.

    The factories exist for tests. ``governance_factory`` defaults to ``governance.wiring.build_governance`` and, like it,
    raises on a policy or probe that cannot be built (the service then does not start: fail closed); an UPDATER that cannot
    be built only disables updates, it never stops the host from being supervised."""
    from ..config import AgentConfig
    from ..hardware import build_capability_report
    from ..thermal import ThermalGuard
    from .enrol import load_protected_config
    from .governance.wiring import build_governance
    from .paths import Layout, install_acl_platform
    from .secrets_dpapi import DpapiSecretStore
    from .supervisor_wiring import (
        RuntimeHealthCache, SupervisorUpdateGate, UpdateScheduler, build_updater, make_runtime_measure, static_update_health,
    )

    layout = layout or Layout.default()
    plat = install_acl_platform(layout)
    from .. import __version__

    plat.supervisor_version = __version__  # reported in the heartbeat `platform` object (plan 3.4)
    if store is None:
        from .paths import restrict_path

        store = DpapiSecretStore(layout.secrets_dir, prepare_directory=restrict_path)
    config = load_protected_config(layout, store)
    if not config.has_machine_credential():
        raise RuntimeError("machine is not enrolled: run `computecoin-agent service enroll --token ... --server ...` first")
    settings = SupervisorSettings.load(layout.supervisor_file)
    token = ensure_control_token(store)
    if launcher_factory is None:
        from .runtime.wsl import subprocess_runner

        def launcher_factory(st: SupervisorSettings) -> Any:  # noqa: E306
            return WslRunnerLauncher(st, run=subprocess_runner)
    launcher = launcher_factory(settings)

    holder: dict[str, Any] = {}  # filled once the Supervisor exists; the collaborators below call back into it

    def active_jobs() -> int:
        sup_ = holder.get("sup")
        return sup_.active_jobs() if sup_ is not None else 0

    def stop_job(decision: Any) -> None:
        sup_ = holder.get("sup")
        if sup_ is not None:
            sup_.abort_running_job(str(getattr(decision, "code", "governance")))

    # GOVERNANCE (W5): the engine's decision is the availability source. None only when the owner switched it off.
    governance = (governance_factory or build_governance)(
        config, settings, layout, store, active_jobs=active_jobs, on_stop_job=stop_job,
    )

    # RUNTIME HEALTH (health.runtime), measured in the background at most every 5 minutes.
    runtime_cache = RuntimeHealthCache(
        runtime_measure or make_runtime_measure(layout), smoke_allowed=lambda: active_jobs() == 0,
    )
    health_extras: list[Callable[[], dict[str, Any]]] = [runtime_cache.provider()]

    # UPDATES (W6): scheduled by the supervisor, never while a job runs, drained through the control channel first.
    update_gate: Any = None
    update_runner: Optional[Callable[[threading.Event], None]] = None
    updater: Any = None
    scheduler: Any = None
    if config.update_enabled:
        try:
            update_gate = SupervisorUpdateGate(__version__, active_jobs)
            updater = (updater_factory or build_updater)(
                config, layout, update_gate,
                health_check=lambda _version: bool(holder.get("sup") is not None and holder["sup"].update_health_ok()),
                version=__version__,
            )
            scheduler = UpdateScheduler(
                updater, update_gate,
                interval_seconds=float(config.update_check_interval_seconds),
                begin_drain=lambda: holder["sup"].begin_update_drain(),
                end_drain=lambda: holder["sup"].end_update_drain(),
                active_jobs=active_jobs,
                drain_complete=lambda: holder["sup"].drain_complete(),
            )
            if scheduler.enabled:
                update_runner = scheduler.run
            health_extras.append(scheduler.provider())
        except Exception as exc:  # noqa: BLE001 - no updates is safe; a half-built updater is not
            logger.error("updates are disabled: the updater could not be built (%s)", type(exc).__name__)
            update_gate, updater, scheduler, update_runner = None, None, None, None
    if scheduler is None:
        health_extras.append(static_update_health(__version__))
    attach = getattr(plat.updater, "attach", None)
    if updater is not None and callable(attach):
        attach(updater)  # the PAL Updater (platform/windows.py) delegates to updater/pal.py

    sup = Supervisor(
        base_url=config.api_url, machine_id=config.machine_id, machine_secret=config.machine_secret, token=token,
        settings=settings, launcher=launcher, report=build_capability_report(config_gpu_vendor=config.gpu_vendor),
        heartbeat_interval=float(config.heartbeat_interval_seconds),
        gpu_limits={"gpu_power_limit_w": config.gpu_power_limit_w},
        token_provisioner=getattr(launcher, "provision_token", None),
        availability_source=governance.source if governance is not None else None,
        on_job_event=governance.on_job_event if governance is not None else None,
        health_extras=health_extras,
        update_gate=update_gate,
        update_runner=update_runner,
        on_shutdown=[governance.close] if governance is not None else [],
    )
    holder["sup"] = sup
    sup.set_thermal_guard(ThermalGuard.from_config(config, sampler=sup.thermal_sampler))
    return sup
