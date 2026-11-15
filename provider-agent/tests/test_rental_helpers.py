"""Container rental, host side: the helpers that touch the host's disk and the network's strings (design note section 8)."""

from __future__ import annotations

import os
import sys
import stat

import pytest

from computecoin_agent import rental as r
from computecoin_agent.rental import RentalError

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="container rental is Linux-only (a Windows host never reports the flag)")

SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


# ---- names from the network -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("good", ["data.csv", "a/b/c.txt", "My Data/file name.bin", "x" * 100, "a" * 100 + "/" + "b" * 100, ".hidden/f", "v1.2+rc=3@x/y"])
def test_good_paths_are_accepted_unchanged(good):
    assert r.clean_relpath(good) == good


@pytest.mark.parametrize("bad", [
    "", "/abs/path", "../x", "a/../b", "a/./b", "a//b", "a/", "./a", "a\\b", "a\x00b", "a\nb", "a\tb", " lead", "trail ", "a/ b", "a /b",
    "x" * 101, "a" * 241, "ünï/code", "semi;colon", "dollar$", "back`tick", "q?x", "star*", "pipe|x", "a/..", "..", ".", None, 5, [],
    "a" * 120 + "/" + "b" * 130,
])
def test_bad_paths_are_refused(bad):
    with pytest.raises(RentalError):
        r.clean_relpath(bad)


def test_the_results_folder_is_off_limits_for_inputs_only():
    assert r.clean_relpath("results/model.pt") == "results/model.pt"
    with pytest.raises(RentalError):
        r.clean_relpath("results/model.pt", allow_results_prefix=False)
    assert r.clean_relpath("resultsx/model.pt", allow_results_prefix=False) == "resultsx/model.pt"


def test_resolve_inside_refuses_a_link_on_the_way_and_stays_inside(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "real").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "evil")
    os.symlink(tmp_path / "nothing", root / "dangling")
    assert r.resolve_inside(str(root), "real/new.txt") == str(root / "real" / "new.txt")
    assert r.resolve_inside(str(root), "fresh/dir/f") == str(root / "fresh" / "dir" / "f")  # not there yet: fine
    for bad in ("evil/x", "evil", "dangling", "dangling/x"):
        with pytest.raises(RentalError):
            r.resolve_inside(str(root), bad)
    with pytest.raises(RentalError):
        r.resolve_inside(str(root), "../outside/x")


# ---- the miner list -------------------------------------------------------------------------------------------------
def test_miners_are_found_by_process_name_and_nothing_else_is_returned():
    out = "COMMAND\nbash\npython3\n/usr/bin/XMRig\nsleep\n"
    assert r.detect_miner(out) == "xmrig"
    assert r.detect_miner("COMMAND\nt-rex\n") == "t-rex" and r.detect_miner("COMMAND\nlolMiner\n") == "lolminer"
    assert r.detect_miner("COMMAND\nkawpowminer-x\n") == "kawpowminer"
    for fine in ("", "COMMAND\npython3\njupyter-lab\ntrain.py\nnvidia-smi\nminer-tutorial-notes\n", "comm\n"):
        assert r.detect_miner(fine) is None
    assert r.detect_miner(None) is None


# ---- the container command guard ------------------------------------------------------------------------------------
GOOD = ["docker", "run", "-d", "--name", "n", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", "type=bind,source=/srv/a,target=/out", "--mount", "type=bind,source=/srv/b,target=/workspace", "--tmpfs", "/tmp:rw",
        "--memory", "8192m", "-e", "HOME=/workspace", "img@sha256:" + "a" * 64]


def test_a_normal_rental_command_passes():
    r.assert_rental_argv_safe(GOOD, ["/srv/a", "/srv/b"])
    r.assert_rental_argv_safe([*GOOD[:5], "--network", "ccoin-net-abc", *GOOD[7:]], ["/srv/a", "/srv/b"])  # the egress network is fine


