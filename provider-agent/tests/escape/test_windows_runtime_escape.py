"""Windows runtime escape tests (cross-platform plan W11.2, X6). WRITTEN, NEVER RUN.

Opt in with BOTH ``COMPUTECOIN_ESCAPE_TESTS=1`` and ``COMPUTECOIN_WINDOWS_RUNTIME=1`` on a Windows host that already has the
managed distro ``computecoin`` provisioned (``provision.ps1 -Action provision``) and NVIDIA-free is fine: these tests use a CPU
container. Every attempt from inside a job container must FAIL:

  * read Windows files through /mnt/c (and other drive mounts);
  * run Windows programs through WSL interop (cmd.exe, powershell.exe, explorer.exe);
  * reach the Windows host or the owner's LAN;
  * reach cloud metadata addresses.

A probe prints ``ESCAPED`` the moment something works, ``BLOCKED`` only after every attempt failed. The container argv is the
REAL ``DockerExecutor.build_argv_parts`` (production hardening flags); only the command is replaced, and the whole command line
is run INSIDE the distro with ``wsl.exe --exec`` as the runner user (no shell anywhere).

Environment:
  COMPUTECOIN_ESCAPE_IMAGE            digest-pinned image with ``python3`` (pulled inside the distro)
  COMPUTECOIN_WINDOWS_LAN_PROBES      optional, comma separated extra LAN addresses of this PC / router to try
  COMPUTECOIN_EGRESS_PROXY_IMAGE      optional, enables the egress proxy denial test
TODO(verify-docs): wsl.exe flags used here, and the WSL NAT gateway discovery (``ip route``).
"""

from __future__ import annotations

import os
import sys

import pytest

ENABLED = os.environ.get("COMPUTECOIN_ESCAPE_TESTS") == "1" and os.environ.get("COMPUTECOIN_WINDOWS_RUNTIME") == "1"
pytestmark = pytest.mark.skipif(
    not (ENABLED and sys.platform == "win32"),
    reason="opt-in: COMPUTECOIN_ESCAPE_TESTS=1 and COMPUTECOIN_WINDOWS_RUNTIME=1 on a Windows host with the runtime distro",
)

IMAGE = os.environ.get("COMPUTECOIN_ESCAPE_IMAGE", "")
PROXY_IMAGE = os.environ.get("COMPUTECOIN_EGRESS_PROXY_IMAGE", "")
RUNNER_USER = "ccrunner"


@pytest.fixture(scope="module")
def cli():
    from computecoin_agent.windows.runtime.wsl import WslCli, subprocess_runner

    c = WslCli(subprocess_runner, "computecoin")
    if c.distro_info() is None:
        pytest.skip("the runtime distro 'computecoin' is not provisioned")
    return c


@pytest.fixture(scope="module")
def image(cli):
    if not IMAGE or "@sha256:" not in IMAGE:
        pytest.skip("set COMPUTECOIN_ESCAPE_IMAGE to a digest-pinned image that has python3")
    cli.exec_ok(["docker", "pull", IMAGE], what="pull the probe image", user=RUNNER_USER, timeout=900)
    return IMAGE


@pytest.fixture(scope="module")
def lan_targets(cli):
    """The WSL NAT gateway (this Windows host as seen from the distro) plus common LAN gateways and any operator extras."""
    targets = {"192.168.0.1", "192.168.1.1", "10.0.0.1", "172.17.0.1"}
    route = cli.exec(["ip", "-4", "route", "show", "default"], user=RUNNER_USER, timeout=30)
    parts = route.stdout.split()
    if "via" in parts:
        targets.add(parts[parts.index("via") + 1])
    for extra in os.environ.get("COMPUTECOIN_WINDOWS_LAN_PROBES", "").split(","):
        if extra.strip():
            targets.add(extra.strip())
    return sorted(targets)


@pytest.fixture
def run_probe(cli, image):
    from computecoin_agent.executor import DockerExecutor, JobSpec

    def _run(code: str, *, timeout: int = 120):
        outdir = cli.exec_ok(["mktemp", "-d", "/tmp/ccoin-winescape-XXXXXX"], what="make output dir", user=RUNNER_USER).stdout.strip()
        try:
            cli.exec_ok(["chmod", "0777", outdir], what="chmod output dir", user=RUNNER_USER)
            spec = JobSpec(job_id="win-escape", workload_type="byoc", model="probe", input={}, execution_mode="hardware",
                           image_digest=image, isolation_tier="container",
                           limits={"memory_mb": 256, "cpus": "1", "pids_limit": 64, "gpu_count": 0, "max_runtime_seconds": timeout})
            prefix, img, suffix = DockerExecutor().build_argv_parts(spec, outdir)
            argv = [*prefix, "--entrypoint", "python3", img, *suffix, "-c", code]
            res = cli.exec(argv, user=RUNNER_USER, timeout=timeout + 30)
            return res.returncode, res.stdout, res.stderr
        finally:
            cli.exec(["rm", "-rf", outdir], user=RUNNER_USER, timeout=60)

    return _run


