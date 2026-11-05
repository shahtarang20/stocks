#!/usr/bin/env python3
"""Predict the TPM PCRs 4, 7 and 11 a host measures when it boots this image, and emit
``out/expected-pcrs.json`` (the body of POST /v1/privacy/trusted-images, plus provenance).

- PCR 4: the firmware extends, in order: the EV_EFI_ACTION "Calling EFI Application from Boot
  Option", the Authenticode SHA-256 of every PE image it loads (boot loader, the UKI, and the kernel
  inside the UKI), and, at ExitBootServices, the EV_SEPARATOR.
- PCR 7: Secure Boot policy: SecureBoot, PK, KEK, db, dbx variable events, a separator, then one
  EV_EFI_VARIABLE_AUTHORITY per image verified against db.
- PCR 11: systemd-stub measures each UKI section name and then its contents, then userspace extends
  phase strings (enter-initrd, leave-initrd, sysinit).

TODO(verify-docs): the event order and exact event data above are firmware (OVMF / real) and
systemd-stub specific and written from memory of the TCG PC Client PFP spec and systemd's
TPM2_PCR_MEASUREMENTS.md. The QEMU+swtpm boot test (tests/e2e/test_sealed_image_boot.py) compares
this prediction with the PCRs a real boot produces; it is the check, not this docstring (R7, R8).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import uuid
from pathlib import Path

GLOBAL_GUID = uuid.UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c")
IMAGE_SECURITY_GUID = uuid.UUID("d719b2cb-3d3a-4596-a3bc-dad00e67656f")
UKI_MEASURED_SECTIONS = (".linux", ".osrel", ".cmdline", ".initrd", ".ucode", ".splash", ".dtb", ".uname", ".sbat", ".pcrpkey")
DEFAULT_PHASES = ("enter-initrd", "leave-initrd", "sysinit")


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def extend(pcr: bytes, digest: bytes) -> bytes:
    return sha256(pcr + digest)


def chain(digests: list[bytes]) -> str:
    pcr = bytes(32)
    for d in digests:
        pcr = extend(pcr, d)
    return pcr.hex()


# ---------------------------------------------------------------------------- PE helpers
class Pe:
    def __init__(self, data: bytes):
        if data[:2] != b"MZ":
            raise ValueError("not a PE file")
        self.data = data
        self.pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[self.pe:self.pe + 4] != b"PE\0\0":
            raise ValueError("bad PE signature")
        nsec, = struct.unpack_from("<H", data, self.pe + 6)
        opt_size, = struct.unpack_from("<H", data, self.pe + 20)
        self.opt = self.pe + 24
        magic, = struct.unpack_from("<H", data, self.opt)
        self.dd = self.opt + (112 if magic == 0x20B else 96)  # data directories
        self.sections = []
        pos = self.opt + opt_size
        for i in range(nsec):
            name = data[pos:pos + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, pos + 8)
            self.sections.append((name, vsize, rsize, raddr))
            pos += 40

    def section(self, name: str) -> bytes | None:
        for n, vsize, rsize, raddr in self.sections:
            if n == name:
                return self.data[raddr:raddr + (min(vsize, rsize) if vsize else rsize)]
        return None

    def authenticode_sha256(self) -> bytes:
        """Authenticode PE image hash: skip the checksum, the certificate-table directory entry and
        the certificate table itself; sections hashed in file-offset order."""
        d = self.data
        checksum = self.opt + 64
        cert_dir = self.dd + 4 * 8
        cert_off, cert_size = struct.unpack_from("<II", d, cert_dir)
        headers_end = struct.unpack_from("<I", d, self.opt + 60)[0]  # SizeOfHeaders
        h = hashlib.sha256()
        h.update(d[:checksum])
        h.update(d[checksum + 4:cert_dir])
        h.update(d[cert_dir + 8:headers_end])
        pos = headers_end
        for _, _, rsize, raddr in sorted(self.sections, key=lambda s: s[3]):
            if rsize:
                h.update(d[raddr:raddr + rsize])
                pos = max(pos, raddr + rsize)
        end = cert_off if cert_size and cert_off else len(d)
        if end > pos:
            h.update(d[pos:end])
        return h.digest()


# ---------------------------------------------------------------------------- PCRs
def pcr4(bootloader: Path, uki: Path) -> str:
    uki_pe = Pe(uki.read_bytes())
    kernel = uki_pe.section(".linux")
    if kernel is None:
        raise SystemExit("UKI has no .linux section")
    events = [sha256(b"Calling EFI Application from Boot Option"),
              Pe(bootloader.read_bytes()).authenticode_sha256(),
              uki_pe.authenticode_sha256(),
              Pe(kernel).authenticode_sha256(),
              sha256(b"\0\0\0\0")]  # EV_SEPARATOR at ExitBootServices
    return chain(events)


def _var_event(guid: uuid.UUID, name: str, data: bytes) -> bytes:
    raw = guid.bytes_le + struct.pack("<QQ", len(name), len(data)) + name.encode("utf-16-le") + data
    return sha256(raw)


def pcr7(keys: Path, authority_images: int) -> str:
    def esl(name: str, required: bool = True) -> bytes:
        p = keys / name
        if not p.exists():
            if required:
                raise SystemExit(f"missing {p}")
            return b""
        return p.read_bytes()

    owner = uuid.UUID((keys / "owner.guid").read_text().strip()) if (keys / "owner.guid").exists() else None
    events = [_var_event(GLOBAL_GUID, "SecureBoot", b"\x01"),
              _var_event(GLOBAL_GUID, "PK", esl("PK.esl")),
              _var_event(GLOBAL_GUID, "KEK", esl("KEK.esl")),
              _var_event(IMAGE_SECURITY_GUID, "db", esl("db.esl")),
              _var_event(IMAGE_SECURITY_GUID, "dbx", esl("dbx.esl", required=False)),
              sha256(b"\0\0\0\0")]  # EV_SEPARATOR
    if owner is None:
        raise SystemExit("keys dir needs owner.guid (the signature owner GUID of db.esl)")
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    der = x509.load_pem_x509_certificate((keys / "db.crt").read_bytes()).public_bytes(Encoding.DER)
    authority = _var_event(IMAGE_SECURITY_GUID, "db", owner.bytes_le + der)
    events += [authority] * authority_images
    return chain(events)


def pcr11(uki: Path, phases: tuple[str, ...]) -> str:
    pe = Pe(uki.read_bytes())
    events = []
    for name in UKI_MEASURED_SECTIONS:
        content = pe.section(name)
        if content is None:
            continue
        events += [sha256(name.encode() + b"\0"), sha256(content)]
    events += [sha256(p.encode()) for p in phases]
    return chain(events)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uki", required=True, type=Path)
    ap.add_argument("--bootloader", required=True, type=Path)
    ap.add_argument("--keys-dir", required=True, type=Path)
    ap.add_argument("--image-hash", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--version-tag", default="")
    ap.add_argument("--phases", default=",".join(DEFAULT_PHASES), help="userspace PCR 11 phases measured before the agent quotes")
    ap.add_argument("--authority-images", type=int, default=2, help="images verified against db (boot loader, UKI)")
    a = ap.parse_args(argv)
    if len(a.image_hash) != 64 or any(c not in "0123456789abcdef" for c in a.image_hash):
        raise SystemExit("--image-hash must be 64 lowercase hex characters")
    pcrs = {"pcr4": pcr4(a.bootloader, a.uki),
            "pcr7": pcr7(a.keys_dir, a.authority_images),
            "pcr11": pcr11(a.uki, tuple(p for p in a.phases.split(",") if p))}
    doc = {"image_hash": a.image_hash, "version_tag": a.version_tag or f"sealed-{a.image_hash[:12]}", "tier": "sealed",
           "expected_pcrs": pcrs,
           "provenance": {"uki_sha256": hashlib.sha256(a.uki.read_bytes()).hexdigest(),
                          "bootloader_sha256": hashlib.sha256(a.bootloader.read_bytes()).hexdigest(),
                          "phases": a.phases, "authority_images": a.authority_images}}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps(pcrs, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
