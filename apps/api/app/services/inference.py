"""Inference orchestration service (Phase 6).

Provides OpenAI-compatible ``/v1/chat/completions`` and ``/v1/embeddings``
by routing requests to eligible provider machines.

EXECUTION MODES
---------------
* **simulated** — deterministic in-process transform.  The response is
  NEVER real model inference; the ``computecoin.execution_mode`` field is
  always ``"simulated"``.  Used when the model version's ``runtime`` is
  ``"simulated"`` (Phase 4 SimulatedExecutor contract).
* **hardware** — routed to a real GPU provider (future — requires Phase 4
  hardened containers with an actual inference server).

BILLING
-------
Each inference request creates a micro-hold on the customer's credits,
captures on success, and releases on failure.  The hold amount is derived
from the matched machine's asking price and an estimated token-time cost.

ROUTING
-------
Reuses the Phase 4 scheduler's machine-eligibility logic (workload
compatibility + online + available + GPU fit) and the Phase 3 marketplace
search to find the best provider.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import math
import re
import time
import uuid
from typing import Any, AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.model_registry import ModelVersion
from app.models.provider import ProviderMachine
from app.schemas.inference import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChunkChoice,
    ChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    CompletionUsage,
    ComputeCoinMeta,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
)
from app.services.gpu_runtime import (
    image_for_runtime,
    is_gpu_compatible,
    machine_gpu_runtime,
    model_image_runtimes,
)
from app.schemas.jobs import PRIVACY_TIERS
from app.services import billing
from app.services import job_fsm as fsm
from app.services import stream_relay
from app.services.ledger import InsufficientFunds, lock_customer, to_minor
from app.services.leases import release_job_reservation
from app.services.provider import derive_status
from app.services.scheduler import JobValidationError, schedule_job, validate_job
from app.services.spend_limits import SpendLimitExceeded, spend_guard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model registry lookup (reuse scheduler pattern)
# ---------------------------------------------------------------------------
async def _resolve_model(
    db: AsyncSession, model_name: str, required_workload: str
) -> ModelVersion | None:
    """Find an approved model version that supports the requested workload."""
    result = await db.execute(
        select(ModelVersion).where(ModelVersion.version_tag == model_name)
    )
    mv = result.scalar_one_or_none()
    if mv is None:
        return None
    if mv.trust_status != "approved":
        return None
    if required_workload not in (mv.supported_workloads or []):
        return None
    return mv


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------
def _machine_query():
    return select(ProviderMachine).options(
        selectinload(ProviderMachine.hardware),
        selectinload(ProviderMachine.capability),
    )


async def _find_eligible_machine(
    db: AsyncSession,
    workload_type: str,
    model_version: ModelVersion | None = None,
    now: dt.datetime | None = None,
) -> tuple[ProviderMachine | None, str | None]:
    """Find the best eligible online machine for the given workload type and model.

    Uses the same compatibility helper (is_gpu_compatible) so 'any' runtime
    goes to the cheapest eligible machine of either vendor, and the job carries
    the image for the chosen machine's runtime.
    Returns (best_machine, chosen_image_digest).
    """
    now = now or utcnow()
    result = await db.execute(
        _machine_query().where(
            ProviderMachine.status != "deregistered",
            ProviderMachine.availability == "online",
            ProviderMachine.asking_price_per_hour.is_not(None),
        )
    )
    best: ProviderMachine | None = None
    best_price = None

    # A hardware model may only go to machines it has an image for, and a hardware model with no
    # image at all goes to no GPU machine (an empty set filters them all, it is not "no filter").
    # A simulated model runs no image, so it is not filtered.
    image_runtimes: set[str] | None = None
    if model_version is not None and model_version.runtime != "simulated":
        image_runtimes = await model_image_runtimes(db, model_version)

    for m in result.scalars().all():
        if getattr(m, "is_banned", False):
            logger.debug("inference skip (banned): machine=%s", m.id)
            continue
        if derive_status(m, now) != "online":
            logger.debug("inference skip (offline): machine=%s avail=%s", m.id, m.availability)
            continue
        workloads = m.capability.supported_workloads if m.capability else []
        if workload_type not in (workloads or []):
            logger.debug("inference skip (workload): machine=%s", m.id)
            continue

        gpu_ok, gpu_reason = is_gpu_compatible(
            machine=m,
            image_runtimes=image_runtimes,
            job_gpu_runtime="any",
            job_gpu_count=1 if (m.hardware and m.hardware.gpu_count > 0) else 0,
            isolation_tier="container",
        )
        if not gpu_ok:
            logger.debug("inference skip (%s): machine=%s", gpu_reason, m.id)
            continue

        price = m.asking_price_per_hour
        if best_price is None or price < best_price:
            best = m
            best_price = price

    image_digest: str | None = None
    if best is not None and model_version is not None:
        image_digest = await image_for_runtime(db, model_version, machine_gpu_runtime(best))

    return best, image_digest


# ---------------------------------------------------------------------------
# Simulated response generators
# ---------------------------------------------------------------------------
_SIM_PREFIX = "[ComputeCoin Simulated] "

# Embedding dimension for the deterministic hash-based embedding.
EMBEDDING_DIM = 256


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (GPT-style heuristic)."""
    return max(1, len(text) // 4)


def _simulated_chat_response(
    request: ChatCompletionRequest,
    model_version: ModelVersion,
    machine: ProviderMachine | None,
    image_digest: str | None = None,
) -> ChatCompletionResponse:
    """Produce a deterministic simulated chat completion.

    The response echoes/transforms the last user message with a prefix so it
    is unmistakably NOT real model output (Phase 4 SimulatedExecutor contract).
    """
    # Extract the last user message for the echo.
    last_user_msg = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            last_user_msg = msg.content
            break

    # Deterministic simulated response.
    simulated_content = (
        f"{_SIM_PREFIX}This is a deterministic simulated response. "
        f"Your input was: \"{last_user_msg[:200]}\". "
        f"Model: {request.model}. "
        f"This is NOT real model inference."
    )

    max_tokens = request.max_tokens or settings.INFERENCE_MAX_TOKENS_DEFAULT

    # Token estimation.
    prompt_tokens = sum(_estimate_tokens(m.content) for m in request.messages)
    completion_tokens = min(_estimate_tokens(simulated_content), max_tokens)

    choices = [
        ChatCompletionChoice(
            index=i,
            message=ChatMessage(role="assistant", content=simulated_content),
            finish_reason="stop",
        )
        for i in range(request.n)
    ]

    return ChatCompletionResponse(
        created=int(time.time()),
        model=request.model,
        choices=choices,
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens * request.n,
            total_tokens=prompt_tokens + completion_tokens * request.n,
        ),
        computecoin=ComputeCoinMeta(
            execution_mode="simulated",
            machine_id=str(machine.id) if machine else None,
            provider_id=str(machine.provider_id) if machine else None,
            model_version=model_version.version_tag,
            image_digest=image_digest,
        ),
    )


