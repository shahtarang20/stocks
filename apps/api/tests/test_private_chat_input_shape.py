"""API side of the private chat input contract (written, NOT run)."""

from __future__ import annotations

import json
import pathlib

from app.services.inference import private_chat_input

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_shape_matches_shared_sdk_vectors():
    cases = json.loads((ROOT / "docs/specs/private-chat-input-vectors.json").read_text())["cases"]
    for c in cases:
        built = private_chat_input(c["messages"], max_tokens=c.get("max_tokens"), temperature=c.get("temperature"))
        got = json.dumps(built, sort_keys=True, separators=(",", ":"))
        assert got == c["expected"], c["name"]


def test_job_input_for_chat_uses_the_contract():
    from app.core.config import settings
    from app.schemas.inference import ChatCompletionRequest
    from app.services.inference import _job_input_for_chat

    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    out = _job_input_for_chat(req)
    assert out["prompt"] == "user: hi"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["max_tokens"] == settings.INFERENCE_MAX_TOKENS_DEFAULT
