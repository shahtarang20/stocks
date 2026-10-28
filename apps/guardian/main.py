"""External Key Guardian (ADR-0005 WP4).

An independent service, run by a party other than ComputeCoin, that holds ONE Data Key share per
private job and releases it, re-sealed to the attested host's in-RAM key, only when it has
checked everything itself:

1. its own configuration is complete (fail closed otherwise);
2. the attestation verdict is signed by the pinned verifier key, current, for this machine, and
   for a privacy tier, and names an OS image this guardian trusts;
3. the platform's transparency log really contains the KEY_RELEASE entry: the request carries a
   checkpoint signed by the pinned LOG key, a consistency proof from the largest log this
   guardian has already seen (so the platform cannot rewrite or fork the log it showed before),
   and an inclusion proof of the entry in that checkpoint;
4. the verdict was attested for THIS job (verdict format v2 carries a signed job_id), and
   a job is released at most once per verdict and at most ``max_releases_per_job`` times
   (retries on another host need a new verdict). State survives restarts.

The guardian never accepts a Merkle root from the caller: roots come only from signed checkpoints.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .config import GuardianConfig
from .crypto import (
    GuardianCryptoError,
    canonical_json,
    hash_leaf,
    is_hex32,
    open_sealed,
    release_aad,
    seal,
    share_aad,
    verdict_is_current,
    verify_checkpoint,
    verify_consistency_proof,
    verify_inclusion_proof,
    verify_signature,
    verify_verdict_signature,
    x25519_public_from_private,
)

logger = logging.getLogger("guardian")
app = FastAPI(title="ComputeCoin Key Guardian", version="1.1.0")
config = GuardianConfig()

PRIVACY_TIERS = {"sealed", "sealed_pro", "confidential"}
_lock = threading.Lock()  # FastAPI runs sync handlers in a thread pool


class ReleaseRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    machine_id: str = Field(min_length=1, max_length=64)
    attestation_verdict: dict[str, Any]
    sealed_share: dict[str, Any]
    log_entry: dict[str, Any]
    inclusion_proof: dict[str, Any]
    checkpoint: dict[str, Any]
    consistency_proof: list[str] = Field(default_factory=list, max_length=64)
    witness_cosignatures: list[dict[str, Any]] = Field(default_factory=list)


class ReleaseResponse(BaseModel):
    guardian: str
    share_index: int
    wrapped_share: dict[str, str]


# --------------------------------------------------------------------------- persistent state

def _load_state() -> dict[str, Any]:
    try:
        with open(config.state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("tree_size", 0)
    state.setdefault("root_hash", None)
    state.setdefault("releases", {})
    return state


def _save_state(state: dict[str, Any]) -> None:
    """Atomic write: a crash never leaves a half-written state that forgets what was released."""
    directory = os.path.dirname(os.path.abspath(config.state_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, config.state_path)


def _refuse(code: str, http: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=http, detail=code)


# --------------------------------------------------------------------------- routes

@app.get("/health")
def health() -> dict[str, Any]:
    missing = config.missing()
    return {
        "status": "ok" if not missing else "misconfigured",
        "guardian": config.name or None,
        "public_key": x25519_public_from_private(config.private_key) if "GUARDIAN_PRIVATE_KEY" not in missing else None,
        "missing": missing,
    }


@app.get("/state")
def state() -> dict[str, Any]:
    """The largest log this guardian has accepted; the platform proves consistency from here."""
    with _lock:
        s = _load_state()
    return {"tree_size": s["tree_size"], "root_hash": s["root_hash"]}


@app.post("/release", response_model=ReleaseResponse)
def release_key_share(body: ReleaseRequest) -> ReleaseResponse:
    if config.missing():
        raise _refuse("guardian_not_configured", status.HTTP_503_SERVICE_UNAVAILABLE)

    verdict = body.attestation_verdict
    if not verify_verdict_signature(verdict, config.verifier_public_key):
        raise _refuse("verdict_signature_invalid")
    if not verdict_is_current(verdict):
        raise _refuse("verdict_expired")
    if verdict.get("machine_id") != body.machine_id:
        raise _refuse("machine_id_mismatch")
    if verdict.get("job_id") != body.job_id:
        # One job, one key set: only a verdict attested for this job releases its share.
        raise _refuse("verdict_not_for_this_job")
    if str(verdict.get("tier_reached", "")).lower() not in PRIVACY_TIERS:
        raise _refuse("tier_not_private")
    if str(verdict.get("image_hash", "")).lower() not in config.trusted_images:
        raise _refuse("image_not_trusted", status.HTTP_403_FORBIDDEN)
    host_key = verdict.get("ephemeral_public_key")
    if not is_hex32(host_key):
        raise _refuse("invalid_host_key")

    share_index = body.sealed_share.get("x")
    if not isinstance(share_index, int) or not 1 <= share_index <= 255:
        raise _refuse("invalid_share_index")

    entry = body.log_entry
    expected = {
        "guardian": config.name,
        "job_id": body.job_id,
        "machine_id": body.machine_id,
        "verdict_id": verdict.get("verdict_id"),
        "share_index": share_index,
    }
    if any(entry.get(k) != v for k, v in expected.items()):
        raise _refuse("log_entry_mismatch")

    cp = body.checkpoint
    if not verify_checkpoint(cp, config.log_public_key):
        raise _refuse("checkpoint_signature_invalid")

    if config.witness_threshold > 0:
        cosigs = body.witness_cosignatures or cp.get("witness_cosignatures") or []
        valid_witnesses = set()
        for cs in cosigs:
            if not isinstance(cs, dict):
                continue
            w_name = str(cs.get("witness", ""))
            w_sig = str(cs.get("signature", ""))
            if w_name in config.witness_keys and w_sig:
                stmt = {
                    "v": "ccpp-witness-v1",
                    "tree_size": cp["tree_size"],
                    "root_hash": str(cp["root_hash"]).lower(),
                    "log_public_key": config.log_public_key.lower(),
                    "witness": w_name,
                }
                if verify_signature(config.witness_keys[w_name], canonical_json(stmt), w_sig):
                    valid_witnesses.add(config.witness_keys[w_name])  # distinct keys, not names
        if len(valid_witnesses) < config.witness_threshold:
            raise _refuse("witness_cosignatures_insufficient")

    proof = body.inclusion_proof
    if proof.get("tree_size") != cp["tree_size"] or not verify_inclusion_proof(
        hash_leaf("KEY_RELEASE", entry), proof.get("leaf_index"), cp["tree_size"], cp["root_hash"], proof.get("audit_path") or []
    ):
        raise _refuse("inclusion_proof_invalid")

    with _lock:
        s = _load_state()
        # The log may only grow, and only consistently with what this guardian saw before.
        if cp["tree_size"] < s["tree_size"]:
            raise _refuse("log_rolled_back", status.HTTP_409_CONFLICT)
        if s["tree_size"] > 0 and not verify_consistency_proof(
            s["tree_size"], cp["tree_size"], s["root_hash"], cp["root_hash"], body.consistency_proof
        ):
            raise _refuse("log_inconsistent", status.HTTP_409_CONFLICT)

        released = s["releases"].get(body.job_id, [])
        if verdict["verdict_id"] in released:
            raise _refuse("key_already_released_for_verdict", status.HTTP_409_CONFLICT)
        if len(released) >= config.max_releases_per_job:
            raise _refuse("release_limit_reached_for_job", status.HTTP_409_CONFLICT)

        try:
            raw_share = open_sealed(config.private_key, body.sealed_share.get("envelope") or {}, share_aad(config.name))
        except GuardianCryptoError as exc:
            raise _refuse("share_unreadable") from exc
        wrapped = seal(host_key, raw_share, release_aad(body.job_id, config.name))

        # Record BEFORE answering, so a crash cannot forget a release that was handed out.
        s["tree_size"], s["root_hash"] = cp["tree_size"], cp["root_hash"]
        s["releases"][body.job_id] = released + [verdict["verdict_id"]]
        _save_state(s)

    logger.info("released share job=%s verdict=%s tree_size=%s", body.job_id, verdict["verdict_id"], cp["tree_size"])
    return ReleaseResponse(guardian=config.name, share_index=share_index, wrapped_share=wrapped)
