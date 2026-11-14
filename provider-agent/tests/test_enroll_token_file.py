"""`enroll --token-file`: the installer hands the one-time token over in a private file, not on a command line."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from computecoin_agent import cli

TOKEN = "enr_abcdefgh12345678XYZ"


def test_exactly_one_way_of_giving_the_token():
    r = CliRunner()
    both = r.invoke(cli.app, ["enroll", "--token", TOKEN, "--token-file", "/x"])
    neither = r.invoke(cli.app, ["enroll"])
    for res in (both, neither):
        assert res.exit_code == 2 and "exactly one of --token or --token-file" in res.output


def test_the_token_comes_from_the_first_line_of_the_file(tmp_path):
    f = tmp_path / "t"
    f.write_text(f"  {TOKEN}  \nsecond line ignored\n")
    assert cli._resolve_enrollment_token(None, str(f)) == TOKEN
    assert cli._resolve_enrollment_token(TOKEN, None) == TOKEN


@pytest.mark.parametrize("content", ["", "\n", "   \n"])
def test_an_empty_token_file_is_refused(tmp_path, content):
    f = tmp_path / "t"
    f.write_text(content)
    res = CliRunner().invoke(cli.app, ["enroll", "--token-file", str(f)])
    assert res.exit_code == 2 and "empty" in res.output


def test_a_missing_token_file_is_refused_without_printing_its_path_contents(tmp_path):
    res = CliRunner().invoke(cli.app, ["enroll", "--token-file", str(tmp_path / "nope")])
    assert res.exit_code == 2 and "Could not read the token file" in res.output


def test_the_token_from_the_file_is_what_gets_used(tmp_path, monkeypatch):
    f = tmp_path / "t"
    f.write_text(TOKEN + "\n")
    seen = {}

    def fake_remote(server, token, report):
        seen["server"], seen["token"] = server, token
        raise cli.EnrollmentError("stop here")

    monkeypatch.setattr(cli, "enroll_remote", fake_remote)
    monkeypatch.setattr(cli, "build_capability_report", lambda **kw: object())
    monkeypatch.setattr(cli, "check_dual_gpu_vendor_conflict", lambda v: False)
    res = CliRunner().invoke(cli.app, ["enroll", "--token-file", str(f), "--server", "https://api.example.com"])
    assert seen == {"server": "https://api.example.com", "token": TOKEN}
    assert res.exit_code == 1 and TOKEN not in res.output  # the token is never echoed back
