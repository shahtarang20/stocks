"""Each test is one attack from inside a BYOC container. EVERY attempt must FAIL (the probe prints BLOCKED).

A probe prints ESCAPED the moment something it should not be able to do works, and BLOCKED only if it reached the end
with every attempt refused. The control test proves the harness itself works (writes to /tmp succeed).
"""

from __future__ import annotations

import os

import pytest

from .conftest import assert_blocked

pytestmark = pytest.mark.skipif(os.environ.get("COMPUTECOIN_ESCAPE_TESTS") != "1", reason="opt-in: COMPUTECOIN_ESCAPE_TESTS=1")


def test_control_harness_works(run_probe):
    """Not an attack: the allowed scratch dir is writable and the user is not root. Proves failures below are real."""
    code, out, err = run_probe(
        "import os\nopen('/tmp/ok','w').write('x')\nprint('CONTROL_OK', os.getuid())\n"
    )
    assert "CONTROL_OK" in out, (out, err[-500:])
    assert " 0" not in out.split("CONTROL_OK", 1)[1][:3], "container runs as root"


def test_write_to_rootfs_fails(run_probe):
    assert_blocked(run_probe(
        "import sys\n"
        "for p in ('/etc/escape','/usr/escape','/bin/escape','/escape','/proc/sys/kernel/escape'):\n"
        "    try:\n        open(p,'w').write('x'); print('ESCAPED', p); sys.exit(0)\n"
        "    except OSError: pass\n"
        "print('BLOCKED')\n"))


def test_mount_fails(run_probe):
    assert_blocked(run_probe(
        "import ctypes\nlibc=ctypes.CDLL(None,use_errno=True)\n"
        "r=libc.mount(b'tmpfs',b'/mnt',b'tmpfs',0,None)\n"
        "print('ESCAPED mount') if r==0 else print('BLOCKED')\n"))


def test_docker_socket_not_reachable(run_probe):
    assert_blocked(run_probe(
        "import os\n"
        "bad=[p for p in ('/var/run/docker.sock','/run/docker.sock','/run/containerd/containerd.sock','/var/run/podman/podman.sock') if os.path.exists(p)]\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_host_pid_namespace_not_shared(run_probe):
    assert_blocked(run_probe(
        "import os\n"
        "pids=[d for d in os.listdir('/proc') if d.isdigit()]\n"
        "comm=open('/proc/1/comm').read().strip()\n"
        "bad = len(pids) > 20 or comm in ('systemd','init','launchd')\n"
        "print('ESCAPED', len(pids), comm) if bad else print('BLOCKED')\n"))


def test_host_network_namespace_not_shared(run_probe):
    assert_blocked(run_probe(
        "import os\n"
        "nics=set(os.listdir('/sys/class/net')) if os.path.isdir('/sys/class/net') else set()\n"
        "bad = bool(nics - {'lo'})\n"
        "print('ESCAPED', nics) if bad else print('BLOCKED')\n"))


def test_cannot_read_host_secrets_via_proc_1_environ(run_probe):
    assert_blocked(run_probe(
        "try:\n    data=open('/proc/1/environ','rb').read()\nexcept OSError:\n    data=b''\n"
        "bad=[k for k in (b'COMPUTECOIN_MACHINE_SECRET',b'host-canary-secret',b'SSH_AUTH_SOCK',b'SUDO_USER',b'XDG_SESSION_ID') if k in data]\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_cloud_metadata_unreachable(run_probe):
    assert_blocked(run_probe(
        "import socket\n"
        "for host in ('169.254.169.254','metadata.google.internal','100.100.100.200'):\n"
        "    try:\n        s=socket.create_connection((host,80),timeout=3); print('ESCAPED', host); raise SystemExit(0)\n"
        "    except OSError: pass\n"
        "print('BLOCKED')\n"))


def test_no_network_at_all(run_probe):
    assert_blocked(run_probe(
        "import socket\n"
        "try:\n    socket.create_connection(('1.1.1.1',443),timeout=3); print('ESCAPED internet')\n"
        "except OSError: print('BLOCKED')\n"))


def test_device_nodes_unavailable(run_probe):
    assert_blocked(run_probe(
        "import os,stat\n"
        "bad=[d for d in ('/dev/mem','/dev/kmem','/dev/kmsg','/dev/kvm','/dev/sda','/dev/nvme0n1','/dev/fuse','/dev/disk') if os.path.exists(d)]\n"
        "try:\n    os.mknod('/tmp/hostdisk', 0o600|stat.S_IFBLK, os.makedev(8,0)); bad.append('mknod-worked')\n"
        "except OSError: pass\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))


def test_ptrace_fails(run_probe):
    assert_blocked(run_probe(
        "import ctypes\nlibc=ctypes.CDLL(None,use_errno=True)\n"
        "PTRACE_ATTACH=16\nr=libc.ptrace(PTRACE_ATTACH,1,0,0)\n"
        "print('ESCAPED ptrace pid1') if r==0 else print('BLOCKED')\n"))


def test_capabilities_empty_and_no_new_privs(run_probe):
    assert_blocked(run_probe(
        "st=dict(l.split(':\\t',1) for l in open('/proc/self/status').read().splitlines() if ':\\t' in l)\n"
        "bad=[k for k in ('CapEff','CapPrm','CapBnd') if int(st.get(k,'0').strip(),16)!=0]\n"
        "if st.get('NoNewPrivs','1').strip()!='1': bad.append('NoNewPrivs')\n"
        "print('ESCAPED', bad) if bad else print('BLOCKED')\n"))
