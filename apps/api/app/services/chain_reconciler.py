"""Phase D2 — real on-chain <-> off-chain reconciliation.

WHY this exists: ``settlement.get_reconciliation_summary`` only compares our own
DB rows against our own DB rows. If the chain diverged from Postgres — a settle
transaction that reverted, an escrow refunded directly on the contract, an
escrow that exists on-chain but was never recorded off-chain — that comparison
reports parity because every row it reads agrees with every other row it reads.
It is self-referential, and a self-referential check is not evidence.

This module reads ACTUAL chain state over JSON-RPC and compares it to both the
``on_chain_escrows`` rows and the off-chain double-entry ledger. Every sweep is
persisted as a :class:`ChainReconciliationRun` so passes AND failures are on the
record (HONESTY rule).

The chain client is INJECTABLE: production uses :class:`JsonRpcChainClient`
against ``settings.CHAIN_RPC_URL``; tests inject a fake and run fully offline.
A client that raises NEVER yields ``ok`` — the finding is classified
``error`` and the run is marked not-ok. "I could not check" is not "it matched".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from eth_utils import keccak
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.billing import LedgerTransaction
from app.models.reconciliation import ChainReconciliationRun
from app.models.settlement import (
    EscrowStatus,
    OnChainEscrow,
    OnChainTransaction,
    OnChainTxType,
)

__all__ = [
    "ChainClientError",
    "ChainEscrowState",
    "ChainClient",
    "JsonRpcChainClient",
    "ChainReconciler",
    "FINDING_OK",
    "FINDING_DRIFT",
    "FINDING_MISSING_ONCHAIN",
    "FINDING_MISSING_OFFCHAIN",
    "FINDING_ERROR",
]

# Finding classifications. Anything that is not FINDING_OK counts as drift.
FINDING_OK = "ok"
FINDING_DRIFT = "drift"
FINDING_MISSING_ONCHAIN = "missing_onchain"
FINDING_MISSING_OFFCHAIN = "missing_offchain"
FINDING_ERROR = "error"

# Cap on how many recent runs we scan when honouring an Idempotency-Key. The
# key is only meaningful for a short retry window, so a bounded scan is enough.
_IDEMPOTENCY_LOOKBACK = 100


class ChainClientError(RuntimeError):
    """The chain could not be read. Never swallowed into a passing result."""


@dataclass(frozen=True)
class ChainEscrowState:
    """Escrow state as the CONTRACT reports it, in raw token units.

    ``status`` uses the same vocabulary as :class:`EscrowStatus` so the two
    sides can be compared without a translation table.
    """

    job_bytes32: str
    status: str
    locked_amount_units: int = 0
    settled_amount_units: int = 0
    refunded_amount_units: int = 0


@runtime_checkable
class ChainClient(Protocol):
    """Minimal read-only view of the settlement contract.

    Implementations must raise :class:`ChainClientError` (not return a default)
    when the chain cannot be read, so an RPC outage can never be mistaken for
    agreement.
    """

    async def fetch_escrow(
        self, *, contract_address: str, job_bytes32: str, customer_address: str | None = None
    ) -> ChainEscrowState | None:
        """Return on-chain state for one escrow, or ``None`` if it does not exist.

        ``customer_address`` is REQUIRED by the real contract: ``escrows`` is keyed
        by ``keccak256(abi.encodePacked(customer, jobId))``, not by ``jobId``.
        """
        ...

    async def list_escrow_job_ids(self, *, contract_address: str) -> list[str]:
        """Return every ``bytes32`` job id the contract has emitted events for."""
        ...


def _norm_b32(value: str) -> str:
    """Normalize a bytes32 hex string for comparison (lowercase, 0x-prefixed)."""
    v = (value or "").strip().lower()
    return v if v.startswith("0x") else "0x" + v


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def _topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


class JsonRpcChainClient:
    """Reads the settlement contract over plain JSON-RPC.

    Uses ``eth_call`` for current escrow state and ``eth_getLogs`` for the
    ``JobSettled`` / ``JobRefunded`` history. Kept dependency-free on purpose:
    two RPC methods are easier to audit than a web3 provider stack.

    NOTE on SSRF: ``CHAIN_RPC_URL`` is operator configuration, not user input,
    and a local dev chain legitimately lives on 127.0.0.1, so private ranges are
    NOT blocked here. Only the scheme is constrained, to stop a misconfigured
    ``file://``/``unix://`` value from being handed to the HTTP client.
    """

    #: ``mapping(bytes32 => EscrowLock) public escrows`` — the generated getter is
    #: keyed by ``escrowId = keccak256(abi.encodePacked(customer, jobId))`` (see
    #: ComputeSettlement.getEscrowId) and returns the SIX struct words
    #: ``(address customer, address provider, uint256 lockedAmount,
    #:   uint256 deadline, uint256 nonce, bool settled)``.
    ESCROW_SELECTOR = _selector("escrows(bytes32)")
    #: Five parameters — jobId, provider, providerPayout, platformFee, customerRefund.
    SETTLED_TOPIC = _topic("JobSettled(bytes32,address,uint256,uint256,uint256)")
    REFUNDED_TOPIC = _topic("JobRefunded(bytes32,address,uint256)")
    #: ``BudgetLocked(bytes32 indexed jobId, address indexed customer,
    #: address indexed provider, uint256 amount, uint256 deadline)``.
    #: Scanned by :meth:`list_escrow_job_ids` so that an escrow which is still
    #: OPEN on-chain but was never recorded off-chain is reported. Verified
    #: against a real deployment in tests/test_d2_chain_recon_live.py.
    LOCKED_TOPIC = _topic("BudgetLocked(bytes32,address,address,uint256,uint256)")

    def __init__(
        self,
        rpc_url: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        from_block: str = "0x0",
    ) -> None:
        url = (rpc_url if rpc_url is not None else settings.CHAIN_RPC_URL or "").strip()
        if not url:
            raise ChainClientError("chain_rpc_url_not_configured")
        if urlparse(url).scheme not in ("http", "https"):
            raise ChainClientError("chain_rpc_url_scheme_not_allowed")
        self._url = url
        self._timeout = timeout_seconds
        self._from_block = from_block
        self._rpc_id = 0
        self._closure_cache: dict[str, dict[str, dict[str, Any]]] = {}

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=payload)
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:  # noqa: BLE001 — surfaced as an explicit failure
            raise ChainClientError(f"rpc_transport_error:{method}") from exc
        if isinstance(body, dict) and body.get("error"):
            raise ChainClientError(f"rpc_error:{method}:{body['error']}")
        if not isinstance(body, dict) or "result" not in body:
            raise ChainClientError(f"rpc_malformed_response:{method}")
        return body["result"]

    @staticmethod
    def _words(hex_result: str) -> list[int]:
        raw = (hex_result or "0x")[2:]
        return [int(raw[i : i + 64] or "0", 16) for i in range(0, len(raw) - 63, 64)]

    @staticmethod
    def escrow_id(customer_address: str, job_bytes32: str) -> str:
        """``keccak256(abi.encodePacked(customer, jobId))`` — the real mapping key.

        ``abi.encodePacked`` of an ``address`` is its 20 raw bytes (NOT left-padded
        to a word), followed by the 32 job bytes.
        """
        addr = (customer_address or "").strip().lower()
        if addr.startswith("0x"):
            addr = addr[2:]
        if len(addr) != 40:
            raise ChainClientError("escrow_key_customer_address_invalid")
        job_hex = _norm_b32(job_bytes32)[2:].rjust(64, "0")
        return "0x" + keccak(bytes.fromhex(addr + job_hex)).hex()

    async def _closures(self, contract_address: str) -> dict[str, dict[str, Any]]:
        """Per-job settle/refund outcome read from the event log, memoized.

        The contract does NOT store settled/refunded amounts — ``EscrowLock`` only
        carries ``settled: bool`` — so the amounts (and whether a closed escrow was
        settled or refunded) can only come from ``JobSettled`` / ``JobRefunded``.
        """
        cached = self._closure_cache.get(contract_address)
        if cached is not None:
            return cached

        out: dict[str, dict[str, Any]] = {}
        for topic in (self.SETTLED_TOPIC, self.REFUNDED_TOPIC):
            for entry in await self._logs(contract_address, topic):
                topics = (entry or {}).get("topics") or []
                if len(topics) < 2:
                    continue
                job_b32 = _norm_b32(topics[1])
                data_words = self._words((entry or {}).get("data") or "0x")
                if topic == self.SETTLED_TOPIC:
                    # data = (providerPayout, platformFee, customerRefund)
                    payout = data_words[0] if len(data_words) > 0 else 0
                    fee = data_words[1] if len(data_words) > 1 else 0
                    out[job_b32] = {
                        "status": EscrowStatus.SETTLED.value,
                        "settled": payout + fee,
                        "refunded": 0,
                    }
                else:
                    # data = (refundAmount,)
                    out[job_b32] = {
                        "status": EscrowStatus.REFUNDED.value,
                        "settled": 0,
                        "refunded": data_words[0] if data_words else 0,
                    }
        self._closure_cache[contract_address] = out
        return out

    async def _logs(self, contract_address: str, topic: str) -> list[dict[str, Any]]:
        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "address": contract_address,
                    "fromBlock": self._from_block,
                    "toBlock": "latest",
                    "topics": [topic],
                }
            ],
        )
        if not isinstance(logs, list):
            raise ChainClientError("getlogs_malformed_response")
        return logs

    async def fetch_escrow(
        self, *, contract_address: str, job_bytes32: str, customer_address: str | None = None
    ) -> ChainEscrowState | None:
        if not customer_address:
            raise ChainClientError("escrow_key_customer_address_missing")
        key = self.escrow_id(customer_address, job_bytes32)
        data = self.ESCROW_SELECTOR + key[2:]
        result = await self._rpc(
            "eth_call", [{"to": contract_address, "data": data}, "latest"]
        )
        words = self._words(result)
        if len(words) < 6:
            raise ChainClientError("escrow_decode_failed")
        customer, _provider, locked, _deadline, _nonce, settled_flag = words[:6]
        if customer == 0:
            # Unoccupied mapping slot: there is no such escrow on-chain.
            return None

        if not settled_flag:
            status, settled_units, refunded_units = EscrowStatus.LOCKED.value, 0, 0
        else:
            closure = (await self._closures(contract_address)).get(
                _norm_b32(job_bytes32)
            )
            if closure is None:
                # Closed on-chain but no matching event: honest "unknown", which
                # can never equal an EscrowStatus and therefore reports drift.
                status, settled_units, refunded_units = "unknown", 0, 0
            else:
                status = closure["status"]
                settled_units = closure["settled"]
                refunded_units = closure["refunded"]

        return ChainEscrowState(
            job_bytes32=_norm_b32(job_bytes32),
            status=status,
            locked_amount_units=locked,
            settled_amount_units=settled_units,
            refunded_amount_units=refunded_units,
        )

    async def list_escrow_job_ids(self, *, contract_address: str) -> list[str]:
        """Every bytes32 job id the contract has emitted an escrow event for.

        ``BudgetLocked`` is scanned as well as the two closure events. Scanning
        only ``JobSettled``/``JobRefunded`` meant an escrow that was locked
        on-chain but never written to ``on_chain_escrows`` stayed invisible to
        the sweep — customer tokens genuinely held by the contract with no
        off-chain record, reported as parity. That gap was found by pointing
        this client at a real deployed contract (tests/test_d2_chain_recon_live.py).
        """
        seen: list[str] = []
        for topic in (self.LOCKED_TOPIC, self.SETTLED_TOPIC, self.REFUNDED_TOPIC):
            for entry in await self._logs(contract_address, topic):
                topics = (entry or {}).get("topics") or []
                if len(topics) >= 2:
                    job_b32 = _norm_b32(topics[1])
                    if job_b32 not in seen:
                        seen.append(job_b32)
        return seen


@dataclass
class _Finding:
    """One escrow's verdict, serialized verbatim into the run's ``detail``."""

    classification: str
    job_bytes32: str
    escrow_id: str | None = None
    job_id: str | None = None
    contract_address: str | None = None
    db_status: str | None = None
    chain_status: str | None = None
    reasons: list[str] = field(default_factory=list)
    expected: dict[str, str] = field(default_factory=dict)
    actual: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "job_bytes32": self.job_bytes32,
            "escrow_id": self.escrow_id,
            "job_id": self.job_id,
            "contract_address": self.contract_address,
            "db_status": self.db_status,
            "chain_status": self.chain_status,
            "reasons": self.reasons,
            "expected": self.expected,
            "actual": self.actual,
        }


class ChainReconciler:
    """Compares on-chain escrow state with our DB rows and the off-chain ledger."""

    # Serializes runs in-process: two concurrent sweeps would write two rows
    # describing the same instant and double-count drift.
    _run_lock = asyncio.Lock()

    def __init__(self, client: ChainClient, *, chain_id: int | None = None) -> None:
        self._client = client
        self._chain_id = (
            chain_id if chain_id is not None else settings.SETTLEMENT_CHAIN_ID
        )

    async def run(
        self,
        db: AsyncSession,
        *,
        idempotency_key: str | None = None,
    ) -> ChainReconciliationRun:
        """Execute one sweep and persist exactly one :class:`ChainReconciliationRun`.

        ``idempotency_key`` makes an externally-triggered retry a no-op: the run
        already recorded under that key is returned unchanged rather than a
        second sweep being charged to the RPC endpoint.
        """
        async with self._run_lock:
            if idempotency_key:
                existing = await self._find_run_by_key(db, idempotency_key)
                if existing is not None:
                    return existing

            run = ChainReconciliationRun(
                chain_id=self._chain_id,
                started_at=utcnow(),
                escrows_checked=0,
                drift_count=0,
                ok=False,  # stays False unless a REAL comparison proves otherwise
                detail=None,
            )
            db.add(run)
            await db.flush()

            findings, errors = await self._collect_findings(db)

            drift = [f for f in findings if f.classification != FINDING_OK]
            run.escrows_checked = len(findings)
            run.drift_count = len(drift)
            run.finished_at = utcnow()
            # ok is derived ONLY from the comparison: no drift, no unreadable
            # escrow, and at least the sweep itself completed.
            run.ok = not drift and not errors
            run.detail = {
                "idempotency_key": idempotency_key,
                "chain_id": self._chain_id,
                "error_count": len(errors),
                "errors": errors,
                "counts": _count_by_classification(findings),
                "findings": [f.as_dict() for f in findings],
            }
            await db.flush()
            return run

    async def _find_run_by_key(
        self, db: AsyncSession, key: str
    ) -> ChainReconciliationRun | None:
        """Look the key up over the recent runs.

        Stored inside ``detail`` rather than a dedicated column because the
        schema is fixed (D2 owns no migrations); the lookback is bounded so this
        stays O(1)-ish regardless of history size.
        """
        rows = (
            await db.scalars(
                select(ChainReconciliationRun)
                .where(ChainReconciliationRun.chain_id == self._chain_id)
                .order_by(desc(ChainReconciliationRun.started_at))
                .limit(_IDEMPOTENCY_LOOKBACK)
            )
        ).all()
        for row in rows:
            if isinstance(row.detail, dict) and row.detail.get("idempotency_key") == key:
                return row
        return None

    async def _collect_findings(
        self, db: AsyncSession
    ) -> tuple[list[_Finding], list[str]]:
        escrows = (
            await db.scalars(
                select(OnChainEscrow).where(OnChainEscrow.chain_id == self._chain_id)
            )
        ).all()

        findings: list[_Finding] = []
        errors: list[str] = []
        seen_by_contract: dict[str, set[str]] = {}
        # Always sweep the CONFIGURED contract, even when no DB row points at it.
        # Deriving the contract list purely from on_chain_escrows meant that an
        # empty (or chain_id-mismatched) table produced zero RPC calls, zero
        # findings and ok=true — a green dashboard for a chain that was never
        # queried, which is precisely the failure this module exists to prevent.
        configured = (settings.SETTLEMENT_CONTRACT_ADDRESS or "").strip()
        if not escrows and configured:
            # With rows present, every one of their contracts is swept below and
            # a real read always happens. With NO rows (fresh replica, or a
            # SETTLEMENT_CHAIN_ID that does not match the stored escrows) the
            # sweep would otherwise make zero RPC calls and still report ok=true
            # — a green dashboard for a chain nobody queried. Sweep the
            # configured contract so the run is backed by an actual chain read.
            seen_by_contract.setdefault(configured, set())
        chain_reads = 0

        for escrow in escrows:
            contract = escrow.contract_address
            seen_by_contract.setdefault(contract, set()).add(
                _norm_b32(escrow.job_bytes32)
            )
            try:
                state = await self._client.fetch_escrow(
                    contract_address=contract,
                    job_bytes32=escrow.job_bytes32,
                    customer_address=escrow.customer_address,
                )
            except Exception as exc:  # noqa: BLE001 — unreadable != matching
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                findings.append(
                    _Finding(
                        classification=FINDING_ERROR,
                        job_bytes32=_norm_b32(escrow.job_bytes32),
                        escrow_id=str(escrow.id),
                        job_id=str(escrow.job_id),
                        contract_address=contract,
                        db_status=escrow.status,
                        chain_status=None,
                        reasons=[f"chain_read_failed:{message}"],
                    )
                )
                continue

            chain_reads += 1
            if state is None:
                findings.append(
                    _Finding(
                        classification=FINDING_MISSING_ONCHAIN,
                        job_bytes32=_norm_b32(escrow.job_bytes32),
                        escrow_id=str(escrow.id),
                        job_id=str(escrow.job_id),
                        contract_address=contract,
                        db_status=escrow.status,
                        chain_status=None,
                        reasons=["escrow_absent_on_chain"],
                    )
                )
                continue

            findings.append(await self._compare(db, escrow, state))

        # Escrows the contract knows about that we never recorded off-chain.
        for contract, known in seen_by_contract.items():
            try:
                onchain_ids = await self._client.list_escrow_job_ids(
                    contract_address=contract
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"list_escrow_job_ids({contract}): {exc}")
                continue
            chain_reads += 1
            for job_b32 in _unique(_norm_b32(j) for j in onchain_ids):
                if job_b32 in known:
                    continue
                findings.append(
                    _Finding(
                        classification=FINDING_MISSING_OFFCHAIN,
                        job_bytes32=job_b32,
                        contract_address=contract,
                        reasons=["escrow_present_on_chain_but_absent_in_db"],
                    )
                )

        if chain_reads == 0:
            # "Nothing to check" is not "everything matched".
            errors.append("no_chain_read_performed")

        return findings, errors

    async def _compare(
        self, db: AsyncSession, escrow: OnChainEscrow, state: ChainEscrowState
    ) -> _Finding:
        """Compare one escrow row against chain state and the off-chain ledger."""
        finding = _Finding(
            classification=FINDING_OK,
            job_bytes32=_norm_b32(escrow.job_bytes32),
            escrow_id=str(escrow.id),
            job_id=str(escrow.job_id),
            contract_address=escrow.contract_address,
            db_status=escrow.status,
            chain_status=state.status,
        )

        if escrow.status != state.status:
            finding.reasons.append("status_mismatch")
            finding.expected["status"] = escrow.status
            finding.actual["status"] = state.status

        db_locked = _to_int(escrow.locked_amount_units)
        if db_locked != state.locked_amount_units:
            finding.reasons.append("locked_amount_mismatch")
            finding.expected["locked_amount_units"] = str(db_locked)
            finding.actual["locked_amount_units"] = str(state.locked_amount_units)

        if escrow.status == EscrowStatus.SETTLED.value:
            db_settled = _to_int(escrow.final_cost_units)
            if db_settled != state.settled_amount_units:
                finding.reasons.append("settled_amount_mismatch")
                finding.expected["settled_amount_units"] = str(db_settled)
                finding.actual["settled_amount_units"] = str(state.settled_amount_units)
            if not await self._has_posted_ledger_txn(db, escrow):
                finding.reasons.append("ledger_txn_missing_or_unposted")

        if escrow.status == EscrowStatus.REFUNDED.value:
            db_refunded = _to_int(escrow.locked_amount_units)
            if db_refunded != state.refunded_amount_units:
                finding.reasons.append("refunded_amount_mismatch")
                finding.expected["refunded_amount_units"] = str(db_refunded)
                finding.actual["refunded_amount_units"] = str(
                    state.refunded_amount_units
                )

        if finding.reasons:
            finding.classification = FINDING_DRIFT
        return finding

    async def _has_posted_ledger_txn(
        self, db: AsyncSession, escrow: OnChainEscrow
    ) -> bool:
        """A settled escrow must have a posted off-chain capture behind it.

        Preference order: the ledger txn explicitly linked to the settle
        OnChainTransaction, then any posted capture referencing the job.
        """
        settle_tx = await db.scalar(
            select(OnChainTransaction).where(
                OnChainTransaction.job_id == escrow.job_id,
                OnChainTransaction.tx_type == OnChainTxType.SETTLE.value,
            )
        )
        if settle_tx is not None and settle_tx.ledger_txn_id is not None:
            linked = await db.get(LedgerTransaction, settle_tx.ledger_txn_id)
            if linked is not None and linked.status == "posted":
                return True
        fallback = await db.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.reference_type == "job",
                LedgerTransaction.reference_id == str(escrow.job_id),
                LedgerTransaction.kind == "capture",
                LedgerTransaction.status == "posted",
            )
        )
        return fallback is not None


def _to_int(value: str | int | None) -> int:
    """Token amounts are stored as decimal strings (uint256 overflows BIGINT)."""
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _count_by_classification(findings: list[_Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.classification] = counts.get(f.classification, 0) + 1
    return counts


def build_default_client() -> ChainClient:
    """Production client factory. Raises if the RPC endpoint is unconfigured."""
    return JsonRpcChainClient()


def run_to_dict(run: ChainReconciliationRun, *, include_detail: bool = False) -> dict[str, Any]:
    """Serialize a run for the API (summary by default, full detail on request)."""
    payload: dict[str, Any] = {
        "id": str(run.id),
        "chain_id": run.chain_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "escrows_checked": run.escrows_checked,
        "drift_count": run.drift_count,
        "ok": run.ok,
        "error_count": (run.detail or {}).get("error_count", 0)
        if isinstance(run.detail, dict)
        else 0,
        "counts": (run.detail or {}).get("counts", {})
        if isinstance(run.detail, dict)
        else {},
    }
    if include_detail:
        payload["detail"] = run.detail
    return payload

