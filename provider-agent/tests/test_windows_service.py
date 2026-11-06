"""W2 service lifecycle with a fake service control (no pywin32, no Windows). Written, not run."""

from __future__ import annotations

import logging
import threading
import types

import pytest

from computecoin_agent.windows import service as svc
from computecoin_agent.windows import logging_setup as wlog


class FakeHost:
    def __init__(self):
        self.states: list[tuple] = []
        self.infos: list[str] = []
        self.errors: list[str] = []

    def report_status(self, state, *, wait_hint_ms=0, checkpoint=0, exit_code=0):
        self.states.append((state, wait_hint_ms, checkpoint, exit_code))

    def log_info(self, m):
        self.infos.append(m)

    def log_error(self, m):
        self.errors.append(m)

    @property
    def names(self):
        return [s[0] for s in self.states]


class FakeSupervisor:
    def __init__(self, shutdown_seconds=0.0, fatal=None, fail_start=False):
        self.started = False
        self.shutdown_timeouts: list[float] = []
        self._shutdown_seconds = shutdown_seconds
        self._fatal = fatal
        self._fail_start = fail_start
        self.release = threading.Event()

    def start(self):
        if self._fail_start:
            raise RuntimeError("machine is not enrolled")
        self.started = True

    def fatal_error(self):
        return self._fatal

    def shutdown(self, timeout):
        self.shutdown_timeouts.append(timeout)
        if self._shutdown_seconds:
            self.release.wait(self._shutdown_seconds)
        return types.SimpleNamespace(note="")


def _run_in_thread(app):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("code", app.run()))
    t.start()
    return t, out


def test_start_run_and_graceful_stop_reports_every_state_in_order():
    host, sup = FakeHost(), FakeSupervisor()
    app = svc.ServiceApp(lambda: sup, host, stop_timeout_s=7.0, poll_seconds=0.01, checkpoint_seconds=0.01)
    t, out = _run_in_thread(app)
    for _ in range(300):
        if "RUNNING" in host.names:
            break
        threading.Event().wait(0.01)
    assert sup.started
    app.request_stop()
    t.join(5)
    assert out["code"] == 0
    names = host.names
    assert names[0] == svc.START_PENDING and svc.RUNNING in names and names[-1] == svc.STOPPED
    assert names.index(svc.RUNNING) < names.index(svc.STOP_PENDING)
    assert sup.shutdown_timeouts == [7.0]                         # the supervisor was given the stop timeout
    first_stop = next(s for s in host.states if s[0] == svc.STOP_PENDING)
    assert first_stop[1] == 7000                                  # wait hint tells the SCM how long to wait


def test_request_stop_returns_immediately_and_does_not_do_the_shutdown_itself():
    host, sup = FakeHost(), FakeSupervisor()
    app = svc.ServiceApp(lambda: sup, host)
    app.request_stop()                                            # before run(): must not block or call the supervisor
    assert sup.shutdown_timeouts == [] and host.names == [svc.STOP_PENDING] and app.stop_event.is_set()


def test_stop_pending_checkpoints_advance_while_the_supervisor_drains():
    host, sup = FakeHost(), FakeSupervisor(shutdown_seconds=0.3)
    app = svc.ServiceApp(lambda: sup, host, stop_timeout_s=5.0, poll_seconds=0.01, checkpoint_seconds=0.05)
    t, _ = _run_in_thread(app)
    for _ in range(300):
        if sup.started:
            break
        threading.Event().wait(0.01)
    app.request_stop()
    threading.Event().wait(0.25)
    sup.release.set()
    t.join(5)
    checkpoints = [s[2] for s in host.states if s[0] == svc.STOP_PENDING]
    assert checkpoints == sorted(checkpoints) and len(set(checkpoints)) >= 3


def test_a_supervisor_that_hangs_cannot_hang_the_service_forever():
    host, sup = FakeHost(), FakeSupervisor(shutdown_seconds=30)
    app = svc.ServiceApp(lambda: sup, host, stop_timeout_s=0.05, poll_seconds=0.01, checkpoint_seconds=0.02)
    # the backstop is stop_timeout + 10 s of the injected clock; give it a clock that jumps
    ticks = iter(range(0, 10_000, 5))
    app._clock = lambda: float(next(ticks))
    app.stop_event.set()
    code = app.run()
    sup.release.set()
    assert host.names[-1] == svc.STOPPED and code == 3 and any("did not finish" in e for e in host.errors)


