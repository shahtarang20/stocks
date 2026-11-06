"""Redacted support bundle (cross-platform plan W10, section 6; L7: no secrets anywhere).

``computecoin-agent support-bundle`` writes ONE zip a host can attach to a support request. It contains diagnostics only:

* versions (agent, Python, a short allow-list of dependencies, OS),
* the platform object and capability flags the agent would advertise,
* the hardware report the agent would send,
* the agent config with every secret replaced,
* the requirement-checker result and runtime health, when those modules exist in this build,
* the tail of the agent / supervisor logs with secrets scrubbed,
* a manifest with the SHA-256 of every file and a count (never the values) of what was redacted.

It never contains credentials, enrolment or machine tokens, signing secrets, private keys, cookies, presigned URLs, e-mail
addresses, the Windows user name in a path, or the process environment.

Redaction is layered and FAILS CLOSED (L1):

1. every secret the process can know (secret-looking config fields and environment variables) is replaced literally wherever
   it appears;
2. key/value, header, token-format, URL, PEM and blob patterns are replaced everywhere in every file;
3. the finished text of every file is checked again: the scan must change nothing, no known secret may remain, and no PEM
   header may remain. If any check fails the bundle is NOT written and :class:`BundleLeakError` is raised.

Logs are bounded (lines and bytes), decoded as UTF-8 or as the UTF-16 Windows PowerShell and some Windows tools write,
and never read from a path the caller did not name or the platform's own log directory.

Nothing here contacts the network and nothing is executed: the collectors only call in-process functions.
"""

from __future__ import annotations

import dataclasses
import getpass
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform as _stdlib_platform
import re
import shutil
import socket
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import __version__

BUNDLE_FORMAT = 1
REDACTED = "[REDACTED]"
DEFAULT_MAX_LOG_LINES = 2000
DEFAULT_MAX_LOG_BYTES = 512 * 1024
MAX_LOG_FILES = 8
MAX_LINE_CHARS = 2000

_SECRET_WORDS = (
    "secret|token|password|passwd|pwd|passphrase|api[_-]?key|apikey|private[_-]?key|privkey|credential|signing|mnemonic|"
    "seed|cookie|session[_-]?id|csrf|auth|bearer|dpapi|blob"
)
_SECRET_KEY_RE = re.compile(rf"(?i)(?:{_SECRET_WORDS})")
_SAFE_DEPENDENCIES = ("httpx", "pydantic", "pydantic-settings", "typer", "click", "tomli-w", "websockets", "cryptography",
                      "psutil", "nvidia-ml-py", "amdsmi")


class BundleLeakError(RuntimeError):
    """The finished bundle still looked like it contained a secret; nothing was written."""


# --------------------------------------------------------------------------- redaction
@dataclasses.dataclass
class _Rule:
    name: str
    pattern: "re.Pattern[str]"
    repl: Any  # str or callable


# A marker this module wrote itself. A rule that meets one leaves it alone, which is what makes a second scrub of
# finished text change nothing (the fail-closed check in ``Redactor.verify_clean``).
_MARKER = r"\[(?:REDACTED|NOT\ SET)[^\]\r\n]*\]"
_MARKER_RE = re.compile(_MARKER + r"|<id>|<user>")


def _kv_repl(m: "re.Match[str]") -> str:
    value = m.group(2).strip("\"'")
    if re.fullmatch(_MARKER, value):
        return m.group(0)
    return f'{m.group(1)}"{REDACTED}"'


