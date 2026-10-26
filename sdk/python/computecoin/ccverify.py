"""ccverify: check a ComputeCoin Privacy Receipt offline (ADR-0005).

Needs only public information: the verifier's Ed25519 public key (``GET /v1/privacy/verifier-key``,
or published by ComputeCoin out of band). Checks:

1. the attestation verdict is signed by that verifier key;
2. the verdict's tier is at least the job's tier, and the image matches;
3. every inclusion proof recomputes its leaf from the logged payload and verifies against the
   receipt's Merkle root with the RFC 6962 / RFC 9162 algorithm (index and tree size bound in);
4. the logged verdict is THIS verdict, key releases name this job, the logged wipe receipt is
   this wipe receipt;
5. the wipe receipt is Ed25519-signed by the host key named in the verdict.

Not checked: that the root was anchored on-chain (not built yet).

    python -m computecoin.ccverify receipt.json --verifier-key <hex> [--image-hash <hex>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

from .privacy_crypto import canonical_json, verify

TIER_RANK = {"open": 0, "container": 0, "gvisor": 1, "microvm": 2, "sealed": 3, "sealed_pro": 4, "confidential": 5}
VERDICT_FIELDS = (
    "verdict_id", "machine_id", "image_hash", "tier_reached", "ephemeral_public_key",
    "receipt_signing_public_key", "nonce", "issued_at", "expires_at", "verifier_public_key",
)
WIPE_FIELDS = ("machine_id", "job_id", "wiped_items", "wiped_at")


def hash_leaf(event_type: str, payload: dict) -> str:
    return hashlib.sha256(b"\x00" + event_type.encode() + b":" + canonical_json(payload)).hexdigest()


def _node(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def verify_inclusion(leaf_hash: str, leaf_index: int, tree_size: int, root: str, path: list[str]) -> bool:
    """RFC 9162 section 2.1.3.2."""
    try:
        if not isinstance(leaf_index, int) or not isinstance(tree_size, int) or not 0 <= leaf_index < tree_size:
            return False
        fn, sn, r = leaf_index, tree_size - 1, leaf_hash
        for p in path:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                r = _node(p, r)
                if not fn & 1:
                    while not fn & 1 and fn != 0:
                        fn >>= 1
                        sn >>= 1
            else:
                r = _node(r, p)
            fn >>= 1
            sn >>= 1
        return sn == 0 and r.lower() == str(root).lower()
    except (ValueError, TypeError):
        return False


def verify_verdict(verdict: dict[str, Any], verifier_public_key: str) -> bool:
    if not isinstance(verdict, dict) or verdict.get("verifier_public_key") != verifier_public_key:
        return False
    if any(f not in verdict for f in VERDICT_FIELDS):
        return False
    return verify(verifier_public_key, canonical_json({f: verdict[f] for f in VERDICT_FIELDS}), verdict.get("signature", ""))


def verify_wipe(wipe: dict[str, Any], receipt_signing_public_key: str) -> bool:
    try:
        body = {f: wipe[f] for f in WIPE_FIELDS}
        body["wiped_items"] = sorted(body["wiped_items"])
        return verify(receipt_signing_public_key, canonical_json(body), wipe.get("signature", ""))
    except (KeyError, TypeError):
        return False


def verify_receipt(receipt: dict[str, Any], verifier_public_key: str, expected_image_hash: str | None = None) -> dict[str, Any]:
    """Return ``{"verified": bool, "checks": {...}}``. ``verified`` is True only if every check passes."""
    checks: dict[str, Any] = {}
    verdict = receipt.get("attestation_verdict") or {}
    job_id = str(receipt.get("job_id", ""))
    checks["verdict_signature"] = verify_verdict(verdict, verifier_public_key)
    checks["tier_matches"] = TIER_RANK.get(str(verdict.get("tier_reached")), -1) >= TIER_RANK.get(str(receipt.get("isolation_tier")), 99)
    checks["image_matches"] = bool(verdict) and receipt.get("image_hash") == verdict.get("image_hash")
    if expected_image_hash is not None:
        checks["expected_image"] = verdict.get("image_hash") == expected_image_hash

    root = receipt.get("merkle_root", "")
    proofs: dict[str, bool] = {}
    for name, p in (receipt.get("inclusion_proofs") or {}).items():
        try:
            ok = hash_leaf(p["event_type"], p["payload"]) == p["leaf_hash"] and verify_inclusion(
                p["leaf_hash"], p["leaf_index"], p["tree_size"], root, p["audit_path"]
            )
            if name == "attestation_verdict":
                ok = ok and p["event_type"] == "ATTESTATION_VERDICT" and p["payload"] == verdict
            elif name.startswith("key_release:"):
                ok = ok and p["event_type"] == "KEY_RELEASE" and p["payload"].get("job_id") == job_id and p["payload"].get("verdict_id") == verdict.get("verdict_id")
            elif name == "wipe_receipt":
                ok = ok and p["event_type"] == "WIPE_RECEIPT" and p["payload"] == receipt.get("wipe_receipt")
        except (KeyError, TypeError, AttributeError):
            ok = False
        proofs[name] = ok
    checks["inclusion_proofs"] = proofs
    checks["verdict_logged"] = proofs.get("attestation_verdict") is True

    wipe = receipt.get("wipe_receipt")
    checks["wipe_receipt"] = (
        (wipe.get("job_id") == job_id and verify_wipe(wipe, str(verdict.get("receipt_signing_public_key", "")))) if wipe else "absent"
    )
    verified = (
        checks["verdict_signature"] is True
        and checks["tier_matches"]
        and checks["image_matches"]
        and checks.get("expected_image", True)
        and checks["verdict_logged"]
        and all(proofs.values())
        and checks["wipe_receipt"] in (True, "absent")
    )
    return {"verified": verified, "isolation_tier": receipt.get("isolation_tier"), "machine_id": receipt.get("machine_id"), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a ComputeCoin Privacy Receipt offline")
    parser.add_argument("receipt_file")
    parser.add_argument("--verifier-key", required=True, help="ComputeCoin verifier Ed25519 public key (hex)")
    parser.add_argument("--image-hash", help="OS image hash you expect the host to have booted")
    args = parser.parse_args(argv)
    try:
        with open(args.receipt_file, "r", encoding="utf-8") as f:
            receipt = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read receipt: {exc}", file=sys.stderr)
        return 2
    result = verify_receipt(receipt, args.verifier_key, args.image_hash)
    print(f"Status:         {'VERIFIED' if result['verified'] else 'FAILED'}")
    print(f"Isolation tier: {result['isolation_tier']}")
    print(f"Machine:        {result['machine_id']}")
    for name, value in result["checks"].items():
        if isinstance(value, dict):
            for sub, ok in value.items():
                print(f"  {name}.{sub}: {'PASS' if ok else 'FAIL'}")
        else:
            label = "PASS" if value is True else "FAIL" if value is False else str(value)
            print(f"  {name}: {label}")
    print("Not checked: on-chain anchoring of the log root (not built yet).")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
