"""B2: ordered, gap-checked token relay (unit level; the agent route is a thin wrapper)."""

from __future__ import annotations

import pytest

from app.services import stream_relay as r
from app.services.inference import split_for_stream


def test_split_for_stream_joins_back():
    text = "Hello  world, this is   a longer sentence.\nWith lines and  spaces. " * 5
    pieces = split_for_stream(text, 24)
    assert "".join(pieces) == text
    assert len(pieces) > 1
    assert "".join(split_for_stream("x" * 1000, 8)) == "x" * 1000


def test_in_order_chunks_accepted_and_read():
    r.open_stream("j1")
    assert r.accept_chunk("j1", 0, "He", False) == (True, 1)
    assert r.accept_chunk("j1", 1, "llo", True) == (True, 2)
    pieces, final, err = r.read_from("j1", 0)
    assert pieces == ["He", "llo"] and final and err is None
    r.close_stream("j1")


def test_gap_ends_stream_with_error():
    r.open_stream("j2")
    r.accept_chunk("j2", 0, "a", False)
    with pytest.raises(r.StreamSequenceError):
        r.accept_chunk("j2", 2, "c", False)
    assert r.read_from("j2", 0)[2] == "sequence_gap"
    with pytest.raises(r.StreamSequenceError):  # stays failed
        r.accept_chunk("j2", 1, "b", False)
    r.close_stream("j2")


def test_reorder_and_modified_repeat_are_errors_but_exact_retry_is_fine():
    r.open_stream("j3")
    r.accept_chunk("j3", 0, "a", False)
    r.accept_chunk("j3", 1, "b", False)
    assert r.accept_chunk("j3", 0, "a", False)[0] is True  # retry
    with pytest.raises(r.StreamSequenceError):
        r.accept_chunk("j3", 0, "X", False)
    r.close_stream("j3")


def test_no_open_stream_drops_chunk():
    assert r.accept_chunk("nobody", 0, "a", False) == (False, 0)
