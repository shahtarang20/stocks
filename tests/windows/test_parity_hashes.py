"""Parity test (plan section 8, W15.2): the same Kit, the same inputs, the same image digest on a Linux and a Windows host must give
equal output hashes where the Kit is deterministic. The comparison logic runs everywhere; the real comparison needs two recorded
artifact folders. WRITTEN, NOT RUN.
"""

from __future__ import annotations

import json

import pytest

from e2e_support import compare_manifests, hash_tree, read_manifest, sha256_file, write_manifest


# ---- logic tests: run on every OS in CI ----------------------------------------------------------------------------------------
def test_equal_trees_are_a_pass(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "sub").mkdir(parents=True)
        (d / "out.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        (d / "sub" / "r.json").write_bytes(b'{"ok":true}\n')
    rep = compare_manifests(hash_tree(a), hash_tree(b))
    assert rep.ok and rep.equal == ["out.png", "sub/r.json"], rep.summary()


def test_a_single_changed_byte_is_a_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "o.bin").write_bytes(b"abc")
    (b / "o.bin").write_bytes(b"abd")
    rep = compare_manifests(hash_tree(a), hash_tree(b))
    assert not rep.ok and rep.different == ["o.bin"]


def test_crlf_versus_lf_is_a_difference_not_hidden(tmp_path):
    """Bytes are compared as bytes: a text file that picked up Windows line endings must NOT count as equal."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "log.txt").write_bytes(b"line\n")
    (b / "log.txt").write_bytes(b"line\r\n")
    assert compare_manifests(hash_tree(a), hash_tree(b)).different == ["log.txt"]


def test_missing_and_extra_files_fail(tmp_path):
    rep = compare_manifests({"a": "1", "w": "2"}, {"a": "1", "l": "3"})
    assert not rep.ok and rep.only_windows == ["w"] and rep.only_linux == ["l"]


def test_an_empty_comparison_proves_nothing():
    assert compare_manifests({}, {}).ok is False


def test_declared_nondeterministic_files_are_listed_not_compared():
    rep = compare_manifests({"a": "1", "t.log": "x"}, {"a": "1", "t.log": "y"}, nondeterministic=["t.log"])
    assert rep.ok and rep.skipped_nondeterministic == ["t.log"]


def test_manifest_keys_use_forward_slashes_and_file_is_lf(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_bytes(b"x")
    m = hash_tree(tmp_path)
    assert list(m) == ["d/f.txt"]
    target = tmp_path.parent / (tmp_path.name + "-manifest.json")
    write_manifest(target, m)
    assert b"\r" not in target.read_bytes() and read_manifest(target) == m
    target.unlink()


def test_sha256_is_computed_over_raw_bytes(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\r\n")
    import hashlib

    assert sha256_file(p) == hashlib.sha256(b"\r\n").hexdigest()


# ---- the real comparison: needs the two artifact folders the workflows produce -----------------------------------------------------
@pytest.mark.windows_gpu
def test_windows_outputs_equal_the_linux_host_outputs(settings):
    assert settings.linux_artifact_dir, "CC_LINUX_ARTIFACT_DIR must point at the Linux host's artifact folder (downloaded from the Linux run)"
    win = read_manifest(settings.artifact_dir / "windows-kit-manifest.json")
    lin = read_manifest(settings.linux_artifact_dir / "linux-kit-manifest.json")
    declared = json.loads((settings.linux_artifact_dir / "nondeterministic.json").read_text(encoding="utf-8")) \
        if (settings.linux_artifact_dir / "nondeterministic.json").exists() else []
    rep = compare_manifests(win, lin, nondeterministic=declared)
    assert rep.ok, rep.summary()
    # Same image digest on both sides: a different digest makes equal hashes meaningless.
    wd = json.loads((settings.artifact_dir / "windows-kit-report.json").read_text(encoding="utf-8")).get("image")
    ld = json.loads((settings.linux_artifact_dir / "linux-kit-report.json").read_text(encoding="utf-8")).get("image")
    assert wd and wd == ld, (wd, ld)
