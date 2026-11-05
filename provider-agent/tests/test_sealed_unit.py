"""os/sealed systemd unit: the agent's job work dirs must be visible to the docker daemon."""

import configparser
import pathlib

UNIT = pathlib.Path(__file__).resolve().parents[2] / "os/sealed/mkosi.extra/etc/systemd/system/computecoin-agent.service"


def _service_section():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    # systemd allows repeated keys (Environment=): keep them all
    lines = [ln for ln in UNIT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    section, out = None, {}
    for ln in lines:
        if ln.startswith("["):
            section = ln.strip("[]")
        elif "=" in ln and section == "Service":
            k, v = ln.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_private_tmp_is_not_the_job_work_dir():
    svc = _service_section()
    env = dict(e.split("=", 1) for e in svc["Environment"])
    if svc.get("PrivateTmp") == ["yes"]:
        # dockerd runs in the host mount namespace: a bind source under the agent's PrivateTmp /tmp does not exist there
        assert env.get("TMPDIR") == "/run/computecoin"
        assert svc.get("RuntimeDirectory") == ["computecoin"]
    # hardening must stay
    assert svc["NoNewPrivileges"] == ["yes"] and svc["ProtectSystem"] == ["strict"]


def test_docker_daemon_json_does_not_set_hosts():
    # Debian docker.io starts dockerd with `-H fd://`; a "hosts" key in daemon.json then makes dockerd refuse to start
    # ("directives are specified both as a flag and in the configuration file"), so no container job could ever run.
    import json

    cfg = json.loads((UNIT.parents[2] / "docker" / "daemon.json").read_text())
    assert "hosts" not in cfg
    assert cfg["icc"] is False and cfg["no-new-privileges"] is True


def test_agent_env_is_not_git_ignored():
    # The unit loads /etc/computecoin/agent.env; the root .gitignore ignores *.env, so without a negation the file
    # never reaches a clean checkout and the agent service fails to start on every sealed image built in CI.
    import shutil
    import subprocess

    if shutil.which("git") is None:
        return
    root = UNIT.parents[6]
    rel = "os/sealed/mkosi.extra/etc/computecoin/agent.env"
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root)
    assert r.returncode == 1, "agent.env must not be git-ignored"


def test_pcr_report_reaches_the_serial_port_the_boot_test_reads():
    # mkosi.conf has no console=ttyS0 (the command line is measured), so /dev/console is the VGA tty and the QEMU
    # -nographic boot test would never see COMPUTECOIN-PCR lines unless the service also writes the serial port.
    unit = (UNIT.parent / "computecoin-pcr-report.service").read_text()
    conf = (UNIT.parents[3] / ".." / "mkosi.conf").resolve().read_text()
    if "console=ttyS0" not in conf:
        assert "/dev/ttyS0" in unit
