"""W6/W11: installer sources. The MSI actions (with a fake runner), and static checks of the WiX, build and winget files.

WRITTEN, NOT RUN. Nothing here builds an MSI or runs a Windows tool.
"""

from __future__ import annotations

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "windows" / "installer"

_spec = importlib.util.spec_from_file_location("msi_actions", INSTALLER / "msi_actions.py")
msi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msi)

PY = r"C:\Program Files\ComputeCoin\python\python.exe"


class FakeRun:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        for key, reply in self.replies.items():
            if key in " ".join(argv):
                return reply if isinstance(reply, msi.RunResult) else msi.RunResult(reply)
        return msi.RunResult(0)

    def joined(self):
        return [" ".join(c) for c in self.calls]


# ---- firewall: outbound only ---------------------------------------------------------------------------------------------------------

def test_firewall_rules_are_outbound_allow_only():
    add = msi.firewall_add_argv("r", PY)
    assert "dir=out" in add and "action=allow" in add and f"program={PY}" in add
    assert not any("dir=in" in a for a in add)
    for f in ("Package.wxs", "msi_actions.py", "build.ps1"):
        text = (INSTALLER / f).read_text(encoding="utf-8")
        assert "dir=in" not in text, f  # no inbound rule is ever created


def test_install_registers_service_and_rule_but_never_starts_a_fresh_install(tmp_path):
    run = FakeRun({"service status": msi.RunResult(0, "not_installed")})
    data = tmp_path / "ComputeCoin"
    assert msi.do_install(PY, r"C:\Program Files\ComputeCoin", data, run) == 0
    joined = run.joined()
    assert any("service install" in c for c in joined)
    assert any("firewall add rule" in c and "dir=out" in c for c in joined)
    assert not any("service start" in c for c in joined)  # not enrolled: nothing to run (X14)


def test_upgrade_of_an_enrolled_host_starts_the_service_and_does_not_reinstall_it(tmp_path):
    data = tmp_path / "ComputeCoin"
    (data / "secrets").mkdir(parents=True)
    (data / "secrets" / "machine_secret.bin").write_bytes(b"dpapi-blob")
    run = FakeRun({"service status": msi.RunResult(0, "stopped")})
    assert msi.do_install(PY, "", data, run) == 0
    joined = run.joined()
    assert not any("service install" in c for c in joined)
    assert any("service start" in c for c in joined)


def test_install_fails_loudly_when_service_registration_fails(tmp_path):
    run = FakeRun({"service status": msi.RunResult(0, "not_installed"), "service install": 5})
    assert msi.do_install(PY, "", tmp_path / "ComputeCoin", run) == 1
    assert not any("firewall" in c for c in run.joined())


# ---- uninstall removes everything (X14) -----------------------------------------------------------------------------------------------

def test_cleanup_removes_service_distro_firewall_credentials_and_data(tmp_path):
    data = tmp_path / "ComputeCoin"
    (data / "secrets").mkdir(parents=True)
    (data / "secrets" / "x.bin").write_bytes(b"1")
    (data / "runtime-settings.json").write_text("{}")
    run = FakeRun({"cmdkey /list": msi.RunResult(0, "    Target: LegacyGeneric:target=ComputeCoin/machine\n    Target: Other/thing\n")})
    assert msi.do_cleanup(PY, data, run) == 0
    joined = run.joined()
    assert any("service stop" in c for c in joined) and any("service remove" in c for c in joined)
    assert any("runtime.provision remove" in c for c in joined)
    assert any("firewall delete rule" in c for c in joined)
    assert any(c.startswith("cmdkey /delete:LegacyGeneric:target=ComputeCoin/machine") for c in joined)
    assert not any("Other/thing" in c and "/delete" in c for c in joined)  # only OUR credentials
    assert not data.exists()


def test_cleanup_without_a_settings_file_unregisters_only_our_distro(tmp_path):
    data = tmp_path / "ComputeCoin"
    data.mkdir()
    run = FakeRun()
    msi.do_cleanup(PY, data, run)
    unreg = [c for c in run.calls if c[:2] == ["wsl.exe", "--unregister"]]
    assert unreg == [["wsl.exe", "--unregister", "computecoin"]]


def test_cleanup_reports_what_it_could_not_remove_and_still_finishes(tmp_path, capsys):
    data = tmp_path / "ComputeCoin"
    data.mkdir()
    run = FakeRun({"firewall delete rule": 7, "cmdkey /list": msi.RunResult(0, "Target: ComputeCoin/x\n"), "cmdkey /delete": 1})
    code = msi.do_cleanup(PY, data, run)
    out = capsys.readouterr().out
    assert code == 1 and "firewall rule" in out and "credential ComputeCoin/x" in out
    assert not data.exists()  # the data directory is removed regardless


