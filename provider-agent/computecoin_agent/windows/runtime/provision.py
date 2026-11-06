"""Managed WSL2 runtime provisioning (W3): the dedicated distro ``computecoin`` that runs the unchanged Linux runner.

What it does, in order (every step is idempotent; see ``RuntimeProvisioner.provision``):

  preflight        WSL present and new enough (``wsl --version``)
  import_distro    download the rootfs named by the SIGNED manifest, verify sha256 (mismatch ABORTS before import),
                   ``wsl --import computecoin <dir> <tar> --version 2``
  runner_user      non-root, locked-password, no-login-shell user ``ccrunner``
  wsl_conf         /etc/wsl.conf: automount off, interop off, Windows PATH off, systemd on (X6); restart the distro only
  limits           systemd slice (memory / CPU) used as Docker cgroup-parent + virtual disk cap; optional .wslconfig merge
  disable_ssh      ssh units disabled and masked
  docker_engine    Docker Engine at the manifest's pinned versions (apt, repo key fingerprint pinned, packages held)
  nvidia_toolkit   NVIDIA container toolkit at the manifest's pinned versions
  docker_config    daemon.json, ``nvidia-ctk runtime configure``, restart docker
  runner_install   venv, hashed dependency lock, runner wheel (sha256 checked on the host AND in the distro)
  egress_proxy     pull the digest-pinned egress proxy image, record it in /etc/computecoin/runtime.env (W8)
  smoke_image      pull the digest-pinned CUDA smoke-test image
  --- verification (never cached) ---
  health           ``health.run_health``
  gpu smoke test   a tiny CUDA container running nvidia-smi as the runner user; ENROLMENT IS BLOCKED UNLESS IT PASSES
  egress verify    W8 spec E1..E4 (failure only withholds the ``egress_proxy`` capability)

CREDENTIALS. This module never reads, writes, copies or deletes credentials. Machine credentials live in the supervisor's
OS secret store (DPAPI / Credential Manager, X7), outside the distro. ``repair`` and ``reset`` therefore cannot lose them;
nothing here refers to a credential path at all.

IDEMPOTENCY. After each completed step a fingerprint (sha256 of the step name, its manifest inputs and the fingerprints of
the steps it depends on) is stored in ``/etc/computecoin/provision-state.json`` inside the distro. A step whose fingerprint
is unchanged is skipped; ``force`` (used by ``repair``) re-runs everything. A new manifest changes the relevant fingerprints,
so only the affected steps (and their dependants) run. The state file is a convenience, never a proof: ``health`` and the
smoke test always look at the live distro.

PER-USER WSL (TODO(verify-docs)): WSL distros are registered per Windows user. The user that runs this module (the
supervisor's service account) owns the distro and every later ``wsl.exe`` call must come from the SAME account. The distro
runs with that account's Windows permissions, so the account must be a dedicated, low-privilege one (see
docs/operations/windows-runtime-provisioning.md).

No command here uses a shell: see ``wsl.py``. Nothing in this module has been run on Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Optional

from . import distro_paths as P
from .config_files import (
    ResourceLimits, desired_wslconfig, disk_size_arg, hardening_problems, merge_wslconfig, remove_managed,
    render_daemon_json, render_slice, render_wsl_conf,
)
from .health import Check, HealthReport, run_gpu_smoke, run_health, state_json
from .manifest import AptRepo, ManifestError, RuntimeManifest, load_verified_manifest
from .network import pull_egress_proxy, verify_egress_proxy
from .wsl import Runner, WslCli, WslError, argv_import, argv_resize, argv_set_sparse, argv_unregister, subprocess_runner, version_at_least

STATE_SCHEMA = 1
MAX_ROOTFS_BYTES = 6 * 1024**3
MAX_WHEEL_BYTES = 512 * 1024**2
MAX_LOCK_BYTES = 8 * 1024**2


def host_join(base: str, name: str) -> str:
    """Join a HOST path (X9: pathlib, never string concatenation). A Windows-style base keeps Windows separators even when
    the code is exercised on Linux in tests."""
    if "\\" in base or (len(base) > 1 and base[1] == ":"):
        return str(PureWindowsPath(base) / name)
    return str(Path(base) / name)


class ProvisionError(RuntimeError):
    """A provisioning step failed. The distro is left as is (never half-deleted); ``repair`` can continue."""


class DigestMismatch(ProvisionError):
    """A downloaded artifact does not match the digest in the signed manifest. ALWAYS aborts (X10)."""


class PrerequisiteError(ProvisionError):
    pass


class RuntimeNotReady(ProvisionError):
    """The enrolment gate refused: health or the GPU smoke test did not pass."""


# --------------------------------------------------------------------------- #
# Settings and injectable dependencies
# --------------------------------------------------------------------------- #
@dataclass
class RuntimeSettings:
    manifest_public_key: str  # PINNED Ed25519 public key (32 bytes, base64 or hex). Empty = every manifest is refused.
    install_dir: str  # host directory that holds the distro's virtual disk
    staging_dir: str  # host directory for downloads and the lock file
    distro_name: str = P.DISTRO_NAME
    runner_user: str = P.RUNNER_USER
    wsl_exe: str = "wsl.exe"
    arch: str = "amd64"
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    wslconfig_policy: str = "never"  # "never" (default) or "add_missing"; see config_files.py
    wslconfig_path: Optional[str] = None
    require_disk_cap: bool = True  # fail closed when the disk cap cannot be applied
    allow_downgrade: bool = False  # accept a manifest older than the installed one (rollback by owner decision only)
    min_gpus: int = 1
    ready_timeout_seconds: float = 180.0

    def validate(self) -> None:
        if self.wslconfig_policy not in ("never", "add_missing"):
            raise ValueError("wslconfig_policy must be 'never' or 'add_missing'")
        if self.distro_name != P.DISTRO_NAME:
            raise ValueError(f"the managed distro must be named {P.DISTRO_NAME!r} (never touch the owner's distros)")
        if not self.install_dir or not self.staging_dir:
            raise ValueError("install_dir and staging_dir are required")
        self.limits.validate()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeSettings":
        d = dict(data)
        lim = d.pop("limits", {}) or {}
        d = {k: v for k, v in d.items() if not k.startswith("_")}  # "_comment" keys in the template
        s = cls(limits=ResourceLimits(**lim), **d)
        s.validate()
        return s

    @classmethod
    def from_file(cls, path: str) -> "RuntimeSettings":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class LocalFs:
    """Host file operations the provisioner needs. Tests may subclass or use a temp directory with this class."""

    def ensure_dir(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, text: str) -> None:
        Path(path).write_text(text, encoding="utf-8", newline="")

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def remove_file(self, path: str) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass

    def remove_tree(self, path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def sha256_file(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def size(self, path: str) -> int:
        return Path(path).stat().st_size

    def acquire_lock(self, path: str, stale_seconds: float, now: float) -> bool:
        """Exclusive create; a lock older than ``stale_seconds`` is taken over."""
        p = Path(path)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if now - p.stat().st_mtime > stale_seconds:
                    p.unlink()
                    return self.acquire_lock(path, stale_seconds, now)
            except OSError:
                pass
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True

    def release_lock(self, path: str) -> None:
        self.remove_file(path)


Fetcher = Callable[[str, str, int], None]  # (https url, destination file, max bytes) -> None; raises on any problem


def https_fetcher(url: str, dest: str, max_bytes: int) -> None:  # pragma: no cover - network
    """Stream ``url`` to ``dest``. https only, redirects must stay on https, hard size cap, writes ``dest`` atomically."""
    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        raise ProvisionError("only https downloads are allowed")

    class _HttpsOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            if not newurl.startswith("https://"):
                raise urllib.error.URLError("redirect to a non-https URL refused")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_HttpsOnly)
    tmp = dest + ".part"
    total = 0
    with opener.open(url, timeout=60) as resp, open(tmp, "wb") as out:  # noqa: S310 - scheme checked above
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            total += len(chunk)
            if total > max_bytes:
                raise ProvisionError(f"download exceeds {max_bytes} bytes; aborted")
            out.write(chunk)
    os.replace(tmp, dest)


@dataclass
class Deps:
    runner: Runner = subprocess_runner
    fetch: Fetcher = https_fetcher
    fs: Any = field(default_factory=LocalFs)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = lambda msg: None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    name: str
    status: str  # "done" | "skipped" | "failed"
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ProvisionResult:
    ok: bool = False
    steps: list[StepResult] = field(default_factory=list)
    health: Optional[HealthReport] = None
    smoke: Optional[Check] = None
    egress: list[Check] = field(default_factory=list)
    enrolment_allowed: bool = False
    restart_required: bool = False  # only after a .wslconfig change: the owner must run `wsl --shutdown` themselves
    error: Optional[str] = None
    manifest_version: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "enrolment_allowed": self.enrolment_allowed, "restart_required": self.restart_required,
            "error": self.error, "manifest_version": self.manifest_version,
            "steps": [s.to_dict() for s in self.steps],
            "health": self.health.to_dict() if self.health else None,
            "smoke": self.smoke.to_dict() if self.smoke else None,
            "egress": [c.to_dict() for c in self.egress],
        }


@dataclass
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    health: Optional[HealthReport] = None
    smoke: Optional[Check] = None

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": self.reasons,
                "health": self.health.to_dict() if self.health else None, "smoke": self.smoke.to_dict() if self.smoke else None}


# --------------------------------------------------------------------------- #
# The provisioner
# --------------------------------------------------------------------------- #
class RuntimeProvisioner:
    LOCK_STALE_SECONDS = 2 * 3600

    def __init__(self, settings: RuntimeSettings, deps: Optional[Deps] = None) -> None:
        settings.validate()
        self.s = settings
        self.d = deps or Deps()
        self.cli = WslCli(self.d.runner, settings.distro_name, wsl_exe=settings.wsl_exe)

    # ---- manifest ---------------------------------------------------------------------------------------------- #
    def load_manifest(self, envelope: bytes) -> RuntimeManifest:
        """Signature first, then schema. Raises ManifestError / SignatureError; nothing runs on a manifest that fails."""
        return load_verified_manifest(envelope, self.s.manifest_public_key, expected_arch=self.s.arch)

    # ---- state file -------------------------------------------------------------------------------------------- #
    def _read_state(self) -> dict[str, Any]:
        return state_json(self.cli.read_file(P.STATE_FILE))

    def _write_state(self, state: dict[str, Any]) -> None:
        state["schema"] = STATE_SCHEMA
        self.cli.write_file(P.STATE_FILE, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    # ---- public operations ------------------------------------------------------------------------------------- #
    def provision(self, envelope: bytes, *, force: bool = False) -> ProvisionResult:
        result = ProvisionResult()
        try:
            manifest = self.load_manifest(envelope)
        except ManifestError as exc:
            result.error = f"manifest refused: {exc}"
            return result
        result.manifest_version = manifest.version
        fs = self.d.fs
        fs.ensure_dir(self.s.staging_dir)
        lock = host_join(self.s.staging_dir, "provision.lock")
        if not fs.acquire_lock(lock, self.LOCK_STALE_SECONDS, self.d.clock()):
            result.error = "another provisioning run is in progress"
            return result
        try:
            self._provision_locked(manifest, result, force)
        except (ProvisionError, WslError, ManifestError, ValueError, OSError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.ok = False
            result.enrolment_allowed = False
        finally:
            fs.release_lock(lock)
        return result

    def repair(self, envelope: bytes) -> ProvisionResult:
        """Re-provision in place: every step re-runs (force). Credentials are never touched (they are not in the distro)."""
        return self.provision(envelope, force=True)

    def reset(self, envelope: bytes) -> ProvisionResult:
        """Remove the distro (all its data) and provision a fresh one from the manifest. Verify the manifest FIRST so a bad
        manifest cannot leave the host with no runtime."""
        try:
            self.load_manifest(envelope)
        except ManifestError as exc:
            r = ProvisionResult()
            r.error = f"manifest refused, distro left untouched: {exc}"
            return r
        removed = self.remove()
        if not removed.ok:
            r = ProvisionResult()
            r.error = f"reset could not remove the distro: {removed.error}"
            return r
        return self.provision(envelope)

    def remove(self) -> ProvisionResult:
        """Unregister the distro and delete its install directory. Idempotent: an absent distro is success."""
        result = ProvisionResult()
        try:
            info = self.cli.distro_info()
            if info is None:
                result.steps.append(StepResult("unregister", "skipped", "distro not registered"))
            else:
                self.cli.terminate()
                res = self.cli.host(argv_unregister(self.s.distro_name, self.s.wsl_exe), timeout=300)
                if not res.ok or self.cli.distro_info() is not None:
                    raise ProvisionError(f"wsl --unregister failed: {(res.stderr or res.stdout).strip()[:200]}")
                result.steps.append(StepResult("unregister", "done"))
            self._remove_install_dir(result)
            if self.s.wslconfig_policy == "add_missing" and self.s.wslconfig_path and self.d.fs.exists(self.s.wslconfig_path):
                cur = self.d.fs.read_text(self.s.wslconfig_path)
                new = remove_managed(cur)
                if new != cur:
                    self.d.fs.write_text(self.s.wslconfig_path, new)
                    result.steps.append(StepResult("wslconfig_cleanup", "done", "removed only the lines ComputeCoin added"))
            result.ok = True
        except (ProvisionError, WslError, OSError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def health(self, envelope: Optional[bytes] = None) -> HealthReport:
        manifest = self.load_manifest(envelope) if envelope else None
        return run_health(self.cli, manifest, runner_user=self.s.runner_user)

    def apply_limits(self, limits: ResourceLimits) -> StepResult:
        """Supervisor ``set_limits``: apply new caps to the existing distro (slice rewritten only if it changed)."""
        limits.validate()
        self.s.limits = limits
        changed = self._write_slice(limits)
        return StepResult("limits", "done" if changed else "skipped", "slice updated" if changed else "unchanged")

    def enrolment_gate(self, envelope: bytes) -> GateResult:
        """LIVE check, not the state file: health plus the GPU smoke test. Enrolment completes only when ``allowed``."""
        gate = GateResult(allowed=False)
        try:
            manifest = self.load_manifest(envelope)
        except ManifestError as exc:
            gate.reasons.append(f"manifest refused: {exc}")
            return gate
        gate.health = run_health(self.cli, manifest, runner_user=self.s.runner_user)
        gate.reasons += [f"{c.name}: {c.detail}" for c in gate.health.failures]
        gate.smoke = run_gpu_smoke(self.cli, manifest.smoke_test_image, min_gpus=self.s.min_gpus, runner_user=self.s.runner_user)
        if not gate.smoke.ok:
            gate.reasons.append(f"{gate.smoke.name}: {gate.smoke.detail}")
        gate.allowed = gate.health.ok and gate.smoke.ok
        return gate

    def assert_ready_for_enrolment(self, envelope: bytes) -> GateResult:
        gate = self.enrolment_gate(envelope)
        if not gate.allowed:
            raise RuntimeNotReady("; ".join(gate.reasons) or "runtime is not ready")
        return gate

    # ---- the pipeline ------------------------------------------------------------------------------------------ #
    def _provision_locked(self, manifest: RuntimeManifest, result: ProvisionResult, force: bool) -> None:
        self._preflight(manifest)
        result.steps.append(StepResult("preflight", "done"))

        imported = self._ensure_distro(manifest, result)
        state = {} if imported else self._read_state()
        if state and not self.s.allow_downgrade and int(state.get("manifest_sequence", -1)) > manifest.sequence:
            raise ProvisionError(
                f"manifest sequence {manifest.sequence} is older than the installed {state['manifest_sequence']}: rollback refused")
        done: dict[str, str] = dict(state.get("steps", {})) if not imported else {}

        plan = self._plan(manifest)
        fps: dict[str, str] = {}
        for name, deps, inputs, fn in plan:
            fp = hashlib.sha256(json.dumps({"step": name, "inputs": inputs, "deps": [fps[x] for x in deps]},
                                           sort_keys=True).encode()).hexdigest()
            fps[name] = fp
            if not force and done.get(name) == fp:
                result.steps.append(StepResult(name, "skipped", "already done for these inputs"))
                continue
            self.d.log(f"step {name}")
            try:
                detail = fn() or ""
            except Exception as exc:
                result.steps.append(StepResult(name, "failed", str(exc)[:300]))
                raise
            done[name] = fp
            self._write_state({**state, "manifest_sequence": manifest.sequence, "manifest_payload_sha256": manifest.payload_sha256,
                               "steps": done})
            result.steps.append(StepResult(name, "done", detail))

        result.restart_required = self._wslconfig_step(result)

        # ---- verification: never cached, and the only thing that can allow enrolment ----
        result.health = run_health(self.cli, manifest, runner_user=self.s.runner_user)
        result.smoke = run_gpu_smoke(self.cli, manifest.smoke_test_image, min_gpus=self.s.min_gpus, runner_user=self.s.runner_user)
        result.egress = verify_egress_proxy(self.cli, manifest.egress_proxy_image, runner_user=self.s.runner_user) if result.smoke.ok else []
        egress_ok = bool(result.egress) and all(c.ok for c in result.egress)
        verified = {"gpu_smoke_passed": result.smoke.ok, "egress_verified": egress_ok, "health_ok": result.health.ok,
                    "at": int(self.d.clock())}
        self._write_state({**self._read_state(), "manifest_sequence": manifest.sequence,
                           "manifest_payload_sha256": manifest.payload_sha256, "steps": done, "verified": verified})
        result.enrolment_allowed = result.health.ok and result.smoke.ok
        result.ok = result.enrolment_allowed
        if not result.ok:
            reasons = [f"{c.name}: {c.detail}" for c in result.health.failures] + ([] if result.smoke.ok else [f"{result.smoke.name}: {result.smoke.detail}"])
            result.error = "runtime is not ready for enrolment: " + "; ".join(reasons)

    def _plan(self, m: RuntimeManifest) -> list[tuple[str, list[str], Any, Callable[[], Optional[str]]]]:
        lim = self.s.limits
        return [
            ("runner_user", [], {"user": self.s.runner_user}, self._step_runner_user),
            ("wsl_conf", ["runner_user"], {"conf": render_wsl_conf(self.s.runner_user)}, self._step_wsl_conf),
            ("limits", ["wsl_conf"], {"limits": lim.__dict__, "require_disk_cap": self.s.require_disk_cap}, self._step_limits),
            ("disable_ssh", ["wsl_conf"], {}, self._step_disable_ssh),
            ("docker_engine", ["wsl_conf"], {"pk": m.docker_packages, "repo": m.docker_repo.__dict__}, lambda: self._step_docker(m)),
            ("nvidia_toolkit", ["docker_engine"], {"pk": m.nvidia_packages, "repo": m.nvidia_repo.__dict__}, lambda: self._step_nvidia(m)),
            ("docker_config", ["docker_engine", "nvidia_toolkit", "limits"], {"daemon": render_daemon_json()}, self._step_docker_config),
            ("runner_install", ["wsl_conf"], {"v": m.runner_version, "w": m.runner_wheel_sha256, "l": m.runner_lock_sha256},
             lambda: self._step_runner(m)),
            ("egress_proxy", ["docker_config"], {"image": m.egress_proxy_image}, lambda: self._step_egress(m)),
            ("smoke_image", ["docker_config"], {"image": m.smoke_test_image}, lambda: self._step_smoke_image(m)),
        ]

    # ---- steps ------------------------------------------------------------------------------------------------- #
    def _preflight(self, m: RuntimeManifest) -> None:
        have = self.cli.version()
        if have is None:
            raise PrerequisiteError("wsl.exe did not answer `wsl --version`: WSL is not installed or is too old")
        if not version_at_least(have, m.min_wsl_version):
            raise PrerequisiteError(f"WSL {'.'.join(map(str, have))} is older than the required {m.min_wsl_version}; run `wsl --update`")

    def _ensure_distro(self, m: RuntimeManifest, result: ProvisionResult) -> bool:
        info = self.cli.distro_info()
        if info is not None:
            if info.version != 2:
                raise ProvisionError("the distro exists but is WSL 1; use reset")
            result.steps.append(StepResult("import_distro", "skipped", "distro already registered"))
            return False
        fs = self.d.fs
        tar = host_join(self.s.staging_dir, f"rootfs-{m.rootfs_sha256[:16]}.tar.gz")
        self._download_verified(m.rootfs_url, tar, m.rootfs_sha256, MAX_ROOTFS_BYTES, m.rootfs_size_bytes)
        fs.ensure_dir(self.s.install_dir)
        res = self.cli.host(argv_import(self.s.distro_name, self.s.install_dir, tar, self.s.wsl_exe), timeout=1800)
        if not res.ok:
            raise ProvisionError(f"wsl --import failed: {(res.stderr or res.stdout).strip()[:300]}")
        info = self.cli.distro_info()
        if info is None or info.version != 2:
            raise ProvisionError("distro is not registered as WSL 2 after import")
        fs.remove_file(tar)
        result.steps.append(StepResult("import_distro", "done", f"rootfs sha256 {m.rootfs_sha256[:12]} verified"))
        return True

    def _download_verified(self, url: str, dest: str, sha256: str, max_bytes: int, expected_size: Optional[int] = None) -> None:
        """Download, then compare the digest with the signed manifest. A mismatch deletes the file and aborts."""
        fs = self.d.fs
        fs.remove_file(dest)
        self.d.fetch(url, dest, max_bytes)
        if expected_size is not None and fs.size(dest) != expected_size:
            fs.remove_file(dest)
            raise DigestMismatch(f"size of {url} differs from the manifest")
        got = fs.sha256_file(dest)
        if got != sha256:
            fs.remove_file(dest)
            raise DigestMismatch(f"sha256 of {url} is {got[:16]}..., manifest says {sha256[:16]}...: aborted")

    def _wait_ready(self) -> None:
        """systemd up and Docker-free basics answering; ``running`` or ``degraded`` both mean booted."""
        deadline = self.d.clock() + self.s.ready_timeout_seconds
        while True:
            res = self.cli.exec(["systemctl", "is-system-running", "--wait"], timeout=120)
            if res.stdout.strip() in ("running", "degraded"):
                return
            if self.d.clock() >= deadline:
                raise ProvisionError(f"systemd did not finish booting: {res.stdout.strip() or res.stderr.strip()}")
            self.d.sleep(2)

    def _step_runner_user(self) -> str:
        u = self.s.runner_user
        if self.cli.exec(["id", "-u", u], timeout=30).ok:
            return "user exists"
        self.cli.exec_ok(["useradd", "--system", "--create-home", "--home-dir", f"/home/{u}", "--shell", "/usr/sbin/nologin",
                          "--user-group", u], what="create runner user", timeout=60)
        self.cli.exec_ok(["passwd", "--lock", u], what="lock runner password", timeout=30)
        return "created"

    def _step_wsl_conf(self) -> str:
        want = render_wsl_conf(self.s.runner_user)
        have = self.cli.read_file(P.WSL_CONF)
        if have == want:
            self._wait_ready()
            return "wsl.conf already as required"
        self.cli.write_file(P.WSL_CONF, want.encode("utf-8"))
        self.cli.terminate()  # ONLY this distro: never `wsl --shutdown`, the owner's other distros keep running
        self.cli.exec_ok(["true"], what="restart the distro", timeout=120)
        self._wait_ready()
        problems = hardening_problems(self.cli.read_file(P.WSL_CONF))
        if problems:
            raise ProvisionError("wsl.conf is not as required after restart: " + "; ".join(problems))
        return "written, distro restarted"

    def _write_slice(self, limits: ResourceLimits) -> bool:
        want = render_slice(limits)
        if self.cli.read_file(P.SLICE_UNIT) == want:
            return False
        self.cli.write_file(P.SLICE_UNIT, want.encode("utf-8"))
        self.cli.exec_ok(["systemctl", "daemon-reload"], what="reload systemd", timeout=60)
        return True

    def _step_limits(self) -> str:
        lim = self.s.limits
        notes = ["slice written" if self._write_slice(lim) else "slice unchanged"]
        size = disk_size_arg(lim)
        if size:
            self.cli.terminate()
            res = self.cli.host(argv_resize(self.s.distro_name, size, self.s.wsl_exe), timeout=600)
            if not res.ok:
                if self.s.require_disk_cap:
                    raise ProvisionError(f"cannot cap the virtual disk at {size} ({(res.stderr or res.stdout).strip()[:200]}); "
                                         "update WSL or set require_disk_cap=false knowingly")
                notes.append("disk cap NOT applied (allowed by settings)")
            else:
                notes.append(f"disk capped at {size}")
            self.cli.host(argv_set_sparse(self.s.distro_name, True, self.s.wsl_exe), timeout=120)  # best effort reclaim
            self.cli.exec_ok(["true"], what="restart the distro", timeout=120)
            self._wait_ready()
        elif self.s.require_disk_cap:
            raise ProvisionError("no disk cap configured (limits.disk_gb) and require_disk_cap is true")
        return "; ".join(notes)

    def _step_disable_ssh(self) -> str:
        units = ["ssh.service", "ssh.socket", "sshd.service"]
        self.cli.exec(["systemctl", "disable", "--now", *units], timeout=60)  # units may not exist: not an error
        self.cli.exec_ok(["systemctl", "mask", *units], what="mask ssh units", timeout=60)
        return "ssh units masked"

    # -- apt helpers (no shell) --
    def _apt_env(self, argv: list[str]) -> list[str]:
        return ["env", "DEBIAN_FRONTEND=noninteractive", *argv]

    def _install_apt_repo(self, name: str, repo: AptRepo) -> None:
        """Download the signing key, REQUIRE the pinned fingerprint, dearmor it, write a deb822 source tied to that key."""
        asc = f"/tmp/{name}-repo-key.asc"
        keyring = f"{P.KEYRING_DIR}/computecoin-{name}.gpg"
        self.cli.exec_ok(["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--tlsv1.2",
                          "--output", asc, repo.key_url], what=f"download {name} repository key", timeout=120)
        shown = self.cli.exec_ok(["gpg", "--show-keys", "--with-colons", "--import-options", "show-only", asc],
                                 what=f"read {name} repository key", timeout=60)
        fprs = {line.split(":")[9].upper() for line in shown.stdout.splitlines() if line.startswith("fpr:") and len(line.split(":")) > 9}
        if repo.key_fingerprint not in fprs:
            self.cli.exec(["rm", "-f", asc], timeout=30)
            raise DigestMismatch(f"{name} repository key fingerprint does not match the manifest pin; aborted")
        self.cli.exec_ok(["gpg", "--dearmor", "--yes", "--output", keyring, asc], what=f"install {name} repository key", timeout=60)
        self.cli.exec_ok(["chmod", "0644", keyring], what="chmod keyring", timeout=30)
        self.cli.exec(["rm", "-f", asc], timeout=30)
        lines = ["Types: deb", f"URIs: {repo.uri}", f"Suites: {repo.suite}"]
        if repo.component:
            lines.append(f"Components: {repo.component}")
        lines += [f"Architectures: {self.s.arch}", f"Signed-By: {keyring}"]
        self.cli.write_file(f"{P.APT_SOURCES_DIR}/computecoin-{name}.sources", ("\n".join(lines) + "\n").encode("utf-8"))
        self.cli.exec_ok(self._apt_env(["apt-get", "update"]), what=f"apt update ({name})", timeout=600)

    def _install_pinned(self, packages: dict[str, str]) -> None:
        specs = [f"{n}={v}" for n, v in packages.items()]
        self.cli.exec_ok(self._apt_env(["apt-get", "install", "-y", "--no-install-recommends", "--allow-downgrades", *specs]),
                         what="install pinned packages", timeout=1800)
        self.cli.exec_ok(["apt-mark", "hold", *packages], what="hold pinned packages", timeout=60)

    def _step_docker(self, m: RuntimeManifest) -> str:
        self._install_apt_repo("docker", m.docker_repo)
        self._install_pinned(m.docker_packages)
        # NOTE: membership of the docker group is root-equivalent INSIDE the distro. The distro, not the runner user, is
        # the security boundary towards Windows (X6); see docs/operations/windows-runtime-provisioning.md.
        self.cli.exec_ok(["usermod", "-aG", "docker", self.s.runner_user], what="add runner to docker group", timeout=30)
        return "docker " + m.docker_engine_version

    def _step_nvidia(self, m: RuntimeManifest) -> str:
        self._install_apt_repo("nvidia", m.nvidia_repo)
        self._install_pinned(m.nvidia_packages)
        return "toolkit " + m.nvidia_toolkit_version

    def _step_docker_config(self) -> str:
        self.cli.write_file(P.DOCKER_DAEMON_JSON, render_daemon_json().encode("utf-8"))
        self.cli.exec_ok(["nvidia-ctk", "runtime", "configure", "--runtime=docker"], what="register the nvidia runtime", timeout=120)
        self.cli.exec_ok(["systemctl", "enable", "docker"], what="enable docker", timeout=60)
        self.cli.exec_ok(["systemctl", "restart", "docker"], what="restart docker", timeout=180)
        deadline = self.d.clock() + 60
        while not self.cli.exec(["docker", "info"], timeout=60).ok:
            if self.d.clock() >= deadline:
                raise ProvisionError("docker did not come up after configuration")
            self.d.sleep(2)
        return "daemon.json written, nvidia runtime registered, docker restarted"

    def _step_runner(self, m: RuntimeManifest) -> str:
        fs, cli = self.d.fs, self.cli
        wheel_host = host_join(self.s.staging_dir, m.runner_wheel_filename)
        lock_host = host_join(self.s.staging_dir, "runner-requirements.lock")
        try:
            self._download_verified(m.runner_wheel_url, wheel_host, m.runner_wheel_sha256, MAX_WHEEL_BYTES)
            self._download_verified(m.runner_lock_url, lock_host, m.runner_lock_sha256, MAX_LOCK_BYTES)
            wheel_bytes, lock_bytes = fs.read_bytes(wheel_host), fs.read_bytes(lock_host)
        finally:
            fs.remove_file(wheel_host)
            fs.remove_file(lock_host)
        wheel_guest = f"{P.WHEEL_DIR}/{m.runner_wheel_filename}"
        lock_guest = f"{P.WHEEL_DIR}/requirements.lock"
        cli.exec_ok(["mkdir", "-p", P.WHEEL_DIR], what="create wheel dir", timeout=30)
        cli.write_file(wheel_guest, wheel_bytes)
        cli.write_file(lock_guest, lock_bytes)
        for path, want in ((wheel_guest, m.runner_wheel_sha256), (lock_guest, m.runner_lock_sha256)):  # verify AGAIN inside
            out = cli.exec_ok(["sha256sum", path], what="checksum inside the distro", timeout=60).stdout.split()
            if not out or out[0] != want:
                raise DigestMismatch(f"{path} changed on the way into the distro; aborted")
        if not cli.exec(["python3", "-m", "venv", "--help"], timeout=30).ok:
            raise PrerequisiteError("the rootfs has no python3 venv module (rootfs build requirement)")
        cli.exec_ok(["python3", "-m", "venv", "--clear", P.VENV_DIR], what="create venv", timeout=120)
        pip = [f"{P.VENV_DIR}/bin/python", "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        cli.exec_ok([*pip, "--require-hashes", "--only-binary=:all:", "--no-deps", "-r", lock_guest], what="install runner dependencies", timeout=1800)
        cli.exec_ok([*pip, "--no-index", "--no-deps", wheel_guest], what="install runner wheel", timeout=600)
        # The code is root-owned and not writable by the runner user: a compromised runner cannot rewrite itself.
        cli.exec_ok(["chown", "-R", "root:root", P.OPT_DIR], what="own runner code", timeout=120)
        cli.exec_ok(["chmod", "-R", "go-w", P.OPT_DIR], what="protect runner code", timeout=120)
        u = self.s.runner_user
        cli.exec_ok(["install", "-d", "-m", "0700", "-o", u, "-g", u, f"/home/{u}/.computecoin"], what="create runner config dir", timeout=30)
        cli.exec_ok(["install", "-d", "-m", "0750", "-o", u, "-g", u, "/var/lib/computecoin"], what="create runner data dir", timeout=30)
        return "runner " + m.runner_version

    def _step_egress(self, m: RuntimeManifest) -> str:
        pull_egress_proxy(self.cli, m.egress_proxy_image)
        return "egress proxy image pulled and recorded"

    def _step_smoke_image(self, m: RuntimeManifest) -> str:
        self.cli.exec_ok(["docker", "pull", m.smoke_test_image], what="pull the smoke test image", timeout=1800)
        return "smoke image pulled"

    def _wslconfig_step(self, result: ProvisionResult) -> bool:
        """Host-side, optional, never clobbering. Returns True when the owner must restart WSL for it to take effect."""
        if self.s.wslconfig_policy != "add_missing":
            result.steps.append(StepResult("wslconfig", "skipped", "policy 'never': the owner's .wslconfig is not touched"))
            return False
        wanted = desired_wslconfig(self.s.limits)
        if not wanted or not self.s.wslconfig_path:
            result.steps.append(StepResult("wslconfig", "skipped", "nothing to add"))
            return False
        fs, path = self.d.fs, self.s.wslconfig_path
        existing = fs.read_text(path) if fs.exists(path) else ""
        plan = merge_wslconfig(existing, wanted)
        if not plan.changed:
            kept = ", ".join(f"{k}={v}" for k, v in plan.kept_owner_values.items())
            result.steps.append(StepResult("wslconfig", "skipped", f"owner already sets: {kept}" if kept else "unchanged"))
            return False
        backup = path + ".computecoin.bak"
        if existing and not fs.exists(backup):
            fs.write_text(backup, existing)
        fs.write_text(path, plan.text)
        result.steps.append(StepResult("wslconfig", "done", "added " + ", ".join(f"{k}={v}" for k, v in plan.added.items())
                                       + " (owner values untouched; effective after the owner runs `wsl --shutdown`)"))
        return True

    def _remove_install_dir(self, result: ProvisionResult) -> None:
        d = self.s.install_dir
        parts = PureWindowsPath(d).parts if "\\" in d else Path(d).parts
        if len(parts) < 3 or P.DISTRO_NAME not in d.lower():
            raise ProvisionError(f"refusing to delete install_dir {d!r}: it must be at least 3 levels deep and name the distro")
        if self.d.fs.exists(d):
            self.d.fs.remove_tree(d)
            result.steps.append(StepResult("remove_install_dir", "done", d))


# --------------------------------------------------------------------------- #
# Command line (called by provision.ps1)
# --------------------------------------------------------------------------- #
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PREREQ, EXIT_SECURITY = 0, 1, 2, 3, 4


def _exit_for(result: ProvisionResult) -> int:
    if result.ok:
        return EXIT_OK
    err = result.error or ""
    if "manifest refused" in err or "DigestMismatch" in err or "SignatureError" in err:
        return EXIT_SECURITY
    if "PrerequisiteError" in err:
        return EXIT_PREREQ
    return EXIT_FAILED


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - thin wrapper over the tested class
    ap = argparse.ArgumentParser(prog="python -m computecoin_agent.windows.runtime.provision",
                                 description="Provision and manage the ComputeCoin runtime distro (Windows hosts).")
    ap.add_argument("command", choices=["provision", "repair", "reset", "remove", "health", "gate", "limits"])
    ap.add_argument("--settings", required=True, help="runtime settings JSON (see provider-agent/windows/runtime-settings.template.json)")
    ap.add_argument("--manifest", help="path to the signed runtime-manifest.json (needed by every command except remove)")
    ap.add_argument("--memory-mb", type=int)
    ap.add_argument("--cpus", type=float)
    ap.add_argument("--disk-gb", type=int)
    args = ap.parse_args(argv)
    try:
        prov = RuntimeProvisioner(RuntimeSettings.from_file(args.settings))
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": f"bad settings: {exc}"}))
        return EXIT_USAGE
    envelope = Path(args.manifest).read_bytes() if args.manifest else None
    if args.command != "remove" and args.command != "limits" and envelope is None:
        print(json.dumps({"ok": False, "error": "--manifest is required"}))
        return EXIT_USAGE
    if args.command == "remove":
        res = prov.remove()
    elif args.command == "provision":
        res = prov.provision(envelope or b"")
    elif args.command == "repair":
        res = prov.repair(envelope or b"")
    elif args.command == "reset":
        res = prov.reset(envelope or b"")
    elif args.command == "limits":
        step = prov.apply_limits(ResourceLimits(memory_mb=args.memory_mb, cpus=args.cpus, disk_gb=args.disk_gb))
        print(json.dumps(step.to_dict()))
        return EXIT_OK
    elif args.command == "health":
        rep = prov.health(envelope)
        print(json.dumps(rep.to_dict()))
        return EXIT_OK if rep.ok else EXIT_FAILED
    else:
        gate = prov.enrolment_gate(envelope or b"")
        print(json.dumps(gate.to_dict()))
        return EXIT_OK if gate.allowed else EXIT_FAILED
    print(json.dumps(res.to_dict()))
    return _exit_for(res)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
