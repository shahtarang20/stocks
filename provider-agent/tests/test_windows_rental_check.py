"""`computecoin-agent rental ...` on a Windows host (EXPERIMENTAL): the Windows check runs inside the managed Linux system through a
scripted wsl.exe, the settings move together, and nothing ever runs a shell or touches the Windows drive. WRITTEN, NOT YET RUN ON WINDOWS."""

from __future__ import annotations

import sys
import tomllib

import pytest
from typer.testing import CliRunner

from computecoin_agent import cli, config as config_mod
from computecoin_agent.platform import PLATFORM_WINDOWS, get_platform, use_platform
from computecoin_agent.rental_check import FAIL, PASS, WARN, summarize
from computecoin_agent.windows import rental_check_windows as rcw
from computecoin_agent.windows.runtime.wsl import SHELL_PROGRAMS, RunResult, WslCli

linux_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="these simulate a Windows host on a Linux test machine, using the real platform's files")

DF_OK = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sdc 262144000 1000 200000000 1% /\n"
DF_FULL = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sdc 262144000 261000000 1000 99% /\n"
LIST_OK = "  NAME          STATE      VERSION\n* computecoin   Running    2\n  Ubuntu        Stopped    2\n"
PROXY = "ghcr.io/x/proxy@sha256:" + "a" * 64


class Wsl:
    """A scripted wsl.exe: answers host commands and in-distro programs, and records every in-distro program."""

    def __init__(self, *, version="WSL version: 2.3.26.0\n", distros=LIST_OK, docker="27.0.1\n", gpu="NVIDIA GeForce RTX 4090, 24564 MiB\n",
                 c_drive="", interop=False, df=DF_OK, run=(0, "CHECK-OK\n", ""), mkdir_ok=True, wrote=True, driver=None, smi_off_path=False):
        self.driver, self.smi_off_path = driver, smi_off_path
        self.version, self.distros, self.docker, self.gpu = version, distros, docker, gpu
        self.c_drive, self.interop, self.df, self.run_result, self.mkdir_ok, self.wrote = c_drive, interop, df, run, mkdir_ok, wrote
        self.programs: list[list[str]] = []

    def __call__(self, argv, *, stdin=None, timeout=None):
        argv = list(argv)
        if "--exec" not in argv:
            if argv[1:] == ["--version"]:
                return RunResult(0, self.version) if self.version is not None else RunResult(1, "", "not found")
            if argv[1:] == ["--list", "--verbose"]:
                return RunResult(0, self.distros)
            return RunResult(1, "", "unexpected host command")
        prog = argv[argv.index("--exec") + 1:]
        self.programs.append(prog)
        name = prog[0]
        if name == "docker" and prog[1] == "version":
            return RunResult(0, self.docker) if self.docker is not None else RunResult(1, "", "Cannot connect")
        if name in ("nvidia-smi", rcw.WSL_SMI):
            if name == "nvidia-smi" and self.smi_off_path:
                return RunResult(127, "", "nvidia-smi: not found")  # not on the path of a program started without a login shell
            if any("driver_version" in a for a in prog):
                return RunResult(0, self.driver) if self.driver is not None else RunResult(1, "", "unknown")
            return RunResult(0, self.gpu) if self.gpu is not None else RunResult(127, "", "not found")
        if name == "ls" and prog[-1] == rcw.WINDOWS_DRIVE:
            return RunResult(0, self.c_drive) if self.c_drive is not None else RunResult(2, "", "No such file or directory")
        if name == "test" and prog[-1] == rcw.INTEROP_FILE:
            return RunResult(0 if self.interop else 1)
        if name == "df":
            return RunResult(0, self.df)
        if name == "mkdir":
            return RunResult(0 if self.mkdir_ok else 1)
        if name == "docker" and prog[1] == "run":
            rc, out, err = self.run_result
            return RunResult(rc, out, err)
        if name == "test" and prog[-1].endswith("/.check"):
            return RunResult(0 if self.wrote else 1)
        if name == "rm":
            return RunResult(0)
        return RunResult(1, "", "unexpected program")

    def ran(self, name, *rest):
        return [p for p in self.programs if p[0] == name and list(p[1:1 + len(rest)]) == list(rest)]


