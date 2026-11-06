"""Isolation tests (plan section 8, W11.2): a job must not read ``C:\\``, call Windows interop, or reach the host LAN or the internet.
SELF-HOSTED RUNNER ONLY. WRITTEN, NOT RUN. Two layers: the distro itself, and a job-like container started with the production
flags (no network, read-only root, no capabilities, no new privileges, no host mounts).
"""

from __future__ import annotations

import json
import os

import pytest

from e2e_support import ISOLATION_PROBE_PY, Wsl, pinned_image, probe_violations

pytestmark = pytest.mark.windows_gpu

# Flags every Windows job container gets (ADR-0003 unchanged). A test that dropped one would no longer test the product.
JOB_FLAGS = ["--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--pids-limit", "256", "--user", "10001:10001", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]


@pytest.fixture
def wsl(settings):
    return Wsl(settings)


def _probe(wsl: Wsl, image: str, extra: list[str]) -> dict:
    res = wsl.exec("docker", "run", *extra, "-e", "PROBE_LAN_HOST", "-e", "PROBE_LAN_PORT", image, "python", "-c", ISOLATION_PROBE_PY, timeout=300)
    assert res.ok, res.text()
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_a_production_job_container_reaches_nothing_it_must_not(wsl, settings):
    image = pinned_image(settings.probe_image, "CC_PROBE_IMAGE")
    result = _probe(wsl, image, JOB_FLAGS)
    assert probe_violations(result) == [], result


def test_the_probe_would_notice_a_leak(wsl, settings):
    """Bug re-insertion made permanent: the same probe on a container that IS allowed the network must see the internet.
    If this does not report ``internet_reachable`` the probe, not the product, is broken and the test above proves nothing."""
    image = pinned_image(settings.probe_image, "CC_PROBE_IMAGE")
    flags = [f for f in JOB_FLAGS if f not in ("--network", "none")]
    result = _probe(wsl, image, flags)
    assert result["internet_reachable"] is True, result


def test_distro_itself_has_no_windows_interop(wsl):
    assert wsl.sh("test -e /mnt/c/Windows && echo LEAK || echo ok").stdout.strip() == "ok"
    assert wsl.sh("echo $PATH | tr ':' '\\n' | grep -c '^/mnt/' || true").stdout.strip() in ("0", "")
    assert wsl.sh("/mnt/c/Windows/System32/cmd.exe /c echo x 2>&1; echo rc=$?").stdout.strip().endswith(("rc=126", "rc=127")), "Windows programs can be started from the distro"


def test_job_container_has_no_host_lan_access_when_a_network_is_off(wsl, settings):
    lan = os.environ.get("PROBE_LAN_HOST")
    if not lan:
        pytest.skip("set PROBE_LAN_HOST to an address on the runner's LAN (for example the router) to run the LAN probe")
    image = pinned_image(settings.probe_image, "CC_PROBE_IMAGE")
    assert _probe(wsl, image, JOB_FLAGS)["lan_host_reachable"] is False


def test_the_existing_escape_suite_passes_against_this_runtime(wsl, settings):
    """``provider-agent/tests/escape/`` run inside the distro, against the Windows runtime's Docker."""
    if not settings.escape_suite_cmd:
        pytest.fail("CC_ESCAPE_SUITE_CMD is not set: the escape suite must run on every release (plan section 8)")
    res = wsl.sh(settings.escape_suite_cmd, timeout=3600)
    assert res.ok, res.text()[-3000:]
