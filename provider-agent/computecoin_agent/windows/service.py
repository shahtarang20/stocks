"""Windows Service entry for the supervisor, with a clean fallback to a console ``run`` mode (W2.1).

Structure (so almost all of it is testable on Linux):

* :class:`ServiceApp`        pure logic: report START_PENDING / RUNNING, wait for stop, shut the supervisor down within
                             a timeout while reporting STOP_PENDING with an advancing checkpoint, report STOPPED.
* :func:`build_service_class` the thin pywin32 adapter (``win32serviceutil.ServiceFramework`` subclass). The pywin32 modules
                             are passed in, so tests pass fakes; the module-level ``ComputeCoinService`` exists only when
                             pywin32 imports.
* :class:`ServiceManager`    install / start / stop / remove / status through ``win32serviceutil`` (injected).
* :func:`run_console`        the same ``ServiceApp`` driven by Ctrl+C / Ctrl+Break / SIGTERM through the PAL's
                             ``SignalsShutdown`` (no direct ``signal`` calls outside the PAL, X8).

Stop timing (documentation read 2026-11-06,
https://learn.microsoft.com/windows/win32/services/service-control-handler-function): the control handler must return
within 30 s, so ``SvcStop`` only reports STOP_PENDING and signals; the real shutdown runs on the ``SvcDoRun`` thread.
At system shutdown a service has about 20 s by default unless it keeps sending STOP_PENDING with a wait hint (limit
125 s via the Services snap-in; ``WaitToKillServiceTimeout`` on reboot). ``ServiceApp`` therefore keeps reporting
STOP_PENDING while the supervisor drains, and the default ``stop_timeout_s`` is 25.

TODO(verify-docs), pywin32 (not read from documentation):
  * ``win32serviceutil.ServiceFramework`` (``_svc_name_``, ``_svc_display_name_``, ``_svc_description_``, ``__init__(args)``,
    ``SvcStop``, ``SvcDoRun``, ``ReportServiceStatus(state, waitHint=, win32ExitCode=, svcExitCode=)``);
  * ``servicemanager.Initialize / PrepareToHostSingle / StartServiceCtrlDispatcher / LogInfoMsg / LogErrorMsg``;
  * ``win32serviceutil.HandleCommandLine(cls, argv=[...])`` with ``--startup auto`` and ``install | remove``;
  * ``win32serviceutil.StartService / StopService / QueryServiceStatus / WaitForServiceStatus``;
  * ERROR_FAILED_SERVICE_CONTROLLER_CONNECT (1063) as the signal "not started by the SCM" -> console fallback;
  * ``SvcShutdown`` being called at system shutdown and how it interacts with ``SvcStop``;
  * the service account: LocalSystem is the default here, but a WSL2 distro is registered PER USER, so the service
    must run as the account that owns the ``computecoin`` distro (W3 decision). Not settled by this package.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger("computecoin_agent.windows.service")

SERVICE_NAME = "ComputeCoinAgent"
SERVICE_DISPLAY_NAME = "ComputeCoin Agent"
SERVICE_DESCRIPTION = "Shares this computer's GPU with the ComputeCoin marketplace (supervisor for the managed runtime)."
SERVICE_CLASS_STRING = "computecoin_agent.windows.service.ComputeCoinService"  # TODO(verify-docs): see HandleCommandLine

START_PENDING, RUNNING, STOP_PENDING, STOPPED = "START_PENDING", "RUNNING", "STOP_PENDING", "STOPPED"
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063  # TODO(verify-docs)
DEFAULT_STOP_TIMEOUT_S = 25.0


class ServiceHost(Protocol):
    """What the SCM adapter offers the app. The fake in the tests implements this too."""

    def report_status(self, state: str, *, wait_hint_ms: int = 0, checkpoint: int = 0, exit_code: int = 0) -> None: ...
    def log_info(self, message: str) -> None: ...
    def log_error(self, message: str) -> None: ...


class ServiceApp:
    """The service body. ``supervisor_factory()`` returns an object with ``start()``, ``shutdown(timeout)`` and
    ``fatal_error()`` (see ``supervisor.Supervisor``)."""

    def __init__(
        self,
        supervisor_factory: Callable[[], Any],
        host: ServiceHost,
        *,
        stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
        poll_seconds: float = 1.0,
        checkpoint_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = supervisor_factory
        self._host = host
        self._stop_timeout = stop_timeout_s
        self._poll = poll_seconds
        self._checkpoint_every = checkpoint_seconds
        self._clock = clock
        self.stop_event = threading.Event()
        self._shutdown_started = threading.Event()
        self.exit_code = 0

    # -- called from the SCM control thread: must return fast ----------------------------------------------------- #
    def request_stop(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._host.report_status(STOP_PENDING, wait_hint_ms=int(self._stop_timeout * 1000), checkpoint=1)
        self.stop_event.set()

    # -- the SvcDoRun thread --------------------------------------------------------------------------------------- #
    def run(self) -> int:
        self._host.report_status(START_PENDING, wait_hint_ms=30000, checkpoint=1)
        try:
            supervisor = self._factory()
            supervisor.start()
        except Exception as exc:  # noqa: BLE001 - not enrolled, bad settings ...
            self._host.log_error(f"supervisor could not start: {exc}")
            self.exit_code = 2
            self._host.report_status(STOPPED, exit_code=self.exit_code)
            return self.exit_code
        self._host.report_status(RUNNING)
        self._host.log_info("ComputeCoin supervisor running")
        while not self.stop_event.wait(self._poll):
            fatal = supervisor.fatal_error()
            if fatal is not None:
                self._host.log_error(f"supervisor stopped on a fatal error: {type(fatal).__name__}")
                self.exit_code = 1
                break
        self._shutdown(supervisor)
        self._host.report_status(STOPPED, exit_code=self.exit_code)
        return self.exit_code

    def _shutdown(self, supervisor: Any) -> None:
        self._shutdown_started.set()
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["r"] = supervisor.shutdown(self._stop_timeout)
            except Exception as exc:  # noqa: BLE001
                result["e"] = exc

        t = threading.Thread(target=work, name="service-shutdown", daemon=True)
        t.start()
        checkpoint = 2
        deadline = self._clock() + self._stop_timeout + 10.0  # the supervisor bounds itself; this is the backstop
        while t.is_alive() and self._clock() < deadline:
            t.join(self._checkpoint_every)
            self._host.report_status(STOP_PENDING, wait_hint_ms=int((self._checkpoint_every + 3) * 1000), checkpoint=checkpoint)
            checkpoint += 1
        if t.is_alive():
            self._host.log_error("supervisor did not finish stopping in time; exiting anyway")
            self.exit_code = self.exit_code or 3
        elif "e" in result:
            self._host.log_error(f"supervisor shutdown failed: {type(result['e']).__name__}")
            self.exit_code = self.exit_code or 3
        else:
            r = result.get("r")
            note = getattr(r, "note", "")
            self._host.log_info("ComputeCoin supervisor stopped" + (f" ({note})" if note else ""))


# --------------------------------------------------------------------------- #
# pywin32 adapter
# --------------------------------------------------------------------------- #
def build_service_class(
    win32serviceutil: Any,
    win32service: Any,
    win32event: Any,
    servicemanager: Any,
    app_factory: Optional[Callable[[ServiceHost], ServiceApp]] = None,
) -> type:
    """Create the ``ServiceFramework`` subclass. Every pywin32 name used is TODO(verify-docs)."""
    state_map = {
        START_PENDING: win32service.SERVICE_START_PENDING,
        RUNNING: win32service.SERVICE_RUNNING,
        STOP_PENDING: win32service.SERVICE_STOP_PENDING,
        STOPPED: win32service.SERVICE_STOPPED,
    }

    def default_app_factory(host: ServiceHost) -> ServiceApp:
        from .supervisor import build_supervisor

        return ServiceApp(build_supervisor, host)

    make_app = app_factory or default_app_factory

    class ComputeCoinService(win32serviceutil.ServiceFramework):  # type: ignore[misc, name-defined]
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self._app: Optional[ServiceApp] = None
            self._pending_stop = False

        # ServiceHost
        def report_status(self, state: str, *, wait_hint_ms: int = 0, checkpoint: int = 0, exit_code: int = 0) -> None:
            kwargs: dict[str, Any] = {}
            if wait_hint_ms:
                kwargs["waitHint"] = wait_hint_ms
            if exit_code:
                kwargs["win32ExitCode"] = 1066  # ERROR_SERVICE_SPECIFIC_ERROR, TODO(verify-docs)
                kwargs["svcExitCode"] = exit_code
            self.ReportServiceStatus(state_map[state], **kwargs)

        def log_info(self, message: str) -> None:
            servicemanager.LogInfoMsg(message)
            logger.info(message)

        def log_error(self, message: str) -> None:
            servicemanager.LogErrorMsg(message)
            logger.error(message)

        # SCM entry points
        def SvcStop(self) -> None:  # noqa: N802 - pywin32 naming
            if self._app is None:
                self._pending_stop = True
                self.report_status(STOP_PENDING, wait_hint_ms=5000, checkpoint=1)
                return
            self._app.request_stop()

        SvcShutdown = SvcStop  # TODO(verify-docs): whether the framework calls SvcShutdown at system shutdown

        def GetAcceptedControls(self) -> int:  # noqa: N802
            """Also accept power events, so the yield engine learns about suspend and resume.
            TODO(verify-docs): pywin32 ServiceFramework.GetAcceptedControls and SERVICE_ACCEPT_POWEREVENT (0x40)."""
            accepted = super().GetAcceptedControls()
            return accepted | getattr(win32service, "SERVICE_ACCEPT_POWEREVENT", 0x40)

        def SvcOtherEx(self, control: int, event_type: int, data: Any) -> int:  # noqa: N802
            """Forward SERVICE_CONTROL_POWEREVENT (0x0D) to the shared latch. Never raises and never blocks: Windows allows
            only about two seconds after a suspend notice. Returns NO_ERROR (0) = no objection.
            TODO(verify-docs): pywin32 SvcOtherEx(control, event_type, data) and the PBT_* codes in event_type."""
            if control == getattr(win32service, "SERVICE_CONTROL_POWEREVENT", 0x0D):
                try:
                    from .governance.probes import shared_power_latch

                    shared_power_latch().handle_power_event(int(event_type))
                except Exception as exc:  # noqa: BLE001 - a bad event must not take the service down
                    logger.warning("power event %r was not recorded (%s)", event_type, type(exc).__name__)
            return 0

        def SvcDoRun(self) -> None:  # noqa: N802
            self._app = make_app(self)
            if self._pending_stop:
                self._app.stop_event.set()
            self._app.run()

    return ComputeCoinService


def _try_pywin32() -> Optional[tuple[Any, Any, Any, Any]]:
    try:
        import servicemanager  # type: ignore[import-not-found]
        import win32event  # type: ignore[import-not-found]
        import win32service  # type: ignore[import-not-found]
        import win32serviceutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    return win32serviceutil, win32service, win32event, servicemanager


_mods = _try_pywin32()
ComputeCoinService: Optional[type] = build_service_class(*_mods) if _mods is not None else None
PYWIN32_AVAILABLE = _mods is not None


# --------------------------------------------------------------------------- #
# install / start / stop / remove / status
# --------------------------------------------------------------------------- #
class ServiceManagerError(RuntimeError):
    pass


class ServiceManager:
    """Service control through pywin32's ``win32serviceutil`` (injected). Names TODO(verify-docs)."""

    def __init__(self, win32serviceutil: Any, win32service: Any, *, runner: Optional[Callable[[list[str]], Any]] = None,
                 name: str = SERVICE_NAME) -> None:
        self._u, self._s, self._run, self.name = win32serviceutil, win32service, runner, name

    def install(self, *, start_type: str = "auto", username: Optional[str] = None) -> None:
        """Install (default account: LocalSystem). A password is deliberately not accepted on argv; for a non-default
        account set it afterwards in the Services console."""
        if start_type not in ("auto", "manual", "disabled", "delayed"):
            raise ServiceManagerError("start_type must be auto, manual, disabled or delayed")
        argv = ["computecoin-service", "--startup", start_type]  # TODO(verify-docs): option spelling
        if username:
            argv += ["--username", username]
        argv.append("install")
        cls = _service_class_or_raise()
        try:
            self._u.HandleCommandLine(cls, argv=argv)
        except Exception as exc:  # noqa: BLE001
            raise ServiceManagerError(f"install failed: {type(exc).__name__}") from None
        self._configure_recovery()

    def _configure_recovery(self) -> None:
        """Restart on crash (3 times, 60 s apart; counter resets after a day). Best effort, never fails the install.
        TODO(verify-docs): ``sc.exe failure`` syntax: ``sc failure <name> reset= <s> actions= restart/<ms>/...``."""
        if self._run is None:
            return
        try:
            self._run(["sc.exe", "failure", self.name, "reset=", "86400",
                       "actions=", "restart/60000/restart/60000/restart/60000"])
        except Exception:  # noqa: BLE001
            logger.warning("could not configure service recovery actions")

    def start(self, wait_seconds: float = 30.0) -> None:
        try:
            self._u.StartService(self.name)
            self._u.WaitForServiceStatus(self.name, self._s.SERVICE_RUNNING, wait_seconds)
        except Exception as exc:  # noqa: BLE001
            raise ServiceManagerError(f"start failed: {type(exc).__name__}") from None

    def stop(self, wait_seconds: float = 60.0) -> None:
        try:
            self._u.StopService(self.name)
            self._u.WaitForServiceStatus(self.name, self._s.SERVICE_STOPPED, wait_seconds)
        except Exception as exc:  # noqa: BLE001
            raise ServiceManagerError(f"stop failed: {type(exc).__name__}") from None

    def remove(self) -> None:
        try:
            self._u.HandleCommandLine(_service_class_or_raise(), argv=["computecoin-service", "remove"])
        except Exception as exc:  # noqa: BLE001
            raise ServiceManagerError(f"remove failed: {type(exc).__name__}") from None

    def status(self) -> str:
        """``running`` | ``stopped`` | ``start_pending`` | ``stop_pending`` | ``paused`` | ``not_installed`` | ``unknown``."""
        try:
            state = self._u.QueryServiceStatus(self.name)[1]
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "winerror", None) == 1060 or (getattr(exc, "args", (None,))[0] == 1060):  # ERROR_SERVICE_DOES_NOT_EXIST
                return "not_installed"
            return "unknown"
        s = self._s
        names = {s.SERVICE_RUNNING: "running", s.SERVICE_STOPPED: "stopped", s.SERVICE_START_PENDING: "start_pending",
                 s.SERVICE_STOP_PENDING: "stop_pending", getattr(s, "SERVICE_PAUSED", -1): "paused"}
        return names.get(state, "unknown")


