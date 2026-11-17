"""``computecoin-agent rental check``: every outcome in plain words, with no Docker, GPU or network used."""

from __future__ import annotations

import os
import re
import sys
import subprocess
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from computecoin_agent import cli
from computecoin_agent import rental_check as rc
from computecoin_agent.rental import GIB

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="container rental is Linux-only (a Windows host never reports the flag)")

PROXY = "ghcr.io/x/proxy@sha256:" + "a" * 64


class Docker:
    """A fake `docker`/`nvidia-smi`. ``run_hook(argv)`` can plant files; ``run`` is (returncode, stdout, stderr) for `docker run`."""

    def __init__(self, *, version=(0, "27.1.1", ""), smi=(0, "NVIDIA GeForce RTX 3060, 12288 MiB", ""), run=(0, "GPU 0: RTX 3060\nCHECK-OK", ""),
                 plant=True):
        self.version, self.smi, self.run, self.plant, self.calls = version, smi, run, plant, []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "version"]:
            return types.SimpleNamespace(returncode=self.version[0], stdout=self.version[1], stderr=self.version[2])
        if argv[0] == "nvidia-smi":
            return types.SimpleNamespace(returncode=self.smi[0], stdout=self.smi[1], stderr=self.smi[2])
        if argv[:2] == ["docker", "run"]:
            if self.plant and self.run[0] == 0:
                src = next(a for a in argv if a.startswith("type=bind,source=")).split("source=", 1)[1].split(",", 1)[0]
                open(os.path.join(src, ".check"), "w").write("x")
            return types.SimpleNamespace(returncode=self.run[0], stdout=self.run[1], stderr=self.run[2])
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def go(tmp_path, d=None, *, allowed=True, proxy=PROXY, image=None, which=lambda n: "/usr/bin/" + n, free=100 * GIB):
    d = d or Docker()
    checks = rc.run_checks(allowed=allowed, root=str(tmp_path / "rental"), image=image, runner=d, which=which,
                           egress_proxy_image=proxy, free_bytes=lambda p: free)
    return checks, d


def by(checks):
    return {c.name: c for c in checks}


def test_a_healthy_computer_passes_everything(tmp_path):
    checks, d = go(tmp_path)
    assert all(c.status == rc.PASS for c in checks), [(c.name, c.status, c.detail) for c in checks]
    code, text = rc.summarize(checks)
    assert code == 0 and "passed the check" in text and "RTX 3060" in text
    assert os.listdir(tmp_path / "rental") == []


def test_the_test_container_is_as_locked_down_as_a_rental_and_is_cleaned_up(tmp_path):
    _checks, d = go(tmp_path)
    run = next(c for c in d.calls if c[:2] == ["docker", "run"])
    for flag in ("--network", "--read-only", "--cap-drop", "--security-opt", "--user", "--gpus"):
        assert flag in run
    assert run[run.index("--network") + 1] == "none" and run[run.index("--user") + 1] == "65534:65534"
    assert "--privileged" not in run and "-p" not in run and run[run.index("--cap-drop") + 1] == "ALL"
    assert os.listdir(tmp_path / "rental") == []  # the throw-away workspace is gone


def test_a_custom_image_is_used(tmp_path):
    _checks, d = go(tmp_path, image="ghcr.io/me/img@sha256:" + "b" * 64)
    run = next(c for c in d.calls if c[:2] == ["docker", "run"])
    assert "ghcr.io/me/img@sha256:" + "b" * 64 in run and rc.CHECK_IMAGE not in run


def test_docker_missing(tmp_path):
    checks, _ = go(tmp_path, which=lambda n: None if n == "docker" else "/usr/bin/" + n)
    c = by(checks)["Docker"]
    assert c.status == rc.FAIL and "not installed" in c.detail and rc.summarize(checks)[0] == 1
    assert "Test container" not in by(checks)  # stops early: nothing else can work


def test_docker_not_running_or_not_allowed(tmp_path):
    checks, _ = go(tmp_path, Docker(version=(1, "", "Cannot connect to the Docker daemon")))
    c = by(checks)["Docker"]
    assert c.status == rc.FAIL and "docker group" in c.fix and "Test container" not in by(checks)


def test_no_graphics_card_is_a_failure_with_the_fix(tmp_path):
    checks, d = go(tmp_path, Docker(smi=(1, "", "NVIDIA-SMI has failed")))
    assert by(checks)["Graphics card"].status == rc.FAIL and "driver" in by(checks)["Graphics card"].fix
    run = next(c for c in d.calls if c[:2] == ["docker", "run"])
    assert "--gpus" not in run  # still tests that a container starts and writes
    assert rc.summarize(checks)[0] == 1


def test_no_nvidia_smi_program(tmp_path):
    checks, _ = go(tmp_path, which=lambda n: None if n == "nvidia-smi" else "/usr/bin/" + n)
    assert by(checks)["Graphics card"].status == rc.FAIL


