"""X8 lint (cross-platform plan W0.2): OS-specific calls live ONLY in ``computecoin_agent/platform/``.

Outside that package the agent must not
  * call ``os.getuid`` / ``os.geteuid`` / ``os.getgid`` / ``os.mkfifo`` / ``os.chmod`` (and friends), ``signal.signal`` or
    ``loop.add_signal_handler``,
  * use a string path under ``/proc/``, ``/sys/``, ``/dev/`` or ``/run/user``,
  * import POSIX-only (fcntl, termios, pty, grp, pwd, resource) or Windows-only (msvcrt, winreg) modules at module level.

Docstrings and comments are ignored; a bare directory name such as ``"/proc"`` (a deny-list entry for in-container paths)
is not a host path and is allowed.

The scanner itself is tested on snippets below, so a regression in the lint fails a test (bug re-insertion without
touching real files).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent / "computecoin_agent"
PAL_DIR = AGENT_DIR / "platform"
# The Windows supervisor package (W2..W7) names GUEST paths inside the Linux runtime distro (/proc/mounts, /dev/dxg ...)
# as strings it hands to ``wsl.exe``; those are not host paths, so only the host-path literal rule is waived there.
# Every other rule (os.chmod, os.getuid, signal.signal ...) still applies to it.
GUEST_PATH_DIRS = (AGENT_DIR / "windows",)

FORBIDDEN_OS_CALLS = {
    "getuid", "geteuid", "getgid", "getegid", "mkfifo", "chmod", "fchmod", "lchmod", "chown", "fchown", "lchown",
    "umask", "killpg", "getpgid", "setsid", "fork", "getlogin",
}
FORBIDDEN_SIGNAL_CALLS = {"signal", "setitimer", "alarm", "pthread_sigmask", "set_wakeup_fd"}
FORBIDDEN_ATTR_CALLS = {"add_signal_handler", "remove_signal_handler"}
FORBIDDEN_PATH_RE = re.compile(r"^/(proc|sys|dev)/|^/run/user")
FORBIDDEN_MODULE_LEVEL_IMPORTS = {"fcntl", "termios", "pty", "grp", "pwd", "resource", "msvcrt", "winreg"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _function_scoped_nodes(tree: ast.AST) -> set[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.walk(node):
                inside.add(id(child))
    return inside


def find_violations(source: str, filename: str = "<memory>", *, check_paths: bool = True) -> list[str]:
    """Every X8 violation in ``source`` as ``file:line: message``. ``check_paths=False`` skips the host-path literal rule."""
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_nodes(tree)
    in_function = _function_scoped_nodes(tree)
    out: list[str] = []

    def add(node: ast.AST, msg: str) -> None:
        out.append(f"{filename}:{getattr(node, 'lineno', 0)}: {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                base = fn.value.id if isinstance(fn.value, ast.Name) else None
                if base == "os" and fn.attr in FORBIDDEN_OS_CALLS:
                    add(node, f"os.{fn.attr}() outside the platform layer")
                elif base == "signal" and fn.attr in FORBIDDEN_SIGNAL_CALLS:
                    add(node, f"signal.{fn.attr}() outside the platform layer")
                elif fn.attr in FORBIDDEN_ATTR_CALLS:
                    add(node, f".{fn.attr}() outside the platform layer")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {a.name for a in node.names}
            if mod == "os" and names & FORBIDDEN_OS_CALLS:
                add(node, f"from os import {sorted(names & FORBIDDEN_OS_CALLS)} outside the platform layer")
            if mod == "signal" and names & FORBIDDEN_SIGNAL_CALLS:
                add(node, f"from signal import {sorted(names & FORBIDDEN_SIGNAL_CALLS)} outside the platform layer")
            if id(node) not in in_function and mod.split(".")[0] in FORBIDDEN_MODULE_LEVEL_IMPORTS:
                add(node, f"module-level import of OS-specific module {mod}")
        elif isinstance(node, ast.Import):
            if id(node) not in in_function:
                for a in node.names:
                    if a.name.split(".")[0] in FORBIDDEN_MODULE_LEVEL_IMPORTS:
                        add(node, f"module-level import of OS-specific module {a.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if check_paths and id(node) not in docstrings and FORBIDDEN_PATH_RE.search(node.value):
                add(node, f"host path literal {node.value[:40]!r} outside the platform layer")
    return out


def _agent_sources() -> list[Path]:
    return sorted(p for p in AGENT_DIR.rglob("*.py") if PAL_DIR not in p.parents and "__pycache__" not in p.parts)


def test_pal_package_exists_with_all_implementations():
    for name in ("__init__.py", "linux.py", "windows.py", "fake.py"):
        assert (PAL_DIR / name).is_file(), name


def test_agent_code_outside_the_pal_has_no_os_specific_calls():
    violations: list[str] = []
    for path in _agent_sources():
        guest = any(d in path.parents for d in GUEST_PATH_DIRS)
        violations += find_violations(path.read_text(encoding="utf-8"), str(path.relative_to(AGENT_DIR.parent)), check_paths=not guest)
    assert not violations, "X8: move these behind computecoin_agent/platform/:\n" + "\n".join(violations)


# ----- the scanner catches what it must (re-insertion of each old bug) ------------------------------------------------
@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nos.getuid()\n",
        "import os\nx = os.geteuid()\n",
        "import os\nos.mkfifo('p', 0o622)\n",
        "import os\nos.chmod('p', 0o600)\n",
        "import os\nos.fchmod(3, 0o600)\n",
        "import signal\nsignal.signal(signal.SIGTERM, h)\n",
        "async def f(loop):\n    loop.add_signal_handler(15, h)\n",
        "x = open('/proc/meminfo')\n",
        "p = '/sys/class/dmi/id/product_name'\n",
        "p = '/dev/shm'.join([]) if False else '/dev/shm/x'\n",
        "p = f'/run/user/{n}'\n",
        "import fcntl\n",
        "import termios\n",
        "from pty import openpty\n",
        "from os import getuid\n",
        "from os import chmod as c\n",
        "from signal import signal\n",
        "import msvcrt\n",
    ],
)
def test_scanner_flags_forbidden_constructs(snippet):
    assert find_violations(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        '"""Reads /proc/cpuinfo and calls os.getuid()."""\nx = 1\n',
        "def f():\n    '''docstring mentions /dev/shm/x'''\n",
        "# os.chmod(p, 0o600) in a comment\nx = 1\n",
        "_DENY = ('/proc', '/sys', '/dev', '/etc')\n",  # bare directory names: in-container deny list
        "msg = 'AMD GPU job requires /dev/kfd'\n",
        "def f():\n    import fcntl\n    return fcntl\n",  # lazy, function-scoped import is allowed
        "import os\nos.path.exists('x')\nos.makedirs('d')\n",
        "import signal\nsignal.SIGTERM\n",
    ],
)
def test_scanner_allows_benign_constructs(snippet):
    assert find_violations(snippet) == [], snippet


def test_guest_path_waiver_only_skips_the_path_rule():
    assert find_violations("p = '/proc/mounts'\n", check_paths=False) == []
    assert find_violations("import os\nos.chmod('p', 1)\n", check_paths=False)


def test_scanner_reports_file_and_line():
    out = find_violations("x = 1\nimport os\nos.getuid()\n", "agent/x.py")
    assert out and out[0].startswith("agent/x.py:3:")
