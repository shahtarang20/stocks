"""Test doubles for the Windows runtime provisioning tests (written, never run).

``FakeWsl`` is an injectable ``Runner``: it records every argv and simulates just enough of ``wsl.exe`` and of a distro
(files, users, packages, docker, nvidia-smi) for the provisioner and the health checks. An unmodelled command returns exit
127, so a test notices when the code starts using a command the fake does not know. Keys are generated fresh per test (L7).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Optional, Sequence

from computecoin_agent.windows.runtime.manifest import sign_manifest
from computecoin_agent.windows.runtime.wsl import RunResult

ROOTFS = b"rootfs-tarball-bytes"
WHEEL = b"runner-wheel-bytes"
LOCK = b"requirements-lock-bytes"
DIGEST = "a" * 64
IMG = "registry.example/cuda@sha256:" + "b" * 64
PROXY = "registry.example/egress-proxy@sha256:" + "c" * 64
DOCKER_FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
NVIDIA_FPR = "EB693B3035CD5710E231E123A4B469963BF863CC"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def new_keypair() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw_priv, base64.b64encode(raw_pub).decode()


def manifest_dict(**over) -> dict:
    d = {
        "schema": 1, "sequence": 5, "version": "2026.11.1", "arch": "amd64", "min_wsl_version": "2.1.5",
        "rootfs": {"url": "https://dl.example/rootfs.tar.gz", "sha256": sha(ROOTFS), "size_bytes": len(ROOTFS)},
        "docker": {"engine_version": "27.3.1",
                   "repo": {"uri": "https://download.docker.example/linux/ubuntu", "suite": "noble", "component": "stable",
                            "key_url": "https://download.docker.example/gpg", "key_fingerprint": DOCKER_FPR},
                   "packages": {"docker-ce": "5:27.3.1-1~ubuntu.24.04~noble", "docker-ce-cli": "5:27.3.1-1~ubuntu.24.04~noble",
                                "containerd.io": "1.7.22-1"}},
        "nvidia_toolkit": {"version": "1.17.0",
                           "repo": {"uri": "https://nvidia.example/stable/deb/amd64", "suite": "/",
                                    "key_url": "https://nvidia.example/gpgkey", "key_fingerprint": NVIDIA_FPR},
                           "packages": {"nvidia-container-toolkit": "1.17.0-1", "nvidia-container-toolkit-base": "1.17.0-1",
                                        "libnvidia-container-tools": "1.17.0-1", "libnvidia-container1": "1.17.0-1"}},
        "runner": {"version": "0.1.0", "wheel_filename": "computecoin_agent-0.1.0-py3-none-any.whl",
                   "wheel_url": "https://dl.example/runner.whl", "wheel_sha256": sha(WHEEL),
                   "lock_url": "https://dl.example/runner.lock", "lock_sha256": sha(LOCK)},
        "egress_proxy": {"image": PROXY},
        "smoke_test": {"image": IMG},
    }
    for k, v in over.items():
        d[k] = v
    return d


def envelope(d: dict, priv: bytes) -> bytes:
    return sign_manifest(json.dumps(d, sort_keys=True).encode(), priv, "test-key")


class FakeFetcher:
    def __init__(self) -> None:
        self.files = {"https://dl.example/rootfs.tar.gz": ROOTFS, "https://dl.example/runner.whl": WHEEL,
                      "https://dl.example/runner.lock": LOCK}
        self.calls: list[str] = []

    def __call__(self, url: str, dest: str, max_bytes: int) -> None:
        self.calls.append(url)
        data = self.files[url]
        assert len(data) <= max_bytes
        Path(dest).write_bytes(data)


class FakeWsl:
    """A Runner. ``distros`` maps name -> dict(files, users, pkgs, running)."""

    def __init__(self, wsl_version: str = "2.3.26.0", gpus: Optional[list[tuple[str, str]]] = None) -> None:
        self.calls: list[list[str]] = []
        self.stdins: list[Optional[bytes]] = []
        self.distros: dict[str, dict] = {}
        self.wsl_version = wsl_version
        self.gpus = gpus if gpus is not None else [("NVIDIA GeForce RTX 4090", "GPU-11111111-aaaa-bbbb-cccc-000000000001")]
        self.smoke_fail = False
        self.resize_fail = False
        self.egress_probe = "E1 BLOCKED\nE2 HTTP/1.1 403 Forbidden\nE3 HTTP/1.1 403 Forbidden\n"
        self.leak_mount = False
        self.listeners = "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
        self.key_fprs = {"docker": DOCKER_FPR, "nvidia": NVIDIA_FPR}
        self.engine_version = "27.3.1"
        self.interop_present = False

    # ---- helpers ----
    def programs(self) -> list[list[str]]:
        """In-distro argv (everything after --exec)."""
        out = []
        for argv in self.calls:
            if "--exec" in argv:
                out.append(argv[argv.index("--exec") + 1:])
        return out

    def count(self, *prefix: str) -> int:
        n = len(prefix)
        return sum(1 for p in self.programs() if p[:n] == list(prefix) or (p[:1] == ["env"] and p[2:2 + n] == list(prefix)))

    def host_calls(self, flag: str) -> list[list[str]]:
        return [a for a in self.calls if len(a) > 1 and a[1] == flag]

    # ---- Runner ----
    def __call__(self, argv: Sequence[str], *, stdin: Optional[bytes] = None, timeout: Optional[float] = None) -> RunResult:
        argv = list(argv)
        assert all(isinstance(a, str) for a in argv), "argv must be a list of str"
        self.calls.append(argv)
        self.stdins.append(stdin)
        assert argv[0] == "wsl.exe"
        flag = argv[1]
        if flag == "--version":
            return RunResult(0, f"WSL version: {self.wsl_version}\nKernel version: 5.15.0\n")
        if flag == "--list":
            rows = "".join(f"{'*' if i == 0 else ' '} {n:<14}{'Running' if d['running'] else 'Stopped':<16}2\n"
                           for i, (n, d) in enumerate(self.distros.items()))
            return RunResult(0, "  NAME            STATE           VERSION\n" + rows)
        if flag == "--import":
            self.distros[argv[2]] = {"files": {}, "users": set(), "pkgs": {}, "running": False, "venv": False, "wheel": None,
                                     "imported_tar": argv[4]}
            return RunResult(0)
        if flag == "--terminate":
            if argv[2] in self.distros:
                self.distros[argv[2]]["running"] = False
            return RunResult(0)
        if flag == "--unregister":
            self.distros.pop(argv[2], None)
            return RunResult(0)
        if flag == "--manage":
            if argv[3] == "--resize" and self.resize_fail:
                return RunResult(1, "", "unknown option --resize")
            return RunResult(0)
        if flag == "--distribution":
            name, user = argv[2], argv[4]
            assert argv[5] == "--exec"
            if name not in self.distros:
                return RunResult(1, "", "There is no distribution with the supplied name.")
            self.distros[name]["running"] = True
            return self._exec(self.distros[name], user, argv[6:], stdin)
        return RunResult(2, "", f"unmodelled wsl.exe flag {flag}")

    # ---- in-distro commands ----
    def _exec(self, d: dict, user: str, p: list[str], stdin: Optional[bytes]) -> RunResult:  # noqa: C901
        files = d["files"]
        prog, args = p[0], p[1:]
        if prog == "env":
            return self._exec(d, user, args[1:], stdin)  # env VAR=x program ...
        if prog == "true":
            return RunResult(0)
        if prog == "cat":
            path = args[0]
            if path == "/proc/1/comm":
                return RunResult(0, "systemd\n")
            if path == "/proc/mounts":
                extra = "drvfs /mnt/c 9p rw 0 0\n" if self.leak_mount else ""
                return RunResult(0, "/dev/sdc / ext4 rw 0 0\nnone /usr/lib/wsl/drivers 9p ro 0 0\n" + extra)
            return RunResult(0, files[path].decode()) if path in files else RunResult(1, "", "No such file")
        if prog == "tee":
            files[args[0]] = stdin or b""
            return RunResult(0, (stdin or b"").decode(errors="replace"))
        if prog in ("mkdir", "chmod", "chown", "passwd", "usermod", "apt-mark", "install", "systemctl", "gpg-agent"):
            if prog == "systemctl":
                return self._systemctl(args)
            if prog == "usermod" and args[-1] not in d["users"]:
                return RunResult(1, "", "no such user")
            return RunResult(0)
        if prog == "mv":
            files[args[-1]] = files.pop(args[-2])
            return RunResult(0)
        if prog == "rm":
            for a in args:
                files.pop(a, None)
            return RunResult(0)
        if prog == "mktemp":
            return RunResult(0, "/tmp/ccoin-winescape-abc123\n")
        if prog == "id":
            u = args[-1]
            if u not in d["users"]:
                return RunResult(1, "", "no such user")
            return RunResult(0, "998\n" if args[0] == "-u" else f"{u} docker\n")
        if prog == "useradd":
            d["users"].add(args[-1])
            return RunResult(0)
        if prog == "test":
            flag, path = args
            if path in ("/dev/dxg",) or path == "/usr/lib/wsl/lib":
                return RunResult(0)
            if path.startswith("/proc/sys/fs/binfmt_misc"):
                return RunResult(0 if self.interop_present else 1)
            return RunResult(0 if path in files else 1)
        if prog == "ss":
            return RunResult(0, self.listeners)
        if prog == "curl":
            out = args[args.index("--output") + 1]
            files[out] = b"key:" + args[-1].encode()
            return RunResult(0)
        if prog == "gpg":
            if args[0] == "--show-keys":
                key = files.get(args[-1], b"").decode()
                which = "docker" if "docker" in key else "nvidia"
                return RunResult(0, f"pub:-:4096:1:ABC:1:::-:::scESCA::::::23::0:\nfpr:::::::::{self.key_fprs[which]}:\n")
            files[args[args.index("--output") + 1]] = b"keyring"
            return RunResult(0)
        if prog == "apt-get":
            if args[0] == "install":
                for spec in args:
                    if "=" in spec and not spec.startswith("-"):
                        n, v = spec.split("=", 1)
                        d["pkgs"][n] = v
            return RunResult(0)
        if prog == "nvidia-ctk":
            if args == ["--version"]:
                v = d["pkgs"].get("nvidia-container-toolkit", "").split("-")[0]
                return RunResult(0, f"NVIDIA Container Toolkit CLI version {v}\n") if v else RunResult(127, "", "not found")
            return RunResult(0)
        if prog == "sha256sum":
            return RunResult(0, f"{sha(files[args[0]])}  {args[0]}\n")
        if prog == "python3":
            return RunResult(0)
        if prog.endswith("/bin/python"):
            if args[:3] == ["-m", "pip", "show"]:
                if d["wheel"]:
                    return RunResult(0, f"Name: computecoin-agent\nVersion: {d['wheel']}\n")
                return RunResult(1, "", "not found")
            if args[:3] == ["-m", "pip", "install"] and args[-1].endswith(".whl"):
                d["wheel"] = re.match(r"computecoin_agent-([^-]+)-", args[-1].rsplit("/", 1)[-1]).group(1)
            return RunResult(0)
        if prog == "docker":
            return self._docker(d, args)
        return RunResult(127, "", f"unmodelled command: {p}")

    def _systemctl(self, args: list[str]) -> RunResult:
        if args[:2] == ["is-system-running", "--wait"]:
            return RunResult(0, "running\n")
        if args[0] == "is-enabled":
            return RunResult(1, "masked\n" * (len(args) - 1))
        if args[0] == "is-active":
            return RunResult(3, "inactive\n" * (len(args) - 1))
        return RunResult(0)

    def _docker(self, d: dict, a: list[str]) -> RunResult:
        if a[0] == "version":
            return RunResult(0, self.engine_version + "\n") if "docker-ce" in d["pkgs"] else RunResult(1, "", "Cannot connect")
        if a[0] == "info":
            if "docker-ce" not in d["pkgs"]:
                return RunResult(1, "", "Cannot connect")
            return RunResult(0, '{"nvidia":{"path":"nvidia-container-runtime"},"runc":{}}' if "--format" in a else "info")
        if a[0] in ("pull", "network", "rm"):
            return RunResult(0)
        if a[0] == "image":
            return RunResult(0, "sha256:abc\n")
        if a[0] == "logs":
            return RunResult(0, "", "WARNING DENIED target_not_allowlisted\nWARNING DENIED resolves_to_forbidden_address\n")
        if a[0] == "run":
            if "-d" in a:
                return RunResult(0, "container-id\n")
            if "--entrypoint" in a:  # egress probe
                return RunResult(0, self.egress_probe)
            if "nvidia-smi" in a:
                return self._smoke(a)
        return RunResult(127, "", f"unmodelled docker: {a}")

    def _smoke(self, a: list[str]) -> RunResult:
        if self.smoke_fail:
            return RunResult(125, "", "could not select device driver with capabilities: [[gpu]]")
        spec = a[a.index("--gpus") + 1].strip('"')
        if spec == "all":
            chosen = list(range(len(self.gpus)))
        else:
            chosen = [int(x) for x in spec.split("=", 1)[1].split(",")]
            if any(i >= len(self.gpus) for i in chosen):
                return RunResult(125, "", "no such device")
        lines = "".join(f"GPU {n}: {self.gpus[i][0]} (UUID: {self.gpus[i][1]})\n" for n, i in enumerate(chosen))
        return RunResult(0, lines)


# ---- assembled environment ------------------------------------------------------------------------------------------ #
class Env:
    """A provisioner wired to fakes. ``envelope(**over)`` signs a manifest with this env's fresh key."""

    def __init__(self, tmp_path, **settings_over) -> None:
        from computecoin_agent.windows.runtime.config_files import ResourceLimits
        from computecoin_agent.windows.runtime.provision import Deps, RuntimeProvisioner, RuntimeSettings

        self.priv, self.pub = new_keypair()
        self.fake = FakeWsl()
        self.fetcher = FakeFetcher()
        self.install_dir = str(tmp_path / "ComputeCoin" / "runtime" / "computecoin")
        self.staging_dir = str(tmp_path / "staging")
        kw = dict(manifest_public_key=self.pub, install_dir=self.install_dir, staging_dir=self.staging_dir,
                  limits=ResourceLimits(memory_mb=12288, cpus=6, disk_gb=120))
        kw.update(settings_over)
        self.settings = RuntimeSettings(**kw)
        self.prov = RuntimeProvisioner(self.settings, Deps(runner=self.fake, fetch=self.fetcher, sleep=lambda s: None))

    def envelope(self, **over) -> bytes:
        return envelope(manifest_dict(**over), self.priv)

    @property
    def distro(self) -> dict:
        return self.fake.distros["computecoin"]