def _mk_rules() -> list[_Rule]:
    flags = re.DOTALL
    return [
        _Rule("pem_block", re.compile(r"-----BEGIN [A-Z0-9 ]*-----.*?-----END [A-Z0-9 ]*-----", flags), "[REDACTED PEM BLOCK]"),
        _Rule("pem_header", re.compile(r"-----(?:BEGIN|END) [A-Z0-9 ]*-----"), "[REDACTED PEM HEADER]"),
        _Rule("auth_header", re.compile(
            r"(?i)\b((?:proxy-)?authorization)\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?(?:" + _MARKER + r"|[^\s\"',;}\]]+)"),
              lambda m: f"{m.group(1)}: {REDACTED}"),
        _Rule("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
        _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED JWT]"),
        _Rule("secret_key_value", re.compile(
            rf"""(?ix)(["']?[\w.\-$:]*(?:{_SECRET_WORDS})[\w.\-]*["']?\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|{_MARKER}|[^\s,;}}\]\)"']+)"""), _kv_repl),
        _Rule("url_credentials", re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
        _Rule("url_secret_query", re.compile(
            r"(?i)([?&](?:token|access_token|refresh_token|id_token|api[_-]?key|apikey|key|secret|signature|sig|password|"
            r"x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature)=)[^&\s\"'<>]+"), r"\1" + REDACTED),
        _Rule("token_formats", re.compile(
            r"\b(?:(?:enr|msk|sk|pk|rk)_[A-Za-z0-9_\-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"xox[abprs]-[A-Za-z0-9-]{10,}|A(?:KIA|SIA)[0-9A-Z]{16})\b"), "[REDACTED TOKEN]"),
        _Rule("hex_0x_key", re.compile(r"\b0x[0-9a-fA-F]{64}\b"), "[REDACTED 0x-KEY]"),
        _Rule("hex64", re.compile(r"(?<!sha256:)(?<!sha256=)(?<![0-9A-Za-z])[0-9a-fA-F]{64}(?![0-9A-Za-z])"), "[REDACTED HEX64]"),
        _Rule("blob", re.compile(
            r"(?<![\w+/=.\-])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{40,}={0,2}(?![\w+/=\-])"),
              "[REDACTED BLOB]"),
        _Rule("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED EMAIL]"),
        _Rule("windows_profile", re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"'<>|:*?]+"), r"\1<user>"),
        _Rule("home_dir", re.compile(r"(/(?:home|Users)/)[^/\s\"'<>|:*?]+"), r"\1<user>"),
        _Rule("public_ipv4", re.compile(
            r"\b(?!(?:10|127)\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|0\.|169\.254\.)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
              "[REDACTED IP]"),
    ]


_RULES = _mk_rules()


IDENTITY_MARKER = "<id>"
# Words that are common enough in logs ("rootfs", "user", "system") that replacing them would only damage the diagnosis.
_COMMON_IDENTITY_WORDS = frozenset({"root", "user", "admin", "administrator", "system", "default", "localhost", "guest",
                                    "ubuntu", "debian", "computecoin", "runner", "service", "agent", "windows", "linux"})


def _outside_markers(text: str, fn: Callable[[str], str]) -> str:
    """Apply ``fn`` to the parts of ``text`` that are not one of our own markers."""
    out: list[str] = []
    pos = 0
    for m in _MARKER_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


class Redactor:
    """Scrubs text and JSON-like data. ``known_secrets`` and ``identity_words`` are replaced literally, everywhere."""

    def __init__(self, known_secrets: Iterable[str] = (), identity_words: Iterable[str] = ()) -> None:
        self.known_secrets = sorted({s for s in known_secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)
        self.identity_words = sorted(
            {w for w in identity_words if isinstance(w, str) and len(w) >= 3 and w.lower() not in _COMMON_IDENTITY_WORDS},
            key=len, reverse=True)
        self.counts: Counter[str] = Counter()

    def text(self, text: str) -> str:
        for secret in self.known_secrets:
            if secret in text:
                self.counts["known_secret"] += text.count(secret)
                text = text.replace(secret, REDACTED)
        for rule in _RULES:
            text, n = rule.pattern.subn(rule.repl, text)
            if n:
                self.counts[rule.name] += n
        for word in self.identity_words:
            pattern = re.compile(re.escape(word), re.IGNORECASE)

            def _swap(part: str, pattern: "re.Pattern[str]" = pattern) -> str:
                part, n = pattern.subn(IDENTITY_MARKER, part)
                if n:
                    self.counts["identity"] += n
                return part

            text = _outside_markers(text, _swap)
        return text

    def data(self, value: Any) -> Any:
        """A deep copy where values under secret-looking keys are replaced and every string is scrubbed."""
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = self.text(str(k))
                if _SECRET_KEY_RE.search(str(k)):
                    if v is None or v == "" or v == [] or v == {}:
                        out[key] = "[NOT SET]"
                    else:
                        out[key] = REDACTED
                        self.counts["secret_key"] += 1
                else:
                    out[key] = self.data(v)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.data(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return self.text(str(value))

    def _probe(self) -> "Redactor":
        return Redactor(self.known_secrets, self.identity_words)

    def verify_clean(self, text: str, what: str) -> None:
        """Fail closed: a finished text must not change under a second scrub and must hold no known secret."""
        for secret in self.known_secrets:
            if secret in text:
                raise BundleLeakError(f"{what}: a known secret is still present")
        if "-----BEGIN" in text:
            raise BundleLeakError(f"{what}: a PEM header is still present")
        probe = self._probe()
        if probe.text(text) != text:
            raise BundleLeakError(f"{what}: a second scrub still matched ({', '.join(sorted(probe.counts))})")

    def verify_clean_data(self, value: Any, what: str) -> None:
        """The same check for a JSON-like structure, string by string (JSON escaping would otherwise make a clean text look dirty)."""
        if isinstance(value, Mapping):
            for k, v in value.items():
                self.verify_clean(str(k), what)
                self.verify_clean_data(v, what)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.verify_clean_data(v, what)
        elif isinstance(value, str):
            self.verify_clean(value, what)


def known_secrets_from(config: Any, environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Values of secret-looking config fields and environment variables (never written anywhere, only used to scrub)."""
    found: list[str] = []
    if config is not None:
        try:
            dumped = config.model_dump()
        except Exception:
            dumped = {}
        for key, value in dumped.items():
            if _SECRET_KEY_RE.search(str(key)):
                found += [v for v in _flatten_strings(value)]
    env = os.environ if environ is None else environ
    for key, value in env.items():
        if _SECRET_KEY_RE.search(key) and isinstance(value, str):
            found.append(value)
    return [s for s in found if len(s) >= 6]


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _flatten_strings(v)]
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _flatten_strings(v)]
    return []


def identity_words(environ: Optional[Mapping[str, str]] = None) -> list[str]:
    env = os.environ if environ is None else environ
    words = [env.get("USERNAME", ""), env.get("USER", ""), env.get("COMPUTERNAME", "")]
    for fn in (getpass.getuser, socket.gethostname):
        try:
            words.append(fn())
        except Exception:
            pass
    return [w for w in words if w and len(w) >= 3]


# --------------------------------------------------------------------------- collectors
def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe(fn: Callable[[], Any]) -> Any:
    """Run a collector. A failure is reported by exception CLASS only: messages (pydantic errors in particular) can echo values."""
    try:
        return _jsonable(fn())
    except Exception as exc:  # noqa: BLE001 - a diagnostics tool reports, never crashes
        return {"error": type(exc).__name__}


def collect_versions() -> dict[str, Any]:
    deps: dict[str, Optional[str]] = {}
    for name in _SAFE_DEPENDENCIES:
        try:
            deps[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            deps[name] = None
    return {
        "agent_version": __version__,
        "python": sys.version.split()[0],
        "python_implementation": _stdlib_platform.python_implementation(),
        "os": _stdlib_platform.system(),
        "os_release": _stdlib_platform.release(),
        "os_version": _stdlib_platform.version(),
        "machine": _stdlib_platform.machine(),
        "dependencies": deps,
    }


def collect_platform() -> dict[str, Any]:
    from .platform import get_platform

    return {"platform": get_platform().info(agent_version=__version__)}


def collect_host_resources(path: Optional[Path] = None) -> dict[str, Any]:
    target = Path(path) if path else Path.home()
    out: dict[str, Any] = {"cpu_count": os.cpu_count()}
    try:
        usage = shutil.disk_usage(target)
        out["disk_free_gb"] = round(usage.free / 1024**3, 1)
        out["disk_total_gb"] = round(usage.total / 1024**3, 1)
    except OSError:
        out["disk_free_gb"] = None
    try:
        import psutil

        vm = psutil.virtual_memory()
        out["ram_total_mb"] = int(vm.total / 1024**2)
        out["ram_available_mb"] = int(vm.available / 1024**2)
    except Exception:  # noqa: BLE001
        out["ram_total_mb"] = None
    return out


def _call_optional(candidates: Sequence[tuple[str, str]]) -> Any:
    """Call the first ``module.function`` that exists and takes no arguments; otherwise report that it is not in this build."""
    for module_name, func_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        func = getattr(module, func_name, None)
        if callable(func):
            return {"source": f"{module_name}.{func_name}", "result": func()}
    return {"available": False, "reason": "no checker is part of this build"}


def collect_requirements() -> Any:
    """The Windows requirements-checker result (``windows/runtime/requirements_check.py``; read-only probes). Not applicable elsewhere."""
    from .platform import PLATFORM_WINDOWS, get_platform

    if get_platform().name != PLATFORM_WINDOWS:
        return {"available": False, "reason": "the requirements checker applies to Windows hosts"}
    from .windows.runtime.requirements_check import check_requirements, default_probes

    return check_requirements(default_probes()).to_dict()


# Runtime health needs the provisioner's settings and a signed manifest, which the support command does not have; until the
# supervisor exposes a read-only summary (names below are candidates, resolved at run time) it is reported as unavailable.
# Pass ``extra_sections={"runtime_health": fn}`` from a caller that has them.
RUNTIME_HEALTH_CHECKERS: tuple[tuple[str, str], ...] = (
    ("computecoin_agent.windows.runtime.status", "health_summary"),
    ("computecoin_agent.windows.runtime.health", "summary"),
)


# --------------------------------------------------------------------------- logs
def read_log_tail(path: Path, *, max_lines: int = DEFAULT_MAX_LOG_LINES, max_bytes: int = DEFAULT_MAX_LOG_BYTES) -> str:
    """The last ``max_lines`` lines of at most the last ``max_bytes`` bytes of ``path`` (binary read, bounded, never the whole file).

    UTF-8 (with or without a BOM) and the UTF-16 that Windows PowerShell 5.1 and some Windows tools write are both read; bytes
    that do not decode become U+FFFD, never an error."""
    with open(path, "rb") as fh:
        head = fh.read(2)
        utf16 = {b"\xff\xfe": "utf-16-le", b"\xfe\xff": "utf-16-be"}.get(head)
        fh.seek(0, io.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        if utf16 and start % 2:
            start += 1  # stay on a 2-byte boundary
        fh.seek(start)
        raw = fh.read(max_bytes)
    text = raw.decode(utf16, errors="replace") if utf16 else raw.decode("utf-8-sig", errors="replace")
    text = text.lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file ended with a newline
    if start > 0 and lines:
        lines = lines[1:]  # the first line is probably cut in half
    lines = [ln if len(ln) <= MAX_LINE_CHARS else ln[:MAX_LINE_CHARS] + " [line cut]" for ln in lines[-max_lines:]]
    return "\n".join(lines)


def find_log_files(log_dir: Optional[Path], explicit: Sequence[Path] = (), *, limit: int = MAX_LOG_FILES) -> list[Path]:
    files: list[Path] = [Path(p) for p in explicit if Path(p).is_file()]
    if log_dir and Path(log_dir).is_dir():
        found = [p for p in Path(log_dir).iterdir() if p.is_file() and (p.suffix.lower() in (".log", ".txt") or ".log." in p.name.lower())]
        files += sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in files:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique[:limit]


def _log_entry_name(index: int, path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:60] or "log"
    return f"logs/{index:02d}-{stem}.redacted.txt"


# --------------------------------------------------------------------------- the bundle
README = """ComputeCoin support bundle (format {fmt})

This file holds diagnostics only: versions, the platform and capability report, hardware as the agent sees it, the agent
settings with every secret replaced, requirement and runtime-health results, and the recent end of the agent logs with
secrets scrubbed. It does NOT contain your credentials, tokens, keys, cookies, e-mail address, your user name in a path or
your environment variables. {redactions} replacement(s) were made; manifest.json lists how many per rule, never the values.

Read it before you send it. If you see anything that should not be here, do not send it and tell us.
"""


def build_bundle(
    out_path: Path,
    *,
    config: Any = None,
    log_files: Sequence[Path] = (),
    log_dir: Optional[Path] = None,
    max_log_lines: int = DEFAULT_MAX_LOG_LINES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    include_hardware: bool = True,
    environ: Optional[Mapping[str, str]] = None,
    extra_sections: Optional[Mapping[str, Callable[[], Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Write the bundle to ``out_path`` (refuses to overwrite) and return its manifest. Raises :class:`BundleLeakError` and writes
    nothing when the finished text of any file does not pass the leak checks."""
    out_path = Path(out_path)
    if out_path.exists():
        raise FileExistsError(f"refusing to overwrite {out_path}")
    now = now or datetime.now(timezone.utc)
    red = Redactor(known_secrets_from(config, environ), identity_words(environ))

    texts: dict[str, str] = {}
    structured: dict[str, Any] = {}

    def add_json(name: str, value: Any) -> None:
        clean = red.data(_jsonable(value))
        structured[name] = clean
        texts[name] = json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=True) + "\n"

    add_json("versions.json", _safe(collect_versions))
    add_json("platform.json", _safe(collect_platform))
    add_json("host_resources.json", _safe(collect_host_resources))
    if config is not None:
        add_json("config.redacted.json", _safe(lambda: config.model_dump(mode="json")))
    else:
        add_json("config.redacted.json", {"available": False})
    if include_hardware:
        def _hardware() -> Any:
            from .hardware import build_capability_report

            vendor = getattr(config, "gpu_vendor", None) if config is not None else None
            return build_capability_report(config_gpu_vendor=vendor)

        report_holder: dict[str, Any] = {}

        def _hardware_and_keep() -> Any:
            report_holder["report"] = _hardware()
            return report_holder["report"]

        add_json("hardware.json", _safe(_hardware_and_keep))

        def _capabilities() -> Any:
            from .capabilities import compute_capabilities

            return {"capabilities": compute_capabilities(report_holder["report"], config=config)}

        add_json("capabilities.json", _safe(_capabilities) if "report" in report_holder else {"available": False})
    add_json("requirements.json", _safe(collect_requirements))
    add_json("runtime_health.json", _safe(lambda: _call_optional(RUNTIME_HEALTH_CHECKERS)))
    for name, fn in (extra_sections or {}).items():
        add_json(f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json", _safe(fn))

    if log_dir is None and not log_files:
        try:
            from .platform import get_platform

            log_dir = get_platform().paths.data_dir() / "logs"
        except Exception:  # noqa: BLE001 - no known log directory on this platform
            log_dir = None
    logs_included: list[str] = []
    for i, path in enumerate(find_log_files(log_dir, log_files), start=1):
        try:
            tail = read_log_tail(path, max_lines=max_log_lines, max_bytes=max_log_bytes)
        except OSError:
            continue
        name = _log_entry_name(i, path)
        texts[name] = red.text(tail) + "\n"
        logs_included.append(name)

    for name, text in texts.items():
        if name in structured:
            red.verify_clean_data(structured[name], name)  # strings, not JSON text: escaping would make clean text look dirty
            for secret in red.known_secrets:
                if secret in text:
                    raise BundleLeakError(f"{name}: a known secret is still present")
            if "-----BEGIN" in text:
                raise BundleLeakError(f"{name}: a PEM header is still present")
        else:
            red.verify_clean(text, name)

    files = {name: text.encode("utf-8") for name, text in texts.items()}
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_version": __version__,
        "redactions": dict(sorted(red.counts.items())),
        "redaction_total": int(sum(red.counts.values())),
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in sorted(files.items())},
        "logs": logs_included,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    readme = README.format(fmt=BUNDLE_FORMAT, redactions=manifest["redaction_total"]).encode("utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".bundle-", suffix=".tmp", dir=str(out_path.parent))
    os.close(fd)  # Windows: an open handle would block the rename below
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", readme)
            zf.writestr("manifest.json", manifest_bytes)
            for name, data in sorted(files.items()):
                zf.writestr(name, data)
        os.replace(tmp_name, out_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        from .platform import get_platform

        get_platform().paths.restrict_file(out_path)  # best effort: owner-only, like the other files this agent writes
    except Exception:  # noqa: BLE001
        pass
    return manifest


def default_bundle_name(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"computecoin-support-{stamp}.zip"