@pytest.mark.parametrize("stderr,expect", [
    ("docker: Error response from daemon: could not select device driver \"\" with capabilities: [[gpu]]", "NVIDIA Container Toolkit"),
    ("Unable to find image 'x' locally\ndocker: Error response from daemon: pull access denied for x", "could not be pulled"),
    ("permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock", "not allowed to use Docker"),
    ("touch: cannot touch '/workspace/.check': Permission denied", "could not write"),
    ("timed out", "took too long"),
])
def test_each_container_failure_is_explained(tmp_path, stderr, expect):
    checks, _ = go(tmp_path, Docker(run=(125, "", stderr)))
    c = by(checks)["Test container"]
    assert c.status == rc.FAIL and expect in (c.detail + c.fix), c


def test_an_unknown_container_failure_still_gives_something_to_run(tmp_path):
    checks, _ = go(tmp_path, Docker(run=(1, "", "weird")))
    c = by(checks)["Test container"]
    assert c.status == rc.FAIL and "nvidia-smi" in c.fix


def test_a_run_that_says_ok_but_made_no_file_is_a_warning_not_a_pass(tmp_path):
    checks, _ = go(tmp_path, Docker(plant=False))
    assert by(checks)["Test container"].status == rc.WARN and rc.summarize(checks)[0] == 0


def test_not_enough_disk(tmp_path):
    checks, _ = go(tmp_path, free=GIB)
    assert by(checks)["Disk space"].status == rc.FAIL and rc.summarize(checks)[0] == 1


def test_no_network_filter_is_a_warning_that_says_what_it_costs(tmp_path):
    for proxy in (None, "ghcr.io/x/proxy:latest"):
        checks, _ = go(tmp_path, proxy=proxy)
        c = by(checks)["Network filter"]
        assert c.status == rc.WARN and "templates" in c.detail and "egress_proxy_image" in c.fix
        assert rc.summarize(checks)[0] == 0


def test_rental_switched_off_is_a_warning_with_the_command(tmp_path):
    checks, _ = go(tmp_path, allowed=False)
    c = by(checks)["Container rental setting"]
    assert c.status == rc.WARN and "rental on" in c.fix


def test_windows_is_refused_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "get_platform", lambda: types.SimpleNamespace(name="windows"))
    checks, d = go(tmp_path)
    assert [c.status for c in checks] == [rc.FAIL] and d.calls == []


def test_the_docker_command_timing_out_or_missing_is_handled(tmp_path):
    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    checks = rc.run_checks(allowed=True, root=str(tmp_path / "r"), runner=boom, which=lambda n: "/x", egress_proxy_image=PROXY, free_bytes=lambda p: 100 * GIB)
    assert by(checks)["Docker"].status == rc.FAIL

    def gone(argv, timeout):
        raise FileNotFoundError("docker")

    checks = rc.run_checks(allowed=True, root=str(tmp_path / "r"), runner=gone, which=lambda n: "/x", egress_proxy_image=PROXY, free_bytes=lambda p: 100 * GIB)
    assert by(checks)["Docker"].status == rc.FAIL


def test_the_check_never_changes_the_hosts_settings_or_leaves_folders(tmp_path):
    go(tmp_path)
    go(tmp_path, Docker(run=(1, "", "x")))
    assert os.listdir(tmp_path / "rental") == []


# ---- the command ----------------------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_config(tmp_path, monkeypatch):
    """The command reads the agent settings; never the real file of whoever runs the tests."""
    cfg = types.SimpleNamespace(allow_container_rental=True, rental_root=str(tmp_path / "rental"), egress_proxy_image=None, work_dir=str(tmp_path))
    monkeypatch.setattr(cli.AgentConfig, "load", classmethod(lambda cls, path=None: cfg))


def test_the_command_prints_the_report_and_exits_with_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "run_checks", lambda **kw: [rc.Check("Docker", rc.PASS, "fine"), rc.Check("Graphics card", rc.FAIL, "none", "install the driver")])
    res = CliRunner().invoke(cli.app, ["rental", "check"])
    assert res.exit_code == 1 and "[FAIL] Graphics card: none" in res.output and "install the driver" in res.output and "NOT ready" in res.output
    monkeypatch.setattr(rc, "run_checks", lambda **kw: [rc.Check("Docker", rc.PASS, "fine")])
    ok = CliRunner().invoke(cli.app, ["rental", "check"])
    assert ok.exit_code == 0 and "passed the check" in ok.output


@pytest.mark.parametrize("bad", ["--privileged", "-v", "a b", " "])
def test_the_image_option_cannot_inject_a_docker_flag(bad):
    res = CliRunner().invoke(cli.app, ["rental", "check", "--image", bad])
    assert res.exit_code == 2 and "not valid" in res.output


