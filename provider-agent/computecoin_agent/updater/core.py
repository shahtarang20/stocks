"""The updater: check, verify, download, install, health-check, roll back (cross-platform plan W6, X10).

Everything with a side effect is injected, so the whole flow runs offline in tests:

* ``fetch_manifest(ring, platform, current_version) -> dict | None``  (the server decides what this host may have)
* ``download(url, dest_path)``
* ``installer``  (:class:`Installer`: ``install(artifact, version)`` and ``installed_version()``)
* ``health_check(version) -> bool``  (does the NEW version work?)
* ``gate``  (:class:`computecoin_agent.updater.gate.UpdateGate`)

Order of operations in :meth:`AgentUpdater.run_once`, each step failing CLOSED:

1. fetch; verify the Ed25519 signature before reading a field; platform, ring, expiry, URL
2. record the forced-update floor (``min_supported``) from the verified manifest
3. decide: up to date / downgrade refused / recently failed / update
4. never while a job runs: ask the gate to drain and come back later (``deferred_job_running``)
5. download into a fresh temp directory, check size and SHA-256, copy into the cache, hash again right before install
6. persist ``in_flight`` BEFORE installing (the install may replace this very process)
7. install; wait for the health check; on any failure reinstall the previous known-good artifact and CHECK that it took
8. success: the new artifact becomes known-good; the temp directory is always removed
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from .gate import UpdateGate
from .manifest import ManifestError, UpdateManifest, parse_manifest, sha256_file
from .state import UpdateState
from .version import compare

logger = logging.getLogger(__name__)

# Result statuses
DISABLED = "disabled"
UP_TO_DATE = "up_to_date"
NO_RELEASE = "no_release"
DEFERRED_JOB = "deferred_job_running"
DEFERRED_COOLDOWN = "deferred_cooldown"
UPDATED = "updated"
ROLLED_BACK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"  # the host is in an unknown state: alert, claim nothing
REJECTED = "rejected"
DOWNGRADE_REFUSED = "downgrade_refused"
ERROR = "error"


class Installer(Protocol):
    """The platform install step (Windows: msiexec through an injected runner; see ``msi.py``)."""

    def install(self, artifact: Path, version: str) -> None: ...

    def installed_version(self) -> str: ...


@dataclasses.dataclass(frozen=True)
class UpdateResult:
    status: str
    code: str = ""
    from_version: str = ""
    to_version: str = ""
    forced: bool = False
    detail: str = ""


class AgentUpdater:
    def __init__(
        self,
        *,
        current_version: str,
        platform: str,
        ring: str,
        public_keys: Sequence[str],
        fetch_manifest: Callable[[str, str, str], Optional[dict]],
        download: Callable[[str, Path], None],
        installer: Installer,
        health_check: Callable[[str], bool],
        gate: UpdateGate,
        state: UpdateState,
        work_dir: Path,
        allowed_hosts: Optional[frozenset] = None,
        ensure_private_dir: Optional[Callable[[Path], None]] = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        health_timeout_seconds: float = 120.0,
        health_interval_seconds: float = 5.0,
        health_successes: int = 2,
        retry_cooldown_seconds: float = 6 * 3600.0,
    ) -> None:
        self.current_version = current_version
        self.platform, self.ring = platform, ring
        self._keys = tuple(public_keys)
        self._fetch, self._download = fetch_manifest, download
        self._installer, self._health = installer, health_check
        self.gate, self.state = gate, state
        self._work = Path(work_dir)
        self._allowed_hosts = allowed_hosts
        # Called on the work directory before anything is downloaded or installed from it; must raise unless only SYSTEM and
        # Administrators can write there (the cached rollback artifact and its recorded hash are trusted because of that).
        self._ensure_private_dir = ensure_private_dir
        self._enabled = enabled
        self._clock, self._sleep = clock, sleeper
        self._h_timeout, self._h_interval, self._h_need = health_timeout_seconds, health_interval_seconds, max(1, health_successes)
        self._cooldown = retry_cooldown_seconds
        floor = self.state.data.get("floor")
        if floor:
            gate.set_floor(floor)
        gate.set_current(current_version)

    # ---- public --------------------------------------------------------- #
    def check(self) -> tuple:
        """(manifest | None, result | None). Fetch and verify only; nothing is changed except the recorded floor."""
        if not self._enabled:
            return None, UpdateResult(DISABLED, from_version=self.current_version)
        try:
            raw = self._fetch(self.ring, self.platform, self.current_version)
        except Exception as exc:  # noqa: BLE001 - a network failure is not a security event
            return None, UpdateResult(ERROR, "fetch_failed", self.current_version, detail=type(exc).__name__)
        if raw is None:
            return None, UpdateResult(NO_RELEASE, from_version=self.current_version)
        try:
            manifest = parse_manifest(raw, self._keys, platform=self.platform, host_ring=self.ring,
                                      now=self._clock(), allowed_hosts=self._allowed_hosts)
        except ManifestError as exc:
            logger.warning("update manifest refused: %s", exc.code)
            return None, UpdateResult(REJECTED, exc.code, self.current_version, detail=exc.detail)
        # The floor comes only from a VERIFIED manifest: an attacker cannot lock the fleet out with an unsigned one.
        self.state.raise_floor(manifest.min_supported)
        self.gate.set_floor(self.state.data["floor"])
        return manifest, None

    def run_once(self) -> UpdateResult:
        manifest, early = self.check()
        if early is not None:
            return early
        assert manifest is not None
        cur = self.current_version
        forced = compare(cur, manifest.min_supported) < 0
        order = compare(manifest.version, cur)
        if order == 0:
            return UpdateResult(UP_TO_DATE, from_version=cur, to_version=manifest.version)
        if order < 0 and not manifest.authorises_downgrade_from(cur):
            return UpdateResult(DOWNGRADE_REFUSED, "not_revoked", cur, manifest.version)
        if self.state.failed_recently(manifest.version, self._clock(), self._cooldown):
            return UpdateResult(DEFERRED_COOLDOWN, "failed_recently", cur, manifest.version, forced)
        return self._apply(manifest, forced)

    def resume_after_restart(self) -> Optional[UpdateResult]:
        """Call once at startup. If an update was in flight when this process (re)started, decide whether it took."""
        flight = self.state.in_flight
        if not flight:
            return None
        to_v, from_v = str(flight.get("to")), str(flight.get("from"))
        try:
            installed = self._installer.installed_version()
        except Exception:  # noqa: BLE001
            installed = ""
        if installed == to_v:
            return self._confirm_or_roll_back(to_v, from_v, forced=False)
        # The new version is not running: the install failed or was rolled back by the installer itself.
        self.state.record_failure(to_v, "install_incomplete", self._clock())
        self.state.data["in_flight"] = None
        self.state.save()
        self.gate.set_current(installed or from_v)
        self.current_version = installed or from_v
        return UpdateResult(ROLLED_BACK, "install_incomplete", from_v, to_v)

    # ---- internals ------------------------------------------------------ #
    def _apply(self, m: UpdateManifest, forced: bool) -> UpdateResult:
        cur = self.current_version
        if not self.gate.try_begin_update():
            self.gate.request_drain()  # finish what runs, claim nothing new, come back on the next call
            return UpdateResult(DEFERRED_JOB, "job_running", cur, m.version, forced)
        tmp: Optional[Path] = None
        try:
            self._work.mkdir(parents=True, exist_ok=True)
            if self._ensure_private_dir is not None:
                try:
                    self._ensure_private_dir(self._work)
                except Exception as exc:  # noqa: BLE001 - an unprotected work directory means the cache cannot be trusted
                    return UpdateResult(ERROR, "work_dir_not_private", cur, m.version, forced, type(exc).__name__)
            tmp = Path(tempfile.mkdtemp(prefix="update-", dir=str(self._work)))
            artifact = tmp / "artifact.pkg"
            try:
                self._download(m.artifact_url, artifact)
            except Exception as exc:  # noqa: BLE001
                return UpdateResult(ERROR, "download_failed", cur, m.version, forced, type(exc).__name__)
            bad = self._check_artifact(artifact, m)
            if bad:
                self.state.record_failure(m.version, bad, self._clock())
                return UpdateResult(REJECTED, bad, cur, m.version, forced)
            cached = self._cache(artifact, m)
            if sha256_file(cached) != m.sha256:  # again, right before the installer reads it
                self.state.record_failure(m.version, "hash_mismatch", self._clock())
                return UpdateResult(REJECTED, "hash_mismatch", cur, m.version, forced)
            self.state.data["in_flight"] = {"from": cur, "to": m.version, "sha256": m.sha256, "path": str(cached),
                                            "started_at": self._clock()}
            self.state.save()
            try:
                self._installer.install(cached, m.version)
            except Exception as exc:  # noqa: BLE001
                logger.warning("installer failed: %s", type(exc).__name__)
                return self._roll_back(cur, m.version, "install_failed", forced)
            return self._confirm_or_roll_back(m.version, cur, forced)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            self.gate.end_update()
            self.gate.cancel_drain()

    def _check_artifact(self, path: Path, m: UpdateManifest) -> str:
        try:
            size = path.stat().st_size
        except OSError:
            return "artifact_missing"
        if size != m.size_bytes:
            return "size_mismatch"
        if sha256_file(path) != m.sha256:
            return "hash_mismatch"
        return ""

    def _cache(self, artifact: Path, m: UpdateManifest) -> Path:
        cache = self._work / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / f"{m.version}-{m.sha256[:16]}.pkg"
        shutil.copyfile(artifact, dest)
        return dest

    def _wait_healthy(self, version: str) -> bool:
        deadline = self._clock() + self._h_timeout
        streak = 0
        while True:
            try:
                ok = bool(self._health(version)) and self._installer.installed_version() == version
            except Exception:  # noqa: BLE001 - an exception in the check is "not healthy"
                ok = False
            streak = streak + 1 if ok else 0
            if streak >= self._h_need:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(self._h_interval)

    def _confirm_or_roll_back(self, to_v: str, from_v: str, forced: bool) -> UpdateResult:
        if not self._wait_healthy(to_v):
            return self._roll_back(from_v, to_v, "health_check_failed", forced)
        flight = self.state.in_flight or {}
        self.state.data["known_good"] = {"version": to_v, "sha256": flight.get("sha256"), "path": flight.get("path")}
        self.state.data["in_flight"] = None
        self.state.data["failed"].pop(to_v, None)
        self.state.save()
        self.current_version = to_v
        self.gate.set_current(to_v)
        self._prune_cache(keep=flight.get("path"))
        return UpdateResult(UPDATED, "", from_v, to_v, forced)

    def _roll_back(self, from_v: str, to_v: str, code: str, forced: bool) -> UpdateResult:
        """Reinstall the previous known-good artifact and verify it is what runs; never report a rollback that did not happen."""
        self.state.record_failure(to_v, code, self._clock())
        good = self.state.known_good
        path = Path(good["path"]) if good and good.get("path") else None
        ok = False
        if path is not None and path.is_file() and sha256_file(path) == good.get("sha256"):
            try:
                self._installer.install(path, str(good["version"]))
                ok = self._wait_healthy(str(good["version"]))
            except Exception as exc:  # noqa: BLE001
                logger.error("rollback install failed: %s", type(exc).__name__)
        self.state.data["in_flight"] = None
        self.state.save()
        if ok:
            self.current_version = str(good["version"])
            self.gate.set_current(self.current_version)
            return UpdateResult(ROLLED_BACK, code, from_v, to_v, forced)
        # No usable previous artifact, or it did not come back healthy: the host is in an unknown state. The gate keeps
        # the host out of the pool (below-floor or this flag) and the caller must alert.
        self.gate.block()
        return UpdateResult(ROLLBACK_FAILED, code, from_v, to_v, forced, "previous version could not be restored")

    def _prune_cache(self, keep: Optional[str]) -> None:
        cache = self._work / "cache"
        if not cache.is_dir():
            return
        for f in cache.iterdir():
            if keep is None or str(f) != keep:
                # keep the artifact we just promoted (it is the rollback source for the NEXT update), drop the rest
                try:
                    f.unlink()
                except OSError:
                    pass
