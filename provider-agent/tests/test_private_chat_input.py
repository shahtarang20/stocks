"""Private chat input contract on the agent side (written, NOT run)."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from computecoin_agent.jobrunner import normalize_private_chat_input as norm

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _entrypoint():
    spec = importlib.util.spec_from_file_location("tg_entry", ROOT / "workloads/text-generation/entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_messages_preferred_over_prompt():
    out = norm({"prompt": "ignored", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], "max_tokens": 5})
    assert out["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert out["max_tokens"] == 5 and out["prompt"] == "ignored"


def test_prompt_only_becomes_one_user_message():
    out = norm({"prompt": "hello"})
    assert out == {"messages": [{"role": "user", "content": "hello"}], "prompt": "hello"}


def test_prompt_rendered_when_missing():
    assert norm({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})["prompt"] == "user: a\nassistant: b"


@pytest.mark.parametrize("bad", [
    None, [], {}, {"prompt": ""}, {"prompt": 3}, {"messages": ["x"]}, {"messages": [{"role": "user", "content": 1}]},
    {"prompt": "x", "max_tokens": 0}, {"prompt": "x", "max_tokens": True}, {"prompt": "x", "temperature": 9},
    {"prompt": "x", "temperature": "hot"},
])
def test_bad_shapes_refused_without_quoting_data(bad):
    with pytest.raises(ValueError):
        norm(bad)


def test_error_text_never_contains_the_secret():
    with pytest.raises(ValueError) as ei:
        norm({"messages": [{"role": "user", "content": 5}], "prompt": "TOP-SECRET"})
    assert "TOP-SECRET" not in str(ei.value)


def test_normalised_shape_matches_sdk_vectors():
    cases = json.loads((ROOT / "docs/specs/private-chat-input-vectors.json").read_text())["cases"]
    for c in cases:
        built = json.loads(c["expected"])
        assert norm(built) == built  # the agent reads exactly what the SDKs build


def test_workload_entrypoint_reads_same_shape():
    ep = _entrypoint()
    msgs, mt = ep.normalise({"prompt": "p", "messages": [{"role": "user", "content": "u"}], "max_tokens": 9, "temperature": 0.7})
    assert msgs == [{"role": "user", "content": "u"}] and mt == 9
    msgs, _ = ep.normalise({"prompt": "only"})
    assert msgs == [{"role": "user", "content": "only"}]
    with pytest.raises(ep.WorkloadError):
        ep.normalise({"prompt": "p", "temperature": "x"})