def _deterministic_embedding(text: str, dim: int | None = None) -> list[float]:
    """Generate a deterministic, reproducible embedding vector from a SHA-256 hash.

    This is NOT a real embedding model.  It produces a fixed-dimension vector
    that is consistent for the same input.  The output is L2-normalized.
    """
    dim = dim or settings.INFERENCE_EMBEDDING_DIM
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand the 32-byte hash to fill the required dimensions.
    expanded = b""
    for i in range(math.ceil(dim * 4 / 32)):
        expanded += hashlib.sha256(h + i.to_bytes(4, "big")).digest()
    # Convert to floats in [-1, 1].
    raw: list[float] = []
    for j in range(dim):
        byte_val = expanded[j % len(expanded)]
        raw.append((byte_val / 127.5) - 1.0)
    # L2 normalize.
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [round(x / norm, 8) for x in raw]


def _simulated_embedding_response(
    request: EmbeddingRequest,
    model_version: ModelVersion,
    machine: ProviderMachine | None,
    image_digest: str | None = None,
) -> EmbeddingResponse:
    """Produce deterministic simulated embeddings.

    Hash-based, reproducible, and explicitly stamped as simulated.
    """
    inputs: list[str] = (
        [request.input] if isinstance(request.input, str) else request.input
    )

    data: list[EmbeddingData] = []
    total_tokens = 0
    for idx, text in enumerate(inputs):
        embedding = _deterministic_embedding(text)
        data.append(EmbeddingData(index=idx, embedding=embedding))
        total_tokens += _estimate_tokens(text)

    return EmbeddingResponse(
        model=request.model,
        data=data,
        usage=EmbeddingUsage(prompt_tokens=total_tokens, total_tokens=total_tokens),
        computecoin=ComputeCoinMeta(
            execution_mode="simulated",
            machine_id=str(machine.id) if machine else None,
            provider_id=str(machine.provider_id) if machine else None,
            model_version=model_version.version_tag,
            image_digest=image_digest,
        ),
    )


