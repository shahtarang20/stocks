"""Paths and names INSIDE the managed Linux distro (never host paths).

X8 note: these are POSIX paths of the guest Linux system that the Windows supervisor addresses through
``wsl.exe --exec``. They are not host file-system access. The X8 lint (PAL owner) should treat this package as
guest-side string constants. All of them are ``PurePosixPath`` strings (X9).
"""

from __future__ import annotations

DISTRO_NAME = "computecoin"
RUNNER_USER = "ccrunner"

WSL_CONF = "/etc/wsl.conf"
STATE_FILE = "/etc/computecoin/provision-state.json"
RUNTIME_ENV_FILE = "/etc/computecoin/runtime.env"  # non-secret: egress proxy image etc. (no credentials, ever)
CONFIG_DIR = "/etc/computecoin"
OPT_DIR = "/opt/computecoin"
VENV_DIR = "/opt/computecoin/venv"
WHEEL_DIR = "/opt/computecoin/wheels"
SLICE_UNIT = "/etc/systemd/system/computecoin.slice"
SLICE_NAME = "computecoin.slice"
DOCKER_DAEMON_JSON = "/etc/docker/daemon.json"
KEYRING_DIR = "/usr/share/keyrings"
APT_SOURCES_DIR = "/etc/apt/sources.list.d"

# Guest paths the health checks look at.
GUEST_MOUNTS = "/proc/mounts"
GUEST_PID1_COMM = "/proc/1/comm"
GUEST_BINFMT_INTEROP = ("/proc/sys/fs/binfmt_misc/WSLInterop", "/proc/sys/fs/binfmt_misc/WSLInterop-late")
GUEST_GPU_DEVICE = "/dev/dxg"  # TODO(verify-docs): WSL2 GPU paravirtualization device
GUEST_GPU_LIB_DIR = "/usr/lib/wsl/lib"  # TODO(verify-docs): where the Windows driver's libcuda is exposed

# 9p / drvfs style mounts that WSL itself creates for GPU support. Any OTHER such mount is a Windows file leak (X6).
# TODO(verify-docs): confirm the exact set on a real WSL2 distro with automount disabled.
ALLOWED_WINDOWS_BACKED_MOUNTS = frozenset({"/usr/lib/wsl/drivers", "/usr/lib/wsl/lib", "/mnt/wslg", "/mnt/wslg/distro"})
