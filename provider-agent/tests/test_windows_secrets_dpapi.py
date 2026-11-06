"""W2 secrets and paths: DPAPI wrapper with an injected fake ``win32crypt`` / ``win32cred``, ACL commands, migration.

The fakes follow the documented DPAPI contract (entropy must match to unprotect; a blob is opaque and not the plaintext),
not the code's assumptions. Written, not run.
"""

from __future__ import annotations

import tomllib

import pytest

from computecoin_agent.windows import paths as wpaths
from computecoin_agent.windows import secrets_dpapi as sd
from windows_fakes import FakeWin32Cred, FakeWin32Crypt

SECRET = "msk_" + "ab12cd34" * 8


def _store(tmp_path, **kw):
    kw.setdefault("win32crypt", FakeWin32Crypt())
    kw.setdefault("win32cred", FakeWin32Cred())
    return sd.DpapiSecretStore(tmp_path / "secrets", **kw)


# --------------------------------------------------------------------------- DPAPI wrapper
def test_round_trip_and_no_plain_text_on_disk(tmp_path):
    store = _store(tmp_path)
    assert store.store("machine_secret", SECRET) == "dpapi"
    assert store.load("machine_secret") == SECRET
    for f in (tmp_path / "secrets").rglob("*"):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes()
            assert SECRET[4:].encode() not in f.read_bytes()


def test_machine_scope_sets_the_local_machine_flag_and_user_scope_does_not(tmp_path):
    crypt = FakeWin32Crypt()
    _store(tmp_path, win32crypt=crypt).store("machine_secret", SECRET)
    assert crypt.protect_calls[0][2] & FakeWin32Crypt.CRYPTPROTECT_LOCAL_MACHINE
    assert crypt.protect_calls[0][2] & FakeWin32Crypt.CRYPTPROTECT_UI_FORBIDDEN
    crypt2 = FakeWin32Crypt()
    _store(tmp_path / "u", win32crypt=crypt2, scope="user").store("machine_secret", SECRET)
    assert not crypt2.protect_calls[0][2] & FakeWin32Crypt.CRYPTPROTECT_LOCAL_MACHINE


def test_a_blob_swapped_to_another_name_does_not_decrypt(tmp_path):
    store = _store(tmp_path)
    store.store("machine_secret", SECRET)
    store.store("control_token", "t" * 64)
    d = tmp_path / "secrets"
    (d / "control_token.dpapi").write_bytes((d / "machine_secret.dpapi").read_bytes())
    with pytest.raises(sd.SecretStoreError):
        store.load("control_token")


def test_missing_secret_is_none_and_bad_names_are_refused(tmp_path):
    store = _store(tmp_path)
    assert store.load("machine_secret") is None
    for bad in ("../x", "A", "", "a b", "x" * 80):
        with pytest.raises(sd.SecretStoreError):
            store.store(bad, SECRET)
    with pytest.raises(sd.SecretStoreError):
        store.store("machine_secret", "")


def test_corrupt_blob_raises_instead_of_returning_junk(tmp_path):
    store = _store(tmp_path)
    store.store("machine_secret", SECRET)
    (tmp_path / "secrets" / "machine_secret.dpapi").write_bytes(b"garbage")
    with pytest.raises(sd.SecretStoreError):
        store.load("machine_secret")


def test_falls_back_to_credential_manager_when_dpapi_fails(tmp_path):
    crypt, cred = FakeWin32Crypt(), FakeWin32Cred()
    crypt.fail = True
    store = _store(tmp_path, win32crypt=crypt, win32cred=cred)
    assert store.store("machine_secret", SECRET) == "credential_manager"
    assert not list((tmp_path / "secrets").glob("*.dpapi")) if (tmp_path / "secrets").exists() else True
    assert store.load("machine_secret") == SECRET
    assert cred.items["ComputeCoin/machine_secret"]["Persist"] == FakeWin32Cred.CRED_PERSIST_LOCAL_MACHINE


def test_fails_closed_when_no_backend_works_and_never_writes_a_plain_file(tmp_path):
    crypt, cred = FakeWin32Crypt(), FakeWin32Cred()
    crypt.fail = cred.fail = True
    store = _store(tmp_path, win32crypt=crypt, win32cred=cred)
    with pytest.raises(sd.SecretStoreError) as err:
        store.store("machine_secret", SECRET)
    assert SECRET not in str(err.value)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes()


def test_no_fallback_option_fails_closed(tmp_path):
    crypt = FakeWin32Crypt()
    crypt.fail = True
    store = _store(tmp_path, win32crypt=crypt, use_credential_manager_fallback=False)
    with pytest.raises(sd.SecretStoreError):
        store.store("machine_secret", SECRET)


def test_refuses_to_store_when_the_directory_cannot_be_locked_down(tmp_path):
    def deny(path):
        raise wpaths.PathSecurityError("icacls refused")

    store = _store(tmp_path, prepare_directory=deny, use_credential_manager_fallback=False)
    with pytest.raises(sd.SecretStoreError):
        store.store("machine_secret", SECRET)
    assert not list((tmp_path / "secrets").glob("*.dpapi"))


def test_delete_removes_blob_and_credential(tmp_path):
    store = _store(tmp_path)
    store.store("machine_secret", SECRET)
    store.delete("machine_secret")
    assert store.load("machine_secret") is None


def test_missing_pywin32_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "win32crypt", None)
    store = sd.DpapiSecretStore(tmp_path / "s", use_credential_manager_fallback=False)
    with pytest.raises(sd.SecretStoreError, match="pywin32"):
        store.store("machine_secret", SECRET)