def test_fatal_supervisor_error_stops_the_service_with_a_failure_code():
    host, sup = FakeHost(), FakeSupervisor(fatal=RuntimeError("heartbeat rejected"))
    app = svc.ServiceApp(lambda: sup, host, poll_seconds=0.01, checkpoint_seconds=0.01)
    assert app.run() == 1
    assert host.states[-1][0] == svc.STOPPED and host.states[-1][3] == 1 and sup.shutdown_timeouts


def test_not_enrolled_exits_without_running_and_without_leaking_details():
    host = FakeHost()
    app = svc.ServiceApp(lambda: FakeSupervisor(fail_start=True), host)
    assert app.run() == 2
    assert host.names == [svc.START_PENDING, svc.STOPPED] and host.errors


# --------------------------------------------------------------------------- pywin32 adapter with fakes
class _Framework:
    def __init__(self, args):
        self.args = args
        self.statuses: list[tuple] = []

    def ReportServiceStatus(self, state, waitHint=0, win32ExitCode=0, svcExitCode=0):  # noqa: N802
        self.statuses.append((state, waitHint, win32ExitCode, svcExitCode))


def _fake_modules():
    win32service = types.SimpleNamespace(SERVICE_START_PENDING=2, SERVICE_RUNNING=4, SERVICE_STOP_PENDING=3,
                                         SERVICE_STOPPED=1, SERVICE_PAUSED=7)
    serviceutil = types.SimpleNamespace(ServiceFramework=_Framework, calls=[])
    serviceutil.HandleCommandLine = lambda cls, argv=None: serviceutil.calls.append(("hcl", cls, argv))
    serviceutil.StartService = lambda n: serviceutil.calls.append(("start", n))
    serviceutil.StopService = lambda n: serviceutil.calls.append(("stop", n))
    serviceutil.WaitForServiceStatus = lambda n, st, secs: serviceutil.calls.append(("wait", n, st, secs))
    serviceutil.QueryServiceStatus = lambda n: (16, 4, 0, 0, 0, 0, 0)
    sm = types.SimpleNamespace(logged=[])
    sm.LogInfoMsg = sm.logged.append
    sm.LogErrorMsg = sm.logged.append
    return serviceutil, win32service, types.SimpleNamespace(), sm


def test_service_class_wires_svcdorun_and_svcstop_to_the_app():
    serviceutil, win32service, win32event, sm = _fake_modules()
    host_seen = {}
    sup = FakeSupervisor()

    def app_factory(host):
        host_seen["host"] = host
        return svc.ServiceApp(lambda: sup, host, stop_timeout_s=3.0, poll_seconds=0.01, checkpoint_seconds=0.01)

    cls = svc.build_service_class(serviceutil, win32service, win32event, sm, app_factory)
    assert (cls._svc_name_, cls._svc_display_name_) == (svc.SERVICE_NAME, svc.SERVICE_DISPLAY_NAME)
    inst = cls(["x"])
    t = threading.Thread(target=inst.SvcDoRun)
    t.start()
    for _ in range(300):
        if sup.started:
            break
        threading.Event().wait(0.01)
    inst.SvcStop()
    t.join(5)
    states = [s[0] for s in inst.statuses]
    assert states[0] == 2 and 4 in states and states[-1] == 1           # START_PENDING ... RUNNING ... STOPPED
    assert 3 in states and inst.statuses[states.index(3)][1] == 3000      # STOP_PENDING with the wait hint


def test_a_stop_that_arrives_before_the_app_exists_is_not_lost():
    serviceutil, win32service, win32event, sm = _fake_modules()
    sup = FakeSupervisor()
    cls = svc.build_service_class(serviceutil, win32service, win32event, sm,
                                  lambda host: svc.ServiceApp(lambda: sup, host, poll_seconds=0.01, checkpoint_seconds=0.01))
    inst = cls([])
    inst.SvcStop()                      # SCM sends stop immediately
    inst.SvcDoRun()                     # must return promptly, having stopped
    assert [s[0] for s in inst.statuses][-1] == 1


def test_service_failure_exit_code_is_reported_as_a_service_specific_error():
    serviceutil, win32service, win32event, sm = _fake_modules()
    cls = svc.build_service_class(serviceutil, win32service, win32event, sm,
                                  lambda host: svc.ServiceApp(lambda: FakeSupervisor(fail_start=True), host))
    inst = cls([])
    inst.SvcDoRun()
    last = inst.statuses[-1]
    assert last[0] == 1 and last[3] == 2


