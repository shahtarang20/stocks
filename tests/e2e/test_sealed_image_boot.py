"""Sealed OS image boot test (P5), opt-in: QEMU + OVMF + a software TPM.

Enable with COMPUTECOIN_SEALED_BOOT_TEST=1 after building the image (os/sealed/scripts/build.sh).
It boots the image with swtpm; the image's computecoin-pcr-report.service prints the real PCRs 4, 7
and 11 to the serial console. (The full `tpm-enrol` + attest flow against the API stays covered by
test_tpm_swtpm_e2e.py with the same PCR values once they match.) Checks:

1. the PCRs equal os/sealed/out/expected-pcrs.json (the prediction), and
2. flipping one byte of the image changes the PCRs, so the prediction no longer matches, which is
   exactly what makes the API refuse the hardware verdict for tier ``sealed``.

Extends tests/e2e/test_tpm_swtpm_e2e.py (same project-local tool layout: .tools/tpm/root).
TODO(verify-docs): OVMF firmware paths and the QEMU TPM options (-chardev socket / tpm-tis) are the
standard ones from the QEMU and swtpm documentation; check against the installed versions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SEALED = REPO / "os/sealed"
IMAGE = SEALED / "out/computecoin-sealed.raw"
EXPECTED = SEALED / "out/expected-pcrs.json"
OVMF_CODE = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd")
OVMF_VARS = os.environ.get("OVMF_VARS", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd")

pytestmark = [
    pytest.mark.skipif(os.environ.get("COMPUTECOIN_SEALED_BOOT_TEST") != "1", reason="set COMPUTECOIN_SEALED_BOOT_TEST=1 (opt-in)"),
    pytest.mark.skipif(not (IMAGE.exists() and EXPECTED.exists()), reason="image not built (os/sealed/scripts/build.sh)"),
    pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("swtpm")), reason="qemu / swtpm not available"),
]


def _boot_and_read_pcrs(image: Path, work: Path) -> dict[str, str]:
    """Boot ``image`` once and return PCRs 4, 7, 11 as the guest printed them to the serial console
    (computecoin-pcr-report.service runs ``tpm2_pcrread sha256:4,7,11`` after sysinit). PCRs are
    volatile, so they must be read while the TPM is running, not from its saved state."""
    state = work / "tpm"
    state.mkdir()
    sock = work / "swtpm.sock"
    swtpm = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={state}",
                              "--ctrl", f"type=unixio,path={sock}", "--flags", "not-need-init"])
    pcrs: dict[str, str] = {}
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        vars_copy = work / "vars.fd"
        shutil.copy(OVMF_VARS, vars_copy)  # NOTE: the test keys must be enrolled into these vars (see build docs)
        qemu = subprocess.Popen([
            "qemu-system-x86_64", "-machine", "q35,smm=on", "-m", "2048", "-nographic", "-no-reboot",
            "-global", "driver=cfi.pflash01,property=secure,value=on",
            "-drive", f"if=pflash,format=raw,unit=0,file={OVMF_CODE},readonly=on",
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
            "-drive", f"file={image},format=raw,if=virtio,snapshot=on",
            "-chardev", f"socket,id=chrtpm,path={sock}", "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
            "-device", "tpm-tis,tpmdev=tpm0",
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 240
        try:
            while time.time() < deadline and len(pcrs) < 3:
                line = qemu.stdout.readline()
                if not line:
                    break
                m = re.match(r"\s*(4|7|11)\s*:\s*(?:0x)?([0-9a-fA-F]{64})", line.replace("COMPUTECOIN-PCR ", "").replace("sha256:", ""))
                if "COMPUTECOIN-PCR" in line and m:
                    pcrs[f"pcr{m.group(1)}"] = m.group(2).lower()
        finally:
            qemu.send_signal(signal.SIGTERM)
            qemu.wait(timeout=30)
    finally:
        swtpm.send_signal(signal.SIGTERM)
        swtpm.wait(timeout=10)
    assert len(pcrs) == 3, "image did not report PCRs 4, 7 and 11 on the console"
    return pcrs


def test_booted_pcrs_equal_the_prediction():
    expected = json.loads(EXPECTED.read_text())["expected_pcrs"]
    with tempfile.TemporaryDirectory() as d:
        measured = _boot_and_read_pcrs(IMAGE, Path(d))
    assert {k: measured[k] for k in expected} == expected


def test_one_changed_byte_changes_the_pcrs():
    expected = json.loads(EXPECTED.read_text())["expected_pcrs"]
    with tempfile.TemporaryDirectory() as d:
        tampered = Path(d) / "tampered.raw"
        shutil.copy(IMAGE, tampered)
        # Flip one byte inside the ESP, where the UKI lives (offset past the GPT, inside the first partition).
        with open(tampered, "r+b") as fh:
            fh.seek(2 * 1024 * 1024)
            b = fh.read(1)
            fh.seek(2 * 1024 * 1024)
            fh.write(bytes([b[0] ^ 0x01]))
        work = Path(d) / "w"
        work.mkdir()
        try:
            measured = _boot_and_read_pcrs(tampered, work)
        except AssertionError:
            return  # a tampered image that no longer boots is also a refusal
    assert {k: measured.get(k) for k in expected} != expected
