"""OpenAI-compatible inference request/response schemas (Phase 6).

Follows the OpenAI API specification for ``/v1/chat/completions`` and
``/v1/embeddings`` so customers can drop-in ComputeCoin as an OpenAI-compatible
provider.  Every response carries a ``computecoin`` extension block with
``execution_mode`` (\"simulated\" | \"hardware\") so callers always know whether
the result came from real GPU inference or the deterministic simulator.

Money is never coerced to float.  Token counts are integer.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Per-string length bound for embedding inputs (DoS guard). The batch item-count
# cap is enforced in the inference service.
_EMBED_MAX_CHARS = 131_072


# ---------------------------------------------------------------------------
# Chat completions — request
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str = Field(..., max_length=32, description="Role: system | user | assistant")
    # Bounded to prevent multi-GB prompt bodies exhausting worker memory/CPU.
    content: str = Field(..., max_length=131_072, description="Message content")
    name: Optional[str] = Field(default=None, max_length=128)


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model_config = ConfigDict(protected_namespaces=())

    model: str = Field(..., min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=512)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    n: int = Field(default=1, ge=1, le=8)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=128_000)
    stop: Optional[list[str] | str] = None
    stream: bool = False
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    user: Optional[str] = None


# ---------------------------------------------------------------------------
# Chat completions — response
# ---------------------------------------------------------------------------
class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ComputeCoinMeta(BaseModel):
    """Extension block surfacing execution honesty metadata."""

    execution_mode: str  # "simulated" | "hardware"
    machine_id: Optional[str] = None
    provider_id: Optional[str] = None
    model_version: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: str = "chat.completion"
    created: int  # Unix timestamp
    model: str
    choices: list[ChatCompletionChoice]
    usage: CompletionUsage
    system_fingerprint: str = "computecoin-v1"
    # ComputeCoin extension — always present so callers know the execution mode.
    computecoin: ComputeCoinMeta


# ---------------------------------------------------------------------------
# Embeddings — request
# ---------------------------------------------------------------------------
class EmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request."""

    model_config = ConfigDict(protected_namespaces=())

    model: str = Field(..., min_length=1, max_length=128)
    input: str | list[str] = Field(...)

    @field_validator("input")
    @classmethod
    def _bound_input(cls, v: "str | list[str]") -> "str | list[str]":
        # Bound each input string's length so a single request cannot exhaust
        # memory/CPU. (The 100-item batch cap is enforced in the service layer,
        # which returns the documented 400 ``batch_size_exceeded``.)
        items = v if isinstance(v, list) else [v]
        for s in items:
            if not isinstance(s, str):
                raise ValueError("input must be a string or list of strings")
            if len(s) > _EMBED_MAX_CHARS:
                raise ValueError(f"each input string must be <= {_EMBED_MAX_CHARS} chars")
        return v
    encoding_format: str = Field(default="float")
    user: Optional[str] = None


# ---------------------------------------------------------------------------
# Embeddings — response
# ---------------------------------------------------------------------------
class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    """OpenAI-compatible embedding response."""

    model_config = ConfigDict(protected_namespaces=())

    object: str = "list"
    model: str
    data: list[EmbeddingData]
    usage: EmbeddingUsage
    computecoin: ComputeCoinMeta


# ---------------------------------------------------------------------------
# Shared error response
# ---------------------------------------------------------------------------
class InferenceErrorDetail(BaseModel):
    message: str
    type: str
    code: Optional[str] = None
    param: Optional[str] = None


class InferenceErrorResponse(BaseModel):
    error: InferenceErrorDetail
