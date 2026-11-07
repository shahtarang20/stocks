"""Install-time and uninstall-time actions the MSI runs (cross-platform plan W6, X14). Standard library only.

The MSI runs this file with its own bundled Python:  ``python.exe msi_actions.py <command> ...``.  Every external command
is built as an argument LIST and run through an injectable runner (never a shell string), so the logic is tested on Linux
with a fake runner. WRITTEN, NEVER RUN ON WINDOWS: every Windows command line below is marked ``TODO(verify-docs)`` (X11).

Commands
--------
``install``   idempotent: make sure the Windows Service is registered; seed the updater's ``known_good`` from this very MSI
              (``--msi-path`` and ``--version``) so the first update can roll back; start the service ONLY when this PC
              already has an enrolled credential (an upgrade of a working host). A fresh install is never started or
              enrolled by the installer.
``cleanup``   full uninstall (X14): stop and remove the service, unregister the runtime distro, delete the firewall rules,
              delete stored credentials, delete the data directory. Best effort: it never aborts the uninstall, it records
              every step and exits 0 when everything is gone, 1 when something was left behind (listed in the output).
``upgrade``   (no-op marker) kept for symmetry: an upgrade never runs ``cleanup``; the MSI conditions guarantee that.

Documentation to verify (record URL and date next to the final code):
* netsh advfirewall firewall add|delete rule   learn.microsoft.com/windows-server/administration/windows-commands/netsh-advfirewall-firewall
* wsl.exe --unregister                         learn.microsoft.com/windows/wsl/basic-commands
* cmdkey /list /delete                         learn.microsoft.com/windows-server/administration/windows-commands/cmdkey
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

SERVICE_NAME = "ComputeCoinAgent"
DISTRO_NAME = "computecoin"
FIREWALL_RULE_NAMES = ("ComputeCoin Agent (outbound)",)
HELPER_TASK_NAME = "ComputeCoin Session Helper"  # the per-user logon task registered by `service helper install`
# Updater state layout, the same as windows/paths.py Layout.update_state_file and update_dir (kept as plain names here so
# this file stays standard-library only).
UPDATE_STATE_PARTS = ("state", "update-state.json")
UPDATE_CACHE_PARTS = ("update", "cache")
CREDENTIAL_TARGET_PREFIX = "ComputeCoin"  # Credential Manager targets created by the supervisor (X7)  TODO(verify-docs)

Runner = Callable[[Sequence[str]], "RunResult"]


class RunResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def subprocess_runner(argv: Sequence[str]) -> RunResult:  # pragma: no cover - the real thing, argument list only
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=600, shell=False)
        return RunResult(p.returncode, p.stdout or "", p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(127, "", f"{type(exc).__name__}")


# ---- argv builders (pure; the tests pin them) ---------------------------------------------------------------------

def firewall_add_argv(rule_name: str, program: str) -> list:
    """Outbound-only: one OUTBOUND allow rule scoped to the agent's own program. No inbound rule is created."""
    return ["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule_name}", "dir=out", "action=allow",
            f"program={program}", "enable=yes", "profile=any"]


def firewall_delete_argv(rule_name: str) -> list:
    return ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"]


def agent_cli_argv(python: str, *args: str) -> list:
    return [python, "-I", "-m", "computecoin_agent.cli", "service", *args]  # -I: ignore PYTHONPATH, the cwd and user site


def provision_remove_argv(python: str, settings: str) -> list:
    return [python, "-I", "-m", "computecoin_agent.windows.runtime.provision", "remove", "--settings", settings]


def distro_unregister_argv(distro: str = DISTRO_NAME) -> list:
    return ["wsl.exe", "--unregister", distro]


def requirements_check_argv(python: str, install_dir: str) -> list:
    return [python, "-I", "-m", "computecoin_agent.windows.runtime.requirements_check", "--json", "--install-dir", install_dir]


# ---- steps -------------------------------------------------------------------------------------------------------

def _has_enrolled_credential(data_dir: Path) -> bool:
    secrets = data_dir / "secrets"
    try:
        return secrets.is_dir() and any(secrets.iterdir())
    except OSError:
        return False


def seed_known_good(data_dir: Path, msi_path: str, version: str) -> dict:
    """Record THIS installer as the updater's known-good rollback source (``updater/state.py: seed_known_good``).

    Best effort and never fatal: without a seed only the first update loses its rollback source (the updater then blocks
    claims instead of guessing), which is better than failing the install. Returns ``{"seeded": bool, "reason": str}``."""
    if not msi_path or not version:
        return {"seeded": False, "reason": "no installer path or version was given"}
    try:
        from computecoin_agent.updater.state import UpdateState, seed_known_good as _seed
    except ImportError:
        return {"seeded": False, "reason": "the agent package is not importable"}
    try:
        state = UpdateState(data_dir.joinpath(*UPDATE_STATE_PARTS))
        done = _seed(state, version=version, artifact=Path(msi_path), cache_dir=data_dir.joinpath(*UPDATE_CACHE_PARTS))
    except (OSError, ValueError) as exc:
        return {"seeded": False, "reason": type(exc).__name__}
    return {"seeded": done, "reason": "" if done else "a known-good installer is already recorded"}


