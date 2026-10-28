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

Optional, when asked for:

6. ``--rpc``/``--vault``: the receipt's tree is consistent with a checkpoint whose root is recorded
   in ComputeStakeVault on chain (``merkleRoots(root) > 0``);
7. ``--witness-keys``/``--witness-threshold``: the receipt's tree is consistent with a checkpoint
   cosigned by at least that many of the given independent witnesses (``--log-key`` pins the log
   key the witnesses must have cosigned for).

    python -m computecoin.ccverify receipt.json --verifier-key <hex> [--image-hash <hex>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

from .privacy_crypto import canonical_json, verify

TIER_RANK = {"open": 0, "container": 0, "gvisor": 1, "microvm": 2, "sealed": 3, "sealed_pro": 4, "confidential": 5}
VERDICT_FIELDS = (
    "verdict_id", "job_id", "machine_id", "image_hash", "tier_reached", "ephemeral_public_key",
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


def verify_consistency_proof(
    first_size: int,
    second_size: int,
    first_root: str,
    second_root: str,
    proof: list[str],
) -> bool:
    """RFC 9162 Section 2.1.4.2 consistency proof verifier."""
    try:
        m, n = first_size, second_size
        if m < 0 or n < 0 or m > n:
            return False
        if m == 0:
            return len(proof) == 0
        if m == n:
            return len(proof) == 0 and first_root.lower() == second_root.lower()

        fn = m - 1
        sn = n - 1
        while fn & 1:
            fn >>= 1
            sn >>= 1

        p = list(proof)
        if fn == 0:
            fr = first_root
            sr = first_root
        else:
            if not p:
                return False
            fr = p[0]
            sr = p[0]
            p = p[1:]

        for c in p:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                fr = _node(c, fr)
                sr = _node(c, sr)
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
            else:
                sr = _node(sr, c)
            fn >>= 1
            sn >>= 1

        return sn == 0 and fr.lower() == first_root.lower() and sr.lower() == second_root.lower()
    except (ValueError, TypeError, IndexError):
        return False


def verify_wipe(wipe: dict[str, Any], receipt_signing_public_key: str) -> bool:
    try:
        body = {f: wipe[f] for f in WIPE_FIELDS}
        body["wiped_items"] = sorted(body["wiped_items"])
        return verify(receipt_signing_public_key, canonical_json(body), wipe.get("signature", ""))
    except (KeyError, TypeError):
        return False


def _consistent(receipt_size: int, receipt_root: str, size: int, root: str, proof: Any) -> bool:
    """The receipt's tree (receipt_size, receipt_root) is a prefix of the tree (size, root)."""
    if not isinstance(proof, list) or not all(isinstance(p, str) for p in proof):
        return False
    if receipt_size == size:
        return str(receipt_root).lower() == str(root).lower() and not proof
    return 0 < receipt_size < size and verify_consistency_proof(receipt_size, size, receipt_root, root, proof)


def witness_statement(tree_size: int, root_hash: str, log_public_key: str, witness: str) -> bytes:
    """What a witness cosignature covers (docs/specs/ccpp-v1.md section 6c)."""
    return canonical_json({
        "v": "ccpp-witness-v1", "tree_size": tree_size, "root_hash": str(root_hash).lower(),
        "log_public_key": str(log_public_key).lower(), "witness": witness,
    })


def check_witnesses(
    receipt: dict[str, Any], receipt_size: int, receipt_root: str, witness_keys: dict[str, str],
    threshold: int, log_public_key: str | None = None,
) -> bool:
    """At least ``threshold`` distinct pinned witness keys cosigned a checkpoint that the receipt's
    tree is consistent with."""
    section = receipt.get("witness")
    if not isinstance(section, dict):
        return False
    cp = section.get("checkpoint") or {}
    try:
        size = cp["tree_size"]
        root = str(cp["root_hash"]).lower()
        log_key = str(cp["log_public_key"]).lower()
    except (KeyError, TypeError):
        return False
    if not isinstance(size, int) or (log_public_key and log_key != log_public_key.lower()):
        return False
    if not _consistent(receipt_size, receipt_root, size, root, section.get("consistency_proof", [])):
        return False
    signers: set[str] = set()
    for cs in section.get("cosignatures") or []:
        if not isinstance(cs, dict):
            continue
        name, sig = str(cs.get("witness", "")), str(cs.get("signature", ""))
        key = witness_keys.get(name)
        if key and verify(key, witness_statement(size, root, log_key, name), sig):
            signers.add(key.lower())  # distinct KEYS: one operator listed twice counts once
    return len(signers) >= threshold


def check_on_chain_anchor(rpc_url: str, vault_address: str, merkle_root: str) -> bool:
    """Check if merkle_root is anchored in ComputeStakeVault at vault_address via eth_call."""
    try:
        import httpx
        clean_root = str(merkle_root).lower()
        if clean_root.startswith("0x"):
            clean_root = clean_root[2:]
        # Selector for merkleRoots(bytes32) is 0xfe5a5377
        call_data = "0xfe5a5377" + clean_root.rjust(64, "0")
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": vault_address, "data": call_data}, "latest"],
            "id": 1,
        }
        resp = httpx.post(rpc_url, json=payload, timeout=10.0)
        if resp.status_code == 200:
            result = resp.json().get("result")
            if result and result != "0x" and int(result, 16) > 0:
                return True
        return False
    except Exception:
        return False


def verify_receipt(
    receipt: dict[str, Any],
    verifier_public_key: str,
    expected_image_hash: str | None = None,
    rpc_url: str | None = None,
    vault_address: str | None = None,
    witness_keys: dict[str, str] | None = None,
    witness_threshold: int = 0,
    log_public_key: str | None = None,
) -> dict[str, Any]:
    """Return ``{"verified": bool, "checks": {...}}``. ``verified`` is True only if every check passes."""
    checks: dict[str, Any] = {}
    verdict = receipt.get("attestation_verdict") or {}
    job_id = str(receipt.get("job_id", ""))
    checks["verdict_signature"] = verify_verdict(verdict, verifier_public_key)
    checks["tier_matches"] = TIER_RANK.get(str(verdict.get("tier_reached")), -1) >= TIER_RANK.get(str(receipt.get("isolation_tier")), 99)
    checks["image_matches"] = bool(verdict) and receipt.get("image_hash") == verdict.get("image_hash")
    checks["verdict_for_this_job"] = bool(job_id) and verdict.get("job_id") == job_id
    if expected_image_hash is not None:
        checks["expected_image"] = verdict.get("image_hash") == expected_image_hash

    root = receipt.get("merkle_root", "")
    proofs: dict[str, bool] = {}
    receipt_tree_size = 0
    for name, p in (receipt.get("inclusion_proofs") or {}).items():
        try:
            # Every proof is against the one tree the receipt's root summarises.
            if not isinstance(p["tree_size"], int) or (receipt_tree_size and p["tree_size"] != receipt_tree_size):
                raise TypeError("inconsistent tree size")
            receipt_tree_size = p["tree_size"]
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

    anchor = receipt.get("anchor")
    if anchor:
        try:
            anchor_root = str(anchor["merkle_root"])
            anchor_size = anchor["tree_size"]
            consistency_ok = isinstance(anchor_size, int) and _consistent(
                receipt_tree_size, root, anchor_size, anchor_root, anchor.get("consistency_proof", [])
            )
        except (KeyError, TypeError):
            anchor_root, consistency_ok = "", False
        checks["anchor_consistency"] = consistency_ok

        if rpc_url is not None or vault_address is not None:
            if rpc_url and vault_address:
                checks["anchored_on_chain"] = consistency_ok and check_on_chain_anchor(rpc_url, vault_address, anchor_root)
            else:
                checks["anchored_on_chain"] = False
    elif rpc_url is not None or vault_address is not None:
        if rpc_url and vault_address:
            anchored = check_on_chain_anchor(rpc_url, vault_address, root)
            checks["anchored_on_chain"] = anchored
            if not anchored:
                checks["anchor_status"] = "not_yet_anchored"
        else:
            checks["anchored_on_chain"] = False

    if witness_threshold > 0:
        checks["witness_cosignatures"] = check_witnesses(
            receipt, receipt_tree_size, root, witness_keys or {}, witness_threshold, log_public_key
        )

    verified = (
        checks["verdict_signature"] is True
        and checks["tier_matches"]
        and checks["image_matches"]
        and checks["verdict_for_this_job"]
        and checks.get("expected_image", True)
        and checks["verdict_logged"]
        and all(proofs.values())
        and checks["wipe_receipt"] in (True, "absent")
        and checks.get("anchor_consistency", True) is True
        and checks.get("anchored_on_chain", True) is True
        and (witness_threshold == 0 or checks.get("witness_cosignatures") is True)
    )
    return {"verified": verified, "isolation_tier": receipt.get("isolation_tier"), "machine_id": receipt.get("machine_id"), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a ComputeCoin Privacy Receipt offline")
    parser.add_argument("receipt_file")
    parser.add_argument("--verifier-key", required=True, help="ComputeCoin verifier Ed25519 public key (hex)")
    parser.add_argument("--image-hash", help="OS image hash you expect the host to have booted")
    parser.add_argument("--rpc", help="Ethereum/BSC RPC endpoint URL to verify anchoring")
    parser.add_argument("--vault", help="ComputeStakeVault contract address")
    parser.add_argument("--witness-keys", help="Path to JSON file with {witness_name: ed25519_pubkey_hex}")
    parser.add_argument("--witness-threshold", type=int, default=0, help="Required number of valid witness cosignatures")
    parser.add_argument("--log-key", help="Pinned transparency-log Ed25519 public key (hex) the witnesses must have cosigned for")
    args = parser.parse_args(argv)
    try:
        with open(args.receipt_file, "r", encoding="utf-8") as f:
            receipt = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read receipt: {exc}", file=sys.stderr)
        return 2

    w_keys: dict[str, str] = {}
    if args.witness_keys:
        val = args.witness_keys.strip()
        try:
            if val.startswith("{"):
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    w_keys = {str(k): str(v).lower() for k, v in parsed.items()}
            elif os.path.isfile(val):
                with open(val, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                    if isinstance(parsed, dict):
                        w_keys = {str(k): str(v).lower() for k, v in parsed.items()}
            else:
                for part in val.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        w_keys[k.strip()] = v.strip().lower()
                    else:
                        raise ValueError(f"invalid witness key specification: {part}")
        except Exception as exc:
            print(f"cannot parse witness keys: {exc}", file=sys.stderr)
            return 2

    result = verify_receipt(
        receipt,
        args.verifier_key,
        args.image_hash,
        rpc_url=args.rpc,
        vault_address=args.vault,
        witness_keys=w_keys,
        witness_threshold=args.witness_threshold,
        log_public_key=args.log_key,
    )
    print(f"Status:         {'VERIFIED' if result['verified'] else 'FAILED'}")
    print(f"Isolation tier: {result['isolation_tier']}")
    print(f"Machine:        {result['machine_id']}")
    for name, value in result["checks"].items():
        if name == "anchored_on_chain" and result["checks"].get("anchor_status") == "not_yet_anchored":
            print("  anchored_on_chain: not yet anchored (receipt has no anchor section)")
        elif isinstance(value, dict):
            for sub, ok in value.items():
                print(f"  {name}.{sub}: {'PASS' if ok else 'FAIL'}")
        else:
            label = "PASS" if value is True else "FAIL" if value is False else str(value)
            print(f"  {name}: {label}")
    if "anchored_on_chain" not in result["checks"]:
        print("Not checked: on-chain anchoring of the log root (specify --rpc and --vault).")
    if args.witness_threshold == 0 and "witness_cosignatures" not in result["checks"]:
        print("Not checked: log witness cosignatures (specify --witness-keys and --witness-threshold).")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