def _service_class_or_raise() -> type:
    if ComputeCoinService is None:
        raise ServiceManagerError("pywin32 is not installed: pip install 'computecoin-agent[windows]'")
    return ComputeCoinService


def load_service_manager(runner: Optional[Callable[[list[str]], Any]] = None) -> ServiceManager:
    mods = _try_pywin32()
    if mods is None:
        raise ServiceManagerError("pywin32 is not installed: pip install 'computecoin-agent[windows]'")
    if runner is None:
        from .paths import subprocess_runner as runner  # type: ignore[assignment]
    return ServiceManager(mods[0], mods[1], runner=runner)


# --------------------------------------------------------------------------- #
# Console mode
# --------------------------------------------------------------------------- #
class _ConsoleHost:
    def report_status(self, state: str, *, wait_hint_ms: int = 0, checkpoint: int = 0, exit_code: int = 0) -> None:
        logger.info("service state: %s", state)

    def log_info(self, message: str) -> None:
        logger.info(message)

    def log_error(self, message: str) -> None:
        logger.error(message)


def run_console(
    supervisor_factory: Callable[[], Any],
    *,
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    install_signals: Optional[Callable[[Callable[[str], None]], Callable[[], None]]] = None,
) -> int:
    """Run the supervisor in the foreground until Ctrl+C / Ctrl+Break / SIGTERM, then shut down gracefully.

    ``install_signals(handler)`` returns a restore function; the default goes through the PAL's ``SignalsShutdown``.
    """
    app = ServiceApp(supervisor_factory, _ConsoleHost(), stop_timeout_s=stop_timeout_s)
    if install_signals is None:
        def install_signals(handler: Callable[[str], None]) -> Callable[[], None]:  # noqa: E306
            from ..platform import get_platform

            sig = get_platform().signals
            sig.install(handler)
            return sig.restore

    restore = install_signals(lambda reason: app.request_stop())
    try:
        return app.run()
    finally:
        restore()


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m computecoin_agent.windows.service``: SCM dispatch when started by the SCM, console mode otherwise,
    ``HandleCommandLine`` when given install/start/stop/remove arguments."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    mods = _try_pywin32()
    if mods is None or ComputeCoinService is None:
        from .supervisor import build_supervisor

        return run_console(build_supervisor)
    win32serviceutil, win32service, _win32event, servicemanager = mods
    if args:
        win32serviceutil.HandleCommandLine(ComputeCoinService, argv=[sys.argv[0], *args])
        return 0
    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ComputeCoinService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "winerror", None) or (exc.args[0] if getattr(exc, "args", None) else None)
        if code == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            from .supervisor import build_supervisor

            return run_console(build_supervisor)  # started from a console, not by the SCM
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