def checks(wsl=None, **kw):
    wsl = wsl or Wsl()
    base = dict(rental_on=True, windows_switch_on=True, egress_proxy_image=PROXY)
    base.update(kw)
    return rcw.run_windows_checks(wsl=WslCli(wsl, "computecoin"), **base), wsl


def by(cs):
    return {c.name: c for c in cs}


# ---- a healthy host -------------------------------------------------------------------------------------------------------
def test_a_healthy_windows_host_passes_every_check():
    cs, _ = checks()
    assert all(c.status == PASS for c in cs), [(c.name, c.status, c.detail) for c in cs if c.status != PASS]
    assert summarize(cs)[0] == 0
    assert {"Docker", "Graphics card", "Windows drive hidden", "Windows programs blocked", "Disk space", "Test container"} <= set(by(cs))


def test_every_check_runs_inside_the_linux_system_and_never_through_a_shell():
    _, wsl = checks()
    assert wsl.programs and all(p[0].replace("\\", "/").rsplit("/", 1)[-1].lower() not in SHELL_PROGRAMS for p in wsl.programs)
    for p in wsl.programs:
        assert isinstance(p, list) and all(isinstance(a, str) for a in p)


# ---- what has to exist first -----------------------------------------------------------------------------------------------
def test_wsl_missing_stops_the_check_with_what_to_do():
    cs, _ = checks(Wsl(version=None))
    assert by(cs)["Windows Subsystem for Linux"].status == FAIL and "wsl --update" in by(cs)["Windows Subsystem for Linux"].fix
    assert "Docker" not in by(cs)


def test_a_missing_linux_system_stops_the_check():
    cs, _ = checks(Wsl(distros="  NAME    STATE    VERSION\n* Ubuntu  Stopped  2\n"))
    assert by(cs)["ComputeCoin Linux system"].status == FAIL and "not installed" in by(cs)["ComputeCoin Linux system"].detail
    assert "Docker" not in by(cs)


def test_a_wsl1_linux_system_is_refused():
    cs, _ = checks(Wsl(distros="  NAME          STATE    VERSION\n* computecoin   Running  1\n"))
    assert by(cs)["ComputeCoin Linux system"].status == FAIL and "wsl --set-version computecoin 2" in by(cs)["ComputeCoin Linux system"].fix


def test_docker_not_answering_stops_the_check():
    cs, wsl = checks(Wsl(docker=None))
    assert by(cs)["Docker"].status == FAIL and "Test container" not in by(cs)
    assert not wsl.ran("docker", "run") and not wsl.ran("mkdir")  # nothing was created


# ---- the graphics card -----------------------------------------------------------------------------------------------------
def test_no_graphics_card_fails_that_check_but_the_container_still_runs_without_the_card():
    cs, wsl = checks(Wsl(gpu=None))
    assert by(cs)["Graphics card"].status == FAIL and "Windows" in by(cs)["Graphics card"].fix
    assert by(cs)["Test container"].status == PASS
    assert "--gpus" not in wsl.ran("docker", "run")[0]
    assert summarize(cs)[0] == 1


def test_a_card_is_passed_to_the_test_container():
    _, wsl = checks()
    argv = wsl.ran("docker", "run")[0]
    assert argv[argv.index("--gpus") + 1] == "all"  # in WSL 2 Docker supports only `--gpus all` (the system sees one card)


# ---- the two checks that only matter on Windows ----------------------------------------------------------------------------
def test_a_visible_windows_drive_is_a_failure_and_a_hidden_one_passes():
    visible, _ = checks(Wsl(c_drive="Users\nWindows\nProgram Files\n"))
    assert by(visible)["Windows drive hidden"].status == FAIL and "automount" in by(visible)["Windows drive hidden"].fix
    for hidden in ("", None):
        ok, _ = checks(Wsl(c_drive=hidden))
        assert by(ok)["Windows drive hidden"].status == PASS