def test_an_unknown_action_still_says_what_is_allowed():
    res = CliRunner().invoke(cli.app, ["rental", "explode"])
    assert res.exit_code == 2 and "'check'" in res.output


# ---- the driver decides the test image (535 is what the setup page asks for; 550 is what the newest image needs) ------------
class DriverDocker(Docker):
    def __init__(self, driver, **kw):
        super().__init__(**kw)
        self.driver = driver

    def __call__(self, argv, timeout):
        if argv[0] == "nvidia-smi" and any("driver_version" in a for a in argv):
            self.calls.append(list(argv))
            return types.SimpleNamespace(returncode=0 if self.driver is not None else 1, stdout=self.driver or "", stderr="")
        return super().__call__(argv, timeout)


@pytest.mark.parametrize("driver,image", [
    ("550.120", "nvidia/cuda:12.4.1-base-ubuntu22.04"), ("560.35.03", "nvidia/cuda:12.4.1-base-ubuntu22.04"),
    ("545.29.06", "nvidia/cuda:12.2.2-base-ubuntu22.04"), ("535.86.10", "nvidia/cuda:12.2.2-base-ubuntu22.04"),
])
def test_the_test_image_is_the_newest_one_the_hosts_driver_can_run(tmp_path, driver, image):
    checks, d = go(tmp_path, DriverDocker(driver))
    assert by(checks)["NVIDIA driver"].status == rc.PASS and driver in by(checks)["NVIDIA driver"].detail
    assert image in next(c for c in d.calls if c[:2] == ["docker", "run"])
    assert by(checks)["Test container"].status == rc.PASS


def test_a_driver_older_than_the_page_asks_for_fails_with_what_to_do(tmp_path):
    checks, d = go(tmp_path, DriverDocker("525.147.05"))
    drv = by(checks)["NVIDIA driver"]
    assert drv.status == rc.FAIL and "535.86" in drv.fix and "525.147.05" in drv.detail
    assert rc.summarize(checks)[0] == 1


def test_an_unreadable_driver_says_nothing_extra_and_uses_the_535_image(tmp_path):
    checks, d = go(tmp_path, DriverDocker(None))
    assert "NVIDIA driver" not in by(checks)
    assert "nvidia/cuda:12.2.2-base-ubuntu22.04" in next(c for c in d.calls if c[:2] == ["docker", "run"])


def test_an_image_the_host_names_is_used_as_given_whatever_the_driver(tmp_path):
    mine = "docker.io/library/python@sha256:" + "b" * 64
    checks, d = go(tmp_path, DriverDocker("550.120"), image=mine)
    run = next(c for c in d.calls if c[:2] == ["docker", "run"])
    assert mine in run and not any(a.startswith("nvidia/cuda:") for a in run)


def test_driver_helpers_read_only_real_versions():
    assert rc.driver_major("550.120") == 550 and rc.driver_major(" 535.86.10\nsecond card") == 535 and rc.driver_major("472.12") == 472
    for bad in (None, "", "NVIDIA GeForce RTX 3060, 12288 MiB", "N/A", "12.4", "abc.123"):
        assert rc.driver_major(bad) is None
    assert rc.pick_check_image(None) == rc.FALLBACK_CHECK_IMAGE == "nvidia/cuda:12.2.2-base-ubuntu22.04"
    assert rc.pick_check_image("999.1") == rc.CHECK_IMAGE


def test_the_setup_page_and_the_check_ask_for_the_same_driver_and_cuda_generation():
    page = (Path(__file__).resolve().parents[2] / "apps/web/src/app/provider/onboarding/page.tsx").read_text()
    assert f"NVIDIA driver {rc.MIN_DRIVER_MAJOR}." in page
    page_image = re.search(r"nvidia/cuda:(\d+\.\d+)", page).group(1)
    assert rc.FALLBACK_CHECK_IMAGE.split(":")[1].startswith(page_image)  # both are the CUDA 12.2 generation


def test_failure_messages_name_the_real_cause():
    why, fix = rc._explain_failure("nvidia-container-cli: requirement error: unsatisfied condition: cuda>=12.4, please update your driver", gpus=True, image="x")
    assert "driver is too old" in why and "535.86" in fix and "toolkit" not in fix.lower()
    why, fix = rc._explain_failure("Failed to initialize NVML: Unknown Error", gpus=True, image="x")
    assert "stopped answering" in why and "systemctl restart docker" in fix
    why, fix = rc._explain_failure("docker: could not select device driver with capabilities: [[gpu]]", gpus=True, image="x")
    assert "NVIDIA Container Toolkit" in fix  # the old advice still applies to the old cause


def test_a_missing_card_mentions_secure_boot(tmp_path):
    checks, _ = go(tmp_path, Docker(smi=(1, "", "NVIDIA-SMI has failed")))
    assert "Secure Boot" in by(checks)["Graphics card"].fix and "restart" in by(checks)["Graphics card"].fix

