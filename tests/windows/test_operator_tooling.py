"""Operator tooling on Windows and Linux (cross-platform plan W10 / W15): tasks.py mirrors the Makefile, key files fail closed, the
PowerShell scripts are plain ASCII and never take a secret as a parameter, line endings are pinned. WRITTEN, NOT RUN.

Bug re-insertion: delete a target from tasks.py (the Makefile mirror test fails); let restrict_to_owner swallow an icacls failure
(the fail-closed key test fails); put a non-ASCII character or a -Token parameter in a .ps1 file (the script hygiene tests fail).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tasks():
    return _load("cc_tasks", ROOT / "tasks.py")


@pytest.fixture(scope="module")
def sign_kit():
    pytest.importorskip("cryptography")
    return _load("cc_sign_kit", ROOT / "scripts" / "sign_kit.py")


# --------------------------------------------------------------------------- tasks.py mirrors the Makefile
def _makefile_targets() -> list[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return re.findall(r"^([a-zA-Z_-]+):.*?## ", text, flags=re.M)


def test_every_makefile_target_exists_in_tasks_py(tasks):
    missing = [t for t in _makefile_targets() if t not in tasks.TASKS and t != "help"]
    assert missing == [], f"add these to tasks.py: {missing}"
    assert len(_makefile_targets()) >= 15


def test_help_lists_every_task(tasks, capsys):
    assert tasks.main(["help"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in tasks.TASKS)


def test_unknown_target_is_refused(tasks, capsys):
    assert tasks.main(["no-such-target"]) == 2
    assert "unknown target" in capsys.readouterr().err


def test_dry_run_prints_commands_and_runs_nothing(tasks, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a process was started during --dry-run")

    monkeypatch.setattr(tasks.subprocess, "run", boom)
    for target in ("agent-install", "agent-test", "web-check", "kits-check", "up", "windows-test"):
        assert tasks.main(["--dry-run", target]) == 0
    assert "pytest" in capsys.readouterr().out


def test_commands_are_argument_lists_with_the_right_working_directory(tasks, monkeypatch):
    recorded = []
    ctx = tasks.Context(dry_run=True, executed=recorded)
    tasks.agent_test(ctx)
    tasks.web_check(ctx)
    cwd0, *argv0 = recorded[0]
    assert Path(cwd0) == tasks.AGENT_DIR and "pytest" in " ".join(argv0) and "-q" in argv0
    cwd1, *argv1 = recorded[1]
    assert Path(cwd1) == tasks.WEB_DIR and argv1[-2:] == ["run", "typecheck"]


def test_extra_arguments_after_double_dash_reach_pytest(tasks):
    recorded = []
    tasks.agent_test(tasks.Context(dry_run=True, executed=recorded, extra=["-k", "heartbeat"]))
    assert recorded[0][-2:] == ["-k", "heartbeat"]


def test_venv_paths_follow_the_os(tasks, monkeypatch):
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    assert tasks.venv_exe(Path("app"), "pytest") == Path("app") / ".venv" / "Scripts" / "pytest.exe"
    monkeypatch.setattr(tasks, "IS_WINDOWS", False)
    assert tasks.venv_exe(Path("app"), "pytest") == Path("app") / ".venv" / "bin" / "pytest"


def test_control_plane_targets_refuse_on_windows_and_name_the_way_out(tasks, monkeypatch, capsys):
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    for t in ("api-install", "api-migrate", "api-run", "api-test"):
        assert tasks.main(["--dry-run", t]) == 2
    err = capsys.readouterr().err
    assert "WSL2" in err and "Docker" in err and "windows-developer-setup.md" in err
    # --wsl runs the matching make target in the default WSL distro instead.
    assert tasks.main(["--dry-run", "--wsl", "api-test"]) == 0
    out = capsys.readouterr().out
    assert "wsl.exe" in out and "make api-test" in out
    recorded = []
    tasks.run_in_wsl("api-test", tasks.Context(dry_run=True, executed=recorded))
    assert recorded[0][1:3] == ["wsl.exe", "--cd"] and recorded[0][-2:] == ["make", "api-test"]


def test_aggregate_test_skips_the_api_suite_on_native_windows(tasks, monkeypatch):
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    recorded = []
    tasks.test_all(tasks.Context(dry_run=True, executed=recorded))
    flat = [" ".join(r) for r in recorded]
    assert not any(str(tasks.API_DIR) in r[0] for r in recorded) and any("provider-agent" in c.replace("\\", "/") for c in flat)


# --------------------------------------------------------------------------- key files fail closed
def test_gen_key_is_deleted_when_it_cannot_be_restricted(sign_kit, tmp_path, monkeypatch, capsys):
    key = tmp_path / "k.pem"

    def refuse(path):
        raise OSError("icacls failed: access denied")

    monkeypatch.setattr(sign_kit, "restrict_to_owner", refuse)
    monkeypatch.setattr(sys, "argv", ["sign_kit.py", "--gen-key", str(key)])
    with pytest.raises(SystemExit) as exc:
        sign_kit.main()
    assert "key was deleted" in str(exc.value) and not key.exists()


def test_restrict_to_owner_is_a_noop_on_posix_and_checks_icacls_on_windows(sign_kit, tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sign_kit.os, "name", "posix")
    sign_kit.restrict_to_owner(str(tmp_path / "x"))
    assert calls == []
    monkeypatch.setattr(sign_kit.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "PC1")
    monkeypatch.setattr(subprocess, "run", fake_run)
    sign_kit.restrict_to_owner(str(tmp_path / "x"))
    assert calls[0][0] == "icacls" and "/inheritance:r" in calls[0] and "/grant:r" in calls[0] and "PC1\\alice:F" in calls[0]
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 5, "", "denied"))
    with pytest.raises(OSError):
        sign_kit.restrict_to_owner(str(tmp_path / "x"))


def test_load_key_accepts_utf16_hex_and_utf8_bom_files(sign_kit, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = os.urandom(32)
    expected = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    for name, data in (("u16.key", (raw.hex() + "\r\n").encode("utf-16")), ("bom.key", b"\xef\xbb\xbf" + raw.hex().encode() + b"\r\n"), ("plain.key", raw.hex().encode())):
        p = tmp_path / name
        p.write_bytes(data)
        assert sign_kit.load_key(str(p)).public_key().public_bytes_raw() == expected, name


def test_signed_output_uses_lf_and_reads_a_bom_manifest(sign_kit, tmp_path, monkeypatch):
    keyfile = tmp_path / "k.hex"
    keyfile.write_text(os.urandom(32).hex(), encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_bytes(b"\xef\xbb\xbf" + b'{"slug": "x"}\r\n')
    out = tmp_path / "signed.json"
    monkeypatch.setattr(sys, "argv", ["sign_kit.py", "--manifest", str(manifest), "--key", str(keyfile), "--out", str(out)])
    assert sign_kit.main() == 0
    assert b"\r" not in out.read_bytes()


# --------------------------------------------------------------------------- PowerShell script hygiene
PS1 = sorted((ROOT / "scripts" / "windows").glob("*.ps1"))


def test_there_is_a_wrapper_for_every_task_the_plan_names():
    names = {p.name for p in PS1}
    assert {"sign_kit.ps1", "gen_fanout_vectors.ps1", "new_kit_signing_key.ps1", "kit_ci.ps1", "smoke-test.ps1", "tasks.ps1"} <= names


@pytest.mark.parametrize("path", PS1, ids=lambda p: p.name)
def test_ps1_is_plain_ascii_with_no_bom(path):
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("ascii")  # Windows PowerShell 5.1 reads a BOM-less file as the legacy code page: ASCII is the only safe content


@pytest.mark.parametrize("path", PS1, ids=lambda p: p.name)
def test_ps1_never_takes_a_secret_as_a_parameter(path):
    text = path.read_text(encoding="ascii")
    params = re.findall(r"\[(?:string|securestring)\]\s*\$(\w+)", text, flags=re.I)
    assert not [p for p in params if re.search(r"token|secret|password|passphrase|apikey", p, re.I)], params
    assert "Invoke-Expression" not in text and not re.search(r"\biex\b", text)


@pytest.mark.parametrize("path", PS1, ids=lambda p: p.name)
def test_ps1_has_a_usage_comment_and_stops_on_errors(path):
    text = path.read_text(encoding="ascii")
    assert text.lstrip().startswith("#"), "a script must start with a comment that says what it does"
    assert "$ErrorActionPreference = 'Stop'" in text or "_common.ps1" in text


# --------------------------------------------------------------------------- line endings and repository hygiene
def test_gitattributes_pins_lf_for_the_types_that_break_with_crlf():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.py", "*.json", "*.sh", "Dockerfile", "*.yml", "*.ps1"):
        assert re.search(rf"^{re.escape(pattern)}\s+text eol=lf", attrs, flags=re.M), pattern


def test_shell_scripts_have_lf_endings():
    for p in list((ROOT / "scripts").glob("*.sh")) + list((ROOT / "os" / "sealed" / "scripts").glob("*.sh")):
        assert b"\r\n" not in p.read_bytes(), p


def test_every_shell_script_is_either_ported_or_listed_as_linux_only():
    sheet = (ROOT / "docs" / "operations" / "operator-cheatsheet.md").read_text(encoding="utf-8")
    for sh in ("scripts/smoke-test.sh", "scripts/setup-multinode-net.sh", "os/sealed/scripts", "tests/qa/stack.sh", "apps/api/scripts/entrypoint.sh"):
        assert sh.split("/")[-1] in sheet or sh in sheet, f"{sh} is not mentioned in the operator cheat sheet"
    assert (ROOT / "scripts" / "windows" / "smoke-test.ps1").exists()