def test_windows_programs_starting_from_inside_is_a_warning():
    cs, _ = checks(Wsl(interop=True))
    assert by(cs)["Windows programs blocked"].status == WARN and "interop" in by(cs)["Windows programs blocked"].fix
    assert summarize(cs)[0] == 0  # a warning does not fail the check


# ---- disk, settings, network -----------------------------------------------------------------------------------------------
def test_too_little_disk_inside_the_linux_system_fails():
    cs, _ = checks(Wsl(df=DF_FULL))
    assert by(cs)["Disk space"].status == FAIL


def test_the_settings_must_both_be_on():
    assert by(checks(rental_on=True, windows_switch_on=True)[0])["Container rental setting"].status == PASS
    for kw in (dict(rental_on=True, windows_switch_on=False), dict(rental_on=False, windows_switch_on=True), dict(rental_on=False, windows_switch_on=False)):
        c = by(checks(**kw)[0])["Container rental setting"]
        assert c.status == WARN and "rental on" in c.fix


def test_a_missing_network_filter_is_a_warning():
    assert by(checks(egress_proxy_image=None)[0])["Network filter"].status == WARN
    assert by(checks(egress_proxy_image="x/y:latest")[0])["Network filter"].status == WARN


# ---- the test container ----------------------------------------------------------------------------------------------------
def test_the_test_container_is_locked_down_and_uses_only_its_own_folder_inside_the_linux_system():
    _, wsl = checks()
    argv = wsl.ran("docker", "run")[0]
    for flag, value in (("--network", "none"), ("--cap-drop", "ALL"), ("--user", "65534:65534"), ("--security-opt", "no-new-privileges")):
        assert argv[argv.index(flag) + 1] == value
    assert "--read-only" in argv and "--privileged" not in argv
    mount = next(a for a in argv if a.startswith("type=bind,source="))
    source = mount.split("source=", 1)[1].split(",", 1)[0]
    assert source.startswith(rcw.SCRATCH_PREFIX) and mount.endswith("target=/workspace")
    assert not any("/mnt/" in a or "docker.sock" in a for a in argv)  # never the Windows drive, never the engine socket


def test_the_test_folder_is_removed_even_when_the_container_fails_and_only_that_folder():
    cs, wsl = checks(Wsl(run=(1, "", "Error: manifest unknown")))
    assert by(cs)["Test container"].status == FAIL and "could not be pulled" in by(cs)["Test container"].detail
    removed = wsl.ran("rm", "-rf", "--")
    assert len(removed) == 1 and removed[0][3].startswith(rcw.SCRATCH_PREFIX) and ".." not in removed[0][3]
    assert removed[0][3] == wsl.ran("mkdir")[0][-1]  # the same folder that was made


def test_no_folder_means_nothing_is_removed():
    cs, wsl = checks(Wsl(mkdir_ok=False))
    assert by(cs)["Workspace folder"].status == FAIL and not wsl.ran("rm") and not wsl.ran("docker", "run")


def test_a_file_the_container_could_not_write_is_a_warning():
    cs, _ = checks(Wsl(wrote=False))
    assert by(cs)["Test container"].status == WARN


# ---- the graphics card on Windows: the path fallback and the driver ----------------------------------------------------------
def test_nvidia_smi_is_found_by_its_full_path_when_it_is_not_on_the_path():
    cs, wsl = checks(Wsl(smi_off_path=True))
    assert by(cs)["Graphics card"].status == PASS
    assert any(p[0] == rcw.WSL_SMI for p in wsl.programs)  # the fallback was used


def test_a_card_that_neither_name_finds_is_still_a_failure():
    cs, _ = checks(Wsl(gpu=None, smi_off_path=True))
    assert by(cs)["Graphics card"].status == FAIL


