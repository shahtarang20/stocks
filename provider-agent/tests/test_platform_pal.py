"""Cross-platform plan W0: the Platform Abstraction Layer, with the fake platform. WRITTEN, NOT YET RUN.

Shared agent code is exercised against ``platform.fake`` (no /proc, no real chmod) and against the Windows stubs, which
must fail closed with ``PlatformUnsupported``. The Linux implementation is checked against real temp files.
"""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

from computecoin_agent import config as cfg
from computecoin_agent import hardware as hw
from computecoin_agent import isolation as iso
from computecoin_agent import jobmonitor as jm
from computecoin_agent import private_kit as pk
from computecoin_agent.platform import (
    PLATFORM_LINUX,
    PLATFORM_NAME,
    PLATFORM_WINDOWS,
    Platform,
    PlatformUnsupported,
    get_platform,
    reset_platform,
    set_platform,
    use_platform,
)
from computecoin_agent.platform.fake import FakeFifo, FakeRam, make_fake_platform
from computecoin_agent.platform.linux import build_linux_platform
from computecoin_agent.platform.windows import WindowsPaths, build_windows_platform

MEMBERS = (
    "paths", "secrets", "service", "signals", "fifo", "owner", "ram", "hardware", "gpu", "power", "activity",
    "runtime", "updater",
)


# --------------------------------------------------------------------------- factory
def test_get_platform_has_every_member_and_a_known_name():
    plat = get_platform()
    assert isinstance(plat, Platform) and plat.name in (PLATFORM_LINUX, PLATFORM_WINDOWS)
    assert PLATFORM_NAME in (PLATFORM_LINUX, PLATFORM_WINDOWS)
    for member in MEMBERS:
        assert getattr(plat, member) is not None, member


def test_native_builders_name_themselves():
    assert build_linux_platform().name == PLATFORM_LINUX
    assert build_windows_platform({}).name == PLATFORM_WINDOWS


def test_use_platform_swaps_and_restores():
    before = get_platform()
    fake = make_fake_platform(name=PLATFORM_WINDOWS)
    with use_platform(fake) as active:
        assert active is fake and get_platform() is fake
    assert get_platform() is before


def test_set_and_reset_platform():
    fake = make_fake_platform()
    try:
        set_platform(fake)
        assert get_platform() is fake
    finally:
        reset_platform()
    assert get_platform() is not fake


def test_platform_info_is_the_heartbeat_platform_object():
    plat = make_fake_platform(name=PLATFORM_WINDOWS, supervisor_version="1.2.3")
    info = plat.info(agent_version="9.9.9")
    assert info["os"] == "windows" and info["runtime_backend"] == "wsl_engine"
    assert info["agent_version"] == "9.9.9" and info["supervisor_version"] == "1.2.3"
    assert set(info) <= {"os", "os_version", "runtime_backend", "agent_version", "supervisor_version"}
    assert "supervisor_version" not in make_fake_platform().info()


def test_platform_unsupported_text_names_the_platform_and_the_hint():
    err = PlatformUnsupported("Secrets.write_file", platform_name="windows", hint="DPAPI is W2")
    assert "Secrets.write_file" in str(err) and "windows" in str(err) and "DPAPI is W2" in str(err)


# --------------------------------------------------------------------------- Windows stubs fail closed
def test_windows_stubs_raise_platform_unsupported(tmp_path):
    plat = build_windows_platform({})
    with pytest.raises(PlatformUnsupported):
        plat.secrets.write_file(tmp_path / "s", b"x")
    with pytest.raises(PlatformUnsupported):
        plat.secrets.read_file(tmp_path / "s")
    with pytest.raises(PlatformUnsupported):
        plat.fifo.create(tmp_path / "p")
    with pytest.raises(PlatformUnsupported):
        plat.ram.candidates()
    with pytest.raises(PlatformUnsupported):
        plat.service.install("computecoin", ["x"])
    # (the WSL engine now delegates to windows/runtime, so docker_command() returns a command instead of raising)
    assert isinstance(plat.runtime.docker_command(), list)
    with pytest.raises(PlatformUnsupported):
        plat.paths.restrict_file(tmp_path / "s")
    with pytest.raises(PlatformUnsupported):
        plat.paths.set_mode(tmp_path / "s", 0o600)
    with pytest.raises(PlatformUnsupported):
        plat.updater.check()
    assert plat.runtime.engine_available() is False
    assert plat.owner.uid() is None
    assert plat.power.snapshot().reboot_pending is None
    assert plat.activity.snapshot().owner_active is None


