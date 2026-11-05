"""Private fan-out (U18, docs/architecture/U18-privacy-modes-design.md; spec: ccpp-v1.md section 6g).

Each part is a private job with its own key set and its own privacy receipt. The job-level receipt commits to the
ORDERED list of part receipt hashes with an RFC 6962 Merkle root and is signed by the verifier key. The merge only
verifies and orders ciphertext; nothing here can open an envelope.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.jobs import Job
from app.models.privacy import PrivacyReceipt
from app.services.privacy_crypto import canonical_json, is_data_envelope, is_hex32, is_sealed_envelope, sign, verify
from app.services.transparency_log import compute_merkle_root, hash_leaf

RECEIPT_VERSION = "ccpp-fanout-v1"
PART_LEAF_EVENT = "FANOUT_PART_RECEIPT"
RECEIPT_LOG_EVENT = "FANOUT_RECEIPT"  # transparency-log event for the issued job-level receipt (ccpp-v1.md 6g)
PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
RECEIPT_BODY_FIELDS = ("job_id", "isolation_tier", "image_hash", "attestation_verdict", "wipe_receipt", "merkle_root", "inclusion_proofs")


class PrivateFanoutError(Exception):
    def __init__(self, code: str, errors: list[dict] | None = None, status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = status
        self.errors = errors or [{"field": "private_parts", "code": code, "message": code}]


def private_fanout_enabled() -> bool:
    return bool(settings.PRIVACY_FANOUT_ENABLED and settings.privacy_active)


def is_private_group(plan: dict | None) -> bool:
    return bool((plan or {}).get("private"))


# ------------------------------------------------------------------------------------------ submit checks
def validate_private_parts(private_parts: Any, part_count: int) -> list[dict]:
    """Normalise the customer's per-part key sets. Raises PrivateFanoutError with a field path."""
    from app.services.key_guardians import validate_sealed_shares

    def err(i: int, field: str, code: str) -> PrivateFanoutError:
        return PrivateFanoutError(code, [{"field": f"private_parts[{i}].{field}".rstrip("."), "code": code, "message": code}])

    if not isinstance(private_parts, list) or len(private_parts) != part_count:
        raise PrivateFanoutError("private_parts_count_mismatch", [{
            "field": "private_parts", "code": "private_parts_count_mismatch",
            "message": f"exactly {part_count} entries are needed, one per part"}])
    out: list[dict] = []
    seen_keys: set[str] = set()
    seen_inputs: set[str] = set()
    seen_shares: set[str] = set()
    for i, p in enumerate(private_parts):
        if not isinstance(p, dict) or set(p) != {"encrypted_input", "dk_shares", "result_public_key"}:
            raise err(i, "", "private_part_shape_invalid")
        if not is_data_envelope(p["encrypted_input"]):
            raise err(i, "encrypted_input", "not_a_data_envelope")
        try:
            validate_sealed_shares(p["dk_shares"])
        except ValueError:
            raise err(i, "dk_shares", "dk_shares_invalid") from None
        key = p["result_public_key"]
        if not is_hex32(key) or key != key.lower():
            raise err(i, "result_public_key", "result_public_key_invalid")
        fp_in = hashlib.sha256(canonical_json(p["encrypted_input"])).hexdigest()
        fp_sh = hashlib.sha256(canonical_json(p["dk_shares"])).hexdigest()
        if key in seen_keys:
            raise err(i, "result_public_key", "key_set_reused_across_parts")
        if fp_in in seen_inputs or fp_sh in seen_shares:
            raise err(i, "", "key_set_reused_across_parts")
        seen_keys.add(key)
        seen_inputs.add(fp_in)
        seen_shares.add(fp_sh)
        out.append({"encrypted_input": p["encrypted_input"], "dk_shares": p["dk_shares"], "result_public_key": key})
    return out


# ------------------------------------------------------------------------------------------ receipt hashing
def part_receipt_body(receipt: Any) -> dict[str, Any]:
    """The offline-verifiable fields of a part receipt (a PrivacyReceipt row or a dict)."""
    get = receipt.get if isinstance(receipt, dict) else (lambda k, d=None: getattr(receipt, k, d))
    return {f: get(f) if f != "job_id" else str(get("job_id")) for f in RECEIPT_BODY_FIELDS}


