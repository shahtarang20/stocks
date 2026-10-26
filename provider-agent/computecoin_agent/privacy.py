"""Sealed execution and wipe receipts on the host (ADR-0005).

Inside the host's RAM: open the guardians' Data Key shares with the in-RAM X25519 key, rebuild
the Data Key, decrypt the customer's input, run the workload, encrypt the result to the
customer's X25519 key, then zero the key material and sign a wipe receipt with the in-RAM
Ed25519 key that the attestation verdict names.

Honest limits of the wipe, which the receipt reflects by listing only what was actually done:
Python cannot reliably erase immutable ``bytes`` copies (the decrypted input, intermediate
strings, interpreter buffers), and GPU memory is not cleared here. Those guarantees belong to
the Sealed OS (RAM-only scratch, process exit, VRAM clear between jobs), not to this function.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from typing import Any, Callable

from .privacy_crypto import (
    INPUT_AAD,
    canonical_json,
    decrypt_data,
    open_sealed,
    output_aad,
    reconstruct_secret,
    release_aad,
    seal,
    sign,
)

WIPE_FIELDS = ("machine_id", "job_id", "wiped_items", "wiped_at")


def _zero(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


def wipe_receipt_signing_bytes(wipe: dict[str, Any]) -> bytes:
    """Must match apps/api/app/services/privacy_receipt.py and the SDK's ccverify."""
    body = {f: wipe[f] for f in WIPE_FIELDS}
    body["wiped_items"] = sorted(body["wiped_items"])
    return canonical_json(body)


def wipe_environment(
    *,
    machine_id: str,
    job_id: str,
    receipt_signing_private_key: str,
    memory_buffers: list[bytearray] | None = None,
    scratch_dir: str | None = None,
    gpu_reset_fn: Callable[[], bool] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Zero the given buffers, remove the scratch dir, optionally reset the GPU; sign what was done."""
    wiped: list[str] = []
    if memory_buffers:
        for buf in memory_buffers:
            _zero(buf)
        wiped.append("key_material")
    if scratch_dir and os.path.isdir(scratch_dir):
        shutil.rmtree(scratch_dir)
        if os.path.exists(scratch_dir):
            raise RuntimeError(f"scratch directory {scratch_dir} could not be removed")
        wiped.append("scratch_dir")
    if gpu_reset_fn is not None and gpu_reset_fn():
        wiped.append("gpu_memory")

    receipt = {
        "machine_id": machine_id,
        "job_id": job_id,
        "wiped_items": sorted(wiped),
        "wiped_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
    }
    receipt["signature"] = sign(receipt_signing_private_key, wipe_receipt_signing_bytes(receipt))
    return receipt


def execute_sealed_job(
    *,
    job_id: str,
    machine_id: str,
    ephemeral_private_key: str,
    receipt_signing_private_key: str,
    result_public_key: str,
    encrypted_input: dict[str, Any],
    wrapped_shares: list[dict[str, Any]],
    workload_runner_fn: Callable[[dict[str, Any]], dict[str, Any]],
    threshold: int = 2,
    scratch_dir: str | None = None,
    gpu_reset_fn: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one sealed job. Returns (encrypted_output, wipe_receipt).

    Key material is zeroed even if the workload fails; the failure is then re-raised.
    """
    share_buffers: list[bytearray] = []
    dk_buffer = bytearray()
    try:
        shares = []
        for s in wrapped_shares:
            buf = bytearray(open_sealed(ephemeral_private_key, s["wrapped_share"], release_aad(job_id, s["guardian"])))
            share_buffers.append(buf)
            shares.append((int(s["share_index"]), bytes(buf)))
        dk_buffer = bytearray(reconstruct_secret(shares, k=threshold))
        decrypted = json.loads(decrypt_data(bytes(dk_buffer), encrypted_input, INPUT_AAD).decode("utf-8"))
        result = workload_runner_fn(decrypted)
        encrypted_output = seal(result_public_key, json.dumps(result, sort_keys=True).encode("utf-8"), output_aad(job_id))
    finally:
        wipe_receipt = wipe_environment(
            machine_id=machine_id,
            job_id=job_id,
            receipt_signing_private_key=receipt_signing_private_key,
            memory_buffers=[dk_buffer, *share_buffers],
            scratch_dir=scratch_dir,
            gpu_reset_fn=gpu_reset_fn,
        )
    return encrypted_output, wipe_receipt