def test_windows_paths_use_programdata_and_localappdata():
    paths = WindowsPaths({"ProgramData": "/pd", "LOCALAPPDATA": "/lad"})
    assert str(paths.config_dir()).replace("\\", "/") == "/pd/ComputeCoin"
    assert str(paths.data_dir()).replace("\\", "/") == "/pd/ComputeCoin"
    assert str(paths.cache_dir()).replace("\\", "/") == "/lad/ComputeCoin/cache"
    assert str(WindowsPaths({"ProgramData": "/pd", "COMPUTECOIN_CONFIG_DIR": "/cfg"}).config_dir()).replace("\\", "/") == "/cfg"


def test_windows_hardware_uses_psutil_and_is_never_bare_metal(monkeypatch):
    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(total=16 * 1024 * 1024 * 1024),
        cpu_count=lambda logical=True: 12 if logical else 6,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    probe = build_windows_platform({}).hardware
    assert probe.memory_total_mb() == 16384
    assert probe.cpu_counts()[1] == 6
    # privacy-relevant probes stay False (X5): no TPM, no confidential device, never bare metal
    assert probe.has_tpm_device() is False and probe.has_confidential_device() is False
    with use_platform(build_windows_platform({})):
        assert iso.probe_bare_metal() is False
        assert iso.probe_confidential_computing() is False
        assert iso.probe_memory_encryption() is False


def test_windows_never_gets_a_ram_workdir(tmp_path):
    with use_platform(build_windows_platform({})):
        with pytest.raises(pk.PrivateKitError) as exc:
            pk.ram_workdir("t-", str(tmp_path))
    assert exc.value.code == "ram_workdir_unavailable"


# --------------------------------------------------------------------------- shared code on the fake
def test_ram_workdir_uses_platform_ram_and_sets_mode(tmp_path):
    fake = make_fake_platform()
    fake.ram = FakeRam({str(tmp_path): "tmpfs"})
    with use_platform(fake):
        d = pk.ram_workdir("t-", str(tmp_path))
    assert os.path.isdir(d) and os.path.dirname(d) == str(tmp_path)
    assert (d, 0o700) in fake.paths.modes


def test_ram_workdir_refuses_when_platform_says_not_ram(tmp_path):
    fake = make_fake_platform()
    fake.ram = FakeRam({str(tmp_path): "ext4"})
    with use_platform(fake), pytest.raises(pk.PrivateKitError) as exc:
        pk.ram_workdir("t-", str(tmp_path))
    assert exc.value.code == "ram_workdir_unavailable"


def test_ram_workdir_default_candidates_come_from_the_platform(tmp_path):
    ram_dir = tmp_path / "ram"
    ram_dir.mkdir()
    fake = make_fake_platform()
    fake.ram = FakeRam({str(ram_dir): "tmpfs"}, dirs=[str(tmp_path / "missing"), str(ram_dir)])
    with use_platform(fake):
        d = pk.ram_workdir("t-")
    assert os.path.dirname(d) == str(ram_dir)


def test_wipe_dir_sets_modes_through_the_platform(tmp_path):
    (tmp_path / "f").write_bytes(b"secret")
    fake = make_fake_platform()
    with use_platform(fake):
        pk.wipe_dir(str(tmp_path))
    assert any(m == 0o600 for _p, m in fake.paths.modes)


def test_job_stream_fifo_comes_from_the_platform(tmp_path):
    fake = make_fake_platform()
    with use_platform(fake):
        m = jm.JobMonitor("http://x", "m", "s", "11111111-1111-1111-1111-111111111111", gpu_sampler=lambda: {})
        m._open_fifo(str(tmp_path))
        assert fake.fifo.created == [(os.path.join(str(tmp_path), jm.STREAM_FILE), 0o622)]
        assert m._fifo_fd is not None and m._stream_dead is False
        m._fifo_fd = None  # the fake descriptor is not a real fd


def test_job_stream_never_falls_back_when_the_platform_has_no_pipe(tmp_path):
    fake = make_fake_platform()
    fake.fifo = FakeFifo()
    fake.fifo.fail = True
    with use_platform(fake):
        m = jm.JobMonitor("http://x", "m", "s", "11111111-1111-1111-1111-111111111111", gpu_sampler=lambda: {})
        m._open_fifo(str(tmp_path))
    assert m._fifo_fd is None and m._stream_dead is True
    assert not (tmp_path / jm.STREAM_FILE).exists()  # no plaintext fallback file


