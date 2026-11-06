"""Provisioning test (plan section 8): the managed distro exists, is hardened, sees the GPU, runs a container, and one real Kit job
produces output. SELF-HOSTED RUNNER ONLY (marker ``windows_gpu``). WRITTEN, NOT RUN: every command and expected string below is an
assumption about the real runtime until a recorded run exists (X11, TODO(verify-docs)).
"""

from __future__ import annotations

import json
import re

import pytest

from e2e_support import Wsl, pinned_image, run

pytestmark = pytest.mark.windows_gpu


@pytest.fixture
def wsl(settings):
    return Wsl(settings)


def test_distro_is_registered_and_is_wsl2(wsl, settings):
    res = wsl.host("--list", "--verbose")
    assert res.ok, res.text()
    line = next((ln for ln in res.stdout.splitlines() if settings.distro in ln.split()), "")
    assert line, f"{settings.distro} is not listed:\n{res.stdout}"
    assert line.split()[-1] == "2", f"the distro must be WSL 2: {line!r}"


def test_wsl_conf_is_hardened(wsl):
    conf = wsl.read_file("/etc/wsl.conf")
    flat = re.sub(r"[ \t]+", "", conf.lower())
    assert "[automount]" in flat and re.search(r"\[automount\][^\[]*enabled=false", flat, re.S), conf
    assert re.search(r"\[interop\][^\[]*enabled=false", flat, re.S), conf
    assert "appendwindowspath=false" in flat, conf
    assert re.search(r"\[boot\][^\[]*systemd=true", flat, re.S), conf


def test_no_windows_drive_is_mounted_in_the_distro(wsl):
    res = wsl.sh("grep -E ' (9p|drvfs|virtiofs) ' /proc/mounts || true")
    bad = [ln for ln in res.stdout.splitlines() if "/usr/lib/wsl" not in ln and "/mnt/wslg" not in ln]
    assert bad == [], "Windows-backed mounts other than the GPU ones:\n" + "\n".join(bad)


def test_the_runner_user_is_not_root_and_ssh_is_off(wsl):
    assert wsl.exec("/usr/bin/id", "-u", user="ccrunner").stdout.strip() not in ("", "0")
    listeners = wsl.sh("ss -ltn 2>/dev/null | tail -n +2").stdout
    assert ":22 " not in listeners, "an SSH listener is open"
    assert not re.search(r"\b0\.0\.0\.0:|\[::\]:", listeners), "a listener is bound to all addresses:\n" + listeners


def test_gpu_is_visible_and_the_smoke_container_runs(wsl, settings):
    smi = wsl.exec("nvidia-smi", "-L")
    assert smi.ok and "GPU 0:" in smi.stdout, smi.text()
    image = pinned_image(settings.probe_image, "CC_PROBE_IMAGE")
    res = wsl.exec("docker", "run", "--rm", "--gpus", "all", "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", image, "nvidia-smi", "-L", timeout=900)
    assert res.ok and "GPU 0:" in res.stdout, res.text()


def test_agent_reports_windows_platform_and_only_container_flags(settings):
    """The heartbeat object the host would send: a Windows host never advertises a private tier (X5)."""
    res = run([*settings.agent_cmd, "detect"], timeout=300)
    assert res.ok, res.text()
    report = json.loads(res.stdout)
    assert report.get("gpus"), "no GPU in the capability report"
    from computecoin_agent.capabilities import compute_capabilities  # noqa: PLC0415 - needs the agent installed on the runner

    flags = set(compute_capabilities(type("R", (), {"gpus": []})()))
    assert not (flags & {"gvisor", "microvm", "sealed", "sealed_pro", "confidential", "vfio_gpu", "multi_node", "amd_rocm"}), flags


def test_one_real_kit_job_runs_and_writes_output(wsl, settings):
    """One real Kit job through the production runner path INSIDE the distro. ``CC_KIT_JOB_CMD`` is a command for the distro that runs the
    Kit (for example ``kits/tools/smoke_runner.py`` with the Kit's smoke spec, or the agent's own runner on a staged job) and prints ONE
    JSON line: ``{"image": "<name@sha256:...>", "passed": true, "outputs": {"<posix relative path>": "<sha256>", ...}}``.
    The hashes are kept as artifacts for the parity comparison (``test_parity_hashes.py``)."""
    from e2e_support import write_manifest

    assert settings.kit_job_cmd, "CC_KIT_JOB_CMD is not set"
    image = pinned_image(settings.kit_image, "CC_KIT_IMAGE")
    res = wsl.sh(settings.kit_job_cmd, timeout=3600)
    assert res.ok, res.text()[-2000:]
    report = json.loads(res.stdout.strip().splitlines()[-1])
    assert report.get("passed") is True, report
    assert report.get("image") == image, "the job ran a different image than the one pinned for this run"
    assert report.get("outputs"), "the Kit produced no output files"
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(settings.artifact_dir / "windows-kit-manifest.json", report["outputs"])
    (settings.artifact_dir / "windows-kit-report.json").write_bytes(
        (json.dumps({"image": report["image"], "passed": True}, indent=2) + "\n").encode("utf-8"))
