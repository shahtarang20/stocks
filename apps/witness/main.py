"""Log Witness Service (ADR-0005 WP21).

An independent service that co-signs the platform's transparency log checkpoints.
Ensures the platform cannot present different versions of the log to different parties
(split-view prevention), cannot roll back the log, and cannot append entries inconsistently.

State persists atomically across service restarts.
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

from .config import WitnessConfig
from .crypto import (
    canonical_json,
    ed25519_public_from_private,
    sign_ed25519,
    verify_checkpoint,
    verify_consistency_proof,
)

logger = logging.getLogger("witness")
app = FastAPI(title="ComputeCoin Log Witness", version="1.0.0")
config = WitnessConfig()

_lock = threading.Lock()


class CosignRequest(BaseModel):
    checkpoint: dict[str, Any]
    consistency_proof: list[str] = Field(default_factory=list, max_length=128)


class CosignResponse(BaseModel):
    witness: str
    signature: str
    tree_size: int
    root_hash: str
    log_public_key: str = ""


# --------------------------------------------------------------------------- persistent state

def _load_state() -> dict[str, Any]:
    try:
        with open(config.state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("tree_size", 0)
    state.setdefault("root_hash", None)
    return state


def _save_state(state: dict[str, Any]) -> None:
    """Atomic write: a crash never leaves a corrupted state."""
    directory = os.path.dirname(os.path.abspath(config.state_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".witness-state-")
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
        "witness": config.name or None,
        "public_key": (
            ed25519_public_from_private(config.private_key)
            if "WITNESS_PRIVATE_KEY" not in missing
            else None
        ),
        "missing": missing,
    }


@app.get("/state")
def state() -> dict[str, Any]:
    """The latest log checkpoint size and root hash accepted by this witness."""
    with _lock:
        s = _load_state()
    return {"tree_size": s["tree_size"], "root_hash": s["root_hash"]}


@app.post("/cosign", response_model=CosignResponse)
def cosign_checkpoint(body: CosignRequest) -> CosignResponse:
    if config.missing():
        raise _refuse("witness_not_configured", status.HTTP_503_SERVICE_UNAVAILABLE)

    cp = body.checkpoint
    if not verify_checkpoint(cp, config.log_public_key):
        raise _refuse("checkpoint_signature_invalid")

    cp_size = int(cp["tree_size"])
    cp_root = str(cp["root_hash"]).lower()

    with _lock:
        s = _load_state()

        if cp_size < s["tree_size"]:
            raise _refuse("log_rolled_back", status.HTTP_409_CONFLICT)

        if cp_size == s["tree_size"]:
            if s["tree_size"] > 0 and str(s["root_hash"]).lower() != cp_root:
                # Same size, different root hash: split view detected!
                logger.error(
                    "SPLIT VIEW DETECTED: size=%d existing_root=%s proposed_root=%s",
                    cp_size,
                    s["root_hash"],
                    cp_root,
                )
                raise _refuse("split_view_detected", status.HTTP_409_CONFLICT)
        else:
            # cp_size > s["tree_size"]
            if s["tree_size"] > 0:
                if not verify_consistency_proof(
                    s["tree_size"],
                    cp_size,
                    str(s["root_hash"]),
                    cp_root,
                    body.consistency_proof,
                ):
                    logger.error(
                        "INCONSISTENT LOG: old=(size=%d, root=%s) new=(size=%d, root=%s)",
                        s["tree_size"],
                        s["root_hash"],
                        cp_size,
                        cp_root,
                    )
                    raise _refuse("log_inconsistent", status.HTTP_409_CONFLICT)

            # Valid forward extension: update state before answering
            s["tree_size"] = cp_size
            s["root_hash"] = cp_root
            _save_state(s)

        statement = {
            "v": "ccpp-witness-v1",
            "tree_size": cp_size,
            "root_hash": cp_root,
            "log_public_key": config.log_public_key.lower(),
            "witness": config.name,
        }
        sig = sign_ed25519(config.private_key, canonical_json(statement))

    logger.info("cosigned checkpoint tree_size=%d root=%s", cp_size, cp_root)
    return CosignResponse(
        witness=config.name,
        signature=sig,
        tree_size=cp_size,
        root_hash=cp_root,
        log_public_key=config.log_public_key.lower(),
    )
