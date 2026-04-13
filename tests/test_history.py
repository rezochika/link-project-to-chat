"""Tests for the in-memory conversation history ring buffer."""
from __future__ import annotations

import time

from link_project_to_chat.history import History, HistoryEntry


def test_add_stores_entry() -> None:
    h = History()
    h.add("user", "hello")
    assert len(h) == 1
    entries = h.recent(10)
    assert entries[0].role == "user"
    assert entries[0].text == "hello"


def test_add_stores_task_id() -> None:
    h = History()
    h.add("assistant", "response", task_id=42)
    entries = h.recent()
    assert entries[0].task_id == 42


def test_recent_returns_last_n() -> None:
    h = History()
    for i in range(20):
        h.add("user", f"msg {i}")
    result = h.recent(5)
    assert len(result) == 5
    assert result[-1].text == "msg 19"
    assert result[0].text == "msg 15"


def test_recent_with_n_greater_than_len_returns_all() -> None:
    h = History()
    h.add("user", "a")
    h.add("assistant", "b")
    result = h.recent(100)
    assert len(result) == 2


def test_ring_buffer_evicts_old_entries_when_maxlen_exceeded() -> None:
    h = History(maxlen=3)
    h.add("user", "first")
    h.add("user", "second")
    h.add("user", "third")
    h.add("user", "fourth")
    assert len(h) == 3
    texts = [e.text for e in h.recent(3)]
    assert "first" not in texts
    assert "fourth" in texts


def test_clear_empties_buffer() -> None:
    h = History()
    h.add("user", "hello")
    h.add("assistant", "hi")
    h.clear()
    assert len(h) == 0
    assert h.recent() == []


def test_text_truncated_to_200_chars() -> None:
    h = History()
    long_text = "x" * 300
    h.add("user", long_text)
    entry = h.recent()[0]
    assert len(entry.text) == 200
    assert entry.text == "x" * 200


def test_text_not_truncated_when_under_200_chars() -> None:
    h = History()
    short_text = "hello world"
    h.add("user", short_text)
    entry = h.recent()[0]
    assert entry.text == short_text


def test_len_returns_count() -> None:
    h = History()
    assert len(h) == 0
    h.add("user", "a")
    assert len(h) == 1
    h.add("assistant", "b")
    assert len(h) == 2


def test_history_entry_has_timestamp() -> None:
    before = time.monotonic()
    entry = HistoryEntry(role="user", text="hi")
    after = time.monotonic()
    assert before <= entry.timestamp <= after


def test_recent_default_n_is_10() -> None:
    h = History()
    for i in range(15):
        h.add("user", f"msg {i}")
    result = h.recent()
    assert len(result) == 10


def test_multiple_roles() -> None:
    h = History()
    h.add("user", "question")
    h.add("assistant", "answer")
    entries = h.recent()
    assert entries[0].role == "user"
    assert entries[1].role == "assistant"
