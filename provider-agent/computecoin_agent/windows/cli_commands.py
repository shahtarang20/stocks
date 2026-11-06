"""``computecoin-agent service ...`` (Windows only): install | start | stop | remove | status | run | enroll | migrate.

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


def enroll_windows(server: str, token: str, report: Any, *, layout: Any = None, store: Any = None) -> None:
    """Shared by ``computecoin-agent enroll --server`` (on Windows) and ``service enroll``: DPAPI, no plain file."""
    from ..enrollment import EnrollmentError
    from .enrol import enroll_protected
    from .paths import Layout, PathSecurityError, restrict_path
    from .secrets_dpapi import DpapiSecretStore, SecretStoreError

    layout = layout or Layout.default()
    store = store or DpapiSecretStore(layout.secrets_dir, prepare_directory=restrict_path)
    try:
        result = enroll_protected(server, token, report, layout=layout, store=store)
    except EnrollmentError as exc:
        typer.echo(f"Enrollment failed: {exc}")
        raise typer.Exit(code=1)
    except (SecretStoreError, PathSecurityError) as exc:
        typer.echo(f"Enrollment stopped: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Enrolled machine {result.machine_id}")
    typer.echo(f"Credential stored with DPAPI under {layout.secrets_dir}; no plain-text copy was kept.")


def build_service_app(*, service_manager_factory: Any = None, store_factory: Any = None, layout_factory: Any = None) -> typer.Typer:
    """The Typer sub-app. The factories exist so tests can inject fakes."""
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

    @svc.command("install")
    def install(
        startup: str = typer.Option("auto", "--startup", help="auto (start on boot), delayed, manual or disabled."),
        username: Optional[str] = typer.Option(None, "--username", help="Run as this account instead of LocalSystem."),
    ) -> None:
        """Create the data directory (ACL: SYSTEM + Administrators), the control token, then install the service."""
        _need_admin("Installing the service")
        layout = _layout()
        from .paths import ensure_layout
        from .supervisor import ensure_control_token

        ensure_layout(layout)
        ensure_control_token(_store(layout))
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
        enroll_windows(server, token, build_capability_report(), layout=layout, store=_store(layout))

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

    return svc


def register(app: typer.Typer, **kwargs: Any) -> None:
    app.add_typer(build_service_app(**kwargs), name="service")
