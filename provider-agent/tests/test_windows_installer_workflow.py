"""The Windows installer build workflow (.github/workflows/windows-installer.yml) and its inputs run only on a Windows runner, so these tests pin what can be
checked from text: the pins, the files it uses, that it never publishes or reads a secret, and that the package agrees with the code about the Windows
version. NOT RUN ON WINDOWS."""

from __future__ import annotations

import re
import xml.dom.minidom
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"
INSTALLER = ROOT / "provider-agent" / "windows" / "installer"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOB = DOC["jobs"]["build"]
ENV = JOB["env"]
STEPS = JOB["steps"]


def _run_text() -> str:
    return "\n".join(str(s.get("run", "")) for s in STEPS)


def test_it_runs_on_a_real_windows_runner_with_read_only_permissions():
    assert JOB["runs-on"] == "windows-latest"
    assert DOC["permissions"] == {"contents": "read"}
    assert JOB["defaults"]["run"]["shell"] == "pwsh"


def test_the_portable_python_and_wix_are_pinned_exactly():
    assert re.fullmatch(r"3\.12\.\d+", ENV["PY_EMBED_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", ENV["PY_EMBED_SHA256"])
    assert re.fullmatch(r"4\.\d+\.\d+", ENV["WIX_VERSION"])
    assert "python-$($env:PY_EMBED_VERSION)-embed-amd64.zip" in TEXT and "https://www.python.org/ftp/python/" in TEXT
    assert "does not match its pinned SHA-256" in TEXT  # the hash is really compared


def test_the_python_minor_version_is_the_same_everywhere():
    setup = next(s for s in STEPS if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.12"
    assert ENV["PY_EMBED_VERSION"].startswith("3.12.") and "python312._pth" in TEXT
    assert re.search(r'requires-python = ">=3\.12"', (ROOT / "provider-agent" / "pyproject.toml").read_text())


def test_it_builds_unsigned_and_never_publishes_or_reads_a_secret():
    run = _run_text()
    assert "-Unsigned" in run and "UNSIGNED" in TEXT
    assert "secrets." not in TEXT and "COMPUTECOIN_SIGN" not in TEXT  # no certificate is ever available to this workflow
    for publishing in ("action-gh-release", "gh release", "create-release", "winget", "signtool", "sign_manifest"):
        assert publishing not in TEXT, publishing
    upload = next(s for s in STEPS if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["retention-days"] <= 14 and "UNSIGNED" in upload["with"]["name"]


def test_the_files_it_uses_exist():
    for name in ("build.ps1", "Package.wxs", "msi_actions.py", "consent.txt"):
        assert (INSTALLER / name).is_file(), name
    assert "provider-agent/windows/installer/msi_actions.py" in TEXT and "provider-agent/windows/installer/build.ps1" in TEXT
    assert (ROOT / "provider-agent" / "computecoin_agent" / "windows" / "runtime" / "requirements_check.py").is_file()


def test_it_checks_what_a_runner_can_check_and_says_what_it_cannot():
    run = _run_text()
    for needed in ("import computecoin_agent, win32serviceutil", "requirements_check --json", "-ne 3", "'/a'", "LicenseAccepted=1",
                   "left the program folder behind", "left the service behind"):
        assert needed in run, needed
    assert "cannot prove" in TEXT.lower() or "cannot prove: wsl2" in TEXT.lower()


def test_a_failing_step_puts_its_reason_where_anyone_can_read_it():
    run = _run_text()
    assert run.count("trap {") >= 4 and "::error" in run
    last = [s for s in STEPS if s.get("if") == "failure()"]
    assert last and "msi-*.log" in last[0]["run"]


def test_the_triggers_cover_the_installer_files_and_do_not_run_on_forks_of_every_push():
    on = DOC.get("on") or DOC.get(True)
    assert "workflow_dispatch" in on and on["push"]["branches"] == ["main"]
    assert ".github/workflows/windows-installer.yml" in on["push"]["paths"] and "provider-agent/windows/**" in on["push"]["paths"]


# ---- the package ---------------------------------------------------------------------------------------------------------------
WXS = (INSTALLER / "Package.wxs").read_text(encoding="utf-8")
BUILD = (INSTALLER / "build.ps1").read_text(encoding="utf-8")


def test_the_package_is_well_formed_and_requires_windows_11_like_the_code_does():
    xml.dom.minidom.parseString(WXS)
    checker = (ROOT / "provider-agent/computecoin_agent/windows/runtime/requirements_check.py").read_text()
    build = int(re.search(r"min_windows_build: int = (\d+)", checker).group(1))
    assert build == 22000 and f"WindowsBuild &gt;= {build}" in WXS
    assert "Windows 11" in WXS and "needs 64-bit Windows 11 or later" in WXS
    assert "Windows 10 or later" not in WXS


def test_the_package_still_demands_consent_and_never_starts_or_enrols_the_service():
    assert 'LicenseAccepted = &quot;1&quot;' in WXS and "ConsentRequired" in WXS
    assert "NOT started, NOT enrolled" in WXS


def test_the_secret_check_exempts_only_the_public_certificate_bundle():
    assert r"site-packages\\certifi\\cacert\.pem$" in BUILD
    for kept in ("'*.pfx'", "'*.p12'", "'*.pem'", "'*.key'", "'agent.toml'", "'*.secret'"):
        assert kept in BUILD, kept
    assert BUILD.count("notmatch") == 1  # one exemption, not a broad one


@pytest.mark.parametrize("path", [WORKFLOW, INSTALLER / "build.ps1"])
def test_the_scripts_are_plain_ascii_so_no_console_code_page_can_misread_them(path):
    assert all(ord(c) < 128 for c in path.read_text(encoding="utf-8")), path.name


def test_the_payload_gets_the_windows_packages_not_just_the_agent():
    # The first real build failed because the agent was installed without its [windows] extra, so pywin32 was missing.
    assert 'pip install --target $site --no-compile "./provider-agent[windows]"' in TEXT
    assert "pip install --target $site --no-compile ./provider-agent\n" not in TEXT


def test_the_portable_python_can_find_pywin32_without_relying_on_a_pth_file():
    for folder in (r"Lib\site-packages\win32", r"Lib\site-packages\win32\lib", r"Lib\site-packages\Pythonwin"):
        assert folder in TEXT
    assert "pywin32_system32" in TEXT and "*.dll" in TEXT  # its two DLLs go next to python.exe
    # the search-path file must still end with "import site"
    assert "$extra + 'import site'" in TEXT


def test_a_failed_import_check_names_the_real_error():
    assert "Select-Object -Last 1" in TEXT  # the last line of the traceback goes into the message


def test_the_agent_dev_extra_has_what_the_tests_import():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    dev = pyproject.split("dev = [", 1)[1].split("]", 1)[0]
    assert re.search(r'"pyyaml==\d+\.\d+\.\d+"', dev, re.IGNORECASE)