def part_receipt_hash(receipt: Any) -> str:
    return hashlib.sha256(canonical_json(part_receipt_body(receipt))).hexdigest()


def part_leaf(index: int, job_id: str, receipt_hash: str) -> str:
    return hash_leaf(PART_LEAF_EVENT, {"index": int(index), "job_id": str(job_id), "receipt_hash": receipt_hash})


def _signing_bytes(r: dict[str, Any]) -> bytes:
    return canonical_json({k: r[k] for k in ("v", "parent_job_id", "isolation_tier", "part_count", "part_receipts", "merkle_root", "verifier_public_key")})


def build_job_receipt(*, parent_job_id: str, isolation_tier: str, parts: list[tuple[int, str, str]]) -> dict[str, Any]:
    """``parts``: (index, part job id, part receipt hash). Indices must be exactly 0..n-1; the order is fixed by index."""
    from app.services.privacy_keys import verifier_public_key, verifier_signing_key

    ordered = sorted(parts, key=lambda t: t[0])
    if [t[0] for t in ordered] != list(range(len(ordered))) or not ordered:
        raise PrivateFanoutError("part_receipts_incomplete")
    entries = [{"index": i, "job_id": str(j), "receipt_hash": h} for i, j, h in ordered]
    receipt = {
        "v": RECEIPT_VERSION, "parent_job_id": str(parent_job_id), "isolation_tier": isolation_tier,
        "part_count": len(entries), "part_receipts": entries,
        "merkle_root": compute_merkle_root([part_leaf(e["index"], e["job_id"], e["receipt_hash"]) for e in entries]),
        "verifier_public_key": verifier_public_key(),
    }
    receipt["signature"] = sign(verifier_signing_key(), _signing_bytes(receipt))
    return receipt


def receipt_log_payload(job_receipt: dict[str, Any]) -> dict[str, Any]:
    """The log payload for an issued job-level receipt: public data only (ids, counts, hashes, the verifier key)."""
    return {
        "parent_job_id": job_receipt["parent_job_id"],
        "isolation_tier": job_receipt["isolation_tier"],
        "part_count": job_receipt["part_count"],
        "merkle_root": job_receipt["merkle_root"],
        "receipt_sha256": hashlib.sha256(canonical_json(job_receipt)).hexdigest(),
        "verifier_public_key": job_receipt["verifier_public_key"],
    }


async def log_job_receipt(db: AsyncSession, job_receipt: dict[str, Any]) -> dict[str, Any]:
    """Append the issued receipt to the transparency log once per parent job (a repeated merge finds its leaf).

    Returns ``{"logged": True, "leaf_index", "leaf_hash"}`` or ``{"logged": False}`` when the log is unavailable: the
    signed receipt stays valid and is still returned, but the caller can see that it is not in the log yet.
    """
    from sqlalchemy import select

    from app.models.privacy import TransparencyLogLeaf
    from app.services.transparency_log import append_transparency_log_entry

    payload = receipt_log_payload(job_receipt)
    try:
        async with db.begin_nested():
            existing = (await db.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.event_type == RECEIPT_LOG_EVENT))).scalars().all()
            for leaf in existing:
                if (leaf.payload or {}).get("parent_job_id") == payload["parent_job_id"]:
                    return {"logged": True, "leaf_index": leaf.leaf_index, "leaf_hash": leaf.leaf_hash}
            leaf = await append_transparency_log_entry(db, RECEIPT_LOG_EVENT, payload)
        return {"logged": True, "leaf_index": leaf.leaf_index, "leaf_hash": leaf.leaf_hash}
    except Exception:
        return {"logged": False}


