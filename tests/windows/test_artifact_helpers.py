"""Tests for the artifact helpers used by windows-host-e2e.yml. Run everywhere (no GPU). WRITTEN, NOT RUN."""

from __future__ import annotations

import json
import secrets

import pytest

import record_linux_reference as rec
import scan_artifacts
from e2e_support import MUST_BE_FALSE, probe_violations, pinned_image

DIGEST = "ghcr.io/example/kit-x@sha256:" + "ab" * 32
GOOD = {"image": DIGEST, "passed": True, "outputs": {"out/a.png": "cd" * 32}}


def test_a_good_job_line_is_recorded_as_lf_json(tmp_path):
    rec.write_reference(rec.parse_job_output("noise\n" + json.dumps(GOOD) + "\n"), tmp_path)
    assert json.loads((tmp_path / "linux-kit-manifest.json").read_text(encoding="utf-8")) == GOOD["outputs"]
    assert b"\r" not in (tmp_path / "linux-kit-report.json").read_bytes()


@pytest.mark.parametrize("bad", [
    {**GOOD, "passed": False}, {**GOOD, "image": "ghcr.io/example/kit-x:latest"}, {**GOOD, "outputs": {}},
    {**GOOD, "outputs": {"a": "not-a-hash"}}, {k: v for k, v in GOOD.items() if k != "outputs"},
])
def test_a_bad_job_line_is_refused(bad):
    with pytest.raises(ValueError):
        rec.parse_job_output(json.dumps(bad))


def test_empty_output_is_refused():
    with pytest.raises(ValueError):
        rec.parse_job_output("\n\n")


def test_scan_flags_a_secret_and_passes_clean_files(tmp_path):
    (tmp_path / "ok.json").write_text(json.dumps({"image": DIGEST, "passed": True}), encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x80")
    token = "enr_" + secrets.token_hex(16)  # fresh, not a real credential
    (tmp_path / "bad.log").write_text(f"enrolling with COMPUTECOIN_ENROLLMENT_TOKEN={token}\n", encoding="utf-8")
    dirty, skipped = scan_artifacts.scan(tmp_path)
    assert [d.split(" ")[0] for d in dirty] == ["bad.log"] and skipped == ["blob.bin"]
    assert scan_artifacts.main([str(tmp_path)]) == 1
    (tmp_path / "bad.log").unlink()
    assert scan_artifacts.main([str(tmp_path)]) == 0


def test_the_probe_helper_fails_closed():
    assert probe_violations({}) == list(MUST_BE_FALSE)  # a missing result is a violation
    assert probe_violations({k: False for k in MUST_BE_FALSE}) == []
    assert probe_violations({**{k: False for k in MUST_BE_FALSE}, "windows_interop_cmd": True}) == ["windows_interop_cmd"]


def test_pinned_image_refuses_a_moving_tag():
    with pytest.raises(ValueError):
        pinned_image("python:3.12", "CC_PROBE_IMAGE")
    with pytest.raises(ValueError):
        pinned_image(None, "CC_PROBE_IMAGE")
    assert pinned_image(DIGEST, "x") == DIGEST