@pytest.mark.parametrize("driver,image", [("551.23", "nvidia/cuda:12.4.1-base-ubuntu22.04"), ("536.40", "nvidia/cuda:12.2.2-base-ubuntu22.04")])
def test_the_windows_test_image_follows_the_windows_driver(driver, image):
    cs, wsl = checks(Wsl(driver=driver))
    assert by(cs)["NVIDIA driver"].status == PASS and driver in by(cs)["NVIDIA driver"].detail
    assert image in wsl.ran("docker", "run")[0]


def test_an_old_windows_driver_fails_and_a_named_image_is_used_as_given():
    cs, _ = checks(Wsl(driver="472.12"))
    assert by(cs)["NVIDIA driver"].status == FAIL and "535.86" in by(cs)["NVIDIA driver"].fix
    mine = "docker.io/library/python@sha256:" + "c" * 64
    cs2, wsl = checks(Wsl(driver="551.23"), image=mine)
    assert mine in wsl.ran("docker", "run")[0]


# ---- the commands ----------------------------------------------------------------------------------------------------------
class _WindowsNamed:
    """The real platform for files and secrets (so the settings file is really read and written in a temp folder), but it reports that
    this is Windows, which is all the rental command looks at."""

    name = PLATFORM_WINDOWS

    def __init__(self, real):
        self._real = real

    def __getattr__(self, item):
        return getattr(self._real, item)


@pytest.fixture
def win(tmp_path, monkeypatch):
    path = tmp_path / "ProgramData" / "ComputeCoin" / "agent.toml"
    monkeypatch.setattr(config_mod, "default_config_path", lambda: path)
    return path


def _run(*args):
    with use_platform(_WindowsNamed(get_platform())):
        return CliRunner().invoke(cli.app, ["rental", *args])


def _saved(path):
    return tomllib.loads(path.read_text())


@linux_only
def test_on_windows_on_moves_both_switches_and_says_how_to_restart_the_service(win):
    out = _run("on")
    assert out.exit_code == 0 and "EXPERIMENTAL" in out.output and "Restart-Service ComputeCoinAgent" in out.output
    assert _saved(win) == {"allow_container_rental": True, "windows_rental_enabled": True}


@linux_only
def test_on_windows_off_turns_both_off_and_keeps_everything_else(win):
    win.parent.mkdir(parents=True)
    win.write_text('machine_id = "m1"\nallow_container_rental = true\nwindows_rental_enabled = true\n')
    out = _run("off")
    assert out.exit_code == 0 and "now off" in out.output
    assert _saved(win) == {"machine_id": "m1", "allow_container_rental": False, "windows_rental_enabled": False}


@linux_only
def test_on_windows_status_shows_both_switches(win):
    assert "container rental (Windows, experimental): off" in _run("status").output
    _run("on")
    out = _run("status").output
    assert "container rental (Windows, experimental): ON" in out and "opted in: yes" in out and "Windows switch: yes" in out
    win.write_text("allow_container_rental = true\n")
    half = _run("status").output
    assert "experimental): off" in half and "opted in: yes" in half and "Windows switch: no" in half


@linux_only
def test_on_windows_check_uses_the_windows_checks_and_the_exit_code(win, monkeypatch):
    win.parent.mkdir(parents=True)
    win.write_text("allow_container_rental = true\nwindows_rental_enabled = true\n")
    monkeypatch.setattr(rcw, "default_wsl", lambda runner=None: WslCli(Wsl(), "computecoin"))
    good = _run("check")
    assert good.exit_code == 0 and "ComputeCoin Linux system" in good.output and "passed the check" in good.output
    monkeypatch.setattr(rcw, "default_wsl", lambda runner=None: WslCli(Wsl(gpu=None), "computecoin"))
    bad = _run("check")
    assert bad.exit_code == 1 and "[FAIL] Graphics card" in bad.output


@linux_only
def test_linux_rental_commands_are_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".computecoin" / "agent.toml"
    monkeypatch.setattr(config_mod, "default_config_path", lambda: path)
    out = CliRunner().invoke(cli.app, ["rental", "on"])
    assert tomllib.loads(path.read_text()) == {"allow_container_rental": True}  # only the one switch
    assert "Restart the agent for it to take effect" in out.output and "Restart-Service" not in out.output