def verify_job_receipt(
    job_receipt: dict[str, Any], part_receipts: dict[str, dict[str, Any]] | None = None, verifier_public_hex: str | None = None
) -> dict[str, Any]:
    """Offline check with public data only. ``part_receipts`` maps part job id to the full part receipt (optional)."""
    from app.services.privacy_receipt import verify_privacy_receipt_offline

    checks: dict[str, Any] = {}
    try:
        entries = job_receipt["part_receipts"]
        n = int(job_receipt["part_count"])
        checks["version"] = job_receipt.get("v") == RECEIPT_VERSION
        checks["verifier_key"] = verifier_public_hex is None or job_receipt.get("verifier_public_key") == verifier_public_hex
        checks["indices"] = isinstance(entries, list) and n == len(entries) and [e["index"] for e in entries] == list(range(n)) and n > 0
        checks["root"] = checks["indices"] and job_receipt["merkle_root"] == compute_merkle_root(
            [part_leaf(e["index"], e["job_id"], e["receipt_hash"]) for e in entries])
        checks["signature"] = verify(str(job_receipt["verifier_public_key"]), _signing_bytes(job_receipt), job_receipt.get("signature", ""))
        checks["distinct_jobs"] = checks["indices"] and len({e["job_id"] for e in entries}) == n
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"valid": False, "checks": {**checks, "malformed": True}}
    parts_ok: Any = "not_supplied"
    if part_receipts is not None and checks["indices"]:
        parts_ok = {}
        for e in entries:
            pr = part_receipts.get(e["job_id"])
            ok = (
                isinstance(pr, dict)
                and part_receipt_hash(pr) == e["receipt_hash"]
                and str(pr.get("job_id")) == e["job_id"]
                and pr.get("isolation_tier") == job_receipt.get("isolation_tier")
                and verify_privacy_receipt_offline(pr, job_receipt.get("verifier_public_key"))["valid"]
            )
            parts_ok[e["job_id"]] = bool(ok)
    checks["part_receipts"] = parts_ok
    valid = all(checks[k] is True for k in ("version", "verifier_key", "indices", "root", "signature", "distinct_jobs")) and (
        parts_ok == "not_supplied" or all(parts_ok.values()))
    return {"valid": valid, "checks": checks}


# ------------------------------------------------------------------------------------------ run validity and merge
async def private_run_valid(db: AsyncSession, job: Job) -> bool:
    """A COMPLETED private child is a valid result only with a sealed output, a commitment and a verified receipt."""
    c = job.output_commitment
    if not is_sealed_envelope(job.encrypted_output) or not (isinstance(c, str) and len(c) == 64):
        return False
    if job.output is not None or job.privacy_receipt_id is None:
        return False
    row = await db.get(PrivacyReceipt, job.privacy_receipt_id)
    return bool(row is not None and row.verified and str(row.job_id) == str(job.id))


async def merge_private(db: AsyncSession, group: Any, parts: list[Any], parent: Job) -> dict:
    """Verify and order ciphertext outputs; build the signed job-level receipt. Raises ``MergeFailure``."""
    from app.services.privacy_receipt import verify_privacy_receipt_offline

    sealed: list[dict] = []
    triples: list[tuple[int, str, str]] = []
    for part in sorted(parts, key=lambda p: p.idx):
        if part.winner_job_id is None:
            raise MergeFailure("part_not_done")
        job = await db.get(Job, part.winner_job_id)
        if job is None or not await private_run_valid(db, job):
            raise MergeFailure("output_missing")
        row = await db.get(PrivacyReceipt, job.privacy_receipt_id)
        if not verify_privacy_receipt_offline(part_receipt_body(row))["valid"]:
            raise MergeFailure("part_receipt_invalid")
        rh = part_receipt_hash(row)
        triples.append((part.idx, str(job.id), rh))
        sealed.append({"index": part.idx, "job_id": str(job.id), "encrypted_output": job.encrypted_output,
                       "output_commitment": job.output_commitment, "receipt_hash": rh})
    try:
        job_receipt = build_job_receipt(parent_job_id=str(parent.id), isolation_tier=parent.isolation_tier or "sealed", parts=triples)
    except PrivateFanoutError as exc:
        raise MergeFailure(exc.code) from None
    except Exception:
        raise MergeFailure("receipt_signing_unavailable") from None
    manifest = hashlib.sha256(json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    transparency = await log_job_receipt(db, job_receipt)
    return {"fanout": {"parts": len(parts), "merge": "ordered_ciphertext", "outputs": {"sealed": sealed},
                       "manifest_sha256": manifest, "job_receipt": job_receipt, "transparency": transparency}}


class MergeFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