def assert_blocked(result):
    code, out, err = result
    assert "ESCAPED" not in out, f"escape succeeded: {out!r}"
    assert "BLOCKED" in out, f"probe did not run to completion (rc={code}): {out!r} {err[-500:]!r}"


def test_control_harness_works(run_probe):
    """Not an attack: proves the harness runs a container in the distro, so the BLOCKED results below are real."""
    code, out, err = run_probe("import os\nopen('/tmp/ok','w').write('x')\nprint('CONTROL_OK', os.getuid())\n")
    assert "CONTROL_OK" in out, (out, err[-500:])
    assert " 0" not in out.split("CONTROL_OK", 1)[1][:3], "container runs as root"


def test_windows_drive_not_readable(run_probe):
    assert_blocked(run_probe(
        "import os\n"
        "bad=[]\n"
        "for p in ('/mnt/c','/mnt/d','/mnt/host','/mnt/wsl','/run/desktop','/mnt/c/Windows/System32/drivers/etc/hosts',\n"
        "          '/mnt/c/Users','/c','/host_mnt'):\n"
        "    if os.path.exists(p): bad.append(p)\n"
        "try:\n    bad += ['listdir:'+','.join(os.listdir('/mnt'))] if os.listdir('/mnt') else []\n"
        "except OSError: pass\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_no_windows_backed_mount_in_container(run_probe):
    assert_blocked(run_probe(
        "bad=[]\n"
        "for line in open('/proc/mounts').read().splitlines():\n"
        "    f=line.split()\n"
        "    if len(f)>=3 and f[2] in ('9p','drvfs','virtiofs') and not f[1].startswith('/usr/lib/wsl'): bad.append(f[1])\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_windows_interop_unavailable(run_probe):
    assert_blocked(run_probe(
        "import os, shutil, subprocess\n"
        "bad=[]\n"
        "for exe in ('cmd.exe','powershell.exe','explorer.exe','wsl.exe','clip.exe'):\n"
        "    if shutil.which(exe): bad.append('which:'+exe)\n"
        "for p in ('/mnt/c/Windows/System32/cmd.exe','/proc/sys/fs/binfmt_misc/WSLInterop','/proc/sys/fs/binfmt_misc/WSLInterop-late',\n"
        "          '/run/WSL','/init'):\n"
        "    if os.path.exists(p): bad.append('exists:'+p)\n"
        "if os.environ.get('WSL_INTEROP') or os.environ.get('WSL_DISTRO_NAME'): bad.append('env')\n"
        "for argv in (['cmd.exe','/c','echo','ESCAPED_EXEC'],['powershell.exe','-NoProfile','-Command','echo ESCAPED_EXEC']):\n"
        "    try:\n        r=subprocess.run(argv,capture_output=True,timeout=10)\n        bad.append('ran:'+argv[0]+':'+str(r.returncode))\n"
        "    except (OSError, subprocess.SubprocessError): pass\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_windows_host_and_lan_unreachable(run_probe, lan_targets):
    targets = repr(list(lan_targets))
    assert_blocked(run_probe(
        "import socket\n"
        f"for host in {targets}:\n"
        "    for port in (80, 135, 139, 445, 3389, 5985, 22, 53):\n"
        "        try:\n            s=socket.create_connection((host,port),timeout=2); print('ESCAPED', host, port); raise SystemExit(0)\n"
        "        except OSError: pass\n"
        "print('BLOCKED')\n"))


def test_cloud_metadata_unreachable(run_probe):
    assert_blocked(run_probe(
        "import socket\n"
        "for host in ('169.254.169.254','metadata.google.internal','100.100.100.200','fd00:ec2::254'):\n"
        "    try:\n        s=socket.create_connection((host,80),timeout=3); print('ESCAPED', host); raise SystemExit(0)\n"
        "    except OSError: pass\n"
        "print('BLOCKED')\n"))


def test_no_network_at_all(run_probe):
    assert_blocked(run_probe(
        "import socket\n"
        "try:\n    socket.create_connection(('1.1.1.1',443),timeout=3); print('ESCAPED internet')\n"
        "except OSError: print('BLOCKED')\n"))


def test_distro_hardening_holds_for_the_runtime_itself(cli):
    """Host-side view of the same boundary: the distro's own health checks for X6 must pass."""
    from computecoin_agent.windows.runtime.health import run_health

    rep = run_health(cli, None, runner_user=RUNNER_USER)
    by_name = {c.name: c for c in rep.checks}
    for name in ("wsl_conf_hardened", "no_windows_mounts", "interop_disabled", "ssh_disabled", "no_network_listeners"):
        assert by_name[name].ok, f"{name}: {by_name[name].detail}"


@pytest.mark.skipif(not PROXY_IMAGE, reason="set COMPUTECOIN_EGRESS_PROXY_IMAGE to test the egress proxy denials")
def test_egress_proxy_denies_metadata_and_unlisted_hosts(cli):
    from computecoin_agent.windows.runtime.network import verify_egress_proxy

    checks = verify_egress_proxy(cli, PROXY_IMAGE, runner_user=RUNNER_USER)
    failed = [c for c in checks if not c.ok]
    assert not failed, [f"{c.name}: {c.detail}" for c in failed]
