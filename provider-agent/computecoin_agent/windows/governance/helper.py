"""The user-session helper: install it, run it, remove it (cross-platform plan W5, W11 B8 and 7.5).

A Windows Service lives in session 0 and cannot see the owner's input, foreground window or full-screen state, so a small
process in the owner's own session samples them (``probes.py``) and writes ONE HMAC-signed snapshot file that the
supervisor verifies (``snapshot.SignedSnapshotSource``). This module is the plumbing around that:

* ``install_helper``  (elevated) creates the per-install key in the secret store, creates ``<ProgramData>\\ComputeCoin\\helper``
  restricted to SYSTEM, Administrators and the helper's account, writes the helper's copy of the key into it, and
  registers a per-user logon task that runs ``python -I -m computecoin_agent.cli service helper run``.
* ``run_helper``      (the task, as the owner) reads the key file and loops, writing the signed snapshot.
* ``remove_helper``   deletes the task and the helper directory's files.

Everything external goes through an injectable runner (argv lists, never a shell) so the logic is tested on Linux with a
fake. WRITTEN, NEVER RUN ON WINDOWS.

TODO(verify-docs) (X11), none of it read from Microsoft's documentation:
* ``schtasks /Create`` switches (``/SC ONLOGON``, ``/RU``, ``/IT``, ``/RL LIMITED``, ``/F``, ``/TR``) and whether ``/RU``
  with ``/IT`` needs no password for the currently logged-on account; ``/Run`` and ``/Delete`` spelling and exit codes.
* The helper account reaching ``<ProgramData>\\ComputeCoin\\helper`` through a parent it has no access to relies on the
  default "bypass traverse checking" privilege of ordinary users; confirm on a real machine.
* Key handling: the helper's copy of the key sits in a file because the helper runs as an ordinary user and cannot read
  the SYSTEM/Administrators-only DPAPI blob. The file's ACL is the protection; see the snapshot module for the residual risk.
"""

from __future__ import annotations

import getpass
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .snapshot import SnapshotError, ensure_snapshot_key, key_bytes

HELPER_TASK_NAME = "ComputeCoin Session Helper"
SNAPSHOT_FILENAME = "snapshot.json"
KEY_FILENAME = "snapshot.key"
SCHTASKS = "schtasks.exe"

Runner = Callable[[Sequence[str]], Any]  # returns an object with .returncode


class HelperError(RuntimeError):
    """The helper could not be installed or removed. Never contains the key."""


def snapshot_path(layout: Any) -> Path:
    return Path(layout.helper_dir) / SNAPSHOT_FILENAME


def key_path(layout: Any) -> Path:
    return Path(layout.helper_dir) / KEY_FILENAME


def default_account() -> str:
    """``DOMAIN\\user`` of the process owner (the elevated person who runs ``helper install``); override with ``--account``
    when the elevation used another account."""
    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain else user


def helper_command(python: str) -> str:
    """The single ``/TR`` string. Refuses a path that could break out of the quoting."""
    if not python or '"' in python or "\n" in python or "\r" in python:
        raise HelperError("the Python path is not usable in a scheduled task")
    return f'"{python}" -I -m computecoin_agent.cli service helper run'


def schtasks_create_argv(account: str, command: str) -> list[str]:
    return [SCHTASKS, "/Create", "/TN", HELPER_TASK_NAME, "/SC", "ONLOGON", "/RU", account, "/IT", "/RL", "LIMITED",
            "/F", "/TR", command]


def schtasks_run_argv() -> list[str]:
    return [SCHTASKS, "/Run", "/TN", HELPER_TASK_NAME]


def schtasks_delete_argv() -> list[str]:
    return [SCHTASKS, "/Delete", "/TN", HELPER_TASK_NAME, "/F"]


def prepare_helper_dir(layout: Any, account: str, key_hex: str, runner: Optional[Runner] = None) -> None:
    """Create and restrict the helper directory, THEN write the helper's key copy (never into a looser directory)."""
    from ..paths import restrict_path_for_user

    key_bytes(key_hex)  # strict check before anything touches the disk
    directory = Path(layout.helper_dir)
    directory.mkdir(parents=True, exist_ok=True)
    restrict_path_for_user(directory, account, runner)
    target = key_path(layout)
    tmp = target.with_name(target.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        tmp.write_text(key_hex, encoding="ascii")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def install_helper(
    layout: Any,
    store: Any,
    *,
    python: str,
    account: Optional[str] = None,
    runner: Optional[Runner] = None,
    start_now: bool = True,
) -> dict:
    """Idempotent. Returns ``{"account", "task", "started"}``. Raises :class:`HelperError` (or ``PathSecurityError`` /
    ``SecretStoreError`` from the layers below) when anything fails: a half-installed helper simply leaves the
    supervisor without snapshots, which blocks claims."""
    if runner is None:
        from ..paths import subprocess_runner as runner  # type: ignore[assignment]
    assert runner is not None
    who = (account or default_account()).strip()
    command = helper_command(python)  # validate before touching anything
    try:
        key_hex = ensure_snapshot_key(store)
    except SnapshotError as exc:
        raise HelperError(str(exc)) from None
    prepare_helper_dir(layout, who, key_hex, runner)
    res = runner(schtasks_create_argv(who, command))
    if getattr(res, "returncode", 1) != 0:
        raise HelperError(f"could not register the session helper task (exit {getattr(res, 'returncode', '?')})")
    started = False
    if start_now:
        started = getattr(runner(schtasks_run_argv()), "returncode", 1) == 0
    return {"account": who, "task": HELPER_TASK_NAME, "started": started}


def remove_helper(layout: Any, *, runner: Optional[Runner] = None) -> dict:
    """Delete the logon task and the helper directory's files. A task that does not exist is not an error."""
    if runner is None:
        from ..paths import subprocess_runner as runner  # type: ignore[assignment]
    assert runner is not None
    res = runner(schtasks_delete_argv())
    task_removed = getattr(res, "returncode", 1) == 0
    removed: list[str] = []
    directory = Path(layout.helper_dir)
    if directory.is_dir():
        for child in directory.iterdir():
            try:
                child.unlink()
                removed.append(child.name)
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"task_removed": task_removed, "files_removed": removed}


def run_helper(
    layout: Any,
    *,
    interval: float = 5.0,
    once: bool = False,
    activity: Any = None,
    owner_gpu: Any = None,
    loop: Optional[Callable[..., int]] = None,
) -> int:
    """What the logon task runs, as the owner: read the key file, sample, write the signed snapshot, repeat.
    ``activity`` / ``owner_gpu`` / ``loop`` are injectable for tests."""
    from . import probes

    key = probes.read_key_file(key_path(layout))
    if activity is None:
        activity = probes.WindowsActivityProbe(probes.Win32Api())
    if owner_gpu is None:
        owner_gpu = probes.WindowsOwnerGpuProbe(activity)
    run = loop or probes.run_snapshot_loop
    return run(activity, owner_gpu, out_path=snapshot_path(layout), key=key, interval=interval, once=once)
