"""W8 networking: the egress allowlist proxy inside the distro, UNCHANGED (X1), and its verification.

The proxy (``workloads/egress-proxy``) and the executor code that starts it (``DockerExecutor._setup_egress``) run inside
the distro exactly as on Linux: a per-job ``--internal`` docker network, the proxy attached to it and to the default
bridge, the job given ``HTTPS_PROXY``. Windows adds only two provisioning steps, implemented here:

1. ``pull_egress_proxy``: pull the manifest's digest-pinned proxy image into the distro's Docker and record it in
   ``/etc/computecoin/runtime.env`` as ``COMPUTECOIN_EGRESS_PROXY_IMAGE`` (the runner reads ``egress_proxy_image`` from the
   environment ``COMPUTECOIN_*``). Nothing secret is written there.
2. ``verify_egress_proxy``: the verification spec below, runnable without internet access.

VERIFICATION SPEC (also in docs/architecture/W8-multinode-on-windows.md section "Egress proxy"):

=====  ==========================================================  =====================================================
id     attempt (from a probe container on the internal net)        expected
=====  ==========================================================  =====================================================
E1     direct TCP connect to a public IP (no proxy)                refused or timeout: the internal network has no route
E2     proxy ``CONNECT 169.254.169.254:80``                        HTTP 403
E3     proxy ``CONNECT not-allowlisted.invalid:443``               HTTP 403
E4     proxy log after E2 and E3                                   lines containing ``DENIED`` with reason codes
=====  ==========================================================  =====================================================

TODO(verify-docs) (X11): Docker ``network create --internal``, container name resolution on user-defined networks, and
that WSL2's NAT does not give an internal network a route (to be proven on a real host; hardware item W8.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import distro_paths as P
from .health import Check
from .wsl import WslCli

VERIFY_NET = "cc-egress-verify"
VERIFY_PROXY = "cc-egress-verify-proxy"
PROXY_PORT = 3128

PROBE_CODE = r"""
import socket, sys
def connect_via_proxy(target):
    s = socket.create_connection(("%(proxy)s", %(port)d), timeout=8)
    s.sendall(("CONNECT %%s HTTP/1.1\r\nHost: %%s\r\n\r\n" %% (target, target)).encode())
    line = s.recv(256).split(b"\r\n", 1)[0].decode("latin-1")
    s.close()
    return line
try:
    socket.create_connection(("1.1.1.1", 443), timeout=4).close()
    print("E1 REACHED")
except OSError:
    print("E1 BLOCKED")
for tag, target in (("E2", "169.254.169.254:80"), ("E3", "not-allowlisted.invalid:443")):
    try:
        print(tag, connect_via_proxy(target))
    except OSError as exc:
        print(tag, "ERROR", type(exc).__name__)
""" % {"proxy": VERIFY_PROXY, "port": PROXY_PORT}


@dataclass(frozen=True)
class SpecItem:
    id: str
    attempt: str
    expected: str


EGRESS_VERIFICATION_SPEC = (
    SpecItem("E1", "direct TCP connect from the internal network to a public IP", "blocked"),
    SpecItem("E2", "proxy CONNECT to the cloud metadata address", "HTTP 403"),
    SpecItem("E3", "proxy CONNECT to a host that is not allowlisted", "HTTP 403"),
    SpecItem("E4", "proxy log after E2 and E3", "contains DENIED"),
)


def runtime_env_text(proxy_image: str) -> str:
    return f"COMPUTECOIN_EGRESS_PROXY_IMAGE={proxy_image}\n"


def pull_egress_proxy(cli: WslCli, image: str) -> None:
    """Pull by digest (the daemon verifies content against the digest), confirm it is present, record it for the runner."""
    if "@sha256:" not in image:
        raise ValueError("egress proxy image must be pinned by @sha256: digest")
    cli.exec_ok(["docker", "pull", image], what="pull egress proxy image", timeout=900)
    cli.exec_ok(["docker", "image", "inspect", "--format", "{{.Id}}", image], what="confirm egress proxy image", timeout=60)
    cli.write_file(P.RUNTIME_ENV_FILE, runtime_env_text(image).encode("utf-8"))


def _hardening() -> list[str]:
    # Same flags as DockerExecutor._setup_egress, so the verified proxy is configured like the real one.
    return ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "10001:10001",
            "--pids-limit", "64", "--memory", "128m"]


def verify_egress_proxy(cli: WslCli, image: str, *, runner_user: str = P.RUNNER_USER) -> list[Check]:
    """Run E1..E4 with a throw-away internal network, proxy and probe. Always cleans up. Never raises."""
    checks: list[Check] = []

    def docker(args: list[str], timeout: float = 120):
        return cli.exec(["docker", *args], user=runner_user, timeout=timeout)

    def cleanup() -> None:
        docker(["rm", "-f", VERIFY_PROXY], 60)
        docker(["network", "rm", VERIFY_NET], 60)

    try:
        cleanup()  # idempotent: a previous aborted run leaves nothing behind
        made = docker(["network", "create", "--internal", VERIFY_NET])
        if not made.ok:
            return [Check("egress_E0_setup", False, f"cannot create the internal network: {made.stderr.strip()[:200]}")]
        started = docker(["run", "-d", "--rm", "--name", VERIFY_PROXY, "--network", VERIFY_NET, *_hardening(),
                          "-e", "COMPUTECOIN_EGRESS_ALLOW=allowed.invalid:443", "-e", f"COMPUTECOIN_PROXY_PORT={PROXY_PORT}", image])
        if not started.ok:
            return [Check("egress_E0_setup", False, f"cannot start the proxy: {started.stderr.strip()[:200]}")]
        probe = docker(["run", "--rm", "--pull", "never", "--network", VERIFY_NET, *_hardening(),
                        "--entrypoint", "python", image, "-c", PROBE_CODE], 120)
        out = probe.stdout
        checks.append(Check("egress_E1_no_direct_route", "E1 BLOCKED" in out and "E1 REACHED" not in out,
                            "direct connect blocked" if "E1 BLOCKED" in out else f"probe output: {out.strip()[:200]}"))
        for tag, name in (("E2", "egress_E2_metadata_denied"), ("E3", "egress_E3_unlisted_denied")):
            line = next((l for l in out.splitlines() if l.startswith(tag + " ")), "")
            checks.append(Check(name, " 403" in line, line or "no answer from the probe"))
        logs = docker(["logs", VERIFY_PROXY], 60)
        text = logs.stdout + "\n" + logs.stderr
        denied = [l for l in text.splitlines() if "DENIED" in l]
        checks.append(Check("egress_E4_denials_logged", len(denied) >= 2, f"{len(denied)} DENIED line(s)"))
    except Exception as exc:
        checks.append(Check("egress_verify", False, f"{type(exc).__name__}: {str(exc)[:200]}"))
    finally:
        try:
            cleanup()
        except Exception:
            pass
    return checks
