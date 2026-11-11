"""Runtime provisioning with fake exec runners (written, never run)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from computecoin_agent.windows.runtime import distro_paths as P
from computecoin_agent.windows.runtime.config_files import hardening_problems, parse_ini, render_wsl_conf
from computecoin_agent.windows.runtime.provision import (
    EXIT_SECURITY, ProvisionError, ProvisionResult, RuntimeNotReady, _exit_for,
)
from computecoin_agent.windows.runtime.wsl import SHELL_PROGRAMS, WslError, argv_exec

from .fakes import ROOTFS, Env, manifest_dict, sha

TEMPLATE = Path(__file__).resolve().parents[2] / "windows" / "wsl.conf.template"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---- happy path and argv shape -------------------------------------------------------------------------------------- #
def test_provision_succeeds_and_import_argv_is_exact(env):
    res = env.prov.provision(env.envelope())
    assert res.ok and res.enrolment_allowed and res.error is None, res.to_dict()
    imports = env.fake.host_calls("--import")
    assert len(imports) == 1
    tar = imports[0][4]
    assert imports[0] == ["wsl.exe", "--import", "computecoin", env.install_dir, tar, "--version", "2"]
    assert tar.endswith(f"rootfs-{sha(ROOTFS)[:16]}.tar.gz")
    assert not Path(tar).exists(), "the downloaded rootfs is deleted after import"


def test_argv_lists_contain_no_shell_strings(env):
    env.prov.provision(env.envelope())
    assert env.fake.calls
    for argv in env.fake.calls:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[0] == "wsl.exe"
    for prog in env.fake.programs():
        name = prog[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        assert name not in SHELL_PROGRAMS, prog
        assert "-c" not in prog or prog[0] in ("python3",) or prog[0].endswith("/python") or "--entrypoint" in prog
        joined = prog[0]
        assert " " not in joined and ";" not in joined and "&" not in joined and "|" not in joined


@pytest.mark.parametrize("shell", ["sh", "bash", "/bin/bash", "cmd.exe", "powershell", "pwsh.exe", "/usr/bin/dash"])
def test_shell_programs_are_refused(shell):
    with pytest.raises(WslError):
        argv_exec("computecoin", [shell, "-c", "echo x"])


def test_no_credential_path_is_ever_referenced(env):
    env.prov.provision(env.envelope())
    env.prov.repair(env.envelope())
    blob = json.dumps(env.fake.calls).lower()
    # pytest's temp folder is named after this test (it contains the word "credential"): drop that prefix first.
    blob = re.sub(r"/[^\"]*?pytest-of-[^\"/]*/pytest-\d+/[^\"/]*", "", blob)
    for word in ("secret", "agent.toml", "credential", "dpapi", "machine_secret"):
        assert word not in blob


# ---- wsl.conf (X6) -------------------------------------------------------------------------------------------------- #
def test_wsl_conf_contents(env):
    env.prov.provision(env.envelope())
    text = env.distro["files"][P.WSL_CONF].decode()
    assert text == render_wsl_conf("ccrunner")
    ini = parse_ini(text)
    assert ini["automount"]["enabled"] == "false"
    assert ini["interop"]["enabled"] == "false"
    assert ini["interop"]["appendwindowspath"] == "false"
    assert ini["boot"]["systemd"] == "true"
    assert ini["user"]["default"] == "ccrunner"
    assert hardening_problems(text) == []


def test_template_file_matches_rendered_conf():
    assert TEMPLATE.read_text(encoding="utf-8").replace("{{runner_user}}", "ccrunner") == render_wsl_conf("ccrunner")


@pytest.mark.parametrize("bad", ["", "[automount]\nenabled=true\n[interop]\nenabled=false\nappendWindowsPath=false\n[boot]\nsystemd=true\n",
                                 "[automount]\nenabled=false\n[interop]\nenabled=true\nappendWindowsPath=false\n[boot]\nsystemd=true\n",
                                 "[automount]\nenabled=false\n[interop]\nenabled=false\n[boot]\nsystemd=true\n"])
def test_hardening_problems_detects_each_missing_key(bad):
    assert hardening_problems(bad)


def test_wsl_conf_restart_terminates_only_our_distro_never_shutdown(env):
    env.prov.provision(env.envelope())
    assert env.fake.host_calls("--terminate"), "the distro is restarted after wsl.conf changes"
    assert all(c[2] == "computecoin" for c in env.fake.host_calls("--terminate"))
    assert not env.fake.host_calls("--shutdown")


# ---- manifest digest / signature aborts ------------------------------------------------------------------------------ #
def test_rootfs_digest_mismatch_aborts_before_import(env):
    env.fetcher.files["https://dl.example/rootfs.tar.gz"] = ROOTFS + b"tampered"
    res = env.prov.provision(env.envelope(rootfs={"url": "https://dl.example/rootfs.tar.gz", "sha256": sha(ROOTFS)}))
    assert not res.ok and res.error.startswith("DigestMismatch")
    assert env.fake.host_calls("--import") == []
    assert env.fake.distros == {}
    assert list(Path(env.staging_dir).glob("rootfs-*")) == [], "the bad download is deleted"
    assert _exit_for(res) == EXIT_SECURITY


def test_wheel_digest_mismatch_aborts_runner_install(env):
    env.fetcher.files["https://dl.example/runner.whl"] = b"evil wheel"
    res = env.prov.provision(env.envelope())
    assert not res.ok and "DigestMismatch" in res.error
    assert env.distro["wheel"] is None
    assert any(s.name == "runner_install" and s.status == "failed" for s in res.steps)


def test_repo_key_fingerprint_mismatch_aborts_docker_install(env):
    env.fake.key_fprs["docker"] = "0" * 40
    res = env.prov.provision(env.envelope())
    assert not res.ok and "DigestMismatch" in res.error
    assert "docker-ce" not in env.distro["pkgs"]


def test_old_wsl_is_a_prerequisite_failure(env):
    env.fake.wsl_version = "1.0.0.0"
    res = env.prov.provision(env.envelope())
    assert not res.ok and "PrerequisiteError" in res.error
    assert env.fake.host_calls("--import") == []


def test_manifest_rollback_is_refused(env):
    assert env.prov.provision(env.envelope(sequence=5)).ok
    res = env.prov.provision(env.envelope(sequence=4))
    assert not res.ok and "rollback refused" in res.error


# ---- idempotency, repair, reset, remove ------------------------------------------------------------------------------ #
def test_second_provision_is_idempotent(env):
    first = env.prov.provision(env.envelope())
    assert first.ok
    installs = env.fake.count("apt-get", "install")
    imports = len(env.fake.host_calls("--import"))
    wrote_conf = sum(1 for p in env.fake.programs() if p[:2] == ["dd", "of=/etc/wsl.conf.cc-tmp"])
    second = env.prov.provision(env.envelope())
    assert second.ok and second.enrolment_allowed
    assert env.fake.count("apt-get", "install") == installs
    assert len(env.fake.host_calls("--import")) == imports
    assert sum(1 for p in env.fake.programs() if p[:2] == ["dd", "of=/etc/wsl.conf.cc-tmp"]) == wrote_conf
    statuses = {s.name: s.status for s in second.steps}
    for name in ("runner_user", "wsl_conf", "limits", "disable_ssh", "docker_engine", "nvidia_toolkit", "docker_config",
                 "runner_install", "egress_proxy", "smoke_image", "import_distro"):
        assert statuses[name] == "skipped", (name, statuses)


def test_provision_resumes_after_a_failed_step(env):
    env.fake.key_fprs["nvidia"] = "0" * 40
    bad = env.prov.provision(env.envelope())
    assert not bad.ok
    env.fake.key_fprs["nvidia"] = "EB693B3035CD5710E231E123A4B469963BF863CC"
    good = env.prov.provision(env.envelope())
    assert good.ok
    statuses = {s.name: s.status for s in good.steps}
    assert statuses["docker_engine"] == "skipped" and statuses["nvidia_toolkit"] == "done"


def test_new_manifest_reruns_only_affected_steps(env):
    assert env.prov.provision(env.envelope(sequence=5)).ok
    d = manifest_dict(sequence=6)
    d["docker"]["packages"]["containerd.io"] = "1.7.23-1"
    from .fakes import envelope
    res = env.prov.provision(envelope(d, env.priv))
    st = {s.name: s.status for s in res.steps}
    assert res.ok
    assert st["runner_user"] == st["wsl_conf"] == st["limits"] == st["runner_install"] == "skipped"
    assert st["docker_engine"] == st["nvidia_toolkit"] == st["docker_config"] == "done"


def test_repair_reruns_every_step_without_unregistering(env):
    assert env.prov.provision(env.envelope()).ok
    installs = env.fake.count("apt-get", "install")
    res = env.prov.repair(env.envelope())
    assert res.ok
    assert env.fake.count("apt-get", "install") == installs + 2  # docker + nvidia again
    assert len(env.fake.host_calls("--import")) == 1
    assert env.fake.host_calls("--unregister") == []
    assert all(s.status in ("done", "skipped") for s in res.steps) and not any(s.status == "skipped" for s in res.steps if s.name != "import_distro" and s.name != "wslconfig" and s.name != "preflight")


def test_remove_is_idempotent_and_deletes_install_dir(env):
    assert env.prov.provision(env.envelope()).ok
    Path(env.install_dir).mkdir(parents=True, exist_ok=True)
    (Path(env.install_dir) / "ext4.vhdx").write_bytes(b"x")
    r1 = env.prov.remove()
    assert r1.ok and env.fake.distros == {} and not Path(env.install_dir).exists()
    r2 = env.prov.remove()
    assert r2.ok and len(env.fake.host_calls("--unregister")) == 1


def test_remove_refuses_a_shallow_or_unrelated_install_dir(tmp_path):
    e = Env(tmp_path, install_dir="C:\\Windows")
    with pytest.raises(ProvisionError):
        e.prov._remove_install_dir(ProvisionResult())


def test_reset_with_bad_manifest_leaves_the_distro_untouched(env):
    assert env.prov.provision(env.envelope()).ok
    from .fakes import new_keypair, envelope
    other, _ = new_keypair()
    res = env.prov.reset(envelope(manifest_dict(), other))
    assert not res.ok and "untouched" in res.error
    assert "computecoin" in env.fake.distros and env.fake.host_calls("--unregister") == []


def test_reset_replaces_the_distro(env):
    assert env.prov.provision(env.envelope()).ok
    env.distro["files"]["/home/ccrunner/marker"] = b"old data"
    res = env.prov.reset(env.envelope())
    assert res.ok
    assert "/home/ccrunner/marker" not in env.distro["files"]
    assert len(env.fake.host_calls("--import")) == 2 and len(env.fake.host_calls("--unregister")) == 1


def test_concurrent_run_is_refused(env):
    Path(env.staging_dir).mkdir(parents=True)
    (Path(env.staging_dir) / "provision.lock").write_text("123")
    res = env.prov.provision(env.envelope())
    assert not res.ok and "in progress" in res.error and env.fake.calls == []


# ---- limits ------------------------------------------------------------------------------------------------------------ #
def test_limits_slice_and_disk_cap(env):
    assert env.prov.provision(env.envelope()).ok
    slice_text = env.distro["files"][P.SLICE_UNIT].decode()
    assert "MemoryMax=12288M" in slice_text and "CPUQuota=600%" in slice_text and "MemorySwapMax=0" in slice_text
    assert ["wsl.exe", "--manage", "computecoin", "--resize", "120GB"] in env.fake.calls
    daemon = json.loads(env.distro["files"][P.DOCKER_DAEMON_JSON].decode())
    assert daemon["cgroup-parent"] == "computecoin.slice"


def test_missing_disk_cap_fails_closed_by_default(env):
    env.fake.resize_fail = True
    res = env.prov.provision(env.envelope())
    assert not res.ok and "cannot cap the virtual disk" in res.error


def test_disk_cap_failure_can_be_allowed_knowingly(tmp_path):
    e = Env(tmp_path, require_disk_cap=False)
    e.fake.resize_fail = True
    res = e.prov.provision(e.envelope())
    assert res.ok
    assert "NOT applied" in next(s for s in res.steps if s.name == "limits").detail


def test_apply_limits_rewrites_slice_only_when_changed(env):
    from computecoin_agent.windows.runtime.config_files import ResourceLimits

    assert env.prov.provision(env.envelope()).ok
    same = env.prov.apply_limits(ResourceLimits(memory_mb=12288, cpus=6, disk_gb=120))
    assert same.status == "skipped"
    changed = env.prov.apply_limits(ResourceLimits(memory_mb=8192, cpus=2, disk_gb=120))
    assert changed.status == "done" and "MemoryMax=8192M" in env.distro["files"][P.SLICE_UNIT].decode()


def test_wslconfig_is_not_touched_by_default(tmp_path):
    cfg = tmp_path / ".wslconfig"
    cfg.write_text("[wsl2]\nmemory=2GB\n", encoding="utf-8")
    e = Env(tmp_path, wslconfig_path=str(cfg))
    res = e.prov.provision(e.envelope())
    assert res.ok and not res.restart_required
    assert cfg.read_text(encoding="utf-8") == "[wsl2]\nmemory=2GB\n"


def test_wslconfig_add_missing_keeps_owner_values_and_makes_a_backup(tmp_path):
    from computecoin_agent.windows.runtime.config_files import ResourceLimits

    cfg = tmp_path / ".wslconfig"
    original = "[wsl2]\nmemory=2GB\nswap=0\n\n[experimental]\nautoMemoryReclaim=gradual\n"
    cfg.write_text(original, encoding="utf-8")
    e = Env(tmp_path, wslconfig_path=str(cfg), wslconfig_policy="add_missing",
            limits=ResourceLimits(memory_mb=12288, cpus=6, disk_gb=120, vm_memory_mb=16384, vm_processors=8))
    res = e.prov.provision(e.envelope())
    assert res.ok and res.restart_required
    text = cfg.read_text(encoding="utf-8")
    assert "memory=2GB" in text and "memory=16384MB" not in text, "the owner's memory value wins"
    assert "processors=8" in text and "swap=0" in text and "autoMemoryReclaim=gradual" in text
    assert Path(str(cfg) + ".computecoin.bak").read_text(encoding="utf-8") == original
    assert not e.fake.host_calls("--shutdown")
    e.prov.remove()
    assert cfg.read_text(encoding="utf-8") == original, "uninstall removes exactly what was added"


# ---- the enrolment gate -------------------------------------------------------------------------------------------------- #
def test_smoke_test_failure_blocks_enrolment(env):
    env.fake.smoke_fail = True
    res = env.prov.provision(env.envelope())
    assert not res.ok and not res.enrolment_allowed
    assert res.smoke is not None and not res.smoke.ok
    assert "gpu_smoke_test" in res.error
    gate = env.prov.enrolment_gate(env.envelope())
    assert not gate.allowed
    with pytest.raises(RuntimeNotReady):
        env.prov.assert_ready_for_enrolment(env.envelope())


def test_zero_gpus_listed_also_blocks_enrolment(env):
    env.fake.gpus = []
    res = env.prov.provision(env.envelope())
    assert not res.enrolment_allowed


def test_gate_is_live_not_read_from_the_state_file(env):
    assert env.prov.provision(env.envelope()).enrolment_allowed
    assert env.prov.enrolment_gate(env.envelope()).allowed
    env.fake.smoke_fail = True
    assert not env.prov.enrolment_gate(env.envelope()).allowed


def test_failed_health_check_blocks_enrolment(env):
    env.fake.leak_mount = True
    res = env.prov.provision(env.envelope())
    assert not res.enrolment_allowed
    assert any(c.name == "no_windows_mounts" and not c.ok for c in res.health.checks)


def test_interop_present_blocks_enrolment(env):
    env.fake.interop_present = True
    assert not env.prov.provision(env.envelope()).enrolment_allowed


def test_listening_ssh_port_blocks_enrolment(env):
    env.fake.listeners = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
    res = env.prov.provision(env.envelope())
    assert not res.enrolment_allowed
    assert any(c.name == "no_network_listeners" and not c.ok for c in res.health.checks)


def test_ssh_units_are_masked(env):
    env.prov.provision(env.envelope())
    assert ["systemctl", "mask", "ssh.service", "ssh.socket", "sshd.service"] in env.fake.programs()


def test_runner_code_is_root_owned_and_user_is_unprivileged(env):
    env.prov.provision(env.envelope())
    progs = env.fake.programs()
    assert ["chown", "-R", "root:root", "/opt/computecoin"] in progs
    assert ["passwd", "--lock", "ccrunner"] in progs
    assert any(p[:2] == ["useradd", "--system"] and "/usr/sbin/nologin" in p for p in progs)


def test_pinned_versions_reach_apt_and_are_held(env):
    env.prov.provision(env.envelope())
    installs = [p for p in env.fake.programs() if p[:1] == ["env"] and "install" in p]
    flat = " ".join(" ".join(p) for p in installs)
    assert "docker-ce=5:27.3.1-1~ubuntu.24.04~noble" in flat and "nvidia-container-toolkit=1.17.0-1" in flat
    assert ["apt-mark", "hold", "docker-ce", "docker-ce-cli", "containerd.io"] in env.fake.programs()


# ---- W8 egress proxy ---------------------------------------------------------------------------------------------------- #
def test_egress_proxy_image_is_pulled_and_recorded(env):
    from .fakes import PROXY

    assert env.prov.provision(env.envelope()).ok
    assert ["docker", "pull", PROXY] in env.fake.programs()
    assert env.distro["files"][P.RUNTIME_ENV_FILE].decode() == f"COMPUTECOIN_EGRESS_PROXY_IMAGE={PROXY}\n"


def test_egress_verification_passes_with_denials(env):
    res = env.prov.provision(env.envelope())
    assert [c.name for c in res.egress] == ["egress_E1_no_direct_route", "egress_E2_metadata_denied",
                                            "egress_E3_unlisted_denied", "egress_E4_denials_logged"]
    assert all(c.ok for c in res.egress)


def test_egress_verification_failure_does_not_block_enrolment_but_is_reported(env):
    env.fake.egress_probe = "E1 REACHED\nE2 HTTP/1.1 200 Connection Established\nE3 HTTP/1.1 403 Forbidden\n"
    res = env.prov.provision(env.envelope())
    assert res.enrolment_allowed
    failed = {c.name for c in res.egress if not c.ok}
    assert {"egress_E1_no_direct_route", "egress_E2_metadata_denied"} <= failed
    assert json.loads(env.distro["files"][P.STATE_FILE])["verified"]["egress_verified"] is False
