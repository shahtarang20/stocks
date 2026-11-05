"""Image scanning for BYOC (U8, docs/architecture/U8-byoc-design.md).

``ImageScanner`` is an injectable interface. The default is ``UnconfiguredScanner``: it never says "clean", so with
no scanner configured BYOC is refused (fail closed). ``TrivyScanner`` shells out to the Trivy CLI with an argument
list (never a shell). Trivy finds vulnerabilities and secrets; it is NOT an antivirus, so the miner check here is a
name match plus the digest denylist. Pair with runtime monitoring (U16/U19) and a second scanner for real malware.

``ClamAVScanner`` (BYOC_SCANNER=clamav, or ``trivy+clamav`` to require both) exports the image with ``docker save`` into
a private temp dir, unpacks the layers (regular files and directories only, size capped, no path escape) and runs
``clamscan`` (or ``clamdscan``) over that tree with an argument list, never a shell. The temp dir is always wiped.
LIMITS: signature based (misses new or packed miners and anything not in the signature DB; many miners are only
"PUA"/potentially-unwanted and are reported only if the DB flags them); needs a local docker daemon on the API/scan
host and an up-to-date ``freshclam`` database; unreadable or oversized images are an error (never clean); a scan is a
point in time (the image digest is pinned, but runtime behaviour is covered only by U16 runtime signals).
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.config import settings

CLEAN = "clean"
DENIED = "denied"
UNCONFIGURED = "unconfigured"
ERROR = "error"

_SAFE_REF = re.compile(r"^[a-z0-9][a-z0-9.\-]*(:[0-9]{1,5})?(/[a-z0-9][a-z0-9._\-]*)+@sha256:[0-9a-f]{64}$")
# Names of well-known miners and attack tools. A match in a package or file name is a refusal.
MINER_NAMES = (
    "xmrig", "cpuminer", "minerd", "cgminer", "bfgminer", "ethminer", "nbminer", "t-rex", "trex-miner",
    "phoenixminer", "lolminer", "gminer", "teamredminer", "nanominer", "xmr-stak", "ccminer", "kdevtmpfsi",
)


@dataclass
class ScanResult:
    status: str  # clean | denied | unconfigured | error
    scanner: str
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.status == CLEAN


class ImageScanner(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    async def scan(self, image_ref: str) -> ScanResult:  # pragma: no cover - interface
        raise NotImplementedError


class UnconfiguredScanner(ImageScanner):
    """Stub default: nothing is scanned, so nothing is cleared."""

    name = "unconfigured"

    async def scan(self, image_ref: str) -> ScanResult:
        return ScanResult(status=UNCONFIGURED, scanner=self.name)


Runner = Callable[[list[str], int], Awaitable[tuple[int, bytes, bytes]]]


async def _subprocess_runner(argv: list[str], timeout: int) -> tuple[int, bytes, bytes]:  # pragma: no cover - needs trivy
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


def evaluate_trivy_report(report: Any, *, max_critical: int) -> tuple[str, list[dict[str, Any]]]:
    """Pure policy over a Trivy JSON report: returns (status, findings). Unparseable shapes are an error."""
    if not isinstance(report, dict):
        return ERROR, [{"code": "bad_report"}]
    findings: list[dict[str, Any]] = []
    critical = 0
    for res in report.get("Results") or []:
        if not isinstance(res, dict):
            return ERROR, [{"code": "bad_report"}]
        for v in res.get("Vulnerabilities") or []:
            if isinstance(v, dict) and str(v.get("Severity", "")).upper() == "CRITICAL":
                critical += 1
        for sec in res.get("Secrets") or []:
            if isinstance(sec, dict):
                findings.append({"code": "secret_in_image", "rule": str(sec.get("RuleID", ""))[:64]})
        for pkg in res.get("Packages") or []:
            name = str(pkg.get("Name", "")).lower() if isinstance(pkg, dict) else ""
            if any(m in name for m in MINER_NAMES):
                findings.append({"code": "known_miner_package", "name": name[:64]})
        target = str(res.get("Target", "")).lower()
        if any(m in target.rsplit("/", 1)[-1] for m in MINER_NAMES):
            findings.append({"code": "known_miner_file", "name": target[-64:]})
    if critical > max_critical:
        findings.append({"code": "critical_vulnerabilities", "count": critical})
    return (DENIED if findings else CLEAN), findings[:50]


class TrivyScanner(ImageScanner):
    name = "trivy"

    def __init__(self, *, binary: str | None = None, timeout: int | None = None, max_critical: int | None = None,
                 runner: Runner | None = None) -> None:
        self._bin = binary or settings.BYOC_TRIVY_BIN
        self._timeout = int(timeout if timeout is not None else settings.BYOC_SCAN_TIMEOUT_SECONDS)
        self._max_critical = int(max_critical if max_critical is not None else settings.BYOC_SCAN_MAX_CRITICAL)
        self._runner: Runner = runner or _subprocess_runner

    async def scan(self, image_ref: str) -> ScanResult:
        if not _SAFE_REF.fullmatch(image_ref):  # also stops option injection: must start with [a-z0-9]
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "bad_image_ref"}])
        argv = [self._bin, "image", "--quiet", "--format", "json", "--scanners", "vuln,secret",
                "--list-all-pkgs", "--timeout", f"{self._timeout}s", image_ref]
        try:
            code, out, _err = await self._runner(argv, self._timeout + 30)
        except FileNotFoundError:
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "scanner_not_installed"}])
        except asyncio.TimeoutError:
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "scan_timeout"}])
        except OSError:
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "scanner_failed"}])
        if code != 0:
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "scanner_failed", "exit": code}])
        try:
            report = json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": "bad_report"}])
        status, findings = evaluate_trivy_report(report, max_critical=self._max_critical)
        return ScanResult(status=status, scanner=self.name, findings=findings)


def _safe_extract(tar_path: str, dest: str, *, budget: list[int]) -> list[str]:
    """Extract regular files and directories only; refuses path escape; ``budget[0]`` is the remaining byte allowance.

    Raises ValueError on a violation. Symlinks, hardlinks, devices and fifos are skipped (not scanned, never created).
    Returns the extracted regular-file names (relative).
    """
    root = os.path.realpath(dest)
    names: list[str] = []
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf:
            # Strip only a leading "./" and absolute slashes. (``lstrip("./")`` also ate the dots of "../", which
            # silently turned a path-escape member into an in-tree file instead of refusing it.)
            name = m.name.lstrip("/")
            while name.startswith("./"):
                name = name[2:].lstrip("/")
            if name in ("", "."):
                continue
            if name == ".." or name.startswith("../") or "/../" in name:
                raise ValueError("path_escape")
            if not name:
                continue
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("path_escape")
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not m.isreg():
                continue
            budget[0] -= m.size
            if budget[0] < 0:
                raise ValueError("image_too_large")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o600)
            names.append(name)
    return names


def _unpack_image_export(export_tar: str, rootfs: str, *, max_bytes: int) -> None:
    """``docker save`` tar -> merged layer files under ``rootfs`` (later layers overwrite earlier ones)."""
    staging = rootfs + ".export"
    os.makedirs(staging, mode=0o700)
    budget = [max_bytes]
    _safe_extract(export_tar, staging, budget=budget)
    with open(os.path.join(staging, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    layers: list[str] = []
    for entry in manifest if isinstance(manifest, list) else []:
        if isinstance(entry, dict):
            layers += [x for x in entry.get("Layers") or [] if isinstance(x, str)]
    if not layers:
        raise ValueError("no_layers")
    os.makedirs(rootfs, mode=0o700, exist_ok=True)
    for layer in layers:
        lp = os.path.realpath(os.path.join(staging, layer))
        if not lp.startswith(os.path.realpath(staging) + os.sep) or not os.path.isfile(lp):
            raise ValueError("bad_layer_path")
        _safe_extract(lp, rootfs, budget=budget)
        os.unlink(lp)  # keep the temp footprint near 1x


def parse_clamscan_output(out: str) -> list[dict[str, Any]]:
    """``path: Signature FOUND`` lines -> findings (paths are shortened to the in-image path)."""
    findings: list[dict[str, Any]] = []
    for line in out.splitlines():
        if line.endswith(" FOUND") and ": " in line:
            path, _, sig = line[:-6].rpartition(": ")
            findings.append({"code": "malware_signature", "signature": sig[:96],
                             "file": path.split("/rootfs/", 1)[-1][-120:]})
    return findings[:50]


class ClamAVScanner(ImageScanner):
    name = "clamav"

    def __init__(self, *, binary: str | None = None, docker_bin: str | None = None, timeout: int | None = None,
                 max_bytes: int | None = None, tmp_dir: str | None = None, runner: Runner | None = None) -> None:
        self._bin = binary or settings.BYOC_CLAMAV_BIN
        self._docker = docker_bin or settings.BYOC_DOCKER_BIN
        self._timeout = int(timeout if timeout is not None else settings.BYOC_CLAMAV_TIMEOUT_SECONDS)
        self._max_bytes = int(max_bytes if max_bytes is not None else settings.BYOC_CLAMAV_MAX_IMAGE_BYTES)
        self._tmp = tmp_dir if tmp_dir is not None else (settings.BYOC_CLAMAV_TMP_DIR or None)
        self._runner: Runner = runner or _subprocess_runner

    def _err(self, code: str, **extra: Any) -> ScanResult:
        return ScanResult(status=ERROR, scanner=self.name, findings=[{"code": code, **extra}])

    async def scan(self, image_ref: str) -> ScanResult:
        if not _SAFE_REF.fullmatch(image_ref):
            return self._err("bad_image_ref")
        workdir = tempfile.mkdtemp(prefix="cc-clamav-", dir=self._tmp)
        try:
            os.chmod(workdir, 0o700)
            export = os.path.join(workdir, "image.tar")
            rootfs = os.path.join(workdir, "rootfs")
            try:
                code, _o, _e = await self._runner([self._docker, "save", "-o", export, image_ref], self._timeout)
                if code != 0:  # not present locally: pull by digest, then save once more
                    pc, _o, _e = await self._runner([self._docker, "pull", "--quiet", image_ref], self._timeout)
                    if pc == 0:
                        code, _o, _e = await self._runner([self._docker, "save", "-o", export, image_ref], self._timeout)
                if code != 0:
                    return self._err("image_export_failed", exit=code)
                if os.path.getsize(export) > self._max_bytes:
                    return self._err("image_too_large")
                try:
                    await asyncio.to_thread(_unpack_image_export, export, rootfs, max_bytes=self._max_bytes)
                except (ValueError, OSError, tarfile.TarError, KeyError) as exc:
                    return self._err("image_unpack_failed", reason=str(exc)[:48])
                finally:
                    try:
                        os.unlink(export)
                    except OSError:
                        pass
                argv = [self._bin, "-r", "-i", "--no-summary", "--", rootfs]
                if os.path.basename(self._bin) == "clamscan":
                    argv = [self._bin, "-r", "-i", "--no-summary", "--max-filesize=512M", "--max-scansize=2048M", "--", rootfs]
                code, out, _err = await self._runner(argv, self._timeout)
            except FileNotFoundError:
                return self._err("scanner_not_installed")
            except asyncio.TimeoutError:
                return self._err("scan_timeout")
            except OSError:
                return self._err("scanner_failed")
            if code == 0:
                return ScanResult(status=CLEAN, scanner=self.name)
            if code == 1:
                found = parse_clamscan_output(out.decode("utf-8", "replace"))
                return ScanResult(status=DENIED, scanner=self.name,
                                  findings=found or [{"code": "malware_signature", "signature": "unparsed"}])
            return self._err("scanner_failed", exit=code)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class CombinedScanner(ImageScanner):
    """All member scanners must say clean. Any denied wins over errors; any error/unconfigured is never clean."""

    def __init__(self, scanners: list[ImageScanner]) -> None:
        self._scanners = scanners
        self.name = "+".join(s.name for s in scanners)

    async def scan(self, image_ref: str) -> ScanResult:
        results = [await s.scan(image_ref) for s in self._scanners]
        findings = [f for r in results for f in r.findings][:50]
        for status in (DENIED, ERROR, UNCONFIGURED):
            if any(r.status == status for r in results):
                return ScanResult(status=status, scanner=self.name, findings=findings)
        return ScanResult(status=CLEAN, scanner=self.name, findings=findings)


_override: ImageScanner | None = None


def set_scanner(scanner: ImageScanner | None) -> None:
    """Inject a scanner (tests, or a custom integration); None restores the settings-based choice."""
    global _override
    _override = scanner


def get_scanner() -> ImageScanner:
    if _override is not None:
        return _override
    choice = (settings.BYOC_SCANNER or "").strip().lower()
    if choice == "trivy":
        return TrivyScanner()
    if choice == "clamav":
        return ClamAVScanner()
    if choice in ("trivy+clamav", "clamav+trivy"):
        return CombinedScanner([TrivyScanner(), ClamAVScanner()])
    return UnconfiguredScanner()