def test_cleanup_refuses_to_delete_a_directory_that_is_not_ours(tmp_path):
    victim = tmp_path / "Documents"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    run = FakeRun()
    assert msi.do_cleanup(PY, victim, run) == 1
    assert (victim / "keep.txt").exists()


def test_keep_data_option_leaves_the_directory(tmp_path):
    data = tmp_path / "ComputeCoin"
    data.mkdir()
    msi.do_cleanup(PY, data, FakeRun(), keep_data=True)
    assert data.exists()


def test_every_external_command_is_an_argument_list():
    for argv in (msi.firewall_add_argv("n", PY), msi.firewall_delete_argv("n"), msi.agent_cli_argv(PY, "status"),
                 msi.provision_remove_argv(PY, "s.json"), msi.distro_unregister_argv(), msi.requirements_check_argv(PY, "C:\\x")):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert "requirements_check" in " ".join(msi.requirements_check_argv(PY, "C:\\x"))
    assert "shell=True" not in (INSTALLER / "msi_actions.py").read_text(encoding="utf-8")


# ---- Package.wxs -------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wxs_text():
    return (INSTALLER / "Package.wxs").read_text(encoding="utf-8")


def test_wxs_is_well_formed_v4_xml(wxs_text):
    root = ET.fromstring(wxs_text)
    assert root.tag == "{http://wixtoolset.org/schemas/v4/wxs}Wix"
    pkg = root.find("{http://wixtoolset.org/schemas/v4/wxs}Package")
    assert pkg is not None and pkg.get("Scope") == "perMachine"
    assert re.fullmatch(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}", pkg.get("UpgradeCode"))


def test_wxs_runs_the_requirements_checker_by_module_path_and_aborts_on_failure(wxs_text):
    assert "computecoin_agent.windows.runtime.requirements_check" in wxs_text
    m = re.search(r'<CustomAction Id="RequirementsCheck"[^>]*>', wxs_text, re.S)
    assert m and 'Return="check"' in m.group(0) and 'Execute="deferred"' in m.group(0)


def test_wxs_cleanup_runs_on_uninstall_only_never_on_an_upgrade(wxs_text):
    m = re.search(r'<Custom Action="CleanupAgent"[^>]*>', wxs_text)
    assert m and "REMOVE=&quot;ALL&quot;" in m.group(0) and "NOT UPGRADINGPRODUCTCODE" in m.group(0)
    assert re.search(r'<CustomAction Id="CleanupAgent"[^>]*Return="ignore"', wxs_text)  # a leftover never traps the uninstall


def test_wxs_needs_consent_for_silent_installs(wxs_text):
    assert "ConsentRequired" in wxs_text and "LicenseAccepted" in wxs_text
    assert 'Id="WixUILicenseRtf"' in wxs_text


def test_every_python_invocation_is_isolated(wxs_text):
    # -I: no PYTHONPATH, no current-directory or user-site imports for code that runs as SYSTEM
    for m in re.finditer(r'ExeCommand="([^"]*)"', wxs_text):
        assert "python.exe&quot; -I " in m.group(1), m.group(1)
    for argv in (msi.agent_cli_argv(PY, "status"), msi.provision_remove_argv(PY, "s"), msi.requirements_check_argv(PY, "d")):
        assert argv[1] == "-I"


def test_wxs_never_ends_a_quoted_argument_with_a_folder_property(wxs_text):
    # [INSTALLFOLDER] and [CommonAppDataFolder] end with a backslash; "C:\\x\\" makes the C runtime read \" as an escaped quote
    assert not re.search(r'(INSTALLFOLDER|CommonAppDataFolder|ProgramFiles64Folder)\]&quot;', wxs_text)


def test_wxs_never_starts_the_service_and_never_enrols(wxs_text):
    assert not re.search(r'Start="(install|both)"', wxs_text)
    assert not re.search(r'ExeCommand="[^"]*\benroll\b', wxs_text)  # the installer never enrols this PC (X14)


def test_wxs_has_no_inbound_firewall_element_and_no_embedded_secret(wxs_text):
    assert "FirewallException" not in wxs_text  # that element creates INBOUND allow exceptions
    assert "PRIVATE KEY" not in wxs_text


# ---- consent text (X14) -----------------------------------------------------------------------------------------------------------------------------

def test_consent_text_says_what_is_installed_shared_and_how_to_stop_and_remove():
    t = (INSTALLER / "consent.txt").read_text(encoding="utf-8")
    for needle in ("WHAT IS INSTALLED", "WHAT IS SHARED", "HOW TO STOP AND REMOVE", "OUTBOUND", "full-screen", "battery",
                   "Uninstall", "credential", "does NOT create it and does NOT enrol"):
        assert needle in t, needle


