"""Supervisor wiring for runtime health (W3) and agent updates (W6). Nothing here is executed on import.

RUNTIME HEALTH. :class:`RuntimeHealthCache` reports ``health.runtime = {distro_ok, gpu_smoke_ok}`` from
``RuntimeProvisioner.health()`` and the GPU smoke test (``runtime/health.py``). Both can take many seconds (they run
``wsl.exe``; the smoke test starts a container), so the cache measures on a background thread, never from the heartbeat,
and measures at most every ``min_interval`` seconds (default and floor 300). While a job runs the container smoke test
is skipped and the previous smoke result is reused; the cheap health checks still run. A key that cannot be measured is
left out (the API treats a missing value as "not reported", never as a failure); only a measured failure is ``False``.

UPDATES. :class:`UpdateScheduler` drives ``updater.core.AgentUpdater`` from the supervisor, in its own thread:

1. at the configured interval it runs ``run_once()`` with the install gate CLOSED, which fetches and verifies the signed
   manifest (``GET /v1/agents/releases/manifest`` with the machine HMAC, ``updater/transport.py``) and answers
   ``deferred_job_running`` when an update is wanted;
2. an update is wanted: the supervisor drains through the control channel (the runner finishes its job, claims nothing new);
3. only when no job is running (and the runner acknowledged the drain, or a short wait passed) the gate opens and
   ``run_once()`` installs, health-checks and rolls back by itself. The drain always ends, whatever the result.

A host below the forced-update floor (``updater/gate.py``) hands out no claim at all (the supervisor reads
:class:`SupervisorUpdateGate`), and its drain has no timeout: it must update. After an install the service is normally
replaced and restarted by the MSI; the new process calls ``resume_after_restart()`` first, which confirms or rolls back.

TODO(verify-docs) (X11), not read from documentation: ``msiexec`` switches and exit codes (``updater/msi.py``);
``Get-AuthenticodeSignature`` and its ``Status`` value ``Valid``; ``powershell.exe`` location; reading the installed
version by running the bundled interpreter. The Authenticode check only proves "validly signed by someone trusted"; pinning
the publisher certificate is an owner decision (the manifest SHA-256 already pins the exact bytes).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..updater.core import (
    DEFERRED_JOB,
    ERROR,
    ROLLBACK_FAILED,
    ROLLED_BACK,
    AgentUpdater,
    UpdateResult,
)
from ..updater.gate import UpdateGate
from ..updater.version import compare, is_valid_version

logger = logging.getLogger("computecoin_agent.windows.supervisor_wiring")

RUNTIME_HEALTH_MIN_INTERVAL_SECONDS = 300.0  # health.runtime is refreshed at most this often
ROLLBACK_FAILURE_CODES = ("install_failed", "health_check_failed", "install_incomplete")
ROLLED_BACK_WINDOW_SECONDS = 7 * 24 * 3600.0  # a rollback older than this no longer counts as "the last update"


# --------------------------------------------------------------------------- #
# Runtime health (health.runtime)
# --------------------------------------------------------------------------- #
Measure = Callable[[Optional[dict], bool], Optional[dict]]  # (previous result, smoke allowed) -> {distro_ok, gpu_smoke_ok} | None


class RuntimeHealthCache:
    """Cached ``{"distro_ok": bool, "gpu_smoke_ok": bool}``. ``get()`` never blocks: it returns the last result and starts
    a refresh in the background when one is due. ``background=False`` measures inline (tests)."""

    def __init__(
        self,
        measure: Measure,
        *,
        min_interval: float = RUNTIME_HEALTH_MIN_INTERVAL_SECONDS,
        smoke_allowed: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self._measure = measure
        self._min = max(float(min_interval), RUNTIME_HEALTH_MIN_INTERVAL_SECONDS)  # never more often than every 5 minutes
        self._smoke_allowed = smoke_allowed
        self._clock = clock
        self._background = background
        self._lock = threading.Lock()
        self._value: Optional[dict] = None
        self._at: Optional[float] = None
        self._running = False

    def _due(self) -> bool:
        return self._at is None or self._clock() - self._at >= self._min

    def refresh(self) -> None:
        """Measure now (blocking). A failing measurement is recorded as a failed distro, never as a pass."""
        with self._lock:
            previous = dict(self._value) if self._value else None
        try:
            allowed = bool(self._smoke_allowed())
        except Exception:  # noqa: BLE001
            allowed = False
        try:
            value = self._measure(previous, allowed)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check (L1)
            logger.warning("runtime health measurement failed: %s", type(exc).__name__)
            value = {"distro_ok": False, "gpu_smoke_ok": False}
        clean = {k: v for k, v in (value or {}).items() if k in ("distro_ok", "gpu_smoke_ok") and isinstance(v, bool)}
        with self._lock:
            self._value = clean or None
            self._at = self._clock()
            self._running = False

    def get(self) -> dict:
        start = False
        with self._lock:
            if self._due() and not self._running:
                self._running = True
                start = True
            current = dict(self._value) if self._value else {}
        if start:
            if self._background:
                threading.Thread(target=self.refresh, name="runtime-health", daemon=True).start()
            else:
                self.refresh()
                with self._lock:
                    current = dict(self._value) if self._value else {}
        return current

    def provider(self) -> Callable[[], dict]:
        """For ``Supervisor(health_extras=[...])``: ``{"runtime": {...}}``, or ``{}`` when nothing is known yet."""
        def extra() -> dict:
            value = self.get()
            return {"runtime": value} if value else {}
        return extra


def make_runtime_measure(layout: Any, *, provisioner_factory: Optional[Callable[[Any], Any]] = None) -> Measure:
    """The production measurement: the provisioner health checks, plus the GPU smoke test when it is allowed.

    Reads ``runtime-settings.json`` and ``runtime-manifest.json`` from the ProgramData layout. A host with no settings file
    has no managed runtime to judge: nothing is reported."""
    def measure(previous: Optional[dict], smoke_allowed: bool) -> Optional[dict]:
        from .runtime.health import run_gpu_smoke
        from .runtime.provision import RuntimeProvisioner, RuntimeSettings

        settings_file = Path(layout.runtime_settings_file)
        if not settings_file.is_file():
            return None
        settings = RuntimeSettings.from_file(str(settings_file))
        prov = provisioner_factory(settings) if provisioner_factory else RuntimeProvisioner(settings)
        envelope: Optional[bytes] = None
        manifest = None
        manifest_file = Path(layout.runtime_manifest_file)
        if manifest_file.is_file():
            try:
                envelope = manifest_file.read_bytes()
                manifest = prov.load_manifest(envelope)  # signature first; a refused manifest is simply not used
            except Exception as exc:  # noqa: BLE001
                logger.debug("runtime manifest not usable for health: %s", type(exc).__name__)
                envelope, manifest = None, None
        report = prov.health(envelope)
        out: dict[str, bool] = {"distro_ok": bool(report.ok)}
        if not report.ok:
            out["gpu_smoke_ok"] = False  # no usable distro, so the GPU path is not proven
        elif manifest is not None and smoke_allowed:
            check = run_gpu_smoke(prov.cli, manifest.smoke_test_image, min_gpus=prov.s.min_gpus, runner_user=prov.s.runner_user)
            out["gpu_smoke_ok"] = bool(check.ok)
        elif previous and isinstance(previous.get("gpu_smoke_ok"), bool):
            out["gpu_smoke_ok"] = previous["gpu_smoke_ok"]  # skipped (a job is running, or no manifest): keep what was proven
        return out

    return measure


# --------------------------------------------------------------------------- #
# Update gate and scheduler
# --------------------------------------------------------------------------- #
class SupervisorUpdateGate(UpdateGate):
    """``UpdateGate`` for a supervisor whose jobs run in the runner: the job count comes from the control channel, not from
    claim-loop slots, and an install is refused until the scheduler has drained the host and opened the gate."""

    def __init__(self, current_version: str, active_jobs: Callable[[], int], floor: Optional[str] = None) -> None:
        super().__init__(current_version, floor)
        self._active_jobs = active_jobs
        self._install_allowed = False

    def allow_install(self, allowed: bool) -> None:
        with self._lock:
            self._install_allowed = bool(allowed)

    def try_begin_update(self) -> bool:
        with self._lock:
            allowed = self._install_allowed
        if not allowed:
            return False
        try:
            if int(self._active_jobs()) > 0:  # never while a job is running, whatever else is true
                return False
        except Exception:  # noqa: BLE001 - an unknown job count is "a job may be running"
            return False
        return super().try_begin_update()

    @property
    def jobs_running(self) -> bool:
        try:
            return int(self._active_jobs()) > 0 or super().jobs_running
        except Exception:  # noqa: BLE001
            return True


class UpdateScheduler:
    """See the module docstring. ``step()`` is one synchronous pass (tests drive it with a fake clock); ``run`` is the
    supervisor's thread body."""

    def __init__(
        self,
        updater: AgentUpdater,
        gate: SupervisorUpdateGate,
        *,
        interval_seconds: float,
        begin_drain: Callable[[], None],
        end_drain: Callable[[], None],
        active_jobs: Callable[[], int],
        drain_complete: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        first_check_delay: float = 120.0,
        error_retry_seconds: float = 900.0,
        drain_ack_seconds: float = 30.0,
        drain_timeout_seconds: float = 6 * 3600.0,
    ) -> None:
        self._u, self._gate = updater, gate
        self._interval = float(interval_seconds)
        self._begin_drain, self._end_drain = begin_drain, end_drain
        self._active_jobs, self._drain_complete = active_jobs, drain_complete
        self._clock, self._wall = clock, wall_clock
        self._error_retry = float(error_retry_seconds)
        self._ack_wait = float(drain_ack_seconds)
        self._drain_timeout = float(drain_timeout_seconds)
        self._next_check_at = clock() + float(first_check_delay)
        self._phase = "idle"
        self._drain_started = 0.0
        self.last_result: Optional[UpdateResult] = None

    # -- scheduling ------------------------------------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self._interval > 0

    def _finish(self, result: UpdateResult) -> None:
        self.last_result = result
        retry = result.status in (ERROR, DEFERRED_JOB)  # a failed fetch or an install that lost a race: try again sooner
        wait = min(self._error_retry, self._interval) if retry else self._interval
        self._next_check_at = self._clock() + wait
        if result.status == ROLLBACK_FAILED:
            logger.error("update failed and the previous version could not be restored: the host claims nothing until repaired")

    def step(self) -> Optional[UpdateResult]:
        if not self.enabled:
            return None
        now = self._clock()
        if self._phase == "idle":
            if now < self._next_check_at:
                return None
            result = self._u.run_once()  # gate closed: fetch + verify; installs nothing
            if result.status != DEFERRED_JOB:
                self._finish(result)
                return result
            self._phase, self._drain_started = "draining", now
            logger.info("an update to %s is available; draining before installing", result.to_version)
            self._begin_drain()
            return None
        # phase == "draining"
        forced = self._gate.below_floor
        if self._active_jobs() > 0:
            if not forced and now - self._drain_started > self._drain_timeout:
                self._phase = "idle"
                self._gate.cancel_drain()  # run_once() set the gate's drain flag when it deferred; nothing else clears it here
                self._end_drain()
                result = UpdateResult(DEFERRED_JOB, "drain_timeout", self._u.current_version)
                self._finish(result)
                return result
            return None
        if not self._drain_complete() and now - self._drain_started < self._ack_wait:
            self._begin_drain()  # (re)send; the runner has not acknowledged yet
            return None
        self._gate.allow_install(True)
        try:
            result = self._u.run_once()
        finally:
            self._gate.allow_install(False)
            self._phase = "idle"
            self._gate.cancel_drain()  # run_once() returns before its own cancel_drain when it never reaches an install
            self._end_drain()
        self._finish(result)
        return result

    def resume_after_restart(self) -> Optional[UpdateResult]:
        """Call once at start-up (the service was replaced by an install): confirm the new version or roll back."""
        result = self._u.resume_after_restart()
        if result is not None:
            self.last_result = result
        return result

    def run(self, stop: threading.Event, poll_seconds: float = 5.0) -> None:
        if self._u.state.known_good is None:
            logger.warning("no known-good installer is recorded, so a failed first update could not be rolled back")
        try:
            self.resume_after_restart()
        except Exception as exc:  # noqa: BLE001
            logger.error("resuming after restart failed: %s", type(exc).__name__)
        while not stop.wait(poll_seconds):
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                logger.error("update step failed: %s", type(exc).__name__)
                self._phase = "idle"
                self._gate.allow_install(False)
                self._gate.cancel_drain()
                self._end_drain()
                self._next_check_at = self._clock() + self._error_retry

    # -- health.update ------------------------------------------------------------------------------------------------ #
    def health(self) -> dict:
        """``{"rolled_back": bool, "version": str}`` from the updater's durable state (and the last result in memory)."""
        version = str(self._u.current_version)
        rolled = bool(self.last_result is not None and self.last_result.status in (ROLLED_BACK, ROLLBACK_FAILED))
        try:
            now = self._wall()
            for failed_version, rec in (self._u.state.data.get("failed") or {}).items():
                if (isinstance(rec, dict) and rec.get("code") in ROLLBACK_FAILURE_CODES and is_valid_version(failed_version)
                        and compare(failed_version, version) > 0 and now - float(rec.get("at", 0)) < ROLLED_BACK_WINDOW_SECONDS):
                    rolled = True
        except Exception:  # noqa: BLE001 - unreadable state: report only what is known in memory
            pass
        return {"rolled_back": rolled, "version": version}

    def provider(self) -> Callable[[], dict]:
        return lambda: {"update": self.health()}


