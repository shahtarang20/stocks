"""Provider output is untrusted: malformed token counts / embeddings answer 502, never a 500 (review-all)."""
from __future__ import annotations

import pytest

from app.schemas.inference import ChatCompletionRequest, ChatMessage, ComputeCoinMeta
from app.services.inference import InferenceError, _chat_response_from_output, _provider_int


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="hello world")])


@pytest.mark.parametrize("bad", ["abc", [1], {"a": 1}, float("nan"), float("inf"), -5, 10**12, True])
def test_provider_int_rejects_malformed(bad):
    with pytest.raises(InferenceError) as ei:
        _provider_int(bad, 3)
    assert ei.value.http_status == 502 and ei.value.code == "invalid_provider_output"


def test_provider_int_defaults_and_accepts():
    assert _provider_int(None, 7) == 7
    assert _provider_int(0, 7) == 7
    assert _provider_int(12, 7) == 12
    assert _provider_int(12.0, 7) == 12


def test_chat_response_from_malformed_output_is_502():
    meta = ComputeCoinMeta(execution_mode="hardware")
    with pytest.raises(InferenceError) as ei:
        _chat_response_from_output(_req(), meta, {"text": "x", "tokens": "many"})
    assert ei.value.http_status == 502
    ok = _chat_response_from_output(_req(), meta, {"text": "hi", "tokens": 2, "prompt_tokens": 3})
    assert ok.usage.total_tokens == 5