@pytest.mark.parametrize("extra", [
    ["--privileged"], ["-p", "80:80"], ["--publish", "80:80"], ["--publish=80:80"], ["-P"], ["--device", "/dev/kvm"], ["--device=/dev/kvm"],
    ["--cap-add", "SYS_ADMIN"], ["--cap-add=ALL"], ["--pid", "host"], ["--pid=host"], ["--ipc", "host"], ["--ipc=host"],
    ["--network", "host"], ["--network=host"], ["--net=host"], ["--network=container:x"], ["--userns", "host"], ["--uts", "host"],
    ["--security-opt", "seccomp=unconfined"], ["--security-opt=apparmor=unconfined"], ["--volumes-from", "x"], ["--sysctl", "a=b"],
    ["--add-host", "x:1.2.3.4"], ["-v", "/var/run/docker.sock:/s"], ["--volume=/var/run/docker.sock:/s"],
    ["--mount", "type=bind,source=/etc,target=/etc"], ["--mount", "type=volume,source=v,target=/v"], ["-v", "/home:/home"],
    ["--mount", "type=bind,source=/srv/a/../../etc,target=/e"], ["-e", "X=/run/containerd/containerd.sock"],
])
def test_dangerous_additions_are_refused(extra):
    with pytest.raises(RentalError):
        r.assert_rental_argv_safe([*GOOD[:-1], *extra, GOOD[-1]], ["/srv/a", "/srv/b"])


def test_a_bind_mount_through_a_link_to_somewhere_else_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink("/etc", link)
    with pytest.raises(RentalError):
        r.assert_rental_argv_safe(["docker", "--mount", f"type=bind,source={link},target=/workspace"], [str(real)])


# ---- limits ---------------------------------------------------------------------------------------------------------
S = r.RentalSettings(allowed=True, root="/x", max_workspace_gb=50, max_memory_gb=0, max_cpus=0)


def test_limits_follow_the_request_and_are_capped_by_the_host():
    mem, cpus, shm, quota = r.resolve_limits(S, {"memory_mb": 8192, "cpus": 4, "workspace_gb": 10}, total_memory_mb=32768, cpu_count=16)
    assert (mem, cpus, shm, quota) == (8192, 4, 2048, 10 * r.GIB)
    mem, cpus, shm, quota = r.resolve_limits(S, {"memory_mb": 262144, "cpus": 64, "workspace_gb": 100}, total_memory_mb=32768, cpu_count=16)
    assert mem == int(32768 * 0.9) and cpus == 15 and quota == 50 * r.GIB  # 90 percent of memory, all but one core, the host's disk cap


def test_host_settings_override_the_automatic_caps_and_defaults_apply():
    s = r.RentalSettings(allowed=True, root="/x", max_workspace_gb=20, max_memory_gb=12, max_cpus=3)
    mem, cpus, shm, quota = r.resolve_limits(s, {"memory_mb": 99999, "cpus": 99, "workspace_gb": 99}, total_memory_mb=1 << 20, cpu_count=64)
    assert (mem, cpus, quota) == (12 * 1024, 3, 20 * r.GIB)
    mem, cpus, shm, quota = r.resolve_limits(S, {}, total_memory_mb=65536, cpu_count=32)
    assert (mem, cpus) == (r.DEFAULT_MEMORY_MB, r.DEFAULT_CPUS) and quota == r.GIB  # workspace_gb 0 means the 1 GiB floor


@pytest.mark.parametrize("junk", [True, "8", -5, None, 1.5, [], {}])
def test_junk_limit_values_fall_back_to_defaults(junk):
    mem, cpus, _shm, quota = r.resolve_limits(S, {"memory_mb": junk, "cpus": junk, "workspace_gb": junk}, total_memory_mb=65536, cpu_count=32)
    assert mem >= 256 and cpus >= 1 and quota == r.GIB


def test_a_tiny_host_never_gets_a_zero_limit():
    mem, cpus, shm, quota = r.resolve_limits(S, {"memory_mb": 4096, "cpus": 8}, total_memory_mb=256, cpu_count=1)
    assert mem >= 256 and cpus == 1 and shm >= 64


def test_settings_come_from_the_config_and_a_real_true_is_required():
    from types import SimpleNamespace

    cfg = SimpleNamespace(allow_container_rental=True, rental_root="/srv/rent", rental_max_workspace_gb=7, rental_max_memory_gb=3, rental_max_cpus=2, work_dir=None)
    assert r.RentalSettings.from_config(cfg) == r.RentalSettings(True, "/srv/rent", 7, 3, 2)
    for lookalike in ("true", 1, None, "yes"):
        cfg.allow_container_rental = lookalike
        assert r.RentalSettings.from_config(cfg).allowed is False
    cfg.rental_root, cfg.work_dir = None, "/data/work"
    assert r.RentalSettings.from_config(cfg).root == "/data/work/rental"


