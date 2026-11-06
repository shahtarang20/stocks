#!/usr/bin/env python3
"""Generate the ccpp-fanout-v1 test vectors (docs/specs/ccpp-v1.md section 6g) into docs/specs/ccpp-v1-vectors.json.

STATUS: UNVERIFIED until this script has been run. The vectors file ships the fixed INPUTS under ``fanout_v1`` and
``null`` for every expected value; nothing there was computed by hand. Run

    python scripts/gen_fanout_vectors.py            # print the computed section
    python scripts/gen_fanout_vectors.py --write    # fill it into docs/specs/ccpp-v1-vectors.json
    python scripts/gen_fanout_vectors.py --check-app  # compare with apps/api (run from the repo root with the API deps installed)
    python scripts/gen_fanout_vectors.py --check    # exit 1 if the file differs from the computed values

then run the three implementations' vector tests (API, guardian, web) and the fan-out tests. The script is an
independent implementation of the section 6g rules (it imports nothing from the app) so that it can catch a bug in
``apps/api/app/services/fanout_privacy.py``; ``--check-app`` additionally compares against that module when the API
package is importable. Only the fixed test keys below are used (L7): they are public test material, never real keys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = pathlib.Path(__file__).resolve().parents[1]
VECTORS = ROOT / "docs" / "specs" / "ccpp-v1-vectors.json"
RAW, RAW_PUB = serialization.Encoding.Raw, serialization.PublicFormat.Raw

# ------------------------------------------------------------------ fixed inputs (public test material)
INPUTS: dict[str, Any] = {
    "verifier_seed_hex": "707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f",
    "parent_job_id": "00000000-0000-4000-8000-0000000000f0",
    "isolation_tier": "sealed",
    "part_job_ids": [
        "00000000-0000-4000-8000-0000000000a0",
        "00000000-0000-4000-8000-0000000000a1",
        "00000000-0000-4000-8000-0000000000a2",
    ],
    # The offline-verifiable body of each part receipt (section "Part receipt hash"). Values are placeholders, not real receipts.
    "part_receipt_bodies": [
        {
            "job_id": f"00000000-0000-4000-8000-0000000000a{i}",
            "isolation_tier": "sealed",
            "image_hash": "sha256:" + f"{i + 1:x}" * 64,
            "attestation_verdict": {"verdict_id": f"verdict-{i}", "tier_reached": "sealed"},
            "wipe_receipt": {"job_id": f"00000000-0000-4000-8000-0000000000a{i}", "wiped_items": ["key_material", "scratch_dir"]},
            "merkle_root": f"{i + 10:x}" * 64,
            "inclusion_proofs": {},  # a name -> proof object map in real receipts (privacy_receipt.py)
        }
        for i in range(3)
    ],
    "stream": {
        "recipient_private_hex": "101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f",
        "ephemeral_private_hex": "505152535455565758595a5b5c5d5e5f606162636465666768696a6b6c6d6e6f",
        "nonce_hex": "a0a1a2a3a4a5a6a7a8a9aaab",
        "job_id": "00000000-0000-4000-8000-0000000000a0",
        "seq": 3,
        "final": False,
        "text_utf8": "hello",
        "pad_bytes": 256,
    },
    "kit_output": {"job_id": "00000000-0000-4000-8000-0000000000a0", "name": "result"},
}


# ------------------------------------------------------------------ primitives (section 2, 3.1, 6g)
def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def leaf_hash(event_type: str, payload: dict) -> str:
    return hashlib.sha256(b"\x00" + event_type.encode() + b":" + canonical(payload)).hexdigest()


def node_hash(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").hexdigest()
    if n == 1:
        return leaves[0]
    k = 1
    while k * 2 < n:  # largest power of two strictly less than n (RFC 6962)
        k *= 2
    return node_hash(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def pad(data: bytes, block: int) -> bytes:
    framed = len(data).to_bytes(4, "big") + data
    return framed + b"\x00" * (-len(framed) % block)


def seal_fixed(recipient_priv_hex: str, eph_priv_hex: str, nonce_hex: str, plaintext: bytes, aad: bytes) -> dict[str, str]:
    rpriv = X25519PrivateKey.from_private_bytes(bytes.fromhex(recipient_priv_hex))
    rpub = rpriv.public_key().public_bytes(RAW, RAW_PUB)
    eph = X25519PrivateKey.from_private_bytes(bytes.fromhex(eph_priv_hex))
    epk = eph.public_key().public_bytes(RAW, RAW_PUB)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(rpub))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ccpp-v1|" + epk + rpub).derive(shared)
    nonce = bytes.fromhex(nonce_hex)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return {"v": "ccpp-v1", "alg": "X25519-HKDF-SHA256-ChaCha20Poly1305", "epk": epk.hex(), "nonce": nonce_hex, "ct": ct.hex()}


# ------------------------------------------------------------------ computation
def compute() -> dict[str, Any]:
    i = INPUTS
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(i["verifier_seed_hex"]))
    vpub = sk.public_key().public_bytes(RAW, RAW_PUB).hex()
    part_hashes = [hashlib.sha256(canonical(b)).hexdigest() for b in i["part_receipt_bodies"]]
    entries = [{"index": n, "job_id": i["part_job_ids"][n], "receipt_hash": part_hashes[n]} for n in range(3)]
    leaves = [leaf_hash("FANOUT_PART_RECEIPT", e) for e in entries]
    root = merkle_root(leaves)
    receipt: dict[str, Any] = {
        "v": "ccpp-fanout-v1", "parent_job_id": i["parent_job_id"], "isolation_tier": i["isolation_tier"],
        "part_count": 3, "part_receipts": entries, "merkle_root": root, "verifier_public_key": vpub,
    }
    signing = canonical({k: receipt[k] for k in ("v", "parent_job_id", "isolation_tier", "part_count", "part_receipts", "merkle_root", "verifier_public_key")})
    receipt["signature"] = sk.sign(signing).hex()
    log_payload = {
        "parent_job_id": receipt["parent_job_id"], "isolation_tier": receipt["isolation_tier"], "part_count": 3,
        "merkle_root": root, "receipt_sha256": hashlib.sha256(canonical(receipt)).hexdigest(), "verifier_public_key": vpub,
    }
    s = i["stream"]
    aad = f"ccpp-v1|stream|{s['job_id']}|{s['seq']}|{1 if s['final'] else 0}"
    padded = pad(s["text_utf8"].encode(), s["pad_bytes"])
    envelope = seal_fixed(s["recipient_private_hex"], s["ephemeral_private_hex"], s["nonce_hex"], padded, aad.encode())
    return {
        "verifier_public_key_hex": vpub,
        "part_receipt_hashes": part_hashes,
        "part_leaf_hashes": leaves,
        "merkle_root": root,
        "signing_bytes_utf8": signing.decode(),
        "job_receipt": receipt,
        "transparency_log": {
            "event_type": "FANOUT_RECEIPT", "payload": log_payload, "leaf_hash": leaf_hash("FANOUT_RECEIPT", log_payload),
        },
        "stream_chunk": {
            "aad_utf8": aad, "padded_plaintext_hex": padded.hex(), "padded_length": len(padded), "envelope": envelope,
        },
        "kit_output_file_aad_utf8": f"ccpp-v1|job-output-file|{i['kit_output']['job_id']}|{i['kit_output']['name']}",
    }


def section() -> dict[str, Any]:
    return {
        "status": "generated by scripts/gen_fanout_vectors.py; UNVERIFIED until the three implementations pass them",
        "inputs": INPUTS,
        "expected": compute(),
    }


def check_against_app(sec: dict[str, Any]) -> None:
    sys.path.insert(0, str(ROOT / "apps" / "api"))
    from app.services import fanout_privacy as fp  # type: ignore

    exp = sec["expected"]
    hashes_ = [fp.part_receipt_hash(b) for b in INPUTS["part_receipt_bodies"]]
    assert hashes_ == exp["part_receipt_hashes"], "part receipt hash differs from the app"
    leaves = [fp.part_leaf(n, INPUTS["part_job_ids"][n], hashes_[n]) for n in range(3)]
    assert leaves == exp["part_leaf_hashes"], "leaf hash differs from the app"
    assert fp.receipt_log_payload(exp["job_receipt"]) == exp["transparency_log"]["payload"], "log payload differs from the app"
    assert fp.verify_job_receipt(exp["job_receipt"], verifier_public_hex=exp["verifier_public_key_hex"])["valid"], "app rejects the receipt"
    assert fp.compute_merkle_root(leaves) == exp["merkle_root"], "merkle root differs from the app"
    assert fp._signing_bytes(exp["job_receipt"]).decode() == exp["signing_bytes_utf8"], "signing bytes differ from the app"
    from app.services import privacy_crypto as pc  # type: ignore

    s = INPUTS["stream"]
    ch = exp["stream_chunk"]
    assert pc.stream_aad(s["job_id"], s["seq"], s["final"]).decode() == ch["aad_utf8"], "stream AAD differs from the app"
    opened = pc.open_sealed(s["recipient_private_hex"], ch["envelope"], ch["aad_utf8"].encode())
    assert opened.hex() == ch["padded_plaintext_hex"], "sealed stream chunk does not open to the padded plaintext"
    assert pc.output_aad("X").decode() == "ccpp-v1|job-output|X", "output AAD differs"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="fill the computed section into docs/specs/ccpp-v1-vectors.json")
    ap.add_argument("--check", action="store_true", help="exit 1 if the file's fanout_v1 section differs from the computed one")
    ap.add_argument("--check-app", action="store_true", help="also compare with apps/api/app/services/fanout_privacy.py")
    args = ap.parse_args()
    sec = section()
    if args.check_app:
        check_against_app(sec)
        print("app and script agree", file=sys.stderr)
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    if args.check:
        same = doc.get("fanout_v1") == json.loads(json.dumps(sec))
        print("vectors file matches" if same else "vectors file DIFFERS (run with --write)", file=sys.stderr)
        return 0 if same else 1
    if args.write:
        new_sec = json.loads(json.dumps(sec))
        if doc.get("fanout_v1") == new_sec:
            print("fanout_v1 already up to date; file untouched", file=sys.stderr)
            return 0
        doc["fanout_v1"] = new_sec
        raw = VECTORS.read_text(encoding="utf-8")
        tail = "\n" if raw.endswith("\n") else ""  # keep the file's trailing-newline convention (idempotent rewrites)
        # Bytes, not write_text: text mode would turn every "\n" into "\r\n" on Windows and rewrite the whole file.
        VECTORS.write_bytes((json.dumps(doc, indent=2, ensure_ascii=False) + tail).encode("utf-8"))
        print(f"wrote fanout_v1 into {VECTORS}", file=sys.stderr)
        return 0
    print(json.dumps(sec, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
