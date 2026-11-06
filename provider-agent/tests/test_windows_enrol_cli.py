"""W2 enrolment without a plain-text credential, protected config loading, and the Windows-only ``service`` commands.

Fakes only (no Windows, no pywin32, no network). Written, not run.
"""

from __future__ import annotations

import sys
import tomllib
import types

import pytest
import tomli_w
from typer.testing import CliRunner

from computecoin_agent import cli
from computecoin_agent.platform import reset_platform
from computecoin_agent.windows import cli_commands as wcli
from computecoin_agent.windows import enrol as wenrol
from computecoin_agent.windows import paths as wpaths
from computecoin_agent.windows import secrets_dpapi as sd
from windows_fakes import FakeWin32Cred, FakeWin32Crypt

SECRET = "msk_" + "0123abcd" * 8


@pytest.fixture(autouse=True)
def _restore_platform():
    yield
    reset_platform()


def _ok_acl(argv):
    return wpaths.ExecResult(0, "", "")


def _store(layout):
    return sd.DpapiSecretStore(layout.secrets_dir, win32crypt=FakeWin32Crypt(), win32cred=FakeWin32Cred())


def _fake_enroll(server, token, report, *, config_path=None):
    """Behaves like enroll_remote: persists the credential into the toml, returns it in the result."""
    cfg = {"api_url": server, "machine_id": "mch_9", "machine_secret": SECRET, "heartbeat_interval_seconds": 30}
    config_path.write_text(tomli_w.dumps(cfg), encoding="utf-8")
    return types.SimpleNamespace(machine_id="mch_9", machine_secret=SECRET, heartbeat_interval_seconds=30,
                                 online_window_seconds=90, config_path=config_path)


def test_enrolment_leaves_no_plain_credential_and_returns_none(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "ComputeCoin")
    store = _store(layout)
    result = wenrol.enroll_protected("https://api.example", "enr_x", object(), layout=layout, store=store,
                                     enroll_fn=_fake_enroll, acl_runner=_ok_acl)
    assert result.machine_id == "mch_9" and result.machine_secret == ""
    text = layout.config_file.read_text()
    assert SECRET not in text and "machine_secret" not in text and "mch_9" in text
    assert store.load("machine_secret") == SECRET
    for f in layout.root.rglob("*"):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes()


def test_a_rejected_token_stores_nothing(tmp_path):
    from computecoin_agent.enrollment import EnrollmentError

    layout = wpaths.Layout.from_root(tmp_path / "CC")
    store = _store(layout)

    def reject(*a, **k):
        raise EnrollmentError("401")

    with pytest.raises(EnrollmentError):
        wenrol.enroll_protected("https://api.example", "enr_bad", object(), layout=layout, store=store,
                                enroll_fn=reject, acl_runner=_ok_acl)
    assert store.load("machine_secret") is None and not layout.config_file.exists()