def test_job_stream_fails_closed_on_windows(tmp_path):
    with use_platform(build_windows_platform({})):
        m = jm.JobMonitor("http://x", "m", "s", "11111111-1111-1111-1111-111111111111", gpu_sampler=lambda: {})
        m._open_fifo(str(tmp_path))
    assert m._stream_dead is True and m._fifo_fd is None


def test_config_save_goes_through_secrets_and_paths(tmp_path):
    fake = make_fake_platform(root=tmp_path)
    target = tmp_path / "cfg" / "agent.toml"
    with use_platform(fake):
        out = cfg.AgentConfig(machine_id="m-1", machine_secret="msk_abc").save(target)
    assert out == target
    assert str(target.parent) in fake.paths.restricted_dirs
    assert b"machine_secret" in fake.secrets.store[str(target)]
    assert not target.exists()  # the fake keeps it in memory: nothing was written by shared code


def test_config_save_refuses_on_windows_until_a_secret_store_exists(tmp_path):
    with use_platform(build_windows_platform({})):
        with pytest.raises(PlatformUnsupported):
            cfg.AgentConfig(machine_id="m-1", machine_secret="msk_abc").save(tmp_path / "agent.toml")
    assert not (tmp_path / "agent.toml").exists()  # never a plain-text secret on Windows (X7)


def test_default_config_dir_comes_from_the_platform(tmp_path):
    with use_platform(make_fake_platform(root=tmp_path)):
        assert cfg.default_config_dir() == tmp_path / "config"


def test_detect_ram_falls_back_to_the_platform_probe_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    with use_platform(make_fake_platform(memory_mb=4096)):
        assert hw.detect_ram_mb() == 4096
    with use_platform(make_fake_platform(memory_mb=None)):
        assert hw.detect_ram_mb() == 0


def test_detect_cpu_model_comes_from_the_platform():
    with use_platform(make_fake_platform(cpu_model="Ryzen Test")):
        assert hw.detect_cpu().model == "Ryzen Test"
    with use_platform(build_windows_platform({})):
        hw.detect_cpu()  # must not raise on a platform without /proc


def test_isolation_probes_read_the_platform_probe():
    with use_platform(make_fake_platform(confidential_device=True, tpm=True, iommu=True, lockdown=True)):
        assert iso.probe_confidential_computing() is True
        assert iso.probe_tpm2() is True
        assert iso.probe_iommu() is True and iso.probe_kernel_lockdown() is True
    with use_platform(make_fake_platform()):
        assert iso.probe_confidential_computing() is False
        assert iso.probe_iommu() is False and iso.probe_kernel_lockdown() is False


def test_bare_metal_and_memory_encryption_use_platform_text():
    with use_platform(make_fake_platform(cpuinfo="flags : fpu hypervisor")):
        assert iso.probe_bare_metal() is False
    with use_platform(make_fake_platform(cpuinfo="flags : fpu", dmi=["Z790", "MSI"])):
        assert iso.probe_bare_metal() is True
    with use_platform(make_fake_platform(cpuinfo="flags : fpu sme", kernel_log="AMD Memory Encryption Features active: SME")):
        assert iso.probe_memory_encryption() is True


def test_executor_amd_probe_uses_the_platform(monkeypatch):
    from computecoin_agent import executor

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    with use_platform(make_fake_platform(kfd=True)):
        assert executor._default_amd_available() is True
    with use_platform(make_fake_platform(kfd=False)):
        assert executor._default_amd_available() is False


def test_tsm_report_refuses_an_empty_root():
    from computecoin_agent import tee

    with pytest.raises(tee.TeeError):
        tee.tsm_report(b"\x00" * 64, expect_provider="sev_guest", root="")


def test_fake_signals_deliver_to_the_installed_handler():
    fake = make_fake_platform()
    seen: list[str] = []
    fake.signals.install(seen.append)
    fake.signals.fire("SIGTERM")
    fake.signals.restore()
    assert seen == ["SIGTERM"] and fake.signals.handler is None