# ---- measuring the workspace ----------------------------------------------------------------------------------------
def test_size_counts_real_files_and_never_follows_links(tmp_path):
    ws = tmp_path / "w"
    (ws / "d").mkdir(parents=True)
    (ws / "a.bin").write_bytes(b"x" * 10_000)
    (ws / "d" / "b.bin").write_bytes(b"y" * 20_000)
    big_outside = tmp_path / "big"
    big_outside.write_bytes(b"z" * 5_000_000)
    os.symlink(big_outside, ws / "link_to_big")
    os.symlink(tmp_path, ws / "link_to_dir")
    used, entries, too_many = r.walk_size(str(ws))
    assert not too_many and 30_000 <= used < 200_000  # the outside file and folder behind the links are not counted
    assert entries >= 5


def test_a_sparse_file_counts_by_the_space_it_really_takes(tmp_path):
    f = tmp_path / "sparse"
    with open(f, "wb") as fh:
        fh.truncate(10 * r.GIB)
    used, _e, _t = r.walk_size(str(tmp_path))
    assert used < r.GIB  # a hole is not disk use


def test_too_many_entries_is_reported(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i}").write_bytes(b"1")
    _used, entries, too_many = r.walk_size(str(tmp_path), max_entries=10)
    assert too_many and entries > 10


# ---- the workspace --------------------------------------------------------------------------------------------------
def test_a_workspace_is_created_private_writable_for_the_container_and_deleted(tmp_path):
    ws = r.Workspace(str(tmp_path / "rental"), SID, 5 * r.GIB)
    ws.create()
    assert os.path.isdir(ws.path) and os.path.isdir(os.path.join(ws.path, "results")) and os.path.isdir(os.path.join(ws.path, ".tmp"))
    assert stat.S_IMODE(os.stat(ws.top).st_mode) == 0o700 and stat.S_IMODE(os.stat(ws.stage).st_mode) == 0o700  # private to the agent
    assert stat.S_IMODE(os.stat(ws.path).st_mode) == 0o777  # the container user is someone else
    assert not ws.stage.startswith(ws.path)  # the staging folder is NOT visible to the container
    assert ws.over_quota() is False
    (open(os.path.join(ws.path, "f"), "wb")).write(b"x" * (6 * r.MIB))
    ws.quota_bytes = 1 * r.MIB
    assert ws.over_quota() is True
    ws.delete()
    assert not os.path.exists(ws.top)
    ws.delete()  # deleting twice is harmless


@pytest.mark.parametrize("sid", ["", "../x", "a/b", "short", "zz" * 8, "x" * 80, "id with space", "id;rm"])
def test_a_session_id_that_is_not_a_plain_id_is_refused(tmp_path, sid):
    with pytest.raises(RentalError):
        r.Workspace(str(tmp_path), sid, r.GIB)


@pytest.mark.parametrize("base", ["/tmp/a,b", "/tmp/a\nb", "/tmp/a\x00b"])
def test_a_rental_folder_path_that_would_break_the_mount_option_is_refused(base):
    with pytest.raises(RentalError):
        r.Workspace(base, SID, r.GIB)


def test_not_enough_free_disk_is_refused_and_a_leftover_of_the_same_id_is_never_reused(tmp_path, monkeypatch):
    ws = r.Workspace(str(tmp_path), SID, r.GIB)
    monkeypatch.setattr(r.shutil, "disk_usage", lambda p: type("U", (), {"free": 500 * r.MIB})())
    with pytest.raises(RentalError):
        ws.create(need_free_bytes=0)  # less than the 1 GiB margin
    monkeypatch.undo()
    ws.create()
    with pytest.raises(FileExistsError):
        r.Workspace(str(tmp_path), SID, r.GIB).create()


def test_stale_folders_are_those_without_a_container(tmp_path):
    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    for name in (a, b, "not-an-id"):
        (tmp_path / name).mkdir()
    (tmp_path / "afile").write_text("x")
    stale = r.list_stale(str(tmp_path), {f"ccoin-sess-{a}"})
    assert [os.path.basename(p) for p in stale] == [b]
    assert r.list_stale(str(tmp_path / "missing"), set()) == []