def test_consent_text_makes_no_promise_the_design_does_not_keep():
    t = (INSTALLER / "consent.txt").read_text(encoding="utf-8").lower()
    assert "guaranteed" in t and "not guaranteed" in t  # earnings are explicitly NOT guaranteed
    assert "private" not in t.replace("private key", "")  # no privacy-tier claim on a Windows host (X5)


# ---- build.ps1: the certificate never lives in the repository ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def build_text():
    return (INSTALLER / "build.ps1").read_text(encoding="utf-8")


def test_build_script_reads_the_certificate_from_the_environment_only(build_text):
    for name in ("COMPUTECOIN_SIGN_CERT_THUMBPRINT", "COMPUTECOIN_SIGN_PFX_PATH", "COMPUTECOIN_SIGN_PFX_PASSWORD",
                 "COMPUTECOIN_SIGN_TIMESTAMP_URL"):
        assert name in build_text
    assert not re.search(r"\b[0-9a-fA-F]{40}\b", build_text)  # no literal thumbprint
    assert not re.search(r"\.pfx['\"]", build_text.replace("*.pfx", ""))  # no literal certificate file name
    assert "BEGIN" not in build_text or "PRIVATE" not in build_text


def test_build_script_signs_timestamps_and_verifies_and_refuses_unsigned_release(build_text):
    assert "/fd" in build_text and "sha256" in build_text and "/tr" in build_text and "/td" in build_text
    assert "verify" in build_text and "/pa" in build_text
    assert "-UNSIGNED.msi" in build_text and "[switch] $Unsigned" in build_text


def test_build_script_refuses_a_payload_that_looks_like_it_carries_secrets(build_text):
    assert "agent.toml" in build_text and "*.pfx" in build_text


def test_no_installer_file_contains_a_private_key_or_a_real_credential():
    for path in INSTALLER.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "PRIVATE KEY-----" not in text, path
            assert not re.search(r"msk_[0-9a-f]{16,}", text), path


# ---- winget templates -------------------------------------------------------------------------------------------------------------------------------------------

def test_winget_templates_are_present_consistent_and_pass_the_acceptance_flag():
    base = INSTALLER / "winget"
    files = {p.name: p.read_text(encoding="utf-8") for p in base.glob("*.yaml")}
    assert set(files) == {"ComputeCoin.Agent.yaml", "ComputeCoin.Agent.installer.yaml", "ComputeCoin.Agent.locale.en-US.yaml"}
    for name, text in files.items():
        assert "PackageIdentifier: ComputeCoin.Agent" in text, name
        assert "<VERSION>" in text, name  # a template: never a stale real version
    installer = files["ComputeCoin.Agent.installer.yaml"]
    assert "LicenseAccepted=1" in installer and "InstallerSha256:" in installer
    assert "{CAA81EEF-06DE-4F86-B012-A8E47FE84EDC}" in installer  # the MSI UpgradeCode
    assert "Agreements:" in files["ComputeCoin.Agent.locale.en-US.yaml"]


# ---- offline manifest signing tool ---------------------------------------------------------------------------------------------------------------------------

def test_sign_manifest_tool_output_verifies_with_the_agents_own_verifier(tmp_path, monkeypatch, capsys):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from computecoin_agent.updater import manifest as agent_manifest

    spec = importlib.util.spec_from_file_location("sign_manifest", INSTALLER / "sign_manifest.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.SIGN_PREFIX == agent_manifest.SIGN_PREFIX  # the two sides must never drift apart

    key = Ed25519PrivateKey.generate()  # a fresh test key (L7)
    key_file = tmp_path / "key.hex"
    key_file.write_text(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex())
    msi_file = tmp_path / "agent.msi"
    msi_file.write_bytes(b"msi bytes")
    monkeypatch.setenv("COMPUTECOIN_UPDATE_SIGNING_KEY_FILE", str(key_file))
    code = tool.main(["--version", "1.4.0", "--platform", "windows", "--ring", "canary", "--url", "https://dl.example.test/a.msi",
                      "--msi", str(msi_file), "--min-supported", "1.2.0", "--revokes", "1.3.9"])
    assert code == 0
    import json

    signed = json.loads(capsys.readouterr().out)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    parsed = agent_manifest.parse_manifest(signed, [pub], platform="windows", host_ring="canary", now=signed["issued_at"] + 1)
    assert parsed.version == "1.4.0" and parsed.revoked_versions == ("1.3.9",) and parsed.size_bytes == len(b"msi bytes")


def test_sign_manifest_tool_refuses_to_run_without_a_key_file(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("sign_manifest2", INSTALLER / "sign_manifest.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.delenv("COMPUTECOIN_UPDATE_SIGNING_KEY_FILE", raising=False)
    (tmp_path / "m.msi").write_bytes(b"x")
    assert tool.main(["--version", "1.0.0", "--platform", "windows", "--ring", "general", "--url", "https://x.test/a",
                      "--msi", str(tmp_path / "m.msi"), "--min-supported", "1.0.0"]) == 2
