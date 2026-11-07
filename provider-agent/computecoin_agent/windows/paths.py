"""ProgramData layout and ACLs for the Windows supervisor (W2, X7).

Layout (root defaults to the PAL's ``Paths.data_dir()``, i.e. ``%ProgramData%\\ComputeCoin``)::

    <root>\\agent.toml        non-secret agent config (the PAL's config_dir() is the root)
    <root>\\supervisor.toml   supervisor settings (non-secret)
    <root>\\secrets\\         DPAPI blobs (secrets_dpapi.py); never plain text
    <root>\\logs\\            rotating log files
    <root>\\state\\           small state files

ACLs: the whole tree is restricted to SYSTEM and the local Administrators group with ``icacls`` driven through an argv
list (no shell). Principals are given by well-known SID, so localized Windows builds behave the same.

Documentation checked (X11):
  * icacls (https://learn.microsoft.com/windows-server/administration/windows-commands/icacls, read 2026-11-06):
    ``*<SID>`` is the numerical-SID form; ``/grant:r`` replaces explicit grants; ``(OI)(CI)`` are the inherit flags;
    ``F`` is full access.
TODO(verify-docs):
  * the inheritance switch. The page above lists ``/inheritancelevel:r`` in its table while the long-standing icacls
    spelling is ``/inheritance:r``. ``INHERITANCE_ARGS`` holds the spelling used; if it is wrong icacls exits non-zero,
    :func:`restrict_path` raises, and nothing is stored (fail closed). Confirm with ``icacls /?`` on a real machine.
  * well-known SIDs S-1-5-18 (SYSTEM) and S-1-5-32-544 (BUILTIN\\Administrators), S-1-1-0 (Everyone),
    S-1-5-32-545 (Users), S-1-5-11 (Authenticated Users).
  * the output format of ``icacls <path> /findsid`` used by :func:`verify_restricted`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

SID_SYSTEM = "S-1-5-18"  # TODO(verify-docs)
SID_ADMINISTRATORS = "S-1-5-32-544"  # TODO(verify-docs)
RISKY_SIDS = ("S-1-1-0", "S-1-5-32-545", "S-1-5-11")  # Everyone, Users, Authenticated Users  TODO(verify-docs)
INHERITANCE_ARGS = ("/inheritance:r",)  # TODO(verify-docs): see module docstring
ICACLS = "icacls"


class PathSecurityError(RuntimeError):
    """A directory could not be restricted or verified. Callers must not store secrets there."""


@dataclass
class ExecResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], Any]  # returns an object with returncode / stdout / stderr


def subprocess_runner(argv: Sequence[str]) -> ExecResult:  # pragma: no cover - runs a real process
    """Run ``argv`` without a shell. Never called by tests (they inject a fake)."""
    proc = subprocess.run(list(argv), shell=False, capture_output=True, text=True, timeout=60, check=False)
    return ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "agent.toml"

    @property
    def supervisor_file(self) -> Path:
        return self.root / "supervisor.toml"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def helper_dir(self) -> Path:
        """Per-install session-helper directory (snapshot file and key file); restricted by ``prepare_helper_dir``."""
        return self.root / "helper"

    @property
    def update_dir(self) -> Path:
        """Updater work directory (downloads, rollback cache); the updater checks it is private before use."""
        return self.root / "update"

    @property
    def update_state_file(self) -> Path:
        return self.state_dir / "update-state.json"

    @property
    def runtime_settings_file(self) -> Path:
        """Runtime distro settings JSON (same file ``msi_actions.py`` and ``provision.ps1`` use)."""
        return self.root / "runtime-settings.json"

    @property
    def runtime_manifest_file(self) -> Path:
        """The signed ``runtime-manifest.json`` the provisioner verified (same location ``provision.ps1`` documents)."""
        return self.root / "runtime-manifest.json"

    def directories(self) -> tuple[Path, ...]:
        return (self.root, self.secrets_dir, self.logs_dir, self.state_dir)

    @classmethod
    def from_root(cls, root: "str | Path") -> "Layout":
        return cls(Path(root))

    @classmethod
    def default(cls) -> "Layout":
        """``%ProgramData%\\ComputeCoin`` via the PAL (so ``COMPUTECOIN_CONFIG_DIR`` is honoured identically)."""
        from ..platform import get_platform

        return cls(get_platform().paths.config_dir())


def acl_argv(path: "str | Path") -> list[str]:
    """The exact icacls command line that restricts ``path`` (and everything created in it) to SYSTEM + Administrators."""
    return [
        ICACLS, str(path), *INHERITANCE_ARGS,
        "/grant:r", f"*{SID_SYSTEM}:(OI)(CI)F", f"*{SID_ADMINISTRATORS}:(OI)(CI)F",
    ]


def restrict_path(path: "str | Path", runner: Optional[Runner] = None) -> None:
    """Restrict ``path`` to SYSTEM and Administrators. Raises :class:`PathSecurityError` on any failure."""
    runner = runner or subprocess_runner
    try:
        result = runner(acl_argv(path))
    except Exception as exc:  # noqa: BLE001 - missing icacls, timeout ...
        raise PathSecurityError(f"could not run icacls: {type(exc).__name__}") from exc
    if getattr(result, "returncode", 1) != 0:
        raise PathSecurityError(f"icacls refused to restrict the directory (exit {getattr(result, 'returncode', '?')})")


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@ -]{1,64}(\\[A-Za-z0-9_.@ -]{1,64})?$")


def acl_argv_with_user(path: "str | Path", account: str) -> list[str]:
    """Like :func:`acl_argv` plus MODIFY for one named account (the user the session helper runs as). The account is
    validated (no shell, but no surprises in an icacls grant either). TODO(verify-docs): ``(M)`` is modify, and the
    ``DOMAIN\\user`` name form is accepted by ``/grant``."""
    if not isinstance(account, str) or not _ACCOUNT_RE.match(account.strip()):
        raise PathSecurityError("not a valid account name")
    return [*acl_argv(path), f"{account.strip()}:(OI)(CI)M"]


def restrict_path_for_user(path: "str | Path", account: str, runner: Optional[Runner] = None) -> None:
    """Restrict ``path`` to SYSTEM, Administrators and ONE account. Used only for the session-helper directory, whose
    writer (the helper) runs as the interactive user. Raises :class:`PathSecurityError` on any failure."""
    runner = runner or subprocess_runner
    argv = acl_argv_with_user(path, account)
    try:
        result = runner(argv)
    except Exception as exc:  # noqa: BLE001
        raise PathSecurityError(f"could not run icacls: {type(exc).__name__}") from exc
    if getattr(result, "returncode", 1) != 0:
        raise PathSecurityError(f"icacls refused to restrict the directory (exit {getattr(result, 'returncode', '?')})")


def verify_restricted(path: "str | Path", runner: Optional[Runner] = None) -> None:
    """Best-effort second look: none of the risky SIDs appears explicitly in the DACL. TODO(verify-docs): output format.

    ``icacls <path> /findsid *<SID>`` is documented as finding files whose DACL explicitly mentions the SID; a hit is
    assumed to print the file name. A hit raises. This is a check on top of :func:`restrict_path`, not instead of it.
    """
    runner = runner or subprocess_runner
    for sid in RISKY_SIDS:
        try:
            result = runner([ICACLS, str(path), "/findsid", f"*{sid}"])
        except Exception as exc:  # noqa: BLE001
            raise PathSecurityError(f"could not verify ACL: {type(exc).__name__}") from exc
        out = (getattr(result, "stdout", "") or "")
        if getattr(result, "returncode", 1) == 0 and str(path).lower() in out.lower():
            raise PathSecurityError("directory ACL still grants access to a broad group")


def ensure_layout(layout: Layout, runner: Optional[Runner] = None, *, verify: bool = True) -> Layout:
    """Create every directory, then restrict the root (children inherit ``(OI)(CI)``). Idempotent.

    The root is restricted BEFORE the secrets directory is created so nothing is ever created world-readable.
    """
    layout.root.mkdir(parents=True, exist_ok=True)
    restrict_path(layout.root, runner)
    for d in layout.directories()[1:]:
        d.mkdir(parents=True, exist_ok=True)
    if verify:
        verify_restricted(layout.root, runner)
    return layout


def make_acl_paths(layout: Layout, runner: Optional[Runner] = None) -> Any:
    """A PAL ``Paths`` whose ``config_dir`` is the layout root and whose ``restrict_*`` apply the ACL.

    Lets ``AgentConfig.save`` work on Windows before the PAL's own Windows ``restrict_*`` stubs are wired to
    :func:`restrict_path` (they currently raise PlatformUnsupported). Built lazily so importing this module never needs the PAL.
    """
    from ..platform import Paths, get_platform

    base = get_platform().paths

    class _AclPaths(Paths):  # type: ignore[misc, valid-type]
        platform_name = "windows"

        def config_dir(self) -> Path:
            return layout.root

        def cache_dir(self) -> Path:
            return base.cache_dir()

        def data_dir(self) -> Path:
            return layout.root

        def restrict_dir(self, path: "str | Any") -> None:
            restrict_path(path, runner)  # not best effort: a directory we cannot lock down must not receive a credential

        def restrict_file(self, path: "str | Any") -> None:
            # A file inside the restricted tree inherits the ACL; nothing further to do (and nothing weaker).
            return None

    return _AclPaths()


def install_acl_platform(layout: Layout, runner: Optional[Runner] = None) -> Any:
    """Make ``get_platform().paths`` the ACL-aware Paths for this process (service, console run, enrolment)."""
    import dataclasses

    from ..platform import get_platform, set_platform

    current = get_platform()
    patched = dataclasses.replace(current, paths=make_acl_paths(layout, runner))
    set_platform(patched)
    return patched
