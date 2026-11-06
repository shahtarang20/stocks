"""W6: signed update manifests, hash check, rollback, no update during a job, rings, forced-update floor.

Fresh random Ed25519 keys only (L7); no network; fakes for the installer, the downloader and the clock.
WRITTEN, NOT RUN (the task forbids running tests).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from computecoin_agent.updater import core, manifest as mf
from computecoin_agent.updater.core import AgentUpdater
from computecoin_agent.updater.gate import UpdateGate
from computecoin_agent.updater.state import UpdateState
from computecoin_agent.updater.version import compare, parse_version

ARTIFACT = b"signed-installer-bytes" * 100
NOW = 1_800_000_000.0


def pub_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def make_manifest(key, data: bytes = ARTIFACT, **over) -> dict:
    payload = {
        "schema": 1, "product": "computecoin-agent", "version": "1.1.0", "platform": "windows",
        "artifact_url": "https://downloads.example.test/agent-1.1.0.msi", "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data), "min_supported": "1.0.0", "ring": "general", "issued_at": 1_700_000_000,
    }
    payload.update(over)
    sig = key.sign(mf.SIGN_PREFIX + mf.canonical_bytes(payload)).hex()
    return {**payload, "signature": sig}


class FakeInstaller:
    def __init__(self, version="1.0.0"):
        self.version = version
        self.installs = []  # (version, bytes) in call order
        self.fail_versions = set()
        self.broken_after_install = set()  # installs fine but the new version never becomes healthy
        self.during_install = None  # callback run inside install()

    def install(self, artifact: Path, version: str) -> None:
        if self.during_install:
            self.during_install()
        data = Path(artifact).read_bytes()
        self.installs.append((version, data))
        if version in self.fail_versions:
            raise RuntimeError("msiexec exited with 1603")
        self.version = version

    def installed_version(self) -> str:
        return self.version


class Harness:
    def __init__(self, tmp_path, *, current="1.0.0", ring="general", key=None, manifest=None, data=ARTIFACT, **kw):
        self.key = key or Ed25519PrivateKey.generate()
        self.manifest = manifest if manifest is not None else make_manifest(self.key, data)
        self.data = data
        self.installer = FakeInstaller(current)
        self.gate = UpdateGate(current)
        self.state = UpdateState(tmp_path / "state.json")
        self.work = tmp_path / "work"
        self.downloads = []
        self.healthy = kw.pop("healthy", lambda v: True)
        self.t = [NOW]
        self.fetch_calls = []
        keys = kw.pop("keys", [pub_hex(self.key)])

        def fetch(ring, platform, current_version):
            self.fetch_calls.append((ring, platform, current_version))
            return self.manifest

        def download(url, dest: Path):
            self.downloads.append(url)
            dest.write_bytes(self.data)

        def sleeper(s):
            self.t[0] += s

        self.updater = AgentUpdater(
            current_version=current, platform="windows", ring=ring, public_keys=keys, fetch_manifest=fetch,
            download=download, installer=self.installer, health_check=lambda v: self.healthy(v), gate=self.gate,
            state=self.state, work_dir=self.work, clock=lambda: self.t[0], sleeper=sleeper,
            health_timeout_seconds=30.0, health_interval_seconds=5.0, health_successes=2, **kw,
        )

    def seed_known_good(self, version="1.0.0", data=b"previous-installer"):
        cache = self.work / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / f"{version}-seed.pkg"
        path.write_bytes(data)
        self.state.data["known_good"] = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "path": str(path)}
        self.state.save()
        return path

    def leftovers(self):
        return [p for p in self.work.glob("update-*")] if self.work.exists() else []


# ---- happy path ---------------------------------------------------------------------------------------------------------

def test_verified_update_installs_and_becomes_known_good(tmp_path):
    h = Harness(tmp_path)
    r = h.updater.run_once()
    assert r.status == core.UPDATED and (r.from_version, r.to_version) == ("1.0.0", "1.1.0")
    assert h.installer.installs == [("1.1.0", ARTIFACT)]
    assert h.state.known_good["version"] == "1.1.0" and h.state.in_flight is None
    assert h.updater.current_version == "1.1.0" and h.gate.current_version == "1.1.0"
    assert h.leftovers() == []  # the temp directory is removed


def test_up_to_date_and_no_release(tmp_path):
    h = Harness(tmp_path, current="1.1.0")
    assert h.updater.run_once().status == core.UP_TO_DATE
    h2 = Harness(tmp_path / "b")
    h2.manifest = None
    assert h2.updater.run_once().status == core.NO_RELEASE


def test_host_reports_its_ring_and_platform_to_the_server(tmp_path):
    h = Harness(tmp_path, ring="early", manifest=None)
    h.manifest = make_manifest(h.key, ring="general")
    h.updater.run_once()
    assert h.fetch_calls == [("early", "windows", "1.0.0")]


def test_disabled_updater_does_nothing(tmp_path):
    h = Harness(tmp_path, enabled=False)
    assert h.updater.run_once().status == core.DISABLED and h.fetch_calls == []


# ---- signature ------------------------------------------------------------------------------------------------------------

def test_manifest_signed_by_another_key_is_refused_and_nothing_is_downloaded(tmp_path):
    attacker = Ed25519PrivateKey.generate()
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(attacker)
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.REJECTED, "signature_invalid")
    assert h.downloads == [] and h.installer.installs == []


def test_tampered_manifest_field_breaks_the_signature(tmp_path):
    h = Harness(tmp_path)
    h.manifest["artifact_url"] = "https://evil.example.test/agent.msi"
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.REJECTED, "signature_invalid")
    assert h.downloads == []


def test_swapped_hash_in_a_signed_manifest_is_caught_by_the_signature(tmp_path):
    h = Harness(tmp_path)
    h.manifest["sha256"] = hashlib.sha256(b"malware").hexdigest()
    assert h.updater.run_once().code == "signature_invalid"


def test_missing_or_malformed_signature_is_refused(tmp_path):
    h = Harness(tmp_path)
    del h.manifest["signature"]
    assert h.updater.run_once().code == "malformed"
    h2 = Harness(tmp_path / "b")
    h2.manifest["signature"] = "zz"
    assert h2.updater.run_once().code == "malformed"


def test_no_pinned_key_means_every_manifest_is_refused(tmp_path):
    h = Harness(tmp_path, keys=[])
    assert h.updater.run_once().code == "no_pinned_key"
    assert mf.PINNED_PUBLIC_KEYS == ()  # nothing is pinned until the owner's key ceremony (L7)
    assert h.installer.installs == []


def test_either_of_two_pinned_keys_verifies_during_a_rotation(tmp_path):
    new_key = Ed25519PrivateKey.generate()
    old_key = Ed25519PrivateKey.generate()
    h = Harness(tmp_path, key=new_key, keys=[pub_hex(old_key), pub_hex(new_key)])
    assert h.updater.run_once().status == core.UPDATED


def test_signature_is_checked_before_any_field_is_trusted(tmp_path):
    attacker = Ed25519PrivateKey.generate()
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(attacker, min_supported="1.1.0")  # would lock the fleet below a floor if it were believed
    h.updater.run_once()
    assert h.state.data["floor"] is None and h.gate.below_floor is False


@pytest.mark.parametrize(
    "over,code",
    [
        ({"platform": "linux"}, "platform_mismatch"),
        ({"artifact_url": "http://plain.example.test/a.msi"}, "bad_url"),
        ({"artifact_url": "https://user:pw@example.test/a.msi"}, "bad_url"),
        ({"expires_at": 1_700_000_001}, "expired"),
        ({"sha256": "XYZ"}, "malformed"),
        ({"size_bytes": 0}, "malformed"),
        ({"version": "not-a-version"}, "malformed"),
        ({"min_supported": "9.0.0"}, "malformed"),
        ({"ring": "beta"}, "malformed"),
        ({"surprise": 1}, "malformed"),
    ],
)
def test_signed_but_invalid_manifests_are_refused(tmp_path, over, code):
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(h.key, **over)
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.REJECTED, code), r
    assert h.downloads == [] and h.installer.installs == []


def test_allowed_host_list_is_enforced(tmp_path):
    h = Harness(tmp_path, allowed_hosts=frozenset({"downloads.other.test"}))
    assert h.updater.run_once().code == "bad_url"


# ---- hash and size ----------------------------------------------------------------------------------------------------------

def test_download_with_the_wrong_bytes_is_rejected_and_never_installed(tmp_path):
    h = Harness(tmp_path, data=ARTIFACT)
    h.data = b"x" * len(ARTIFACT)  # same size, different content: a hash mismatch
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.REJECTED, "hash_mismatch")
    assert h.installer.installs == [] and h.leftovers() == []
    assert h.state.data["failed"]["1.1.0"]["code"] == "hash_mismatch"
    assert not (h.work / "cache").exists() or list((h.work / "cache").iterdir()) == []  # a bad file is never kept


def test_download_with_the_wrong_size_is_rejected(tmp_path):
    h = Harness(tmp_path)
    h.data = ARTIFACT + b"extra"
    assert h.updater.run_once().code == "size_mismatch" and h.installer.installs == []


def test_download_failure_is_an_error_not_an_install(tmp_path):
    h = Harness(tmp_path)

    def boom(url, dest):
        raise OSError("connection reset")

    h.updater._download = boom
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.ERROR, "download_failed") and h.installer.installs == []
    assert h.gate.may_claim() is True  # the gate is released again


def test_an_unprotected_work_directory_stops_the_update_before_anything_is_downloaded(tmp_path):
    def not_private(path):
        raise PermissionError("Users can write here")

    h = Harness(tmp_path, ensure_private_dir=not_private)
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.ERROR, "work_dir_not_private")
    assert h.downloads == [] and h.installer.installs == [] and h.gate.may_claim() is True


def test_artifact_replaced_between_check_and_install_is_caught(tmp_path):
    h = Harness(tmp_path)
    real_cache = h.updater._cache

    def tampering_cache(artifact, m):
        dest = real_cache(artifact, m)
        dest.write_bytes(b"swapped after the first check")  # an attacker with write access to the cache
        return dest

    h.updater._cache = tampering_cache
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.REJECTED, "hash_mismatch") and h.installer.installs == []


# ---- rollback ----------------------------------------------------------------------------------------------------------------

def test_failed_health_check_rolls_back_to_the_previous_version(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: v != "1.1.0")  # the new version never becomes healthy
    h.seed_known_good("1.0.0", b"previous-installer")
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.ROLLED_BACK, "health_check_failed")
    assert [v for v, _ in h.installer.installs] == ["1.1.0", "1.0.0"]
    assert h.installer.installs[-1][1] == b"previous-installer"  # the OLD artifact, re-hashed first
    assert h.installer.installed_version() == "1.0.0" and h.updater.current_version == "1.0.0"
    assert h.state.known_good["version"] == "1.0.0" and h.state.in_flight is None
    assert "1.1.0" in h.state.data["failed"]
    assert h.leftovers() == []


def test_installer_failure_rolls_back_too(tmp_path):
    h = Harness(tmp_path)
    h.installer.fail_versions = {"1.1.0"}
    h.seed_known_good("1.0.0")
    r = h.updater.run_once()
    assert (r.status, r.code) == (core.ROLLED_BACK, "install_failed")
    assert h.installer.installed_version() == "1.0.0"


def test_health_must_hold_for_consecutive_checks_not_just_once(tmp_path):
    seq = iter([True, False, True, False, False, False, False, False, False, False, False])
    h = Harness(tmp_path, healthy=lambda v: True if v == "1.0.0" else next(seq, False))
    h.seed_known_good("1.0.0")
    r = h.updater.run_once()
    assert r.status == core.ROLLED_BACK  # never two in a row


def test_health_check_that_raises_counts_as_unhealthy(tmp_path):
    def boom(v):
        if v == "1.0.0":
            return True  # the previous version is fine; only the new one's probe crashes
        raise RuntimeError("probe crashed")

    h = Harness(tmp_path, healthy=boom)
    h.seed_known_good("1.0.0")
    assert h.updater.run_once().status == core.ROLLED_BACK


def test_health_requires_the_new_version_to_be_the_one_installed(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: True)
    original = h.installer.install

    def install_but_stay_on_old(artifact, version):
        original(artifact, version)
        h.installer.version = "1.0.0"  # the old service came back up: the update did not take

    h.installer.install = install_but_stay_on_old
    h.seed_known_good("1.0.0")
    assert h.updater.run_once().status == core.ROLLED_BACK


def test_rollback_without_a_previous_artifact_blocks_the_host_and_says_so(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: False)  # no known_good seeded: the first update of a fresh install
    r = h.updater.run_once()
    assert r.status == core.ROLLBACK_FAILED
    assert h.gate.acquire_job_slot() is False  # a host in an unknown state claims nothing


def test_rollback_with_a_tampered_previous_artifact_is_refused(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: False)
    path = h.seed_known_good("1.0.0", b"previous-installer")
    path.write_bytes(b"not what was verified")
    assert h.updater.run_once().status == core.ROLLBACK_FAILED
    assert [v for v, _ in h.installer.installs] == ["1.1.0"]  # the tampered file was never installed


def test_rollback_that_does_not_come_back_healthy_is_reported_as_failed(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: False)
    h.seed_known_good("1.0.0")
    assert h.updater.run_once().status == core.ROLLBACK_FAILED


def test_failed_version_is_not_retried_inside_the_cooldown(tmp_path):
    h = Harness(tmp_path, healthy=lambda v: v != "1.1.0")
    h.seed_known_good("1.0.0")
    assert h.updater.run_once().status == core.ROLLED_BACK
    installs = len(h.installer.installs)
    again = h.updater.run_once()
    assert again.status == core.DEFERRED_COOLDOWN and len(h.installer.installs) == installs
    h.t[0] += 7 * 3600
    h.healthy = lambda v: True
    assert h.updater.run_once().status == core.UPDATED


def test_restart_after_a_good_update_promotes_it_and_after_a_dead_one_records_failure(tmp_path):
    h = Harness(tmp_path)
    h.state.data["in_flight"] = {"from": "1.0.0", "to": "1.1.0", "sha256": "ab" * 32, "path": str(tmp_path / "x.pkg"), "started_at": NOW}
    h.installer.version = "1.1.0"
    assert h.updater.resume_after_restart().status == core.UPDATED
    assert h.state.known_good["version"] == "1.1.0" and h.state.in_flight is None

    h2 = Harness(tmp_path / "b")
    h2.state.data["in_flight"] = {"from": "1.0.0", "to": "1.1.0", "sha256": "ab" * 32, "path": "x", "started_at": NOW}
    h2.installer.version = "1.0.0"  # the installer rolled itself back
    r = h2.updater.resume_after_restart()
    assert r.status == core.ROLLED_BACK and "1.1.0" in h2.state.data["failed"] and h2.state.in_flight is None
    assert Harness(tmp_path / "c").updater.resume_after_restart() is None


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    s = UpdateState(p)
    assert s.known_good is None and s.in_flight is None and s.data["failed"] == {}


# ---- never during a job --------------------------------------------------------------------------------------------------------------

def test_no_update_while_a_job_runs_it_drains_and_waits(tmp_path):
    h = Harness(tmp_path)
    assert h.gate.acquire_job_slot()  # a job is running
    r = h.updater.run_once()
    assert r.status == core.DEFERRED_JOB
    assert h.downloads == [] and h.installer.installs == []  # nothing downloaded, nothing installed
    assert h.gate.acquire_job_slot() is False  # draining: no NEW job is accepted
    h.gate.release_job_slot()  # the job finishes
    assert h.updater.run_once().status == core.UPDATED
    assert h.gate.acquire_job_slot() is True  # claims resume after the update


def test_no_job_can_start_while_the_installer_runs(tmp_path):
    h = Harness(tmp_path)
    seen = []
    h.installer.during_install = lambda: seen.append(h.gate.acquire_job_slot())
    assert h.updater.run_once().status == core.UPDATED
    assert seen == [False]


def test_forced_update_still_waits_for_the_running_job(tmp_path):
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(h.key, min_supported="1.1.0")
    h.gate.acquire_job_slot()
    r = h.updater.run_once()
    assert r.status == core.DEFERRED_JOB and r.forced is True and h.installer.installs == []


def test_gate_pieces():
    g = UpdateGate("1.0.0")
    assert g.acquire_job_slot() and g.try_begin_update() is False
    g.release_job_slot()
    assert g.try_begin_update() is True and g.acquire_job_slot() is False
    assert g.try_begin_update() is False  # one update at a time
    g.end_update()
    assert g.may_claim()
    g.release_job_slot()  # extra release never goes negative
    assert g.jobs_running is False


# ---- rings ---------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host,manifest_ring,ok",
    [("general", "general", True), ("general", "early", False), ("general", "canary", False),
     ("early", "general", True), ("early", "early", True), ("early", "canary", False),
     ("canary", "canary", True), ("canary", "early", True), ("canary", "general", True)],
)
def test_a_host_never_takes_a_build_meant_for_a_narrower_ring(tmp_path, host, manifest_ring, ok):
    h = Harness(tmp_path, ring=host, manifest=None)
    h.manifest = make_manifest(h.key, ring=manifest_ring)
    r = h.updater.run_once()
    if ok:
        assert r.status == core.UPDATED
    else:
        assert (r.status, r.code) == (core.REJECTED, "ring_mismatch") and h.installer.installs == []


def test_ring_order_is_narrow_to_wide():
    assert mf.RINGS == ("canary", "early", "general")
    assert mf.ring_rank("canary") < mf.ring_rank("early") < mf.ring_rank("general")


def test_config_ring_validator_accepts_only_known_rings():
    from computecoin_agent.config import AgentConfig

    assert AgentConfig(update_ring=" Early ").update_ring == "early"
    with pytest.raises(ValueError):
        AgentConfig(update_ring="everyone")


# ---- forced-update floor ---------------------------------------------------------------------------------------------------------------

def test_host_below_the_floor_claims_nothing_until_it_has_updated(tmp_path):
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(h.key, version="1.2.0", min_supported="1.1.0")
    h.updater.check()
    assert h.gate.below_floor is True and h.gate.acquire_job_slot() is False and h.gate.may_claim() is False
    assert h.state.data["floor"] == "1.1.0"
    r = h.updater.run_once()
    assert r.status == core.UPDATED and r.forced is True
    assert h.gate.below_floor is False and h.gate.acquire_job_slot() is True


def test_floor_survives_a_restart_and_only_ever_rises(tmp_path):
    h = Harness(tmp_path, manifest=None)
    h.manifest = make_manifest(h.key, version="1.2.0", min_supported="1.1.0")
    h.updater.check()
    h.manifest = make_manifest(h.key, version="1.2.0", min_supported="1.0.0")  # an older, laxer manifest
    h.updater.check()
    assert h.state.data["floor"] == "1.1.0"
    restarted = Harness(tmp_path, manifest=None)  # same state file
    assert restarted.gate.below_floor is True


def test_failed_forced_update_leaves_the_host_out_of_the_pool(tmp_path):
    h = Harness(tmp_path, manifest=None, healthy=lambda v: False)
    h.manifest = make_manifest(h.key, version="1.2.0", min_supported="1.1.0")
    h.seed_known_good("1.0.0")
    r = h.updater.run_once()
    assert r.status in (core.ROLLED_BACK, core.ROLLBACK_FAILED)
    assert h.gate.below_floor is True and h.gate.acquire_job_slot() is False


def test_unforced_update_flag_is_false_at_or_above_the_floor(tmp_path):
    h = Harness(tmp_path)
    assert h.updater.run_once().forced is False


# ---- downgrade protection -----------------------------------------------------------------------------------------------------------------

def test_older_manifest_is_a_refused_downgrade_unless_it_revokes_the_running_version(tmp_path):
    h = Harness(tmp_path, current="1.2.0", manifest=None)
    h.manifest = make_manifest(h.key, version="1.1.0")
    r = h.updater.run_once()
    assert r.status == core.DOWNGRADE_REFUSED and h.installer.installs == []
    h.manifest = make_manifest(h.key, version="1.1.0", revoked_versions=["1.2.0"])
    h.seed_known_good("1.2.0")
    assert h.updater.run_once().status == core.UPDATED  # an authorised, signed rollback to 1.1.0


# ---- versions ---------------------------------------------------------------------------------------------------------------------------------

def test_version_ordering():
    assert compare("1.10.0", "1.9.9") == 1 and compare("1.0.0", "1.0.0") == 0
    assert compare("1.2.0-rc1", "1.2.0") == -1 and compare("1.2.0-rc.2", "1.2.0-rc.10") == -1
    assert compare("1.2.0+build5", "1.2.0") == 0
    for bad in ("1.2", "v1.2.3", "1.2.3.4", "", "1.2.x"):
        with pytest.raises(ValueError):
            parse_version(bad)
