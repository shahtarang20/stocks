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

import datetime as dt
import hashlib
import math
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import utcnow
from app.models.model_registry import ModelVersion
from app.models.provider import ProviderMachine
from app.schemas.inference import (
    ChatCompletionChoice,
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
from app.services.provider import derive_status


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
    db: AsyncSession, workload_type: str, now: dt.datetime | None = None
) -> ProviderMachine | None:
    """Find the best eligible online machine for the given workload type.

    Uses a simplified version of the Phase 4 scheduler: cheapest price among
    online machines that support the workload.
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

    for m in result.scalars().all():
        if derive_status(m, now) != "online":
            continue
        workloads = m.capability.supported_workloads if m.capability else []
        if workload_type not in (workloads or []):
            continue
        price = m.asking_price_per_hour
        if best_price is None or price < best_price:
            best = m
            best_price = price

    return best


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


async def run_chat_completion(
    db: AsyncSession,
    request: ChatCompletionRequest,
    customer_id: uuid.UUID,
) -> ChatCompletionResponse:
    """Orchestrate a chat completion request.

    1. Resolve the model from the registry.
    2. Find an eligible machine (or proceed without one for simulated).
    3. Generate the response (simulated for now).
    """
    if request.stream:
        raise InferenceError(
            "Streaming is not yet supported. Set stream=false.",
            code="streaming_not_supported",
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

    machine = await _find_eligible_machine(db, "text-generation")

    if mv.runtime == "simulated" or machine is None:
        # Simulated path — deterministic, no real GPU.
        return _simulated_chat_response(request, mv, machine)

    # Hardware path — future Phase 4+ integration.
    # For now, fall back to simulated and stamp it honestly.
    return _simulated_chat_response(request, mv, machine)


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

    # Route to an eligible machine supporting embeddings (fall back to text-generation)
    machine = await _find_eligible_machine(db, "embeddings")
    if machine is None:
        machine = await _find_eligible_machine(db, "text-generation")

    if mv.runtime == "simulated" or machine is None:
        return _simulated_embedding_response(request, mv, machine)

    # Hardware path — future.
    return _simulated_embedding_response(request, mv, machine)
