"""OpenAI-compatible inference router (Phase 6).

Endpoints:
    POST /v1/chat/completions   — chat completion (simulated or routed)
    POST /v1/embeddings         — embedding generation (simulated or routed)

Auth: Bearer token (customer role) or API key required.
Rate limited per user.

Every response includes a ``computecoin`` extension block with
``execution_mode`` so callers always know whether results came from real GPU
inference or the deterministic simulator.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.deps import get_current_user
from app.schemas.auth import CurrentUser
from app.schemas.inference import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    InferenceErrorDetail,
    InferenceErrorResponse,
)
from app.services.inference import InferenceError, run_chat_completion, run_embedding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["inference"])


def _enforce_inference_rate_limit(user_id: str) -> None:
    key = f"inference:{user_id}"
    if not rate_limiter.allow(
        key,
        settings.INFERENCE_RATE_LIMIT,
        settings.INFERENCE_RATE_WINDOW_SECONDS,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=InferenceErrorResponse(
                error=InferenceErrorDetail(
                    message="Rate limit exceeded. Please slow down.",
                    type="rate_limit_error",
                    code="rate_limit_exceeded",
                )
            ).model_dump(),
        )


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        400: {"model": InferenceErrorResponse},
        404: {"model": InferenceErrorResponse},
        429: {"model": InferenceErrorResponse},
        503: {"model": InferenceErrorResponse},
    },
    summary="Create a chat completion (OpenAI-compatible)",
    description=(
        "Generate a chat completion using a registered model routed to an "
        "eligible provider machine. Returns an OpenAI-compatible response with "
        "a `computecoin` extension block indicating the execution mode."
    ),
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatCompletionResponse:
    _enforce_inference_rate_limit(str(current_user.id))
    try:
        return await run_chat_completion(db, request, current_user.id)
    except InferenceError as exc:
        logger.warning(
            "inference_error user=%s model=%s code=%s msg=%s",
            current_user.id,
            request.model,
            exc.code,
            exc.message,
        )
        raise HTTPException(
            status_code=exc.http_status,
            detail=InferenceErrorResponse(
                error=InferenceErrorDetail(
                    message=exc.message,
                    type="invalid_request_error",
                    code=exc.code,
                )
            ).model_dump(),
        )


@router.post(
    "/embeddings",
    response_model=EmbeddingResponse,
    responses={
        400: {"model": InferenceErrorResponse},
        404: {"model": InferenceErrorResponse},
        429: {"model": InferenceErrorResponse},
        503: {"model": InferenceErrorResponse},
    },
    summary="Create embeddings (OpenAI-compatible)",
    description=(
        "Generate embeddings for input text using a registered model routed "
        "to an eligible provider machine. Returns an OpenAI-compatible response "
        "with a `computecoin` extension block indicating the execution mode."
    ),
)
async def create_embeddings(
    request: EmbeddingRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingResponse:
    _enforce_inference_rate_limit(str(current_user.id))
    try:
        return await run_embedding(db, request, current_user.id)
    except InferenceError as exc:
        logger.warning(
            "inference_error user=%s model=%s code=%s msg=%s",
            current_user.id,
            request.model,
            exc.code,
            exc.message,
        )
        raise HTTPException(
            status_code=exc.http_status,
            detail=InferenceErrorResponse(
                error=InferenceErrorDetail(
                    message=exc.message,
                    type="invalid_request_error",
                    code=exc.code,
                )
            ).model_dump(),
        )