# ---- the safe copy --------------------------------------------------------------------------------------------------
def test_a_regular_file_is_copied_exactly(tmp_path):
    root = tmp_path / "res"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "m.bin").write_bytes(b"model-bytes")
    dest = tmp_path / "staged"
    assert r.copy_regular_nofollow(str(root), "sub/m.bin", str(dest)) == len(b"model-bytes")
    assert dest.read_bytes() == b"model-bytes" and stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_a_link_in_place_of_a_file_or_a_folder_is_never_followed(tmp_path):
    root = tmp_path / "res"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    os.symlink(secret, root / "innocent.txt")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "k").write_text("host key")
    os.symlink(tmp_path / "private", root / "dir")
    for rel in ("innocent.txt", "dir/k"):
        dest = tmp_path / "out"
        with pytest.raises(RentalError):
            r.copy_regular_nofollow(str(root), rel, str(dest))
        assert not dest.exists()  # nothing was written


def test_special_files_and_missing_files_and_oversize_are_refused_without_leftovers(tmp_path):
    root = tmp_path / "res"
    root.mkdir()
    os.mkfifo(root / "pipe")
    (root / "big").write_bytes(b"x" * 100)
    (root / "folder").mkdir()
    for rel, limit in (("pipe", 1000), ("missing", 1000), ("big", 50), ("folder", 1000)):
        dest = tmp_path / "o"
        with pytest.raises(RentalError):
            r.copy_regular_nofollow(str(root), rel, str(dest), max_bytes=limit)
        assert not dest.exists()


def test_a_file_that_grows_while_it_is_copied_is_refused_and_removed(tmp_path, monkeypatch):
    root = tmp_path / "res"
    root.mkdir()
    (root / "f").write_bytes(b"x" * 100)
    dest = tmp_path / "o"
    with pytest.raises(RentalError):
        r.copy_regular_nofollow(str(root), "f", str(dest), max_bytes=99, chunk=10)  # too big when looked at
    assert not dest.exists()
    # looks small when first checked (the size is read before the copy), then more bytes than allowed arrive
    real_fstat = os.fstat
    monkeypatch.setattr(r.os, "fstat", lambda fd: type("S", (), {"st_mode": stat.S_IFREG | 0o600, "st_size": 1})())
    with pytest.raises(RentalError):
        r.copy_regular_nofollow(str(root), "f", str(tmp_path / "o2"), max_bytes=50, chunk=10)
    monkeypatch.setattr(r.os, "fstat", real_fstat)
    assert not (tmp_path / "o2").exists()


def test_the_copy_refuses_a_bad_name_and_does_not_overwrite(tmp_path):
    root = tmp_path / "res"
    root.mkdir()
    (root / "f").write_bytes(b"1")
    (tmp_path / "taken").write_bytes(b"old")
    for bad in ("../f", "/etc/passwd", "a//b"):
        with pytest.raises(RentalError):
            r.copy_regular_nofollow(str(root), bad, str(tmp_path / "o"))
    with pytest.raises(RentalError):
        r.copy_regular_nofollow(str(root), "f", str(tmp_path / "taken"))
    assert (tmp_path / "taken").read_bytes() == b"old"


# ---- listing the results --------------------------------------------------------------------------------------------
def test_results_listing_skips_links_special_files_and_odd_names_and_never_leaves_the_folder(tmp_path):
    res = tmp_path / "results"
    (res / "a" / "b").mkdir(parents=True)
    (res / "a" / "b" / "ok.txt").write_text("1")
    (res / "top.bin").write_text("2")
    (res / "bad;name").write_text("3")
    os.symlink(tmp_path, res / "to_dir")
    os.symlink(res / "top.bin", res / "to_file")
    os.mkfifo(res / "pipe")
    names, skipped, truncated = r.list_results(str(res))
    assert sorted(names) == ["a/b/ok.txt", "top.bin"] and skipped >= 4 and truncated is False


def test_results_listing_stops_at_the_limit(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    for i in range(12):
        (res / f"f{i:02d}").write_text("x")
    names, _skipped, truncated = r.list_results(str(res), max_files=5)
    assert len(names) == 5 and truncated is True and names == sorted(names)
    assert r.list_results(str(tmp_path / "nope")) == ([], 0, False)