# ---------------------------------------------------------------------------
# Public orchestration API
# ---------------------------------------------------------------------------
class InferenceError(Exception):
    """Raised when inference cannot proceed."""

    def __init__(self, message: str, code: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Real (hardware) path: a real job, placed by the scheduler and billed as a job (P3)
# ---------------------------------------------------------------------------
def _render_prompt(request: ChatCompletionRequest) -> str:
    """Plain-text rendering used for the job's required ``prompt`` field (the image applies the
    model's own chat template to ``messages``)."""
    return "\n".join(f"{m.role}: {m.content}" for m in request.messages)


async def _create_inference_job(
    db: AsyncSession,
    customer_id: uuid.UUID,
    *,
    workload_type: str,
    model: str,
    job_input: dict,
    private: dict | None = None,
) -> uuid.UUID:
    """Create, validate, schedule (hold + reserve) a job for an inference request; commit it."""
    await lock_customer(db, customer_id)
    job = Job(
        customer_id=customer_id,
        workload_type=workload_type,
        model=model,
        input=job_input,
        status=fsm.CREATED,
        gpu_count=1,
        estimated_minutes=1,
        max_runtime_seconds=settings.INFERENCE_TIMEOUT_SECONDS,
        retry_policy={"max_retries": 0},
        priority=0,
        execution_mode="hardware",
        isolation_tier=private["tier"] if private else "container",
        required_gpu_runtime="any",
        # U18: a private chat job carries sealed input and the result key; the platform holds no plaintext.
        encrypted_input=private["encrypted_input"] if private else None,
        dk_shares=private["dk_shares"] if private else None,
        data_policy=({"result_public_key": private["result_public_key"], "stream_sealed": True} if private else None),
    )
    db.add(job)
    await db.flush()
    await fsm.transition(db, job, fsm.VALIDATING, reason="inference_request", actor="customer")
    try:
        await validate_job(db, job)
    except JobValidationError as exc:
        await fsm.transition(db, job, fsm.FAILED, reason=exc.code, actor="system")
        job.error_code = exc.code
        await db.commit()
        raise InferenceError(f"Job validation failed: {exc.code}", exc.code, exc.http_status)
    await fsm.transition(db, job, fsm.QUEUED, reason="validated", actor="system")
    try:
        async with spend_guard(db, customer_id, None, 0):
            await schedule_job(db, job)
            await db.commit()
    except SpendLimitExceeded:
        await fsm.transition(db, job, fsm.FAILED, reason="spend_limit_exceeded", actor="system")
        job.error_code = "spend_limit_exceeded"
        await db.commit()
        raise InferenceError("Spend limit exceeded.", "spend_limit_exceeded", 403)
    except InsufficientFunds:
        await fsm.transition(db, job, fsm.FAILED, reason="insufficient_credits", actor="system")
        job.error_code = "insufficient_credits"
        await db.commit()
        raise InferenceError("Insufficient credits.", "insufficient_credits", 402)
    if job.status == fsm.QUEUED:
        # No eligible provider right now: do not leave a queued job behind a failed request.
        job_id = job.id
        await cancel_inference_job(job_id, reason="no_eligible_provider")
        raise InferenceError("No eligible provider is available.", "no_eligible_provider", 503)
    return job.id


async def cancel_inference_job(job_id: uuid.UUID, *, reason: str) -> None:
    """Cancel a non-terminal inference job and release its reservation and its whole hold.

    Own session and commit, so it also works from a disconnect handler. Idempotent; a job that
    already finished (and was billed) is left alone.
    """
    sm = get_sessionmaker()
    async with sm() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
        if job is None or job.status in fsm.TERMINAL_STATES:
            return
        job.cancel_requested = True
        if job.status in fsm.CANCELLABLE_STATES:
            await release_job_reservation(s, job, utcnow())
            await fsm.transition(s, job, fsm.CANCELLED, reason=reason, actor="system")
            await billing.release_hold(s, job)
        await s.commit()


async def _load_job(job_id: uuid.UUID) -> Job | None:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()


async def _wait_for_job(job_id: uuid.UUID) -> Job:
    """Poll until the job is terminal. On timeout: cancel it, release the hold, 504."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + settings.INFERENCE_TIMEOUT_SECONDS
    while True:
        job = await _load_job(job_id)
        if job is not None and job.status in fsm.TERMINAL_STATES:
            return job
        if loop.time() >= deadline:
            await cancel_inference_job(job_id, reason="inference_timeout")
            raise InferenceError("Inference timed out.", "inference_timeout", 504)
        await asyncio.sleep(settings.INFERENCE_POLL_INTERVAL_SECONDS)


def _require_completed(job: Job) -> dict:
    if job.status != fsm.COMPLETED or not isinstance(job.output, dict):
        raise InferenceError(
            f"The provider did not complete the job ({job.status}).",
            job.error_code or "inference_job_failed",
            502,
        )
    return job.output


async def _hardware_meta(job: Job, mv: ModelVersion) -> ComputeCoinMeta:
    machine_id = job.machine_id
    provider_id = None
    digest = None
    if machine_id is not None:
        async with get_sessionmaker()() as s:
            m = (await s.execute(_machine_query().where(ProviderMachine.id == machine_id))).scalar_one_or_none()
            if m is not None:
                provider_id = str(m.provider_id)
                digest = await image_for_runtime(s, mv, machine_gpu_runtime(m))
    return ComputeCoinMeta(
        execution_mode="hardware",
        machine_id=str(machine_id) if machine_id else None,
        provider_id=provider_id,
        model_version=mv.version_tag,
        image_digest=digest,
    )


def private_chat_input(messages: list[dict], max_tokens: int | None = None, temperature: float | int | None = None) -> dict:
    """The chat input contract, identical to the decrypted private chat plaintext the SDKs seal and the agent reads:
    {"prompt": "role: content" lines, "messages": [{"role","content"}], "max_tokens"?, "temperature"?}."""
    msgs = [{"role": str(m["role"]), "content": str(m["content"])} for m in messages]
    out: dict = {"prompt": "\n".join(f"{m['role']}: {m['content']}" for m in msgs), "messages": msgs}
    if max_tokens is not None:
        out["max_tokens"] = int(max_tokens)
    if temperature is not None:
        out["temperature"] = int(temperature) if float(temperature).is_integer() else float(temperature)
    return out


def _job_input_for_chat(request: ChatCompletionRequest) -> dict:
    # Private chat jobs carry this same shape, but sealed (job.input stays {}); the SDK builds it client side.
    return private_chat_input(
        [{"role": m.role, "content": m.content} for m in request.messages],
        max_tokens=request.max_tokens or settings.INFERENCE_MAX_TOKENS_DEFAULT,
    )


def _provider_int(value: Any, default: int) -> int:
    """A token count reported by a provider. The output is untrusted: a non-numeric, negative or absurd value
    must answer 502 (``invalid_provider_output``), not a 500 from ``int()``."""
    if value is None or value == 0 or value is False:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise InferenceError("The provider returned a malformed token count.", "invalid_provider_output", 502)
    n = int(value)
    if n < 0 or n > 10_000_000:
        raise InferenceError("The provider returned a malformed token count.", "invalid_provider_output", 502)
    return n


def _chat_response_from_output(
    request: ChatCompletionRequest, meta: ComputeCoinMeta, output: dict
) -> ChatCompletionResponse:
    text = str(output.get("text", ""))
    completion = _provider_int(output.get("tokens"), _estimate_tokens(text))
    prompt = _provider_int(output.get("prompt_tokens"), sum(_estimate_tokens(m.content) for m in request.messages))
    return ChatCompletionResponse(
        created=int(time.time()),
        model=request.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="length" if completion >= (request.max_tokens or settings.INFERENCE_MAX_TOKENS_DEFAULT) else "stop",
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
        computecoin=meta,
    )


async def _run_hardware_chat(
    db: AsyncSession, request: ChatCompletionRequest, customer_id: uuid.UUID, mv: ModelVersion
) -> ChatCompletionResponse:
    job_id = await _create_inference_job(
        db, customer_id, workload_type="text-generation", model=mv.version_tag,
        job_input=_job_input_for_chat(request),
    )
    job = await _wait_for_job(job_id)
    output = _require_completed(job)
    return _chat_response_from_output(request, await _hardware_meta(job, mv), output)


async def _run_hardware_embedding(
    db: AsyncSession, request: EmbeddingRequest, customer_id: uuid.UUID, mv: ModelVersion, inputs: list[str]
) -> EmbeddingResponse:
    job_id = await _create_inference_job(
        db, customer_id, workload_type="embeddings", model=mv.version_tag, job_input={"input": inputs},
    )
    job = await _wait_for_job(job_id)
    output = _require_completed(job)
    vectors = output.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(inputs):
        raise InferenceError("The provider returned malformed embeddings.", "invalid_provider_output", 502)
    tokens = _provider_int(output.get("tokens"), sum(_estimate_tokens(t) for t in inputs))
    try:
        rows = [[float(x) for x in v] for v in vectors]
    except (TypeError, ValueError):
        raise InferenceError("The provider returned malformed embeddings.", "invalid_provider_output", 502) from None
    if any(not math.isfinite(x) for row in rows for x in row):
        raise InferenceError("The provider returned malformed embeddings.", "invalid_provider_output", 502)
    return EmbeddingResponse(
        model=request.model,
        data=[EmbeddingData(index=i, embedding=row) for i, row in enumerate(rows)],
        usage=EmbeddingUsage(prompt_tokens=tokens, total_tokens=tokens),
        computecoin=await _hardware_meta(job, mv),
    )


# ---------------------------------------------------------------------------
# Public orchestration API
# ---------------------------------------------------------------------------
async def run_chat_completion(
    db: AsyncSession,
    request: ChatCompletionRequest,
    customer_id: uuid.UUID,
) -> ChatCompletionResponse:
    """Orchestrate a (non-streaming) chat completion request.

    Simulated models answer in-process. Any other model runs as a real job (P3).
    """
    if request.stream:
        raise InferenceError(
            "Use open_chat_stream for stream=true.", code="streaming_not_supported", http_status=400
        )

    if (request.isolation_tier or "").lower() in PRIVACY_TIERS:
        # Fail closed: a private tier on the non-streaming route used to be ignored, so the plaintext messages ran as an
        # ordinary container job (and were sent to the host) while the caller believed they were sealed.
        raise InferenceError(
            "Private isolation tiers are not available on non-streaming chat. Use stream=true with an encrypted_input "
            "(PRIVACY_STREAMING_ENABLED) or submit a private job through POST /v1/jobs.",
            code="private_tier_requires_streaming",
            http_status=400,
        )

    mv = await _resolve_model(db, request.model, "text-generation")
    if mv is None:
        raise InferenceError(
            f"Model '{request.model}' not found, not approved, or does not "
            f"support text-generation.",
            code="model_not_found",
            http_status=404,
        )

    if mv.runtime == "simulated":
        machine, image_digest = await _find_eligible_machine(db, "text-generation", model_version=mv)
        return _simulated_chat_response(request, mv, machine, image_digest=image_digest)

    return await _run_hardware_chat(db, request, customer_id, mv)


async def run_embedding(
    db: AsyncSession,
    request: EmbeddingRequest,
    customer_id: uuid.UUID,
) -> EmbeddingResponse:
    """Orchestrate an embedding request."""
    inputs: list[str] = (
        [request.input] if isinstance(request.input, str) else request.input
    )
    if not inputs:
        raise InferenceError(
            "Input array cannot be empty.",
            code="empty_input",
            http_status=400,
        )
    if len(inputs) > settings.INFERENCE_MAX_INPUT_ITEMS:
        raise InferenceError(
            f"Input array contains {len(inputs)} items, exceeding the maximum allowed of {settings.INFERENCE_MAX_INPUT_ITEMS}.",
            code="batch_size_exceeded",
            http_status=400,
        )

    mv = await _resolve_model(db, request.model, "embeddings")
    if mv is None:
        # Also try "text-generation" models that may support embeddings.
        mv = await _resolve_model(db, request.model, "text-generation")
    if mv is None:
        raise InferenceError(
            f"Model '{request.model}' not found, not approved, or does not "
            f"support embeddings.",
            code="model_not_found",
            http_status=404,
        )

    if mv.runtime == "simulated":
        machine, image_digest = await _find_eligible_machine(db, "embeddings", model_version=mv)
        if machine is None:
            machine, image_digest = await _find_eligible_machine(db, "text-generation", model_version=mv)
        return _simulated_embedding_response(request, mv, machine, image_digest=image_digest)

    return await _run_hardware_embedding(db, request, customer_id, mv, inputs)


# ---------------------------------------------------------------------------
# Streaming (B2) — see docs/architecture/streaming-chat.md
# ---------------------------------------------------------------------------
def split_for_stream(text: str, size: int | None = None) -> list[str]:
    """Cut text into pieces of about ``size`` characters at word boundaries; ''.join == text."""
    size = max(1, size or settings.INFERENCE_STREAM_CHUNK_CHARS)
    pieces: list[str] = []
    cur = ""
    for word in re.findall(r"\S+\s*|\s+", text):
        if cur and len(cur) + len(word) > size:
            pieces.append(cur)
            cur = ""
        cur += word
        while len(cur) > size * 4:  # a single huge token: hard cut
            pieces.append(cur[: size * 4])
            cur = cur[size * 4 :]
    if cur:
        pieces.append(cur)
    return pieces


def _sse(obj: dict | str) -> str:
    return f"data: {obj if isinstance(obj, str) else json.dumps(obj, separators=(',', ':'))}\n\n"


def _stream_error_event(message: str, code: str) -> str:
    return _sse({"error": {"message": message, "type": "stream_error", "code": code}})


def _private_stream_event(job_id: uuid.UUID, stream_id: str, created: int, seq: int, final: bool, envelope: dict) -> str:
    return _sse({
        "id": stream_id, "object": "chat.completion.chunk.sealed", "created": created, "job_id": str(job_id),
        "seq": seq, "final": final, "sealed": envelope,
    })


async def _open_private_chat_stream(
    db: AsyncSession, request: ChatCompletionRequest, customer_id: uuid.UUID
) -> AsyncIterator[str]:
    """U18: end-to-end sealed chat stream. The relay only ever holds ciphertext (design note U18, section 2)."""
    from app.services.key_guardians import validate_sealed_shares
    from app.services.privacy_crypto import is_data_envelope

    tier = (request.isolation_tier or "").lower()
    placeholder = len(request.messages) == 1 and request.messages[0].role == "user" and request.messages[0].content == "sealed"
    if not placeholder:
        raise InferenceError("Private streams take the messages inside encrypted_input; send the single placeholder message 'sealed'.",
                             code="private_messages_must_be_sealed", http_status=422)
    if not is_data_envelope(request.encrypted_input) or not request.dk_shares or not request.result_public_key:
        raise InferenceError("Private streams need encrypted_input, dk_shares and result_public_key.",
                             code="private_fields_required", http_status=422)
    try:
        validate_sealed_shares(request.dk_shares)
    except ValueError:
        raise InferenceError("dk_shares is invalid.", code="dk_shares_invalid", http_status=422) from None
    mv = await _resolve_model(db, request.model, "text-generation")
    if mv is None:
        raise InferenceError(
            f"Model '{request.model}' not found, not approved, or does not support text-generation.",
            code="model_not_found", http_status=404,
        )
    if mv.runtime == "simulated":
        raise InferenceError("A private stream needs a hardware model.", code="private_requires_hardware_model", http_status=400)

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    job_id = await _create_inference_job(
        db, customer_id, workload_type="text-generation", model=mv.version_tag, job_input={},
        private={"tier": tier, "encrypted_input": request.encrypted_input, "dk_shares": request.dk_shares,
                 "result_public_key": request.result_public_key},
    )
    stream_relay.open_stream(str(job_id), sealed=True)

    async def private_events() -> AsyncIterator[str]:
        finished = False
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + settings.INFERENCE_TIMEOUT_SECONDS
            idx = 0
            saw_final = False  # set from the relay's stream-level final flag
            terminal: Job | None = None
            while True:
                pieces, final, err = stream_relay.read_from(str(job_id), idx)
                if err:
                    yield _stream_error_event("Token stream was out of order.", f"stream_sequence_error:{err}")
                    return
                for p in pieces:
                    rec = json.loads(p)
                    yield _private_stream_event(job_id, stream_id, created, idx, bool(rec["final"]), rec["sealed"])
                    idx += 1
                saw_final = saw_final or final
                if terminal is not None:
                    break
                job = await _load_job(job_id)
                if job is not None and job.status in fsm.TERMINAL_STATES:
                    terminal = job
                    continue
                if loop.time() >= deadline:
                    yield _stream_error_event("Inference timed out.", "inference_timeout")
                    return
                await asyncio.sleep(settings.INFERENCE_POLL_INTERVAL_SECONDS)
            if terminal.status != fsm.COMPLETED:
                yield _stream_error_event(f"The provider did not complete the job ({terminal.status}).", terminal.error_code or "inference_job_failed")
                return
            if not saw_final:
                yield _stream_error_event("The sealed stream ended without its final chunk; fetch the sealed result from the job.", "sealed_stream_incomplete")
                finished = True  # the job itself completed and is billed; do not cancel it
                return
            yield _sse("[DONE]")
            finished = True
        finally:
            stream_relay.close_stream(str(job_id))
            if not finished:
                await asyncio.shield(cancel_inference_job(job_id, reason="stream_closed"))

    return private_events()


async def open_chat_stream(
    db: AsyncSession, request: ChatCompletionRequest, customer_id: uuid.UUID
) -> AsyncIterator[str]:
    """Validate, resolve and (for real models) start the job; return the SSE event iterator.

    Everything that can fail before the first byte raises InferenceError here so the router can
    answer with a normal HTTP error.
    """
    if (request.isolation_tier or "").lower() in PRIVACY_TIERS:
        if not (settings.PRIVACY_STREAMING_ENABLED and settings.privacy_active):
            raise InferenceError(
                "Streaming is not supported for private isolation tiers.",
                code="streaming_not_supported_for_private",
                http_status=400,
            )
        return await _open_private_chat_stream(db, request, customer_id)
    mv = await _resolve_model(db, request.model, "text-generation")
    if mv is None:
        raise InferenceError(
            f"Model '{request.model}' not found, not approved, or does not support text-generation.",
            code="model_not_found",
            http_status=404,
        )

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(meta: ComputeCoinMeta, delta: ChunkDelta, finish: str | None = None) -> str:
        return _sse(
            ChatCompletionChunk(
                id=stream_id, created=created, model=request.model,
                choices=[ChunkChoice(delta=delta, finish_reason=finish)], computecoin=meta,
            ).model_dump(exclude_none=True)
        )

    if mv.runtime == "simulated":
        machine, image_digest = await _find_eligible_machine(db, "text-generation", model_version=mv)
        full = _simulated_chat_response(request, mv, machine, image_digest=image_digest)
        text = full.choices[0].message.content
        meta = full.computecoin

        async def sim_events() -> AsyncIterator[str]:
            yield chunk(meta, ChunkDelta(role="assistant", content=""))
            for piece in split_for_stream(text):
                yield chunk(meta, ChunkDelta(content=piece))
            yield chunk(meta, ChunkDelta(), "stop")
            yield _sse("[DONE]")

        return sim_events()

    job_id = await _create_inference_job(
        db, customer_id, workload_type="text-generation", model=mv.version_tag,
        job_input=_job_input_for_chat(request),
    )
    stream_relay.open_stream(str(job_id))

    async def hw_events() -> AsyncIterator[str]:
        finished = False
        try:
            job0 = await _load_job(job_id)
            meta = await _hardware_meta(job0, mv) if job0 else ComputeCoinMeta(execution_mode="hardware")
            yield chunk(meta, ChunkDelta(role="assistant", content=""))
            loop = asyncio.get_event_loop()
            deadline = loop.time() + settings.INFERENCE_TIMEOUT_SECONDS
            idx = 0
            streamed = ""
            terminal: Job | None = None
            while True:
                pieces, _final, err = stream_relay.read_from(str(job_id), idx)
                if err:
                    yield _stream_error_event("Token stream was out of order.", f"stream_sequence_error:{err}")
                    return
                for p in pieces:
                    idx += 1
                    streamed += p
                    yield chunk(meta, ChunkDelta(content=p))
                if terminal is not None:
                    break
                job = await _load_job(job_id)
                if job is not None and job.status in fsm.TERMINAL_STATES:
                    terminal = job
                    continue  # one more pass to drain chunks that arrived before completion
                if loop.time() >= deadline:
                    yield _stream_error_event("Inference timed out.", "inference_timeout")
                    return
                await asyncio.sleep(settings.INFERENCE_POLL_INTERVAL_SECONDS)
            if terminal.status != fsm.COMPLETED or not isinstance(terminal.output, dict):
                yield _stream_error_event(
                    f"The provider did not complete the job ({terminal.status}).",
                    terminal.error_code or "inference_job_failed",
                )
                return
            final_text = str(terminal.output.get("text", ""))
            if not final_text.startswith(streamed):
                yield _stream_error_event("Streamed tokens do not match the final output.", "stream_mismatch")
                return
            for piece in split_for_stream(final_text[len(streamed):]):
                yield chunk(meta, ChunkDelta(content=piece))
            yield chunk(meta, ChunkDelta(), "stop")
            yield _sse("[DONE]")
            finished = True
        finally:
            stream_relay.close_stream(str(job_id))
            if not finished:
                # Error, timeout or client disconnect: cancel and release the unused hold.
                await asyncio.shield(cancel_inference_job(job_id, reason="stream_closed"))

    return hw_events()
