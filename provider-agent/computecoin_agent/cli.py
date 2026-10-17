"""Command-line interface for the ComputeCoin provider agent.

Commands:
  computecoin-agent detect              Print the capability report (JSON).
  computecoin-agent enroll --token ...  Enroll this machine; write 0600 config.
  computecoin-agent run [--iterations]  Send a few signed heartbeats.

This skeleton does NOT execute customer jobs -- only detect / enroll / heartbeat.
"""

from __future__ import annotations

import json
import time

import typer

from typing import Optional

from . import security
from .config import AgentConfig
from .enrollment import EnrollmentError
from .enrollment import enroll as enroll_machine
from .enrollment import enroll_remote
from .hardware import build_capability_report
from .heartbeat import HeartbeatAuthError, HeartbeatStaleError, heartbeat_loop
from .jobrunner import poll_loop

app = typer.Typer(add_completion=False, help="ComputeCoin provider agent.")


@app.command()
def detect() -> None:
    """Detect local hardware and print the capability report as JSON."""
    report = build_capability_report()
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))


@app.command()
def enroll(
    token: str = typer.Option(..., "--token", help="Enrollment token (enr_...)."),
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL. Hits the real enroll endpoint when set."
    ),
) -> None:
    """Pair this machine to a provider account using an enrollment token.

    With ``--server`` the agent POSTs to ``{server}/v1/agents/enroll`` per the
    Phase 2 contract and stores the returned machine credential (0600). Without
    it, the Phase 1 offline path mints a local signing secret instead.
    """
    if server:
        report = build_capability_report()
        try:
            result = enroll_remote(server, token, report)
        except EnrollmentError as exc:
            typer.echo(f"Enrollment failed: {exc}")
            raise typer.Exit(code=1)
        typer.echo(f"Enrolled machine {result.machine_id}")
        typer.echo(
            f"Heartbeat interval: {result.heartbeat_interval_seconds}s "
            f"(online window {result.online_window_seconds}s)"
        )
        typer.echo(f"Credential written to {result.config_path} (0600)")
        return

    result = enroll_machine(token)
    typer.echo(f"Enrolled machine {result.machine_id} (offline/local)")
    typer.echo(f"Config written to {result.config_path} (0600)")


@app.command()
def run(
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL (defaults to the stored api_url)."
    ),
    iterations: int = typer.Option(
        0, help="Number of heartbeats to send; 0 means loop until interrupted."
    ),
    interval: Optional[float] = typer.Option(
        None, help="Seconds between heartbeats (defaults to heartbeat_interval_seconds)."
    ),
) -> None:
    """Send signed heartbeats to the backend at ``heartbeat_interval_seconds``.

    Loops until interrupted by default; pass ``--iterations N`` for a bounded run
    (also what tests do, with an injected transport).
    """
    config = AgentConfig.load()
    if not config.has_machine_credential():
        typer.echo(
            "Machine has no backend credential. Run "
            "`computecoin-agent enroll --token enr_... --server <url>` first."
        )
        raise typer.Exit(code=1)

    base_url = server or config.api_url
    period = interval if interval is not None else float(config.heartbeat_interval_seconds)
    report = build_capability_report()

    def _emit(i: int, result) -> None:
        label = f"{i + 1}/{iterations}" if iterations > 0 else f"{i + 1}"
        if isinstance(result, HeartbeatStaleError):
            typer.echo(f"heartbeat {label} stale: {result}")
        elif isinstance(result, Exception):
            typer.echo(f"heartbeat {label} failed: {result}")
        else:
            typer.echo(f"heartbeat {label} -> HTTP {result.status_code}")

    try:
        heartbeat_loop(
            base_url,
            config.machine_id,
            config.machine_secret,
            report=report,
            iterations=iterations,
            interval=period,
            emit=_emit,
        )
    except HeartbeatAuthError as exc:
        typer.echo(f"heartbeat rejected (re-enroll needed): {exc}")
        raise typer.Exit(code=1)


@app.command()
def work(
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL (defaults to the stored api_url)."
    ),
    once: bool = typer.Option(
        False, "--once", help="Claim + execute at most one job, then exit."
    ),
    iterations: int = typer.Option(
        0,
        "--iterations",
        help="Number of claim attempts; 0 loops until interrupted. Ignored with --once.",
    ),
    interval: float = typer.Option(
        5.0, "--interval", help="Seconds to wait after an empty claim (204)."
    ),
) -> None:
    """Run the claim -> execute -> result loop.

    Each iteration claims the next job RESERVED for this machine, runs it through
    the approved Executor (simulated in-process, or a hardened container -- never
    arbitrary shell), uploads artifacts via the signed grant, and submits the
    idempotent receipt. A failed job is reported ``failed`` with a sanitized
    error, never as a fabricated success.
    """
    config = AgentConfig.load()
    if not config.has_machine_credential():
        typer.echo(
            "Machine has no backend credential. Run "
            "`computecoin-agent enroll --token enr_... --server <url>` first."
        )
        raise typer.Exit(code=1)

    base_url = server or config.api_url
    n = 1 if once else iterations

    def _emit(i: int, summary) -> None:
        label = f"{i + 1}"
        if summary.get("error"):
            typer.echo(f"iter {label}: error {summary['error']}")
        elif summary.get("claimed"):
            typer.echo(
                f"iter {label}: job {summary.get('job_id')} -> {summary.get('status')}"
            )
        else:
            typer.echo(f"iter {label}: no job available")

    poll_loop(
        base_url,
        config.machine_id,
        config.machine_secret,
        iterations=n,
        interval=interval,
        emit=_emit,
    )


@app.command()
def security_info() -> None:
    """Print what this agent will and will not do with customer workloads."""
    typer.echo(f"Job execution implemented: {security.JOB_EXECUTION_IMPLEMENTED}")
    typer.echo("Forbidden in workloads (even once job execution lands):")
    for item in security.FORBIDDEN_IN_WORKLOADS:
        typer.echo(f"  - {item}")


if __name__ == "__main__":
    app()
