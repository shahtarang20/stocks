"""Command-line interface for the ComputeCoin provider agent.

Commands:
  computecoin-agent detect              Print the capability report (JSON).
  computecoin-agent enroll --token ...  Enroll this machine; write 0600 config.
  computecoin-agent run [--iterations]  Send a few signed heartbeats.

This skeleton does NOT execute customer jobs -- only detect / enroll / heartbeat.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import typer

from typing import Optional

from . import security
from .config import AgentConfig
from .enrollment import EnrollmentError
from .enrollment import enroll as enroll_machine
from .enrollment import enroll_remote
from .hardware import build_capability_report, check_dual_gpu_vendor_conflict
from .heartbeat import HeartbeatAuthError, HeartbeatStaleError, heartbeat_loop
from .jobrunner import poll_loop

app = typer.Typer(add_completion=False, help="ComputeCoin provider agent.")


@app.command()
def detect() -> None:
    """Detect local hardware and print the capability report as JSON."""
    config = AgentConfig.load()
    report = build_capability_report(config_gpu_vendor=config.gpu_vendor)
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
    config = AgentConfig.load()
    if check_dual_gpu_vendor_conflict(config.gpu_vendor):
        typer.echo(
            "Enrollment refused: both NVIDIA and AMD GPUs were detected. "
            "Set gpu_vendor ('nvidia' or 'amd') in ~/.computecoin/agent.toml "
            "or COMPUTECOIN_GPU_VENDOR before enrolling."
        )
        raise typer.Exit(code=1)

    if sys.platform == "win32":
        # X7: never leave a plain-text credential in a Windows profile. Enrol through the DPAPI-backed path instead.
        if not server:
            typer.echo("Offline enrollment writes a plain-text secret and is not supported on Windows. Use --server.")
            raise typer.Exit(code=1)
        from .windows.cli_commands import enroll_windows

        enroll_windows(server, token, build_capability_report(config_gpu_vendor=config.gpu_vendor))
        return

    if server:
        report = build_capability_report(config_gpu_vendor=config.gpu_vendor)
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
    report = build_capability_report(config_gpu_vendor=config.gpu_vendor)
    from .thermal import ThermalGuard

    thermal_guard = ThermalGuard.from_config(config)  # B6: report "throttled" when the GPU is too hot

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
            availability_fn=thermal_guard.availability,
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
    private_tier: Optional[str] = typer.Option(
        None, "--private-tier", help="Also take private jobs (sealed, sealed_pro or confidential). Each job is attested with its own fresh in-RAM keys, wiped when it ends."
    ),
    control_socket: Optional[str] = typer.Option(
        None, "--control-socket",
        help="HOST:PORT to listen on for a supervisor (W4 control channel; loopback only). Off by default: without it "
             "the runner behaves exactly as before. With it no job is claimed until the supervisor sends `start`.",
    ),
    control_token_file: Optional[str] = typer.Option(
        None, "--control-token-file",
        help="File holding the control token (mode 600). Or set COMPUTECOIN_CONTROL_TOKEN. Never put it on the command line.",
    ),
    control_allow_private: bool = typer.Option(
        False, "--control-allow-private", help="Allow HOST to be a private address (the WSL2 distro address) instead of loopback.",
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
        if summary.get("throttled"):
            typer.echo(f"iter {label}: GPU too hot, not claiming")
        elif summary.get("held"):
            typer.echo(f"iter {label}: not claiming ({summary['held']})")
        elif summary.get("error"):
            typer.echo(f"iter {label}: error {summary['error']}")
        elif summary.get("claimed"):
            typer.echo(
                f"iter {label}: job {summary.get('job_id')} -> {summary.get('status')}"
            )
        else:
            typer.echo(f"iter {label}: no job available")

    from .thermal import ThermalGuard

    control = None
    control_server = None
    if control_socket:
        from .control_channel import AuthError, load_token, start_runner_control
        from . import __version__ as _agent_version

        try:
            _token = load_token(control_token_file)
            control, control_server = start_runner_control(
                control_socket, _token, allow_private=control_allow_private, agent_version=_agent_version,
            )
        except (AuthError, ValueError, OSError) as exc:
            typer.echo(f"Control channel not started: {exc}")
            raise typer.Exit(code=1)
        # the supervisor's native temperature (Windows NVML) feeds the guard; the guard's limits are set over the channel
        thermal_guard = ThermalGuard.from_config(config, sampler=control.temperature_sampler)
        control.bind_thermal_guard(thermal_guard)
    else:
        thermal_guard = ThermalGuard.from_config(config)  # B6: no new claim while the GPU is at its temperature cap

    try:
        _work_poll(base_url, config, n, interval, _emit, private_tier, thermal_guard, control)
    finally:
        if control_server is not None:
            control_server.close()


def _work_poll(base_url, config, n, interval, _emit, private_tier, thermal_guard, control) -> None:
    poll_loop(
        base_url,
        config.machine_id,
        config.machine_secret,
        iterations=n,
        interval=interval,
        emit=_emit,
        attestation_manager=_attestation_manager(base_url, config, private_tier),
        thermal_guard=thermal_guard,
        egress_proxy_image=config.egress_proxy_image,
        multinode_network=config.multinode_network,
        multinode_network_firewalled=config.multinode_network_firewalled,
        cluster_addr=config.cluster_addr,
        amd_sealed_enabled=config.amd_sealed_enabled,
        **({"control": control} if control is not None else {}),  # W4: absent unless --control-socket is used
    )


@app.command()
def sessions(
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL (defaults to the stored api_url)."
    ),
    once: bool = typer.Option(False, "--once", help="Serve at most one session claim attempt, then exit."),
    interval: float = typer.Option(5.0, "--interval", help="Seconds to wait after an empty claim."),
) -> None:
    """Serve interactive sessions (U10): outbound-only, end-to-end encrypted to the browser."""
    config = AgentConfig.load()
    if not config.has_machine_credential():
        typer.echo(
            "Machine has no backend credential. Run "
            "`computecoin-agent enroll --token enr_... --server <url>` first."
        )
        raise typer.Exit(code=1)
    from .executor import DockerExecutor
    from .session_agent import SessionApi, run_forever

    api = SessionApi(server or config.api_url, config.machine_id, config.machine_secret)
    run_forever(api, DockerExecutor(), poll_seconds=interval, once=once)


@app.command()
def start(
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL (defaults to the stored api_url)."
    ),
    interval: Optional[float] = typer.Option(
        None, "--interval", help="Seconds between heartbeats (defaults to heartbeat_interval_seconds)."
    ),
    poll_interval: float = typer.Option(
        5.0, "--poll-interval", help="Seconds to wait between job claims when idle."
    ),
    private_tier: Optional[str] = typer.Option(
        None, "--private-tier", help="Also take private jobs (sealed, sealed_pro or confidential). Each job is attested with its own fresh in-RAM keys, wiped when it ends."
    ),
) -> None:
    """Run BOTH the heartbeat presence daemon AND the job worker concurrently.

    This is the all-in-one command for GPU hosts: it keeps the machine ONLINE
    in the marketplace and automatically claims and executes jobs when scheduled.
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

    typer.echo(f"Starting ComputeCoin Provider Node [{config.machine_id}]")
    typer.echo(f"Connecting to: {base_url}")
    typer.echo(f"Heartbeat interval: {period}s | Job poll interval: {poll_interval}s")

    from .thermal import ThermalGuard, apply_power_limit

    thermal_guard = ThermalGuard.from_config(config)
    apply_power_limit(config.gpu_power_limit_w)  # best effort; never fails startup

    hb_thread = threading.Thread(
        target=heartbeat_loop,
        args=(base_url, config.machine_id, config.machine_secret),
        kwargs={
            "availability_fn": thermal_guard.availability,
            "report": report,
            "iterations": 0,
            "interval": period,
            "emit": lambda i, res: typer.echo(f"[heartbeat] pulse {i + 1} -> HTTP 200")
            if not isinstance(res, Exception)
            else typer.echo(f"[heartbeat] pulse {i + 1} -> {res}"),
        },
        daemon=True,
    )
    hb_thread.start()

    if config.netbench_enabled:  # U13: off by default; the responder binds ONLY to cluster_addr
        from .netbench import start_netbench

        if not config.cluster_addr:
            typer.echo("[netbench] netbench_enabled but cluster_addr is not set; benchmark not started")
        else:
            try:
                start_netbench(base_url, config.machine_id, config.machine_secret, config.cluster_addr,
                               emit=typer.echo)
            except ValueError as exc:
                typer.echo(f"[netbench] not started: {exc}")

    def _worker_emit(i: int, summary: dict) -> None:
        if summary.get("claimed"):
            typer.echo(f"[worker] claimed job {summary.get('job_id')} -> {summary.get('status')}")
        elif summary.get("error"):
            typer.echo(f"[worker] claim error: {summary.get('error')}")

    try:
        poll_loop(
            base_url,
            config.machine_id,
            config.machine_secret,
            iterations=0,
            interval=poll_interval,
            emit=_worker_emit,
            attestation_manager=_attestation_manager(base_url, config, private_tier),
            thermal_guard=thermal_guard,
            egress_proxy_image=config.egress_proxy_image,
            multinode_network=config.multinode_network,
            multinode_network_firewalled=config.multinode_network_firewalled,
            cluster_addr=config.cluster_addr,
            amd_sealed_enabled=config.amd_sealed_enabled,
        )
    except (KeyboardInterrupt, SystemExit):
        typer.echo("\nStopping ComputeCoin agent gracefully...")


