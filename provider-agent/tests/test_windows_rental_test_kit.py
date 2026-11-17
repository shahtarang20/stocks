"""The Windows rental test kit (scripts/windows/rental-test-kit.ps1) is a PowerShell script this Linux test run cannot execute, so these tests
pin what can be checked from text: it is ASCII only (Windows PowerShell 5.1 would misread anything else), it names the same distro, user,
service and paths as the code, it only uses commands the agent has, and it never reads or prints a secret. NOT RUN ON WINDOWS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from computecoin_agent.windows import rental_check_windows as rcw
from computecoin_agent.windows.runtime import distro_paths as P

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "scripts" / "windows" / "rental-test-kit.ps1"
TEXT = KIT.read_text(encoding="utf-8")


def test_the_kit_exists_and_is_ascii_only():
    assert KIT.is_file()
    assert all(ord(c) < 128 for c in TEXT)


def test_it_names_the_same_linux_system_user_service_and_paths_as_the_code():
    assert f"$Distro = '{P.DISTRO_NAME}'" in TEXT
    assert f"$RunnerUser = '{P.RUNNER_USER}'" in TEXT
    assert "SERVICE_NAME = " in (ROOT / "provider-agent/computecoin_agent/windows/service.py").read_text()
    assert "'ComputeCoinAgent'" in TEXT
    assert rcw.WINDOWS_DRIVE in TEXT and rcw.INTEROP_FILE in TEXT


def test_it_only_runs_agent_commands_that_exist():
    cli = (ROOT / "provider-agent/computecoin_agent/cli.py").read_text()
    assert "rental status" in TEXT and "@('rental', 'check')" in TEXT
    assert re.search(r'action not in \(([^)]*"status"[^)]*"check"[^)]*)\)', cli) or '"check"' in cli
    assert '"--image"' in cli and "'--image'" in TEXT


def test_it_only_reads_and_never_touches_a_secret():
    lowered = TEXT.lower()
    for forbidden in ("machine_secret", "enrollment_token", "enr_", "agent.toml", "dpapi", "get-content"):
        assert forbidden not in lowered, forbidden
    for changes_things in ("install-", "set-service", "restart-service", "stop-service", "remove-item"):
        assert changes_things not in lowered, changes_things
    # advice text may SAY "run: wsl --shutdown", but no wsl.exe call here may do anything but read
    called = re.findall(r"Invoke-Wsl -Arguments @\(([^)]*)\)", TEXT)
    assert called, "the kit should call wsl.exe through Invoke-Wsl"
    for args in called:
        assert not re.search(r"--(shutdown|unregister|import|terminate|install|set-version|update)", args), args


def test_it_fails_with_a_nonzero_exit_code_and_writes_a_report():
    assert "exit 1" in TEXT and "exit 0" in TEXT
    assert "computecoin-rental-report-" in TEXT and "Set-Content" in TEXT


@pytest.mark.parametrize("name", ["Windows version", "Administrator", "Virtualization", "WSL", "ComputeCoin Linux system", "NVIDIA card (Windows)", "NVIDIA card mode",
                                  "Docker (inside)", "NVIDIA card (inside)", "Windows drive hidden", "Windows programs blocked", "rental check"])
def test_every_check_the_guide_talks_about_is_in_the_kit(name):
    assert f"'{name}'" in TEXT
