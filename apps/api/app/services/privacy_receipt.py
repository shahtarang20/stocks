"""Privacy Receipts for Proof-of-Privacy (ADR-0005 section 5.6).

A receipt bundles the signed attestation verdict, the guardians' key releases, the host's
signed wipe receipt, and RFC 6962 inclusion proofs for every one of them against a single
transparency-log root. ``verify_privacy_receipt_offline`` (and the SDK's ``ccverify``) checks it
with nothing but the verifier's public key: signatures are Ed25519, and every proof is checked
against the payload it claims to cover.

What a receipt does NOT prove: that the log root was anchored on-chain (anchoring is not built),
or what the host's hardware really did (see attestation_verifier for the attestation limits).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jobs import Job
from app.models.privacy import PrivacyReceipt
from app.models.provider import ProviderMachine
from app.services.attestation_verifier import TIER_RANK, verify_verdict_signature
from app.services.privacy_crypto import canonical_json, verify
from app.services.transparency_log import append_transparency_log_entry, get_inclusion_proofs, hash_leaf, verify_inclusion_proof

WIPE_FIELDS = ("machine_id", "job_id", "wiped_items", "wiped_at")


def wipe_receipt_signing_bytes(wipe: dict[str, Any]) -> bytes:
    body = {f: wipe[f] for f in WIPE_FIELDS}
    body["wiped_items"] = sorted(body["wiped_items"])
    return canonical_json(body)


def verify_wipe_receipt(wipe: dict[str, Any], receipt_signing_public_key: str) -> bool:
    try:
        return verify(receipt_signing_public_key, wipe_receipt_signing_bytes(wipe), wipe.get("signature", ""))
    except (KeyError, TypeError):
        return False


async def create_privacy_receipt_for_job(
    db: AsyncSession,
    *,
    job: Job,
    machine: ProviderMachine,
    attestation_verdict: dict[str, Any],
    verdict_leaf_index: int,
    guardian_releases: list[dict[str, Any]],
    wipe_receipt: dict[str, Any] | None = None,
) -> PrivacyReceipt:
    """Record the receipt for a finished job. Refuses evidence that does not verify."""
    if not verify_verdict_signature(attestation_verdict):
        raise ValueError("attestation verdict signature is invalid")
    if attestation_verdict.get("machine_id") != str(machine.id) or job.machine_id != machine.id:
        raise ValueError("verdict, job and machine do not belong together")

    leaf_indices = [verdict_leaf_index] + [int(r["log_leaf_index"]) for r in guardian_releases]
    if wipe_receipt is not None:
        if wipe_receipt.get("job_id") != str(job.id) or wipe_receipt.get("machine_id") != str(machine.id):
            raise ValueError("wipe receipt is for a different job or machine")
        if not verify_wipe_receipt(wipe_receipt, attestation_verdict["receipt_signing_public_key"]):
            raise ValueError("wipe receipt signature does not verify with the key in the verdict")
        leaf = await append_transparency_log_entry(db, "WIPE_RECEIPT", wipe_receipt)
        leaf_indices.append(leaf.leaf_index)

    root, _, proofs = await get_inclusion_proofs(db, leaf_indices)
    inclusion_proofs: dict[str, Any] = {"attestation_verdict": proofs[verdict_leaf_index]}
    for r in guardian_releases:
        inclusion_proofs[f"key_release:{r['guardian']}"] = proofs[int(r["log_leaf_index"])]
    if wipe_receipt is not None:
        inclusion_proofs["wipe_receipt"] = proofs[leaf_indices[-1]]

    receipt_id = uuid.uuid4()  # set explicitly: the column default only fills in at flush time
    receipt = PrivacyReceipt(
        id=receipt_id,
        job_id=job.id,
        machine_id=machine.id,
        isolation_tier=job.isolation_tier or "open",
        image_hash=attestation_verdict["image_hash"],
        attestation_verdict=attestation_verdict,
        guardian_releases=[{k: r[k] for k in ("guardian", "share_index", "log_leaf_index", "log_leaf_hash")} for r in guardian_releases],
        wipe_receipt=wipe_receipt,
        merkle_root=root,
        inclusion_proofs=inclusion_proofs,
        verified=False,
    )
    receipt.verified = verify_privacy_receipt_offline(
        {
            "job_id": str(job.id),
            "isolation_tier": receipt.isolation_tier,
            "image_hash": receipt.image_hash,
            "attestation_verdict": attestation_verdict,
            "wipe_receipt": wipe_receipt,
            "merkle_root": root,
            "inclusion_proofs": inclusion_proofs,
        }
    )["valid"]
    db.add(receipt)
    job.privacy_receipt_id = receipt_id
    await db.flush()
    return receipt


def verify_privacy_receipt_offline(receipt: dict[str, Any], verifier_public_hex: str | None = None) -> dict[str, Any]:
    """Check a receipt using only public information. ``valid`` is True only if every check passes."""
    checks: dict[str, Any] = {}
    verdict = receipt.get("attestation_verdict") or {}
    job_id = str(receipt.get("job_id", ""))

    checks["attestation_verdict_signature"] = verify_verdict_signature(verdict, verifier_public_hex) if verdict else False
    checks["tier_matches"] = TIER_RANK.get(str(verdict.get("tier_reached")), -1) >= TIER_RANK.get(str(receipt.get("isolation_tier")), 99)
    checks["image_matches"] = bool(verdict) and receipt.get("image_hash") == verdict.get("image_hash")

    root = receipt.get("merkle_root", "")
    proofs = receipt.get("inclusion_proofs") or {}
    proof_results: dict[str, bool] = {}
    for name, p in proofs.items():
        try:
            ok = (
                hash_leaf(p["event_type"], p["payload"]) == p["leaf_hash"]
                and verify_inclusion_proof(p["leaf_hash"], p["leaf_index"], p["tree_size"], root, p["audit_path"])
            )
            if name == "attestation_verdict":
                ok = ok and p["event_type"] == "ATTESTATION_VERDICT" and p["payload"] == verdict
            elif name.startswith("key_release:"):
                ok = ok and p["event_type"] == "KEY_RELEASE" and p["payload"].get("job_id") == job_id and p["payload"].get("verdict_id") == verdict.get("verdict_id")
            elif name == "wipe_receipt":
                ok = ok and p["event_type"] == "WIPE_RECEIPT" and p["payload"] == receipt.get("wipe_receipt")
        except (KeyError, TypeError, AttributeError):
            ok = False
        proof_results[name] = ok
    checks["inclusion_proofs"] = proof_results
    checks["verdict_logged"] = proof_results.get("attestation_verdict") is True

    wipe = receipt.get("wipe_receipt")
    if wipe:
        checks["wipe_receipt_signature"] = (
            wipe.get("job_id") == job_id and verify_wipe_receipt(wipe, str(verdict.get("receipt_signing_public_key", "")))
        )
    else:
        checks["wipe_receipt_signature"] = "absent"

    valid = (
        checks["attestation_verdict_signature"] is True
        and checks["tier_matches"]
        and checks["image_matches"]
        and checks["verdict_logged"]
        and all(proof_results.values())
        and checks["wipe_receipt_signature"] in (True, "absent")
    )
    return {"valid": valid, "isolation_tier": receipt.get("isolation_tier"), "checks": checks}