def _attestation_manager(base_url: str, config: "AgentConfig", tier: Optional[str]):
    if not tier:
        return None
    if tier not in ("sealed", "sealed_pro", "confidential"):
        typer.echo("--private-tier must be sealed, sealed_pro or confidential")
        raise typer.Exit(code=2)
    from .attestation import AttestationManager
    from .tee import hardware_for_tier

    return AttestationManager(
        base_url, config.machine_id, config.machine_secret, tier=tier,
        on_error=lambda exc: typer.echo(f"[attest] {exc}"),
        hardware=hardware_for_tier(config, tier),
    )


@app.command()
def attest(
    server: Optional[str] = typer.Option(
        None, "--server", help="Backend base URL (defaults to the stored api_url)."
    ),
    tier: str = typer.Option(
        "sealed", "--tier", help="Requested isolation tier: sealed, sealed_pro, or confidential."
    ),
) -> None:
    """Obtain a signed Attestation Verdict from the backend using machine HMAC authentication.

    Generates ephemeral session keys strictly in RAM and verifies that host measurements
    and security locks match trusted criteria (ADR-0005, WP2).
    """
    from .attestation import AttestationClientError, perform_attestation
    from .tee import hardware_for_tier
    from .tpm import TpmError

    config = AgentConfig.load()
    if not config.has_machine_credential():
        typer.echo("Machine has no backend credential. Run `computecoin-agent enroll` first.")
        raise typer.Exit(code=1)

    base_url = server or config.api_url
    typer.echo(f"Requesting remote attestation for tier '{tier}' from {base_url}...")
    try:
        session = perform_attestation(
            base_url,
            config.machine_id,
            config.machine_secret,
            tier=tier,
            hardware=hardware_for_tier(config, tier),
        )
    except (AttestationClientError, TpmError) as exc:
        typer.echo(f"Attestation failed: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"Attestation verified: verdict_id={session.verdict.get('verdict_id')}")
    typer.echo(f"Tier reached: {session.isolation_tier}")
    typer.echo(f"Expires at: {session.expires_at.isoformat()}")
    typer.echo(f"Ephemeral X25519 in RAM: {session.ephemeral_public_key}")
    typer.echo(f"Receipt Ed25519 in RAM: {session.receipt_signing_public_key}")
    if session.log_leaf_index is not None:
        typer.echo(f"Transparency log leaf index: #{session.log_leaf_index}")
    typer.echo(
        "This was a check only: the keys above lived in this process and are now gone. "
        "To take private jobs run `computecoin-agent start --private-tier <tier>`."
    )


@app.command()
def tpm_enrol(
    server: Optional[str] = typer.Option(None, "--server", help="Backend base URL (defaults to the stored api_url)."),
) -> None:
    """Bind this machine's TPM attestation key to its EK with the backend (credential activation).

    Creates the EK and AK inside the TPM if needed (persistent handles; nothing secret on disk),
    sends the EK certificate and the AK's public area, answers the backend's MakeCredential
    challenge with TPM2_ActivateCredential, and returns the recovered secret.
    """
    from .jobrunner import JobRunnerError
    from .tpm import TpmError, enrol

    config = AgentConfig.load()
    if not config.has_machine_credential():
        typer.echo("Machine has no backend credential. Run `computecoin-agent enroll` first.")
        raise typer.Exit(code=1)
    tpm = config.tpm()
    if tpm is None:
        typer.echo("No TPM configured: set tpm_tcti (e.g. device:/dev/tpmrm0) in the agent config.")
        raise typer.Exit(code=1)
    try:
        ak_name = enrol(server or config.api_url, config.machine_id, config.machine_secret, tpm)
    except (TpmError, JobRunnerError) as exc:
        typer.echo(f"TPM enrolment failed: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"TPM attestation key enrolled: {ak_name}")


@app.command()
def security_info() -> None:
    """Print what this agent will and will not do with customer workloads."""
    typer.echo(f"Job execution implemented: {security.JOB_EXECUTION_IMPLEMENTED}")
    typer.echo("Forbidden in workloads (even once job execution lands):")
    for item in security.FORBIDDEN_IN_WORKLOADS:
        typer.echo(f"  - {item}")


@app.command("support-bundle")
def support_bundle_cmd(
    out: Optional[str] = typer.Option(
        None, "--out", help="Where to write the zip (default: computecoin-support-<time>.zip in the current folder)."
    ),
    log_file: list[str] = typer.Option(
        [], "--log-file", help="An agent or supervisor log to include (repeatable). Default: the platform's log folder."
    ),
    log_dir: Optional[str] = typer.Option(None, "--log-dir", help="A folder of .log files to include."),
    max_log_lines: int = typer.Option(2000, "--max-log-lines", min=0, max=20000, help="Lines kept from the end of each log."),
    no_hardware: bool = typer.Option(False, "--no-hardware", help="Skip hardware detection."),
) -> None:
    """Write a REDACTED diagnostics zip to attach to a support request (W10, L7).

    It holds versions, the platform report, hardware, the settings with every secret replaced, requirement and runtime
    health results and the end of the logs with secrets scrubbed. It never holds credentials or keys. Read it before you
    send it. If a secret still looks present, nothing is written.
    """
    from pathlib import Path

    from .support_bundle import BundleLeakError, build_bundle, default_bundle_name

    try:
        config = AgentConfig.load()
    except Exception as exc:  # the bundle is most useful exactly when the config is broken; never echo the message (it can hold values)
        typer.echo(f"Note: the agent settings could not be read ({type(exc).__name__}); they are left out.")
        config = None
    target = Path(out) if out else Path.cwd() / default_bundle_name()
    try:
        manifest = build_bundle(
            target,
            config=config,
            log_files=[Path(p) for p in log_file],
            log_dir=Path(log_dir) if log_dir else None,
            max_log_lines=max_log_lines,
            include_hardware=not no_hardware,
        )
    except FileExistsError:
        typer.echo(f"Refusing to overwrite {target}. Choose another --out.")
        raise typer.Exit(code=1)
    except BundleLeakError as exc:
        typer.echo(f"Not written: the redaction check failed ({exc}). Nothing was saved. Please report this.")
        raise typer.Exit(code=2)
    typer.echo(f"Support bundle written: {target}")
    typer.echo(f"  {len(manifest['files'])} file(s), {manifest['redaction_total']} replacement(s) made, {len(manifest['logs'])} log file(s).")
    typer.echo("  Open it and read it before you send it. It must not contain any password, token or key.")


if sys.platform == "win32":  # W2: `computecoin-agent service install|start|stop|remove|status|run|enroll|migrate`
    from .windows.cli_commands import register as _register_windows_service

    _register_windows_service(app)


if __name__ == "__main__":
    app()
