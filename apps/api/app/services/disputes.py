"""Dispute evidence collection and adjudication (Phase B5).

WHY a service rather than inline router code
--------------------------------------------
Evidence capture has to happen at exactly one moment — the instant a dispute is
opened — and has to be reproducible for an auditor months later. Keeping it in a
service means the customer router, the admin router and any future automated
adjudicator all snapshot the *same* facts in the *same* shape.

WHY evidence stores hashes, not payloads
----------------------------------------
``dispute_evidence.payload`` deliberately holds digests and pointers (object
keys, artifact ids, sha256) rather than raw prompts or outputs. Two reasons:

1. **Privacy across parties.** A dispute has at least two sides — the customer
   who supplied the input and the provider who produced the output. Neither may
   read the other's private bytes just because a dispute exists. A sha256 proves
   "this is the same input you were given" without disclosing it.
2. **Immutability.** The evidence table is append-only; storing bytes there
   would duplicate (and eventually contradict) the artifact store.

Money note: resolution refunds are NOT re-implemented here. They go through
:func:`app.services.billing.refund`, which is the single ledger choke point and
enforces the cumulative refund cap.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.billing import Dispute, Refund
from app.models.jobs import Artifact, Job
from app.models.reputation import DisputeEvidence
from app.models.verification import RedundancyCheck, VerificationResult
from app.services import billing as billing_svc
from app.services import ledger
from app.services.audit import record_audit

logger = logging.getLogger("computecoin.disputes")

# Evidence kinds we auto-attach, in the order they are written (see
# DISPUTE_EVIDENCE_KINDS in app/models/reputation.py for the full vocabulary).
EVIDENCE_INPUT = "input"
EVIDENCE_OUTPUT = "output"
EVIDENCE_HASH = "hash"
EVIDENCE_CHALLENGE = "challenge"
EVIDENCE_REDUNDANCY = "redundancy"

# Payload keys that identify the *provider* side of the trade. They are legible
# to administrators but redacted from the customer view so a dispute cannot be
# used as an oracle for provider topology.
_PROVIDER_PRIVATE_KEYS = frozenset(
    {"machine_id", "shadow_job_id", "shadow_machine_id", "object_key", "provider_id"}
)
# Payload keys that describe the *customer's* private submission. Redacted from
# any non-customer view for symmetry.
_CUSTOMER_PRIVATE_KEYS = frozenset({"input_field_names"})


def canonical_sha256(value: Any) -> str:
    """Stable sha256 of a JSON-serialisable value.

    ``sort_keys`` + fixed separators make the digest reproducible across Python
    versions and dict ordering, which is the whole point of evidence: both
    parties must be able to recompute it.
    """
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _existing_kinds(db: AsyncSession, dispute_id: uuid.UUID) -> set[str]:
    rows = (
        await db.execute(
            select(DisputeEvidence.kind).where(DisputeEvidence.dispute_id == dispute_id)
        )
    ).scalars().all()
    return set(rows)


async def collect_evidence(
    db: AsyncSession,
    *,
    dispute: Dispute,
    job: Job,
    submitted_by_user_id: uuid.UUID | None = None,
) -> list[DisputeEvidence]:
    """Snapshot the job's evidence onto ``dispute``.

    Idempotent by construction: a kind that already exists for this dispute is
    never written twice, so re-opening/retrying cannot inflate the record or
    change a digest that an adjudicator already relied on.

    The caller is responsible for the surrounding transaction (we only flush).
    """
    already = await _existing_kinds(db, dispute.id)
    created: list[DisputeEvidence] = []

    def _add(kind: str, payload: dict, note: str | None = None) -> None:
        if kind in already:
            return
        row = DisputeEvidence(
            dispute_id=dispute.id,
            kind=kind,
            payload=payload,
            note=note,
            submitted_by_user_id=submitted_by_user_id,
        )
        db.add(row)
        created.append(row)

    # --- 1. the input the customer submitted (digest + shape only) ----------
    job_input = job.input if isinstance(job.input, dict) else {}
    _add(
        EVIDENCE_INPUT,
        {
            "job_id": str(job.id),
            "workload_type": job.workload_type,
            "model": job.model,
            "input_sha256": canonical_sha256(job_input),
            "input_field_names": sorted(job_input.keys()),
            "submitted_at": _iso(job.created_at),
        },
        note="Digest of the customer-supplied input; raw bytes are never copied here.",
    )

    # --- 2. the output the provider returned --------------------------------
    _add(
        EVIDENCE_OUTPUT,
        {
            "job_id": str(job.id),
            "status": job.status,
            # Honesty: a simulated run stays stamped as simulated in evidence.
            "execution_mode": job.execution_mode,
            "output_sha256": (
                canonical_sha256(job.output) if job.output is not None else None
            ),
            "machine_id": str(job.machine_id) if job.machine_id else None,
            "metrics": job.metrics if isinstance(job.metrics, dict) else None,
            "error_code": job.error_code,
            "finished_at": _iso(job.finished_at),
            "final_cost": str(job.final_cost) if job.final_cost is not None else None,
        },
        note="Digest of the provider-returned output plus the declared execution mode.",
    )

    # --- 3. stored artifact hashes -----------------------------------------
    artifacts = (
        await db.execute(
            select(Artifact).where(Artifact.job_id == job.id).order_by(Artifact.created_at)
        )
    ).scalars().all()
    _add(
        EVIDENCE_HASH,
        {
            "job_id": str(job.id),
            "artifact_count": len(artifacts),
            "artifacts": [
                {
                    "artifact_id": str(a.id),
                    "kind": a.kind,
                    "sha256": a.sha256,
                    "size_bytes": a.size_bytes,
                    "content_type": a.content_type,
                    "object_key": a.object_key,
                }
                for a in artifacts
            ],
        },
        note="Content hashes of every stored artifact at the time the dispute opened.",
    )

    # --- 4. verification results (known-answer challenges, model identity) --
    results = (
        await db.execute(
            select(VerificationResult)
            .where(VerificationResult.job_id == job.id)
            .order_by(VerificationResult.created_at)
        )
    ).scalars().all()
    if results:
        _add(
            EVIDENCE_CHALLENGE,
            {
                "job_id": str(job.id),
                "results": [
                    {
                        "verification_result_id": str(r.id),
                        "kind": r.kind,
                        "passed": r.passed,
                        "expected_hash": r.expected_hash,
                        "actual_hash": r.actual_hash,
                        "detail": r.detail,
                        "created_at": _iso(r.created_at),
                    }
                    for r in results
                ],
            },
            note="Verification attempts recorded against this job.",
        )

    # --- 5. redundant (shadow) execution checks -----------------------------
    checks = (
        await db.execute(
            select(RedundancyCheck)
            .where(RedundancyCheck.primary_job_id == job.id)
            .order_by(RedundancyCheck.created_at)
        )
    ).scalars().all()
    if checks:
        _add(
            EVIDENCE_REDUNDANCY,
            {
                "job_id": str(job.id),
                "checks": [
                    {
                        "redundancy_check_id": str(c.id),
                        "status": c.status,
                        "matched": c.matched,
                        "similarity": (
                            str(c.similarity) if c.similarity is not None else None
                        ),
                        "shadow_job_id": (
                            str(c.shadow_job_id) if c.shadow_job_id else None
                        ),
                        "detail": c.detail,
                        "resolved_at": _iso(c.resolved_at),
                    }
                    for c in checks
                ],
            },
            note="Shadow re-execution comparisons for this job.",
        )

    if created:
        await db.flush()
    return created


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _strip(value: Any, drop: frozenset[str]) -> Any:
    """Recursively remove ``drop`` keys from nested dicts/lists.

    Recursion is not optional: the artifact digests live in
    ``payload["artifacts"][i]["object_key"]`` and the shadow job id lives in
    ``payload["checks"][i]["shadow_job_id"]``, so a top-level-only filter would
    leak exactly the fields it claims to hide.
    """
    if isinstance(value, dict):
        return {k: _strip(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_strip(v, drop) for v in value]
    return value


def redact_payload(payload: dict, *, viewer: str) -> dict:
    """Return ``payload`` with the other party's private fields removed.

    ``viewer`` is ``"admin"`` (sees everything), ``"customer"`` or
    ``"provider"``. Adjudication requires an administrator to see both sides;
    neither trading party gets to mine a dispute for the other's data.
    """
    if viewer == "admin":
        return payload
    drop: frozenset[str]
    if viewer == "customer":
        drop = _PROVIDER_PRIVATE_KEYS
    else:
        drop = _CUSTOMER_PRIVATE_KEYS | frozenset({"input_sha256"})
    return _strip(payload, drop)


def shape_evidence(row: DisputeEvidence, *, viewer: str) -> dict:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return {
        "evidence_id": str(row.id),
        "kind": row.kind,
        "payload": redact_payload(payload, viewer=viewer),
        "note": row.note,
        "created_at": _iso(row.created_at),
    }


async def list_evidence(
    db: AsyncSession, dispute_id: uuid.UUID, *, viewer: str
) -> list[dict]:
    rows = (
        await db.execute(
            select(DisputeEvidence)
            .where(DisputeEvidence.dispute_id == dispute_id)
            .order_by(DisputeEvidence.created_at, DisputeEvidence.kind)
        )
    ).scalars().all()
    return [shape_evidence(r, viewer=viewer) for r in rows]


class DisputeResolutionError(ValueError):
    """Raised when a dispute cannot be adjudicated as requested."""


async def resolve_dispute(
    db: AsyncSession,
    *,
    dispute: Dispute,
    resolution: str,
    refund_amount_minor: int | None,
    actor_user_id: uuid.UUID,
    ip: str | None = None,
) -> dict:
    """Adjudicate an open dispute, optionally refunding part of the charge.

    The refund is delegated to :func:`app.services.billing.refund` so it passes
    through the balanced-ledger choke point and the cumulative refund cap; this
    function never touches an account balance itself. The caller commits.
    """
    # Lock the dispute row and RE-READ its status inside this transaction. The
    # caller's ``db.get`` is an unlocked read, so two concurrent adjudications
    # (two admins, a double-click, a retried request) would both observe "open"
    # and both refund. Postgres serializes here; on SQLite the single writer does.
    locked = (
        await db.execute(
            select(Dispute).where(Dispute.id == dispute.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        raise DisputeResolutionError("dispute_not_found")
    dispute = locked
    if dispute.status != "open":
        raise DisputeResolutionError("dispute_not_open")

    dispute.resolution = resolution
    dispute.status = "resolved"
    dispute.resolved_at = utcnow()
    dispute.actor_user_id = actor_user_id

    if refund_amount_minor is not None:
        job = await db.get(Job, dispute.job_id)
        if job is None:  # pragma: no cover - FK makes this unreachable
            raise DisputeResolutionError("job_not_found")
        # Serialize this customer's money movements before billing.refund reads
        # ``prior_refunded``, so the cumulative cap cannot be evaluated against a
        # stale view of a concurrent refund.
        await ledger.lock_customer(db, job.customer_id)
        refund_row = Refund(
            job_id=job.id,
            customer_id=job.customer_id,
            amount_minor=refund_amount_minor,
            reason=f"dispute_resolution:{resolution}"[:512],
            actor_user_id=actor_user_id,
        )
        db.add(refund_row)
        await db.flush()
        # Idempotency keyed on the DISPUTE, not on the freshly-inserted Refund
        # row: two concurrent adjudications of the same dispute would mint two
        # row ids and therefore two DIFFERENT keys, which the ledger's
        # idempotency check could not collapse. One dispute = at most one
        # refund transaction.
        txn = await billing_svc.refund(
            db,
            job=job,
            amount_minor=refund_amount_minor,
            actor_user_id=actor_user_id,
            reason=resolution,
            idempotency_key=f"dispute_refund:{dispute.id}",
        )
        refund_row.transaction_id = txn.id
        dispute.refund_amount_minor = refund_amount_minor

    await record_audit(
        db,
        action="admin.dispute.resolve",
        outcome="success",
        actor_user_id=actor_user_id,
        ip=ip,
        resource_type="dispute",
        resource_id=str(dispute.id),
        reason=resolution,
    )
    await db.flush()
    return {
        "dispute_id": str(dispute.id),
        "status": dispute.status,
        "resolution": dispute.resolution,
        "refund_amount": (
            ledger.minor_to_str(dispute.refund_amount_minor)
            if dispute.refund_amount_minor is not None
            else None
        ),
        "resolved_at": _iso(dispute.resolved_at),
    }