def static_update_health(version: str) -> Callable[[], dict]:
    """``health.update`` for a host with no updater (updates off or not built): the version, no rollback."""
    return lambda: {"update": {"rolled_back": False, "version": str(version)}}


# --------------------------------------------------------------------------- #
# The Windows install step's two checks and the updater factory
# --------------------------------------------------------------------------- #
def _capture(argv: Sequence[str], *, env: Optional[dict] = None, timeout: float = 120.0) -> tuple[int, str]:  # pragma: no cover - runs a process
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(list(argv), shell=False, capture_output=True, text=True, timeout=timeout, check=False,
                              env=env, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout or ""


def _run_msiexec(argv: list) -> int:  # pragma: no cover - runs msiexec
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(list(argv), shell=False, timeout=1800, check=False, creationflags=flags).returncode


def authenticode_valid(path: "str | Path", capture: Callable[..., tuple] = _capture) -> bool:
    """True only when Windows reports the file's Authenticode signature as ``Valid``. The path travels in an environment
    variable, never inside the PowerShell command text. Any error is False (X10, L1)."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    env = {**os.environ, "COMPUTECOIN_VERIFY_PATH": str(path)}
    argv = [powershell, "-NoProfile", "-NonInteractive", "-Command",
            "(Get-AuthenticodeSignature -LiteralPath $env:COMPUTECOIN_VERIFY_PATH).Status"]
    try:
        code, out = capture(argv, env=env)
    except Exception:  # noqa: BLE001
        return False
    return code == 0 and out.strip() == "Valid"


def installed_agent_version(capture: Callable[..., tuple] = _capture, python: Optional[str] = None) -> str:
    """The agent version that is INSTALLED (not the running process's, which an upgrade does not change until restart):
    ask the bundled interpreter. Returns '' when it cannot be read, which never equals a target version."""
    exe = python or str(Path(sys.base_prefix) / "python.exe")
    try:
        code, out = capture([exe, "-I", "-c", "import computecoin_agent; print(computecoin_agent.__version__)"])
    except Exception:  # noqa: BLE001
        return ""
    version = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
    return version if is_valid_version(version) else ""


def build_updater(
    config: Any,
    layout: Any,
    gate: UpdateGate,
    *,
    health_check: Callable[[str], bool],
    version: str,
    manifest_transport: Any = None,
    download_transport: Any = None,
    public_keys: Optional[Sequence[str]] = None,
    run: Optional[Callable[[list], int]] = None,
    capture: Optional[Callable[..., tuple]] = None,
    ensure_private_dir: Optional[Callable[[Path], None]] = None,
    **updater_kwargs: Any,
) -> AgentUpdater:
    """An ``AgentUpdater`` for this host: interval-independent (the scheduler owns timing), ring and enablement from the
    ``update_*`` config fields, the manifest polled with the machine HMAC through ``updater/transport.py``, the pinned
    public keys from ``updater/manifest.py`` (none pinned means every manifest is refused). All collaborators are injectable."""
    from ..updater.manifest import load_pinned_public_keys
    from ..updater.msi import MsiInstaller
    from ..updater.state import UpdateState
    from ..updater.transport import ApiManifestSource, HttpDownloader

    cap = capture or _capture
    if ensure_private_dir is None:
        from .paths import restrict_path as ensure_private_dir  # type: ignore[assignment]
    installer = MsiInstaller(
        run or _run_msiexec,
        lambda: installed_agent_version(cap),
        verify_signature=lambda p: authenticode_valid(p, cap),
        log_dir=Path(layout.logs_dir),
    )
    return AgentUpdater(
        current_version=version,
        platform="windows",
        ring=str(config.update_ring),
        public_keys=tuple(public_keys if public_keys is not None else load_pinned_public_keys()),
        fetch_manifest=ApiManifestSource(config.api_url, config.machine_id, config.machine_secret, transport=manifest_transport),
        download=HttpDownloader(transport=download_transport),
        installer=installer,
        health_check=health_check,
        gate=gate,
        state=UpdateState(layout.update_state_file),
        work_dir=Path(layout.update_dir),
        ensure_private_dir=ensure_private_dir,
        enabled=bool(config.update_enabled),
        **updater_kwargs,
    )