# --------------------------------------------------------------------------- Linux implementation (real temp files)
@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_linux_secrets_file_is_owner_only_and_round_trips(tmp_path):
    plat = build_linux_platform()
    target = tmp_path / "s.toml"
    plat.secrets.write_file(target, b"machine_secret = 'x'")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert plat.secrets.read_file(target) == b"machine_secret = 'x'"
    plat.secrets.write_file(target, b"shorter")  # truncates, stays 0600
    assert plat.secrets.read_file(target) == b"shorter" and stat.S_IMODE(os.stat(target).st_mode) == 0o600


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_linux_paths_restrict_and_set_mode(tmp_path):
    paths = build_linux_platform().paths
    d = tmp_path / "d"
    d.mkdir()
    paths.restrict_dir(d)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    f = tmp_path / "f"
    f.write_text("x")
    paths.restrict_file(f)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    paths.set_mode(f, 0o644)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o644
    paths.restrict_dir(tmp_path / "missing")  # best effort, never raises
    with pytest.raises(OSError):
        paths.set_mode(tmp_path / "missing", 0o600)  # like os.chmod


def test_linux_config_dir_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTECOIN_CONFIG_DIR", str(tmp_path / "c"))
    assert build_linux_platform().paths.config_dir() == tmp_path / "c"
    monkeypatch.delenv("COMPUTECOIN_CONFIG_DIR")
    assert build_linux_platform().paths.config_dir().name == ".computecoin"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_linux_fifo_is_a_fifo_opened_nonblocking(tmp_path):
    fifo = build_linux_platform().fifo
    p = tmp_path / "stream"
    fifo.create(p, 0o622)
    assert stat.S_ISFIFO(os.stat(p).st_mode) and stat.S_IMODE(os.stat(p).st_mode) == 0o622
    fd = fifo.open_reader(p)
    try:
        assert os.get_blocking(fd) is False
    finally:
        os.close(fd)


def test_linux_mount_type_picks_the_longest_mount(tmp_path):
    base = os.path.realpath(tmp_path)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"rootfs / ext4 rw 0 0\ntmpfs {base}/ram tmpfs rw 0 0\ntmpfs {base}/ram/sub ramfs rw 0 0\n")
    ram = build_linux_platform().ram
    assert ram.mount_type(f"{base}/ram/x", str(mounts)) == "tmpfs"
    assert ram.mount_type(f"{base}/ram/sub/y", str(mounts)) == "ramfs"
    assert ram.mount_type(f"{base}/other", str(mounts)) == "ext4"
    assert ram.mount_type(f"{base}/x", str(tmp_path / "no-such-file")) is None


def test_linux_ram_candidates_match_the_old_defaults():
    ram = build_linux_platform().ram
    assert ram.candidates("/custom") == ["/custom"]
    cands = ram.candidates()
    assert cands[0] == "/dev/shm" and cands[1].startswith("/run/user/")


def test_linux_lockdown_and_iommu_probes_read_given_paths(tmp_path):
    probe = build_linux_platform().hardware
    lock = tmp_path / "lockdown"
    lock.write_text("none [integrity] confidentiality")
    assert probe.kernel_lockdown_active(str(lock)) is True
    lock.write_text("[none] integrity confidentiality")
    assert probe.kernel_lockdown_active(str(lock)) is False
    empty = tmp_path / "iommu"
    empty.mkdir()
    assert probe.iommu_active(str(empty)) is False
    (empty / "dmar0").mkdir()
    assert probe.iommu_active(str(empty)) is True


def test_linux_amd_render_node_by_minor_then_by_bdf(tmp_path):
    probe = build_linux_platform().hardware
    dev = tmp_path / "dri"
    dev.mkdir()
    (dev / "renderD129").touch()
    assert probe.amd_render_node(129, None, dev_root=str(dev)) == str(dev / "renderD129")
    assert probe.amd_render_node(130, None, dev_root=str(dev)) is None  # never guessed
    sysd = tmp_path / "pci" / "0000:03:00.0" / "drm"
    sysd.mkdir(parents=True)
    (sysd / "renderD129").mkdir()
    assert probe.amd_render_node(None, "0000:03:00.0", sys_root=str(tmp_path / "pci"), dev_root=str(dev)) == str(dev / "renderD129")
    (sysd / "renderD130").mkdir()  # two render nodes: ambiguous, so None
    assert probe.amd_render_node(None, "0000:03:00.0", sys_root=str(tmp_path / "pci"), dev_root=str(dev)) is None
