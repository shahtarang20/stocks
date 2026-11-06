#!/usr/bin/env python3
"""Offline Kit signer (U1). Run on the machine that holds the Kit signing key; never in CI or on a server.

    python scripts/sign_kit.py --manifest kit.json --key /path/to/ed25519_private.pem [--out publish.json]
    python scripts/sign_kit.py --gen-key /path/to/new_private.pem     # prints the public key hex to configure

The private key is read from the path given at run time (PEM PKCS8, or 32 raw bytes as 64 hex chars).
No key is stored in the repository (L7). Output is the body for POST /v1/admin/kits.
The signed bytes must match app/services/kits.py: SIGN_PREFIX + canonical JSON (sorted keys, no spaces, UTF-8).
Library calls (cryptography Ed25519PrivateKey) to be verified against current docs (L4).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

SIGN_PREFIX = b"computecoin-kit-v1\n"  # keep identical to app.services.kits.SIGN_PREFIX


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pub_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


def load_key(path: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # Windows PowerShell 5.1 `>` / Out-File writes UTF-16 with a BOM
        data = data.decode("utf-16").encode("utf-8")
    data = data.removeprefix(b"\xef\xbb\xbf").strip()  # UTF-8 BOM
    if re.fullmatch(rb"[0-9a-fA-F]{64}", data):
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data.decode()))
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("key is not an Ed25519 private key")
    return key


def restrict_to_owner(path: str) -> None:
    """Make a freshly written private key readable by the current user only.

    POSIX: the 0600 given when the file was created is enough. Windows ignores POSIX modes (os.open's 0o600 does nothing),
    so the inherited permissions are removed and only the current user is granted access with icacls.
    TODO(verify-docs): icacls switches (/inheritance:r, /grant:r) against Microsoft's icacls reference before relying on it.
    Fails closed: if the permissions cannot be restricted the key file is deleted and the caller must stop."""
    if os.name != "nt":
        return
    import subprocess

    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        raise OSError("cannot tell the current Windows user")
    who = f"{domain}\\{user}" if domain else user
    result = subprocess.run(
        ["icacls", path, "/inheritance:r", "/grant:r", f"{who}:F"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise OSError("icacls failed: " + (result.stderr or result.stdout).strip()[:300])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest")
    ap.add_argument("--key")
    ap.add_argument("--out")
    ap.add_argument("--gen-key", help="write a new private key (mode 0600) to this path and print the public key")
    a = ap.parse_args()

    if a.gen_key:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if os.path.exists(a.gen_key):
            sys.exit("refusing to overwrite an existing file")
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(a.gen_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        try:
            restrict_to_owner(a.gen_key)
        except OSError as exc:
            os.unlink(a.gen_key)  # never leave a key that anyone else can read
            sys.exit(f"could not restrict the key file to the current user ({exc}); the key was deleted, nothing was created")
        print("KIT_SIGNING_PUBLIC_KEY=" + _pub_hex(key))
        return 0

    if not (a.manifest and a.key):
        ap.error("--manifest and --key are required")
    with open(a.manifest, encoding="utf-8-sig") as mf:  # utf-8-sig: a BOM from Windows PowerShell must not break the parse
        manifest = json.load(mf)
    key = load_key(a.key)
    body = json.dumps({"manifest": manifest, "signature": key.sign(SIGN_PREFIX + canonical(manifest)).hex()}, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as of:  # LF on every OS: the file is POSTed as it is
            of.write(body)
    else:
        print(body)
    print("signed with public key " + _pub_hex(key), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