# --------------------------------------------------------------------------- migration of a plain credential
def _plain(tmp_path, name=".computecoin/agent.toml"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'api_url = "https://api.example"\nmachine_id = "mch_1"\nmachine_secret = "{SECRET}"\n'
        'signing_secret = "deadbeef"\nheartbeat_interval_seconds = 30\n',
        encoding="utf-8",
    )
    return p


def test_migration_moves_secrets_and_deletes_the_old_file(tmp_path):
    old = _plain(tmp_path)
    store = _store(tmp_path)
    dest = tmp_path / "ProgramData" / "agent.toml"
    res = sd.migrate_plain_config(old, store, dest=dest)
    assert res.migrated and res.old_file_removed and not old.exists()
    assert sorted(res.names) == ["machine_secret", "signing_secret"]
    assert store.load("machine_secret") == SECRET and store.load("signing_secret") == "deadbeef"
    cfg = tomllib.loads(dest.read_text())
    assert cfg == {"api_url": "https://api.example", "machine_id": "mch_1", "heartbeat_interval_seconds": 30}
    assert SECRET not in dest.read_text()
    assert SECRET not in repr(res)


def test_in_place_migration_leaves_a_redacted_file_and_no_secret_bytes(tmp_path):
    p = _plain(tmp_path, "pd/agent.toml")
    store = _store(tmp_path)
    res = sd.migrate_plain_config(p, store)
    assert res.migrated and p.exists()
    text = p.read_text()
    assert "machine_secret" not in text and SECRET not in text and "mch_1" in text


def test_old_bytes_are_overwritten_before_deletion(tmp_path, monkeypatch):
    old = _plain(tmp_path)
    seen: dict = {}
    real_unlink = type(old).unlink

    def spy(self, *a, **k):
        if self == old:
            seen["content_at_delete"] = self.read_bytes()
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(old), "unlink", spy)
    sd.migrate_plain_config(old, _store(tmp_path), dest=tmp_path / "new.toml")
    assert set(seen["content_at_delete"]) == {0}  # zero-filled, same length, before the delete


def test_old_file_is_kept_when_the_secret_does_not_round_trip(tmp_path):
    class Lossy(sd.DpapiSecretStore):
        def load(self, name):
            return "something else"

    old = _plain(tmp_path)
    store = Lossy(tmp_path / "secrets", win32crypt=FakeWin32Crypt(), win32cred=FakeWin32Cred())
    with pytest.raises(sd.SecretStoreError):
        sd.migrate_plain_config(old, store, dest=tmp_path / "new.toml")
    assert old.exists() and SECRET in old.read_text() and not (tmp_path / "new.toml").exists()


def test_old_file_is_kept_when_storing_fails(tmp_path):
    crypt, cred = FakeWin32Crypt(), FakeWin32Cred()
    crypt.fail = cred.fail = True
    old = _plain(tmp_path)
    with pytest.raises(sd.SecretStoreError):
        sd.migrate_plain_config(old, _store(tmp_path, win32crypt=crypt, win32cred=cred), dest=tmp_path / "new.toml")
    assert old.exists() and SECRET in old.read_text()


def test_nothing_to_migrate_is_not_an_error(tmp_path):
    res = sd.migrate_plain_config(tmp_path / "nope.toml", _store(tmp_path))
    assert not res.migrated and not res.names


# --------------------------------------------------------------------------- paths and ACLs
class Recorder:
    def __init__(self, returncode=0, stdout=""):
        self.calls: list[list[str]] = []
        self.returncode, self.stdout = returncode, stdout

    def __call__(self, argv):
        self.calls.append(list(argv))
        return wpaths.ExecResult(self.returncode, self.stdout, "")


def test_acl_command_is_an_argv_list_restricted_to_system_and_administrators(tmp_path):
    argv = wpaths.acl_argv(tmp_path / "ComputeCoin")
    assert isinstance(argv, list) and argv[0] == "icacls"
    joined = " ".join(argv)
    assert "*S-1-5-18:(OI)(CI)F" in argv and "*S-1-5-32-544:(OI)(CI)F" in argv
    assert "/grant:r" in argv and any(a.startswith("/inheritance") for a in argv)
    for risky in ("S-1-1-0", "S-1-5-32-545", "S-1-5-11", "Everyone", "Users"):
        assert risky not in joined


def test_restrict_failure_raises(tmp_path):
    with pytest.raises(wpaths.PathSecurityError):
        wpaths.restrict_path(tmp_path, Recorder(returncode=5))

    def boom(argv):
        raise FileNotFoundError("icacls")

    with pytest.raises(wpaths.PathSecurityError):
        wpaths.restrict_path(tmp_path, boom)


def test_layout_is_restricted_before_the_secrets_directory_exists(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "ComputeCoin")
    order: list[str] = []

    def runner(argv):
        if "/findsid" not in argv:
            order.append("secrets_exists" if layout.secrets_dir.exists() else "secrets_absent")
        return wpaths.ExecResult(0, "", "")

    wpaths.ensure_layout(layout, runner)
    assert order == ["secrets_absent"]
    assert all(d.is_dir() for d in layout.directories())


def test_verify_detects_a_broad_group_in_the_acl(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    layout.root.mkdir()
    out = f"Successfully processed 1 files; {layout.root}"
    with pytest.raises(wpaths.PathSecurityError):
        wpaths.verify_restricted(layout.root, Recorder(0, out))
    wpaths.verify_restricted(layout.root, Recorder(0, "No files with a matching SID"))


def test_layout_file_names(tmp_path):
    layout = wpaths.Layout.from_root(tmp_path / "CC")
    assert layout.config_file.name == "agent.toml" and layout.secrets_dir.name == "secrets"
    assert layout.logs_dir.parent == layout.root