def test_service_manager_install_start_stop_remove_status_use_the_expected_calls(monkeypatch):
    serviceutil, win32service, _e, _sm = _fake_modules()
    monkeypatch.setattr(svc, "ComputeCoinService", type("Cls", (), {}))
    ran = []
    mgr = svc.ServiceManager(serviceutil, win32service, runner=lambda argv: ran.append(argv))
    mgr.install(start_type="auto")
    hcl = [c for c in serviceutil.calls if c[0] == "hcl"][0]
    assert hcl[2][-1] == "install" and "auto" in hcl[2]
    assert ran and ran[0][:3] == ["sc.exe", "failure", svc.SERVICE_NAME] and isinstance(ran[0], list)  # argv list, no shell
    mgr.start()
    mgr.stop()
    mgr.remove()
    kinds = [c[0] for c in serviceutil.calls]
    assert kinds.count("start") == 1 and kinds.count("stop") == 1 and kinds.count("wait") == 2
    assert mgr.status() == "running"
    with pytest.raises(svc.ServiceManagerError):
        mgr.install(start_type="whenever")


def test_service_manager_status_not_installed_and_errors_do_not_leak(monkeypatch):
    serviceutil, win32service, _e, _sm = _fake_modules()

    def missing(name):
        err = OSError("x")
        err.winerror = 1060
        raise err

    serviceutil.QueryServiceStatus = missing
    assert svc.ServiceManager(serviceutil, win32service).status() == "not_installed"
    serviceutil.StartService = lambda n: (_ for _ in ()).throw(OSError("secret detail"))
    with pytest.raises(svc.ServiceManagerError) as e:
        svc.ServiceManager(serviceutil, win32service).start()
    assert "secret detail" not in str(e.value)


def test_without_pywin32_the_service_class_is_absent_and_install_says_how_to_fix_it():
    if svc.PYWIN32_AVAILABLE:
        pytest.skip("pywin32 present")
    assert svc.ComputeCoinService is None
    with pytest.raises(svc.ServiceManagerError, match="windows"):
        svc.load_service_manager()


# --------------------------------------------------------------------------- console fallback
def test_console_mode_runs_and_stops_on_a_signal_and_restores_handlers():
    sup = FakeSupervisor()
    holder = {}
    restored = []

    def install(handler):
        holder["handler"] = handler
        return lambda: restored.append(True)

    out = {}

    def go():
        out["code"] = svc.run_console(lambda: sup, stop_timeout_s=2.0, install_signals=install)

    t = threading.Thread(target=go)
    t.start()
    for _ in range(300):
        if sup.started and "handler" in holder:
            break
        threading.Event().wait(0.01)
    holder["handler"]("SIGINT")
    t.join(5)
    assert out["code"] == 0 and restored == [True] and sup.shutdown_timeouts == [2.0]


# --------------------------------------------------------------------------- logging
def test_logging_redacts_secrets_and_rotates_to_a_file(tmp_path):
    logger = wlog.configure_logging(tmp_path / "logs", event_log=False, logger_name="cc_test_logger")
    secret = "msk_" + "ab12cd34" * 8
    token = "f" * 64
    logger.info("enrolled with %s and token %s, enr_abcdefghijkl", secret, token)
    try:
        raise ValueError(f"bad {secret}")
    except ValueError:
        logger.exception("failed")
    for h in logger.handlers:
        h.flush()
    text = (tmp_path / "logs" / "supervisor.log").read_text()
    assert secret not in text and token not in text and "enr_abcdefghijkl" not in text and "[redacted]" in text
    handler = next(h for h in logger.handlers if hasattr(h, "maxBytes"))
    assert handler.maxBytes > 0 and handler.backupCount > 0


def test_event_log_handler_failure_is_not_fatal(tmp_path):
    def broken(appname):
        raise ImportError("no pywin32")

    logger = wlog.configure_logging(tmp_path / "logs", logger_name="cc_test_logger2", nt_handler_factory=broken)
    assert any(isinstance(h, logging.Handler) for h in logger.handlers)


def test_event_log_handler_is_warning_and_above_only(tmp_path):
    made = []

    class FakeNt(logging.Handler):
        def __init__(self, appname):
            super().__init__()
            made.append(appname)

        def emit(self, record):
            pass

    logger = wlog.configure_logging(tmp_path / "logs", logger_name="cc_test_logger3", nt_handler_factory=FakeNt)
    nt = [h for h in logger.handlers if isinstance(h, FakeNt)][0]
    assert made == [wlog.APP_NAME] and nt.level == logging.WARNING
