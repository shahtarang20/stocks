"""Build and sign an agent update manifest OFFLINE (cross-platform plan W6, X10, L7). Run by the release engineer, never by CI
without a hardware-backed key.

    python sign_manifest.py --version 1.4.0 --platform windows --ring canary --url https://.../ComputeCoinAgent-1.4.0.msi \\
        --msi out/ComputeCoinAgent-1.4.0.msi --min-supported 1.2.0 [--revokes 1.3.9] [--expires-days 90] > manifest-1.4.0-canary.json

The signing key is read from the file named by the environment variable COMPUTECOIN_UPDATE_SIGNING_KEY_FILE (64 hex characters
or 32 raw bytes, an Ed25519 seed). It is never a command-line argument and never written anywhere. The output is the flat signed
manifest the API publishes (POST /v1/admin/agent-releases takes {"manifest": <without signature>, "signature": ...}; use --split)
and the agent verifies (computecoin_agent.updater.manifest).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

SIGN_PREFIX = b"computecoin-agent-update-v1\n"  # must equal computecoin_agent.updater.manifest.SIGN_PREFIX (a test pins this)


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_signing_key(path: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = Path(path).read_bytes().strip()
    seed = bytes.fromhex(raw.decode("ascii")) if re.fullmatch(rb"[0-9a-fA-F]{64}", raw) else raw
    if len(seed) != 32:
        raise ValueError("the signing key file must hold a 32-byte Ed25519 seed (64 hex characters or 32 raw bytes)")
    return Ed25519PrivateKey.from_private_bytes(seed)


def build_payload(*, version: str, platform: str, ring: str, url: str, msi: Path, min_supported: str,
                  revokes: Sequence[str] = (), expires_days: Optional[int] = None, now: Optional[int] = None) -> dict:
    data = msi.read_bytes()
    issued = int(time.time() if now is None else now)
    payload = {
        "schema": 1, "product": "computecoin-agent", "version": version, "platform": platform, "artifact_url": url,
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "min_supported": min_supported,
        "ring": ring, "issued_at": issued,
    }
    if revokes:
        payload["revoked_versions"] = list(revokes)
    if expires_days:
        payload["expires_at"] = issued + int(expires_days) * 86400
    return payload


def sign_payload(key, payload: dict) -> str:
    return key.sign(SIGN_PREFIX + canonical_bytes(payload)).hex()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--platform", required=True, choices=["windows", "linux"])
    ap.add_argument("--ring", required=True, choices=["canary", "early", "general"])
    ap.add_argument("--url", required=True)
    ap.add_argument("--msi", required=True, help="the SIGNED installer file whose hash and size go into the manifest")
    ap.add_argument("--min-supported", required=True, help="the forced-update floor (use the oldest version still allowed)")
    ap.add_argument("--revokes", action="append", default=[], help="a version to replace even by an older build (rollback)")
    ap.add_argument("--expires-days", type=int)
    ap.add_argument("--split", action="store_true", help='print {"manifest": ..., "signature": ...} for the release API')
    args = ap.parse_args(argv)
    key_file = os.environ.get("COMPUTECOIN_UPDATE_SIGNING_KEY_FILE")
    if not key_file:
        print("COMPUTECOIN_UPDATE_SIGNING_KEY_FILE is not set", file=sys.stderr)
        return 2
    try:
        key = load_signing_key(key_file)
        payload = build_payload(version=args.version, platform=args.platform, ring=args.ring, url=args.url,
                                msi=Path(args.msi), min_supported=args.min_supported, revokes=args.revokes,
                                expires_days=args.expires_days)
    except (OSError, ValueError) as exc:
        print(f"sign_manifest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    sig = sign_payload(key, payload)
    print(json.dumps({"manifest": payload, "signature": sig} if args.split else {**payload, "signature": sig}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
