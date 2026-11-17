"""`computecoin-agent rental on|off|status`, and `start` serving sessions in a background thread without ever breaking the job worker."""

from __future__ import annotations

import os
import stat
import sys
import tomllib
import types

import pytest
from typer.testing import CliRunner

from computecoin_agent import cli, config as config_mod
from computecoin_agent.config import AgentConfig

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="the Linux behaviour of `rental`; the Windows behaviour is tested in test_windows_rental_check.py"
)


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """The settings file in a tmp dir (never the real home): every place that asks for the default path gets this one."""
    path = tmp_path / "home" / ".computecoin" / "agent.toml"
    monkeypatch.setattr(config_mod, "default_config_path", lambda: path)
    return path


def _run(*args):
    return CliRunner().invoke(cli.app, ["rental", *args])


def test_status_says_off_by_default_and_changes_nothing(cfgfile):
    out = _run("status")
    assert out.exit_code == 0 and "container rental: off" in out.output and "serving interactive sessions: yes" in out.output
    assert not cfgfile.exists()
    assert _run().output == out.output  # `status` is the default


def test_on_warns_the_host_saves_privately_and_status_agrees(cfgfile):
    out = _run("on")
    assert out.exit_code == 0 and "ordinary hardened container" in out.output and "cannot afford to lose" in out.output
    assert "now ON" in out.output and "Restart the agent" in out.output
    assert tomllib.loads(cfgfile.read_text()) == {"allow_container_rental": True}
    assert stat.S_IMODE(os.stat(cfgfile).st_mode) & 0o077 == 0 and stat.S_IMODE(os.stat(cfgfile.parent).st_mode) & 0o077 == 0
    assert "container rental: ON" in _run("status").output
    assert AgentConfig.load().allow_container_rental is True


def test_off_turns_it_off_and_keeps_every_other_setting(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text('machine_id = "m1"\nmachine_secret = "s3cret"\nhost_kind = "dedicated"\nallow_container_rental = true\n')
    out = _run("off")
    assert out.exit_code == 0 and "now off" in out.output
    assert tomllib.loads(cfgfile.read_text()) == {"machine_id": "m1", "machine_secret": "s3cret", "host_kind": "dedicated", "allow_container_rental": False}
    assert "container rental: off" in _run("status").output


def test_on_twice_is_harmless(cfgfile):
    _run("on")
    again = _run("on")
    assert again.exit_code == 0 and tomllib.loads(cfgfile.read_text()) == {"allow_container_rental": True}


def test_a_wrong_word_changes_nothing(cfgfile):
    for word in ("maybe", "ON", "yes", ""):
        out = _run(word)
        assert out.exit_code == 2 and "Say 'on', 'off', 'status' or 'check'" in out.output
    assert not cfgfile.exists()


def test_an_unreadable_settings_file_is_left_alone_with_a_plain_message(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text("this = = broken [[")
    out = _run("on")
    assert out.exit_code == 1 and "Could not change the setting" in out.output
    assert cfgfile.read_text() == "this = = broken [["


def test_the_command_does_not_print_secrets(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text('machine_secret = "TOPSECRETVALUE"\n')
    assert "TOPSECRETVALUE" not in _run("on").output + _run("status").output + _run("off").output


# ----------------------------------------------------------------------------------------------- start integration
@pytest.fixture
def start_env(monkeypatch):
    """Everything `start` touches replaced by a recorder, so the real job loop, heartbeat, telemetry and docker never run."""
    log = {"poll": 0, "sessions": []}
    cfg = AgentConfig(machine_id="m", machine_secret="s", api_url="https://api.example", telemetry_enabled=False)
    monkeypatch.setattr(cli.AgentConfig, "load", classmethod(lambda cls, path=None: cfg))
    monkeypatch.setattr(cli, "build_capability_report", lambda **k: object())
    monkeypatch.setattr(cli, "_heartbeat_until_auth_failure", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_start_safeguards", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_apply_default_power_cap", lambda *a, **k: (lambda: None))
    monkeypatch.setattr(cli, "_attestation_manager", lambda *a, **k: None)
    monkeypatch.setattr(cli, "poll_loop", lambda *a, **k: log.__setitem__("poll", log["poll"] + 1))
    return log, cfg


def _start():
    return CliRunner().invoke(cli.app, ["start"])


def test_start_serves_sessions_in_the_background_and_still_runs_the_job_worker(start_env, monkeypatch):
    log, cfg = start_env
    from computecoin_agent import session_agent

    monkeypatch.setattr(session_agent, "start_background_sessions", lambda config, **k: log["sessions"].append((config, sorted(k))) or object())
    out = _start()
    assert out.exit_code == 0 and log["poll"] == 1
    assert "[sessions] serving interactive sessions" in out.output and "container rental is ON" not in out.output
    assert log["sessions"][0][0] is cfg and log["sessions"][0][1] == ["owner", "thermal_guard"]  # heat and owner rules follow it


def test_start_says_when_container_rental_is_on(start_env, monkeypatch):
    log, cfg = start_env
    from computecoin_agent import session_agent

    cfg.allow_container_rental = True
    monkeypatch.setattr(session_agent, "start_background_sessions", lambda config, **k: object())
    assert "(container rental is ON)" in _start().output


def test_start_says_nothing_when_sessions_are_not_served_here(start_env, monkeypatch):
    log, _ = start_env
    from computecoin_agent import session_agent

    monkeypatch.setattr(session_agent, "start_background_sessions", lambda config, **k: None)
    out = _start()
    assert "[sessions]" not in out.output and log["poll"] == 1


def test_a_failure_starting_sessions_never_stops_the_job_worker(start_env, monkeypatch):
    log, _ = start_env
    from computecoin_agent import session_agent

    def boom(config, **k):
        raise RuntimeError("no docker group")

    monkeypatch.setattr(session_agent, "start_background_sessions", boom)
    out = _start()
    assert out.exit_code == 0 and log["poll"] == 1 and "[sessions] not started: RuntimeError" in out.output


def test_a_crash_inside_the_session_thread_never_stops_the_job_worker(start_env):
    """The real starter with a loop that crashes: the worker still runs, and the crash is logged, not raised."""
    log, cfg = start_env
    from computecoin_agent import session_agent
    from computecoin_agent.platform import use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    done = []
    real = session_agent.start_background_sessions

    def start_with_crashing_loop(config, **k):
        def loop(*a, **kw):
            done.append(1)
            raise RuntimeError("thread crash")

        t = real(config, engine_available=lambda: True, run_loop=loop, api=object(), executor=object(), **k)
        t.join(5)
        return t

    import computecoin_agent.session_agent as sa_mod

    sa_mod.start_background_sessions = start_with_crashing_loop
    try:
        with use_platform(make_fake_platform()):
            out = _start()
    finally:
        sa_mod.start_background_sessions = real
    assert out.exit_code == 0 and log["poll"] == 1 and done == [1]
