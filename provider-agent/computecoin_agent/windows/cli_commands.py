"""``computecoin-agent service ...`` (Windows only): install | start | stop | remove | status | run | enroll | migrate |
helper install | helper run | helper remove.

``register(app)`` is called from ``cli.py`` ONLY when ``sys.platform == "win32"``, so on Linux these commands do not
exist. Each command builds its collaborators lazily so ``--help`` works without pywin32.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer


def _is_admin() -> bool:
    from ..platform import get_platform

    return bool(get_platform().owner.is_admin())


def _need_admin(what: str) -> None:
    if not _is_admin():
        typer.echo(f"{what} needs an elevated (Administrator) prompt.")
        raise typer.Exit(code=1)


def enroll_windows(server: str, token: str, report: Any, *, layout: Any = None, store: Any = None, runtime_gate: Any = None) -> None:
    """Shared by ``computecoin-agent enroll --server`` (on Windows) and ``service enroll``: DPAPI, no plain file.

    The managed runtime must pass its health and GPU smoke test first (``RuntimeProvisioner.assert_ready_for_enrolment``,
    see ``enrol.default_runtime_gate``); ``runtime_gate`` replaces that check in tests."""
    from ..enrollment import EnrollmentError
    from .enrol import enroll_protected
    from .paths import Layout, PathSecurityError, restrict_path
    from .runtime.provision import RuntimeNotReady
    from .secrets_dpapi import DpapiSecretStore, SecretStoreError

    layout = layout or Layout.default()
    store = store or DpapiSecretStore(layout.secrets_dir, prepare_directory=restrict_path)
    try:
        result = enroll_protected(server, token, report, layout=layout, store=store, runtime_gate=runtime_gate)
    except RuntimeNotReady as exc:
        typer.echo(f"Enrollment stopped: the managed runtime is not ready, so this machine was not enrolled and your token was not used. {exc}")
        raise typer.Exit(code=1)
    except EnrollmentError as exc:
        typer.echo(f"Enrollment failed: {exc}")
        raise typer.Exit(code=1)
    except (SecretStoreError, PathSecurityError) as exc:
        typer.echo(f"Enrollment stopped: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Enrolled machine {result.machine_id}")
    typer.echo(f"Credential stored with DPAPI under {layout.secrets_dir}; no plain-text copy was kept.")


def build_service_app(
    *, service_manager_factory: Any = None, store_factory: Any = None, layout_factory: Any = None,
    runtime_gate: Any = None, helper_runner: Any = None,
) -> typer.Typer:
    """The Typer sub-app. The factories exist so tests can inject fakes (``runtime_gate``: the enrolment gate;
    ``helper_runner``: the argv runner used to register the session-helper task)."""
    svc = typer.Typer(add_completion=False, help="Manage the ComputeCoin Windows Service (Windows only).")

    def _layout():
        from .paths import Layout

        return layout_factory() if layout_factory else Layout.default()

    def _store(layout):
        if store_factory:
            return store_factory(layout)
        from .paths import restrict_path
        from .secrets_dpapi import DpapiSecretStore

        return DpapiSecretStore(layout.secrets_dir, prepare_directory=restrict_path)

    def _manager():
        if service_manager_factory:
            return service_manager_factory()
        from .service import ServiceManagerError, load_service_manager

        try:
            return load_service_manager()
        except ServiceManagerError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)

    def _guard(fn):
        from .service import ServiceManagerError

        try:
            return fn()
        except ServiceManagerError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)

    def _seed_known_good(layout, msi: Optional[str], version: Optional[str]) -> None:
        """Best effort, never fatal (see ``updater.state.seed_known_good``): without a seed the first update cannot roll back."""
        from .. import __version__
        from ..updater.state import UpdateState, seed_known_good

        if not msi:
            typer.echo("Note: no --seed-msi given, so the first update will have no installer to roll back to.")
            return
        try:
            done = seed_known_good(UpdateState(layout.update_state_file), version=version or __version__,
                                   artifact=msi, cache_dir=layout.update_dir / "cache")
        except (OSError, ValueError) as exc:
            typer.echo(f"Warning: could not record the known-good installer ({type(exc).__name__}); the first update cannot roll back.")
            return
        typer.echo("Recorded the installer as the known-good rollback source." if done
                   else "A known-good installer is already recorded; left as is.")

    @svc.command("install")
    def install(
        startup: str = typer.Option("auto", "--startup", help="auto (start on boot), delayed, manual or disabled."),
        username: Optional[str] = typer.Option(None, "--username", help="Run as this account instead of LocalSystem."),
        seed_msi: Optional[str] = typer.Option(None, "--seed-msi", help="The installer being installed: recorded as the updater's known-good rollback source."),
        seed_version: Optional[str] = typer.Option(None, "--seed-version", help="Version of --seed-msi (default: this agent's version)."),
    ) -> None:
        """Create the data directory (ACL: SYSTEM + Administrators), the control token, seed the updater's known-good, then install the service."""
        _need_admin("Installing the service")
        layout = _layout()
        from .paths import ensure_layout
        from .supervisor import ensure_control_token

        ensure_layout(layout)
        ensure_control_token(_store(layout))
        _seed_known_good(layout, seed_msi, seed_version)
        _guard(lambda: _manager().install(start_type=startup, username=username))
        typer.echo(f"Service installed (start: {startup}). Enrol with `computecoin-agent service enroll`, then `service start`.")

    @svc.command("start")
    def start() -> None:
        """Start the service."""
        _need_admin("Starting the service")
        _guard(lambda: _manager().start())
        typer.echo("Service started.")

    @svc.command("stop")
    def stop() -> None:
        """Stop the service: the runner drains its current job first, up to a short limit."""
        _need_admin("Stopping the service")
        _guard(lambda: _manager().stop())
        typer.echo("Service stopped.")

    @svc.command("remove")
    def remove() -> None:
        """Remove the service (credentials and the runtime distro are NOT touched here)."""
        _need_admin("Removing the service")
        _guard(lambda: _manager().remove())
        typer.echo("Service removed.")

    @svc.command("status")
    def status() -> None:
        """Print the service state: running, stopped, not_installed ..."""
        typer.echo(_guard(lambda: _manager().status()))

    @svc.command("run")
    def run() -> None:
        """Run the supervisor in this console (same code as the service). Ctrl+C drains and stops."""
        from .service import run_console
        from .supervisor import build_supervisor

        layout = _layout()
        from .logging_setup import configure_logging

        configure_logging(layout.logs_dir, console=True, event_log=False)
        try:
            code = run_console(lambda: build_supervisor(layout, store=_store(layout)))
        except RuntimeError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        raise typer.Exit(code=code)

    @svc.command("enroll")
    def enroll(
        token: str = typer.Option(..., "--token", help="Enrollment token (enr_...)."),
        server: str = typer.Option(..., "--server", help="Backend base URL."),
    ) -> None:
        """Enrol this machine; the credential goes straight into DPAPI, never a plain file."""
        _need_admin("Enrolling")
        from ..hardware import build_capability_report

        layout = _layout()
        enroll_windows(server, token, build_capability_report(), layout=layout, store=_store(layout), runtime_gate=runtime_gate)

    @svc.command("migrate")
    def migrate(
        source: Optional[str] = typer.Option(None, "--from", help="Plain agent.toml to migrate (default ~/.computecoin/agent.toml)."),
    ) -> None:
        """Move an existing plain-text credential into DPAPI and securely delete the old file."""
        _need_admin("Migrating credentials")
        from .enrol import migrate_legacy_credential
        from .secrets_dpapi import SecretStoreError

        layout = _layout()
        legacy = Path(source) if source else Path.home() / ".computecoin" / "agent.toml"
        try:
            res = migrate_legacy_credential(layout, _store(layout), legacy)
        except SecretStoreError as exc:
            typer.echo(f"Migration failed (old file left in place): {exc}")
            raise typer.Exit(code=1)
        if not res.migrated:
            typer.echo(res.note or "Nothing to migrate.")
            return
        typer.echo(f"Migrated {', '.join(res.names)} into the secret store. Old file removed: {res.old_file_removed}.")
        typer.echo("If that file may have been copied or backed up, re-enrol to rotate the credential.")

    helper = typer.Typer(add_completion=False, help="The user-session helper that tells the service whether the owner is using the PC.")
    svc.add_typer(helper, name="helper")

    @helper.command("install")
    def helper_install(
        account: Optional[str] = typer.Option(None, "--account", help="The owner's Windows account (default: the account running this command)."),
        no_start: bool = typer.Option(False, "--no-start", help="Register the logon task but do not start it now."),
    ) -> None:
        """Create the snapshot key, the restricted helper directory and the per-user logon task."""
        _need_admin("Installing the session helper")
        import sys

        from .governance.helper import HelperError, install_helper
        from .paths import PathSecurityError
        from .secrets_dpapi import SecretStoreError

        layout = _layout()
        from .paths import ensure_layout

        try:
            ensure_layout(layout)
            res = install_helper(layout, _store(layout), python=sys.executable, account=account, runner=helper_runner,
                                 start_now=not no_start)
        except (HelperError, PathSecurityError, SecretStoreError) as exc:
            typer.echo(f"Session helper not installed: {exc}")
            raise typer.Exit(code=1)
        typer.echo(f"Session helper registered for {res['account']} (task {res['task']!r}); started now: {res['started']}.")
        typer.echo("Until it reports, the service treats the owner as present and takes no work.")

    @helper.command("run")
    def helper_run(
        interval: float = typer.Option(5.0, "--interval", help="Seconds between snapshots (minimum 1)."),
        once: bool = typer.Option(False, "--once", help="Write one snapshot and exit."),
    ) -> None:
        """Run in the owner's session: sample idle time, full-screen and GPU use, write the signed snapshot file."""
        from .governance.helper import run_helper
        from .governance.snapshot import SnapshotError
        from .governance.policy import ProbeUnsupported

        try:
            code = run_helper(_layout(), interval=interval, once=once)
        except (SnapshotError, ProbeUnsupported) as exc:
            typer.echo(f"Session helper cannot run: {exc}")
            raise typer.Exit(code=1)
        raise typer.Exit(code=code)

    @helper.command("remove")
    def helper_remove() -> None:
        """Delete the logon task and the helper directory's files."""
        _need_admin("Removing the session helper")
        from .governance.helper import remove_helper

        res = remove_helper(_layout(), runner=helper_runner)
        typer.echo(f"Task removed: {res['task_removed']}. Files removed: {', '.join(res['files_removed']) or 'none'}.")

    return svc


def register(app: typer.Typer, **kwargs: Any) -> None:
    app.add_typer(build_service_app(**kwargs), name="service")
