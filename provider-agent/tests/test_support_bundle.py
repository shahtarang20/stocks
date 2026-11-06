"""W10 / L7: the support bundle is redacted. WRITTEN, NOT RUN.

Every secret below is generated fresh for the test (L7): nothing here is a real credential or key. The tests plant secrets in
every place a secret can travel (config fields, environment variables, log lines in many shapes, a UTF-16 log), build a real
zip, open EVERY entry and assert that no planted value survives.

Bug re-insertion list (each must make a named test fail): turn ``Redactor.text`` into a no-op (the log and config tests fail);
drop the ``secret_key_value`` rule (test_every_secret_shape_is_replaced fails); drop ``verify_clean`` from build_bundle (the
leak-guard test fails); dump ``os.environ`` into the bundle (the environment canary test fails); read logs without bounds
(the bounded-tail test fails).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import string
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from computecoin_agent import support_bundle as sb
from computecoin_agent.config import AgentConfig


def _hex(n: int = 32) -> str:
    return secrets.token_hex(n)


def _word(n: int = 10) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


def _blob() -> str:
    while True:
        b = base64.b64encode(os.urandom(48)).decode()
        if any(c.isupper() for c in b) and any(c.islower() for c in b) and any(c.isdigit() for c in b):
            return b


def _jwt() -> str:
    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join([enc(b'{"alg":"HS256","typ":"JWT"}'), enc(json.dumps({"sub": _word(8)}).encode()), enc(os.urandom(32))])


class Planted:
    """Fresh secrets and the log text that carries them in every shape the agent could print them."""

    def __init__(self) -> None:
        self.machine_secret = "msk_" + _hex(24)
        self.enroll_token = "enr_" + _hex(16)
        self.signing_secret = _hex(32)
        self.bearer = _word(6) + _hex(20)
        self.jwt = _jwt()
        self.password = "pw-" + _hex(8)
        self.url_password = "up-" + _hex(8)
        self.presigned = _hex(32)
        self.eth_key = "0x" + _hex(32)
        self.aws_key = "AKIA" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        self.email_local = "person" + _word(5)
        self.email = f"{self.email_local}@example.test"
        self.win_user = "winuser" + _word(5)
        self.home_user = "homeuser" + _word(5)
        self.pem_body = base64.b64encode(os.urandom(120)).decode()
        self.blob = _blob()
        self.public_ip = "8.8.4.4"
        self.raw_hex = _hex(32)
        self.digest = "sha256:" + _hex(32)

    def values(self) -> list[str]:
        return [self.machine_secret, self.enroll_token, self.signing_secret, self.bearer, self.jwt, self.password,
                self.url_password, self.presigned, self.eth_key, self.aws_key, self.email, self.email_local, self.win_user,
                self.home_user, self.pem_body, self.blob, self.public_ip, self.raw_hex]

    def log_lines(self) -> list[str]:
        return [
            f'2026-11-06T10:00:00Z INFO enrolling machine_secret = "{self.machine_secret}"',
            f"COMPUTECOIN_ENROLLMENT_TOKEN={self.enroll_token}",
            f"POST /v1/agents/heartbeat Authorization: Bearer {self.bearer}",
            f"retrying with bearer {self.bearer}",
            f"got session {self.jwt} from the server",
            f"password={self.password} was rejected",
            f'{{"signing_secret": "{self.signing_secret}", "ok": true}}',
            f"GET https://svc:{self.url_password}@api.example.test/v1/x failed",
            f"PUT https://s3.example.test/b/o?X-Amz-Signature={self.presigned}&X-Amz-Expires=300",
            f"chain key {self.eth_key}",
            f"aws key {self.aws_key} found in the environment",
            f"contact {self.email} for access",
            f"opening C:\\Users\\{self.win_user}\\AppData\\Local\\ComputeCoin\\agent.toml",
            f"opening C:\\\\Users\\\\{self.win_user}\\\\AppData (escaped)",
            f"opening /home/{self.home_user}/.computecoin/agent.toml",
            f"-----BEGIN PRIVATE KEY-----\n{self.pem_body}\n-----END PRIVATE KEY-----",
            f"blob {self.blob} end",
            f"connecting to {self.public_ip}:443 and 192.168.1.5:8000",
            f"raw hex {self.raw_hex} and image {self.digest}",
        ]


@pytest.fixture
def planted() -> Planted:
    return Planted()


def _all_bytes(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None
        return {n: zf.read(n) for n in zf.namelist()}


def _assert_clean(files: dict[str, bytes], values: list[str]) -> None:
    for name, data in files.items():
        text = data.decode("utf-8")  # the bundle is UTF-8 text only
        for v in values:
            assert v not in text, f"{name} still contains a planted secret ({v[:6]}...)"
            assert v.lower() not in text.lower() or len(v) < 8, f"{name} contains {v[:6]}... in another case"


# --------------------------------------------------------------------------- the redaction rules
def test_every_secret_shape_is_replaced(planted):
    out = sb.Redactor().text("\n".join(planted.log_lines()))
    for v in planted.values():
        assert v not in out, v[:8]
    assert "-----BEGIN" not in out


def test_what_should_stay_stays(planted):
    out = sb.Redactor().text("\n".join(planted.log_lines()))
    assert planted.digest in out  # image digests are diagnostics, not secrets
    assert "192.168.1.5" in out  # a private address helps and exposes nothing
    assert "2026-11-06T10:00:00Z INFO" in out
    assert "enrolling" in out and "failed" in out


def test_a_second_scrub_changes_nothing(planted):
    """The fail-closed check depends on this: scrubbed text must be a fixed point of the scrubber."""
    red = sb.Redactor()
    once = red.text("\n".join(planted.log_lines()))
    assert sb.Redactor().text(once) == once


def test_known_secrets_are_replaced_wherever_they_appear():
    secret = "plain-" + _hex(10)  # not matching any pattern on its own
    out = sb.Redactor(known_secrets=[secret]).text(f"value is {secret}, again {secret}.")
    assert secret not in out and out.count(sb.REDACTED) == 2


def test_identity_words_are_replaced_but_markers_are_left_alone():
    out = sb.Redactor(identity_words=["mail", "alice-pc"]).text("host Alice-PC sent [REDACTED EMAIL] to <id>")
    assert "Alice-PC" not in out
    assert "[REDACTED EMAIL]" in out  # the word "mail" must not eat our own marker
    assert sb.Redactor(identity_words=["mail", "alice-pc"]).text(out) == out


def test_common_words_are_never_treated_as_identity():
    red = sb.Redactor(identity_words=["root", "user", "windows"])
    assert red.text("rootfs of the user in windows") == "rootfs of the user in windows"


def test_data_redacts_by_key_and_by_value(planted):
    data = {"machine_secret": planted.machine_secret, "enrollment_token": None, "api_url": "https://x.test",
            "nested": {"Authorization": f"Bearer {planted.bearer}", "note": f"mail {planted.email}"},
            "list": [f"token={planted.password}", 5, True]}
    out = sb.Redactor().data(data)
    dumped = json.dumps(out)
    for v in (planted.machine_secret, planted.bearer, planted.email, planted.password):
        assert v not in dumped
    assert out["machine_secret"] == sb.REDACTED
    assert out["enrollment_token"] == "[NOT SET]"  # unset stays visibly unset: useful for support, leaks nothing
    assert out["api_url"] == "https://x.test"
    assert out["list"][1:] == [5, True]


# --------------------------------------------------------------------------- reading logs
def test_log_tail_is_bounded_and_decodes_utf16(tmp_path):
    p = tmp_path / "agent.log"
    p.write_bytes(("".join(f"line {i}\r\n" for i in range(10_000))).encode("utf-8"))
    tail = sb.read_log_tail(p, max_lines=50, max_bytes=4096)
    lines = tail.split("\n")
    assert len(lines) <= 50 and lines[-1].startswith("line ") and "\r" not in tail
    assert "line 0" not in tail  # the head of the file is never read
    u16 = tmp_path / "ps.log"
    u16.write_bytes("héllo wörld\r\nsecond\r\n".encode("utf-16"))
    assert sb.read_log_tail(u16).startswith("héllo")
    bad = tmp_path / "bad.log"
    bad.write_bytes(b"ok \xff\xfe\x80 end")
    assert "ok" in sb.read_log_tail(bad)  # undecodable bytes never raise


def test_very_long_lines_are_cut(tmp_path):
    p = tmp_path / "long.log"
    p.write_text("x" * 50_000 + "\n", encoding="utf-8")
    assert all(len(ln) <= sb.MAX_LINE_CHARS + 20 for ln in sb.read_log_tail(p).split("\n"))


def test_log_entry_names_cannot_escape_the_zip():
    for nasty in ("..\\..\\evil.log", "../../evil.log", "C:\\x\\y.log", "a b?.log"):
        name = sb._log_entry_name(1, Path(nasty))
        assert name.startswith("logs/") and ".." not in name.split("/") and "\\" not in name and ":" not in name


# --------------------------------------------------------------------------- the zip
def _config(planted: Planted) -> AgentConfig:
    return AgentConfig(machine_id="m-1", provider_id="p-1", machine_secret=planted.machine_secret,
                       enrollment_token=planted.enroll_token, signing_secret=planted.signing_secret,
                       api_url=f"https://svc:{planted.url_password}@api.example.test")


def _logs(tmp_path: Path, planted: Planted) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("\n".join(planted.log_lines()) + "\n", encoding="utf-8")
    (logs / "supervisor.log").write_bytes(("\r\n".join(planted.log_lines()) + "\r\n").encode("utf-16"))  # PowerShell style
    return logs


def test_bundle_contains_no_planted_secret_in_any_entry(tmp_path, planted, monkeypatch):
    canary = "canary-" + _hex(8)
    monkeypatch.setenv("COMPUTECOIN_SUPPORT_TEST_CANARY", canary)  # a plain env var: the environment is never dumped
    monkeypatch.setenv("MY_SERVICE_API_TOKEN", planted.bearer + "-env")  # a secret-looking env var: scrubbed even if logged
    out = tmp_path / "bundle.zip"
    log_dir = _logs(tmp_path, planted)
    manifest = sb.build_bundle(
        out, config=_config(planted), log_dir=log_dir, include_hardware=False,
        extra_sections={"custom": lambda: {"note": f"token={planted.password}", "mail": planted.email}},
    )
    files = _all_bytes(out)
    _assert_clean(files, planted.values() + [planted.bearer + "-env"])
    assert not any(canary in d.decode("utf-8") for d in files.values())  # no environment dump
    assert {"README.txt", "manifest.json", "versions.json", "platform.json", "config.redacted.json"} <= set(files)
    assert manifest["redaction_total"] > 0 and len(manifest["logs"]) == 2
    # The config entry shows that credentials exist without showing them.
    cfg = json.loads(files["config.redacted.json"])
    assert cfg["machine_secret"] == sb.REDACTED and cfg["machine_id"] == "m-1"


def test_manifest_hashes_match_and_names_are_safe(tmp_path, planted):
    out = tmp_path / "b.zip"
    sb.build_bundle(out, config=_config(planted), log_dir=_logs(tmp_path, planted), include_hardware=False)
    files = _all_bytes(out)
    manifest = json.loads(files["manifest.json"])
    for name, meta in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == meta["sha256"] and len(files[name]) == meta["bytes"]
    for name in files:
        assert not name.startswith("/") and ".." not in name.split("/") and "\\" not in name and ":" not in name
    assert all(b"\r\n" not in d for n, d in files.items() if n.endswith(".json"))  # LF, whatever the OS


def test_a_leak_fails_closed_and_leaves_nothing_behind(tmp_path, planted, monkeypatch):
    monkeypatch.setattr(sb.Redactor, "text", lambda self, t: t)  # the scrubber is broken
    out = tmp_path / "must-not-exist.zip"
    with pytest.raises(sb.BundleLeakError):
        sb.build_bundle(out, config=_config(planted), log_dir=_logs(tmp_path, planted), include_hardware=False)
    assert not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".bundle-")]


def test_refuses_to_overwrite(tmp_path):
    out = tmp_path / "exists.zip"
    out.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        sb.build_bundle(out, config=None, include_hardware=False, log_dir=tmp_path / "none")
    assert out.read_bytes() == b"old"


def test_a_missing_config_and_missing_logs_still_give_a_bundle(tmp_path):
    out = tmp_path / "min.zip"
    manifest = sb.build_bundle(out, config=None, include_hardware=False, log_dir=tmp_path / "nope")
    assert manifest["logs"] == [] and out.exists()
    assert json.loads(_all_bytes(out)["config.redacted.json"]) == {"available": False}


def test_a_failing_collector_is_reported_by_class_only(tmp_path, planted):
    secret = planted.password

    def boom():
        raise ValueError(f"bad value {secret}")  # a pydantic-style error that echoes the input

    out = tmp_path / "boom.zip"
    sb.build_bundle(out, config=None, include_hardware=False, log_dir=tmp_path / "none", extra_sections={"boom": boom})
    entry = _all_bytes(out)["boom.json"].decode("utf-8")
    assert json.loads(entry) == {"error": "ValueError"} and secret not in entry


def test_zip_member_order_and_readme_are_stable(tmp_path):
    out = tmp_path / "s.zip"
    sb.build_bundle(out, config=None, include_hardware=False, log_dir=tmp_path / "none")
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist()[:2] == ["README.txt", "manifest.json"]
        assert b"does NOT contain your credentials" in zf.read("README.txt")


# --------------------------------------------------------------------------- the command
def test_cli_support_bundle_writes_a_clean_zip(tmp_path, planted, monkeypatch):
    from computecoin_agent.cli import app

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("COMPUTECOIN_CONFIG_DIR", str(cfg_dir))
    _config(planted).save(cfg_dir / "agent.toml")
    out = tmp_path / "cli.zip"
    result = CliRunner().invoke(app, ["support-bundle", "--out", str(out), "--log-dir", str(_logs(tmp_path, planted)), "--no-hardware"])
    assert result.exit_code == 0, result.output
    assert "Support bundle written" in result.output and "must not contain" in result.output
    for v in planted.values():
        assert v not in result.output
    _assert_clean(_all_bytes(out), planted.values())
    again = CliRunner().invoke(app, ["support-bundle", "--out", str(out), "--no-hardware", "--log-dir", str(tmp_path / "none")])
    assert again.exit_code == 1 and "Refusing to overwrite" in again.output


def test_cli_with_a_broken_config_never_echoes_its_contents(tmp_path, monkeypatch):
    from computecoin_agent.cli import app

    value = "gpu-" + _hex(6)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "agent.toml").write_text(f'gpu_vendor = "{value}"\n', encoding="utf-8")  # fails validation, the error would echo it
    monkeypatch.setenv("COMPUTECOIN_CONFIG_DIR", str(cfg_dir))
    out = tmp_path / "broken.zip"
    result = CliRunner().invoke(app, ["support-bundle", "--out", str(out), "--no-hardware", "--log-dir", str(tmp_path / "none")])
    assert result.exit_code == 0 and value not in result.output
    assert value not in b"".join(_all_bytes(out).values()).decode("utf-8")


def test_requirements_section_is_not_applicable_off_windows(tmp_path):
    import sys

    if sys.platform == "win32":
        pytest.skip("on Windows the real read-only probes run")
    assert sb.collect_requirements() == {"available": False, "reason": "the requirements checker applies to Windows hosts"}
    out = tmp_path / "r.zip"
    sb.build_bundle(out, config=None, include_hardware=False, log_dir=tmp_path / "none")
    assert json.loads(_all_bytes(out)["requirements.json"])["available"] is False