def do_install(python: str, install_dir: str, data_dir: Path, run: Runner, *, msi_path: str = "", version: str = "") -> int:
    program = python  # the firewall rule is scoped to the bundled interpreter that hosts the supervisor
    status = run(agent_cli_argv(python, "status"))
    if "not_installed" in (status.stdout or "") or status.returncode != 0:
        res = run(agent_cli_argv(python, "install", "--startup", "auto"))
        if res.returncode != 0:
            print(json.dumps({"ok": False, "step": "service_install", "code": res.returncode}))
            return 1
    for rule in FIREWALL_RULE_NAMES:
        run(firewall_delete_argv(rule))  # idempotent: replace, never duplicate
        res = run(firewall_add_argv(rule, program))
        if res.returncode != 0:
            print(json.dumps({"ok": False, "step": "firewall", "code": res.returncode}))
            return 1
    seed = seed_known_good(data_dir, msi_path, version)
    started = False
    if _has_enrolled_credential(data_dir):
        started = run(agent_cli_argv(python, "start")).returncode == 0
    print(json.dumps({"ok": True, "service_started": started, "known_good_seeded": seed["seeded"],
                      "known_good_note": seed["reason"]}))
    return 0


def do_cleanup(python: str, data_dir: Path, run: Runner, *, keep_data: bool = False) -> int:
    left: list = []

    def step(name: str, argv: Sequence[str], ok_codes: Sequence[int] = (0,)) -> None:
        res = run(argv)
        if res.returncode not in ok_codes:
            left.append(f"{name} (exit {res.returncode})")

    step("service stop", agent_cli_argv(python, "stop"), ok_codes=(0, 1))  # already stopped / not installed is fine
    step("service remove", agent_cli_argv(python, "remove"), ok_codes=(0, 1))
    step("session helper task", ["schtasks.exe", "/Delete", "/TN", HELPER_TASK_NAME, "/F"], ok_codes=(0, 1))  # TODO(verify-docs): no such task is non-zero
    settings = data_dir / "runtime-settings.json"
    if settings.is_file():
        step("runtime distro remove", provision_remove_argv(python, str(settings)))
    else:  # no settings file: fall back to the plain unregister of OUR distro (never any other distro)
        step("runtime distro unregister", distro_unregister_argv(), ok_codes=(0, 1))
    for rule in FIREWALL_RULE_NAMES:
        step(f"firewall rule {rule}", firewall_delete_argv(rule), ok_codes=(0, 1))  # exit 1 = no such rule
    left.extend(_delete_credentials(run))
    if not keep_data:
        left.extend(_delete_tree(data_dir))
    print(json.dumps({"ok": not left, "left_behind": left}))
    return 0 if not left else 1


def _delete_credentials(run: Runner) -> list:
    """Credential Manager entries the supervisor created (DPAPI blobs live in the data directory and go with it)."""
    left: list = []
    listing = run(["cmdkey", "/list"])
    for line in (listing.stdout or "").splitlines():
        text = line.strip()
        if text.lower().startswith("target:") and CREDENTIAL_TARGET_PREFIX.lower() in text.lower():
            target = text.split(":", 1)[1].strip()
            res = run(["cmdkey", f"/delete:{target}"])
            if res.returncode != 0:
                left.append(f"credential {target}")
    return left


def _delete_tree(path: Path) -> list:
    if not path.exists():
        return []
    # Refuse anything that is not clearly OUR directory: a wrong argument must never delete another folder.
    if path.name.lower() != "computecoin" or path.parent == path:
        return [f"data directory {path} (refused: not a ComputeCoin directory)"]
    shutil.rmtree(path, ignore_errors=True)
    return [f"data directory {path}"] if path.exists() else []


def main(argv: Optional[Sequence[str]] = None, run: Runner = subprocess_runner) -> int:
    ap = argparse.ArgumentParser(prog="msi_actions")
    ap.add_argument("command", choices=["install", "cleanup"])
    ap.add_argument("--python", default=sys.executable, help="the bundled python.exe that has computecoin-agent installed")
    ap.add_argument("--install-dir", default="")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--msi-path", default="", help="the MSI being installed (becomes the updater's known-good rollback source)")
    ap.add_argument("--version", default="", help="the ProductVersion of that MSI")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    if args.command == "install":
        return do_install(args.python, args.install_dir, data_dir, run, msi_path=args.msi_path, version=args.version)
    return do_cleanup(args.python, data_dir, run, keep_data=args.keep_data)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