def test_when_the_store_fails_after_enrolment_the_credential_is_not_destroyed(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    crypt, cred = FakeWin32Crypt(), FakeWin32Cred()
    crypt.fail = cred.fail = True
    store = sd.DpapiSecretStore(layout.secrets_dir, win32crypt=crypt, win32cred=cred)
    with pytest.raises(sd.SecretStoreError, match="migrate"):
        wenrol.enroll_protected("https://api.example", "enr_x", object(), layout=layout, store=store,
                                enroll_fn=_fake_enroll, acl_runner=_ok_acl)
    assert SECRET in layout.config_file.read_text()      # single-use token: do not lose the only copy


def test_protected_config_gets_secrets_from_the_store_only(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    layout.root.mkdir()
    layout.config_file.write_text(tomli_w.dumps({"api_url": "https://api.example", "machine_id": "mch_9"}))
    store = _store(layout)
    store.store("machine_secret", SECRET)
    cfg = wenrol.load_protected_config(layout, store)
    assert cfg.machine_id == "mch_9" and cfg.machine_secret == SECRET and cfg.has_machine_credential()
    assert SECRET not in layout.config_file.read_text()


def test_legacy_credential_migration_to_programdata(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    old = tmp_path / "home" / ".computecoin" / "agent.toml"
    old.parent.mkdir(parents=True)
    old.write_text(tomli_w.dumps({"machine_id": "mch_1", "machine_secret": SECRET}))
    store = _store(layout)
    # ensure_layout inside migrate_legacy_credential shells out to icacls; use the fake by pre-creating the layout
    import computecoin_agent.windows.enrol as e

    orig = e.ensure_layout
    e.ensure_layout = lambda lay, runner=None, **k: orig(lay, _ok_acl, **k)
    try:
        res = wenrol.migrate_legacy_credential(layout, store, old)
    finally:
        e.ensure_layout = orig
    assert res.migrated and not old.exists() and store.load("machine_secret") == SECRET
    assert tomllib.loads(layout.config_file.read_text()) == {"machine_id": "mch_1"}


# --------------------------------------------------------------------------- Windows-only CLI group
def test_service_commands_are_not_registered_on_other_platforms():
    if sys.platform == "win32":
        pytest.skip("this check is for non-Windows")
    names = {c.name for c in cli.app.registered_groups}
    assert "service" not in {g.name for g in cli.app.registered_groups} and not names


def test_work_command_has_the_control_options():
    result = CliRunner().invoke(cli.app, ["work", "--help"])
    assert result.exit_code == 0
    for opt in ("--control-socket", "--control-token-file", "--control-allow-private"):
        assert opt in result.output


class FakeManager:
    def __init__(self):
        self.calls = []

    def install(self, **kw):
        self.calls.append(("install", kw))

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def remove(self):
        self.calls.append(("remove",))

    def status(self):
        return "running"


def _service_app(tmp_path, manager, admin=True, monkeypatch=None):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    monkeypatch.setattr(wcli, "_is_admin", lambda: admin)
    store = _store(layout)
    import typer

    app = typer.Typer()
    wcli.register(app, service_manager_factory=lambda: manager, store_factory=lambda lay: store, layout_factory=lambda: layout)
    return app, layout


def test_service_commands_exist_and_drive_the_manager(tmp_path, monkeypatch):
    mgr = FakeManager()
    app, layout = _service_app(tmp_path, mgr, monkeypatch=monkeypatch)
    runner = CliRunner()
    for cmd in ("start", "stop", "remove"):
        assert runner.invoke(app, ["service", cmd]).exit_code == 0
    out = runner.invoke(app, ["service", "status"])
    assert out.exit_code == 0 and out.output.strip() == "running"
    assert [c[0] for c in mgr.calls] == ["start", "stop", "remove"]
    help_out = runner.invoke(app, ["service", "--help"]).output
    for cmd in ("install", "start", "stop", "remove", "status", "run", "enroll", "migrate"):
        assert cmd in help_out


def test_service_install_creates_the_acl_layout_and_control_token(tmp_path, monkeypatch):
    mgr = FakeManager()
    app, layout = _service_app(tmp_path, mgr, monkeypatch=monkeypatch)
    monkeypatch.setattr(wpaths, "subprocess_runner", _ok_acl)
    res = CliRunner().invoke(app, ["service", "install", "--startup", "delayed"])
    assert res.exit_code == 0, res.output
    assert mgr.calls == [("install", {"start_type": "delayed", "username": None})]
    assert layout.secrets_dir.is_dir()
    assert (layout.secrets_dir / "control_token.dpapi").exists()


def test_service_commands_refuse_without_administrator(tmp_path, monkeypatch):
    mgr = FakeManager()
    app, _ = _service_app(tmp_path, mgr, admin=False, monkeypatch=monkeypatch)
    for cmd in ("install", "start", "stop", "remove"):
        res = CliRunner().invoke(app, ["service", cmd])
        assert res.exit_code == 1 and "elevated" in res.output
    assert mgr.calls == []
