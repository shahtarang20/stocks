#!/usr/bin/env python3
"""ComputeCoin developer task runner for Windows (and Linux, macOS): the same targets as the Makefile (cross-platform plan W10).

    python tasks.py help
    python tasks.py agent-install
    python tasks.py agent-test
    python tasks.py web-check
    python tasks.py --dry-run test          # print what would run, run nothing

Windows PowerShell:   py -3 tasks.py agent-test        (or:  .\\scripts\\windows\\tasks.ps1 agent-test)
Linux / macOS bash:   python3 tasks.py agent-test      (or:  make agent-test)

Every Makefile target is here under the same name (``tests/windows/test_operator_tooling.py`` fails when one is missing).
Commands run as argument lists, never through a shell, so paths with spaces and quoting behave the same everywhere.

The control plane (API, worker, database) is a Linux target (X3). On Windows the ``api-*`` targets refuse to run natively and
say how to use WSL2 or Docker; ``--wsl`` runs the matching ``make`` target inside the default WSL distro instead.
TODO(verify-docs): ``wsl.exe --cd <dir> -- <command>`` against Microsoft's current wsl command reference before relying on it.

No secrets are read or printed here. Nothing is installed outside the project: virtual environments live in ``<app>/.venv``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"
AGENT_DIR = ROOT / "provider-agent"
SDK_PY_DIR = ROOT / "sdk" / "python"
SDK_TS_DIR = ROOT / "sdk" / "typescript"


def venv_exe(app_dir: Path, name: str) -> Path:
    """``<app>/.venv/bin/<name>`` on POSIX, ``<app>\\.venv\\Scripts\\<name>.exe`` on Windows."""
    if IS_WINDOWS:
        return app_dir / ".venv" / "Scripts" / f"{name}.exe"
    return app_dir / ".venv" / "bin" / name


def tool(app_dir: Path, name: str) -> list[str]:
    """The app's own virtualenv tool when it exists, else this interpreter's module (``python -m pytest``), so CI and
    machines without a ``.venv`` work too."""
    exe = venv_exe(app_dir, name)
    return [str(exe)] if exe.exists() else [sys.executable, "-m", name]


def _which(name: str) -> str:
    """Resolve a command the way the OS would (npm is ``npm.cmd`` on Windows); fall back to the bare name."""
    return shutil.which(name) or name


@dataclass
class Context:
    dry_run: bool = False
    wsl: bool = False
    extra: Sequence[str] = ()
    executed: Optional[list[list[str]]] = None  # every command, in order, with its cwd as the first element's note

    def run(self, argv: Sequence[str], cwd: Optional[Path] = None) -> int:
        argv = [str(a) for a in argv]
        shown = subprocess.list2cmdline(argv) if IS_WINDOWS else " ".join(_quote(a) for a in argv)
        where = f"  (in {cwd})" if cwd else ""
        print(f"+ {shown}{where}", flush=True)
        if self.executed is not None:
            self.executed.append([str(cwd) if cwd else "", *argv])
        if self.dry_run:
            return 0
        try:
            return subprocess.run(argv, cwd=str(cwd) if cwd else None, check=False).returncode
        except FileNotFoundError:
            print(f"error: command not found: {argv[0]}", file=sys.stderr)
            return 127


def _quote(arg: str) -> str:
    return arg if arg and all(c.isalnum() or c in "-_./:=@%+," for c in arg) else "'" + arg.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class Task:
    name: str
    help: str
    fn: Callable[[Context], int]
    linux_only: bool = False  # control plane: needs WSL2 or Docker on Windows (X3)


TASKS: dict[str, Task] = {}


def task(name: str, help: str, *, linux_only: bool = False):
    def deco(fn: Callable[[Context], int]) -> Callable[[Context], int]:
        TASKS[name] = Task(name, help, fn, linux_only)
        return fn

    return deco


def _all(*codes: int) -> int:
    return next((c for c in codes if c != 0), 0)


# ---- Infra (docker compose; Docker Desktop or an Engine in WSL2 on Windows) ----
@task("up", "Start the full local stack (db, redis, minio, api, web)")
def up(ctx: Context) -> int:
    code = ctx.run(["docker", "compose", "up", "--build", "-d"], ROOT)
    if code == 0:
        print("API:  http://localhost:8000/health   Web: http://localhost:3000")
    return code


@task("down", "Stop the stack")
def down(ctx: Context) -> int:
    return ctx.run(["docker", "compose", "down"], ROOT)


@task("logs", "Tail stack logs")
def logs(ctx: Context) -> int:
    return ctx.run(["docker", "compose", "logs", "-f"], ROOT)


@task("ps", "Show stack status")
def ps(ctx: Context) -> int:
    return ctx.run(["docker", "compose", "ps"], ROOT)


@task("infra", "Start only backing services (db, redis, minio) for local app dev")
def infra(ctx: Context) -> int:
    return ctx.run(["docker", "compose", "up", "-d", "db", "redis", "minio"], ROOT)


# ---- API (Linux target: X3) ----
@task("api-install", "Install API deps into a venv (Linux / WSL2)", linux_only=True)
def api_install(ctx: Context) -> int:
    return _all(
        ctx.run([sys.executable, "-m", "venv", ".venv"], API_DIR),
        ctx.run([venv_exe(API_DIR, "python"), "-m", "pip", "install", "-U", "pip"], API_DIR),
        ctx.run([venv_exe(API_DIR, "python"), "-m", "pip", "install", "-e", ".[dev]"], API_DIR),
    )


@task("api-migrate", "Apply DB migrations (Linux / WSL2)", linux_only=True)
def api_migrate(ctx: Context) -> int:
    return ctx.run([*tool(API_DIR, "alembic"), "upgrade", "head"], API_DIR)


@task("api-run", "Run the API locally (Linux / WSL2)", linux_only=True)
def api_run(ctx: Context) -> int:
    return ctx.run([*tool(API_DIR, "uvicorn"), "app.main:app", "--reload", "--port", "8000"], API_DIR)


@task("api-test", "Run API tests (Linux / WSL2)", linux_only=True)
def api_test(ctx: Context) -> int:
    return ctx.run([*tool(API_DIR, "pytest"), "-q", *ctx.extra], API_DIR)


# ---- Web ----
@task("web-install", "Install web deps")
def web_install(ctx: Context) -> int:
    return ctx.run([_which("npm"), "install"], WEB_DIR)


@task("web-dev", "Run the web app in dev mode")
def web_dev(ctx: Context) -> int:
    return ctx.run([_which("npm"), "run", "dev"], WEB_DIR)


@task("web-build", "Production build of the web app")
def web_build(ctx: Context) -> int:
    return ctx.run([_which("npm"), "run", "build"], WEB_DIR)


@task("web-check", "Typecheck the web app")
def web_check(ctx: Context) -> int:
    return ctx.run([_which("npm"), "run", "typecheck"], WEB_DIR)


# ---- Provider agent ----
@task("agent-install", "Install the provider agent")
def agent_install(ctx: Context) -> int:
    return _all(
        ctx.run([sys.executable, "-m", "venv", ".venv"], AGENT_DIR),
        ctx.run([venv_exe(AGENT_DIR, "python"), "-m", "pip", "install", "-U", "pip"], AGENT_DIR),
        ctx.run([venv_exe(AGENT_DIR, "python"), "-m", "pip", "install", "-e", ".[dev]"], AGENT_DIR),
    )


@task("agent-test", "Test the provider agent")
def agent_test(ctx: Context) -> int:
    return ctx.run([*tool(AGENT_DIR, "pytest"), "-q", *ctx.extra], AGENT_DIR)


# ---- Aggregate ----
@task("test", "Run all Python test suites (the API suite only where the control plane can run)")
def test_all(ctx: Context) -> int:
    codes = []
    if not IS_WINDOWS or ctx.wsl:
        codes.append(api_test(ctx))
    else:
        print("note: the API suite is skipped on native Windows (control plane = Linux, X3); use --wsl or Docker for it")
    codes.append(agent_test(ctx))
    return _all(*codes)


# ---- Extras that have no Makefile target (the Windows CI jobs run these) ----
@task("sdk-python-test", "Test the Python SDK (extra)")
def sdk_python_test(ctx: Context) -> int:
    return ctx.run([sys.executable, "-m", "pytest", "-q", *ctx.extra], SDK_PY_DIR)


@task("sdk-ts-test", "Test the TypeScript SDK (extra)")
def sdk_ts_test(ctx: Context) -> int:
    return ctx.run([_which("npm"), "test"], SDK_TS_DIR)


@task("kits-check", "Kit offline gate: manifests, schemas, Dockerfiles, smoke specs (extra)")
def kits_check(ctx: Context) -> int:
    return ctx.run([sys.executable, "scripts/kit_ci.py", "check", "--kits-dir", "kits"], ROOT)


@task("kits-test", "Kit unit tests (extra)")
def kits_test(ctx: Context) -> int:
    return ctx.run([sys.executable, "-m", "pytest", "-q", "kits/tests", *ctx.extra], ROOT)


@task("windows-test", "Everything the Windows CI job runs: agent, SDKs, kit gates (extra)")
def windows_test(ctx: Context) -> int:
    return _all(agent_test(ctx), sdk_python_test(ctx), sdk_ts_test(ctx), kits_check(ctx), kits_test(ctx))


# ---- CLI ----
def format_help() -> str:
    width = max(len(t.name) for t in TASKS.values())
    rows = [f"  {t.name:<{width}}  {t.help}" for t in sorted(TASKS.values(), key=lambda t: t.name)]
    return "targets:\n" + "\n".join(rows)


def run_in_wsl(name: str, ctx: Context) -> int:
    """Run ``make <name>`` in the default WSL distro, in this repository's folder (Linux target on a Windows machine)."""
    return ctx.run(["wsl.exe", "--cd", str(ROOT), "--", "make", name], ROOT)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ComputeCoin task runner (mirrors the Makefile).", epilog=format_help(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default="help")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--wsl", action="store_true", help="Windows: run Linux-only targets inside WSL2 with make")
    raw = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in raw:  # everything after "--" goes to the test runner untouched: python tasks.py agent-test -- -k heartbeat
        cut = raw.index("--")
        raw, extra = raw[:cut], raw[cut + 1:]
    args = ap.parse_args(raw)
    if args.target in ("help", "-h", "--help"):
        print(format_help())
        return 0
    chosen = TASKS.get(args.target)
    if chosen is None:
        print(f"unknown target: {args.target}\n{format_help()}", file=sys.stderr)
        return 2
    ctx = Context(dry_run=args.dry_run, wsl=args.wsl, extra=extra)
    if chosen.linux_only and IS_WINDOWS:
        if args.wsl:
            return run_in_wsl(chosen.name, ctx)
        print(
            f"'{chosen.name}' is a Linux target: the control plane (API, worker, database) is not built for native Windows (X3).\n"
            "Use WSL2 (clone the repository inside the WSL filesystem and run `make " + chosen.name + "` there), or Docker:\n"
            "  python tasks.py up        # the whole stack in containers\n"
            "or pass --wsl to run it in your default WSL distro. See docs/operations/windows-developer-setup.md.",
            file=sys.stderr,
        )
        return 2
    return chosen.fn(ctx)


if __name__ == "__main__":
    sys.exit(main())
