"""ChatHistory — in-memory per-room message log used for inter-message
group context. Process-local; not persisted. Transport-portable
(keyed on ChatRef)."""
from __future__ import annotations

from link_project_to_chat.chat_history import (
    ChatHistory,
    _DEFAULT_MAX_SERIALIZED_CHARS,
    _DEFAULT_MAXLEN,
    _TRUNCATION_PREFIX,
)
from link_project_to_chat.transport.base import ChatKind, ChatRef


def _room(transport: str = "fake", native: str = "100") -> ChatRef:
    return ChatRef(transport_id=transport, native_id=native, kind=ChatKind.ROOM)


def _dm() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)


def test_record_then_retrieve_full_buffer():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "hello")
    h.record(chat, "2", "bob", "hi")
    result = h.since_last_llm(chat, "999")
    assert "[Recent discussion]" in result
    assert "alice: hello" in result
    assert "bob: hi" in result


def test_since_last_llm_excludes_messages_before_mark():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "early")
    h.record(chat, "2", "bob", "mid")
    h.mark_llm_call(chat, "2")  # bot just replied to msg 2
    h.record(chat, "3", "alice", "after-mark")
    result = h.since_last_llm(chat, "999")
    assert "early" not in result
    assert "mid" not in result
    assert "after-mark" in result


def test_since_last_llm_excludes_before_msg_id_itself():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "first")
    h.record(chat, "2", "bob", "second")
    h.record(chat, "3", "alice", "third")
    # Bot will reply to msg 3 — context should be msgs 1 and 2, not 3.
    result = h.since_last_llm(chat, "3")
    assert "first" in result
    assert "second" in result
    assert "third" not in result


def test_returns_empty_when_no_messages_between_mark_and_before():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "hi")
    h.mark_llm_call(chat, "1")
    # No messages recorded after the mark.
    assert h.since_last_llm(chat, "999") == ""


def test_returns_empty_when_chat_unknown():
    h = ChatHistory()
    other = _room(native="999")
    assert h.since_last_llm(other, "1") == ""


def test_dm_record_is_skipped():
    h = ChatHistory()
    dm = _dm()
    h.record(dm, "1", "alice", "private message")
    assert h.since_last_llm(dm, "999") == ""


def test_own_bot_record_is_skipped():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "MyBot", "auto reply", is_own_bot=True)
    h.record(chat, "2", "alice", "user message")
    result = h.since_last_llm(chat, "999")
    assert "auto reply" not in result
    assert "user message" in result


def test_empty_text_record_is_skipped():
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "")
    h.record(chat, "2", "bob", "   ")  # whitespace-only
    h.record(chat, "3", "alice", "real")
    result = h.since_last_llm(chat, "999")
    assert "real" in result
    # Only one entry — the two empties were skipped.
    assert result.count(": ") == 1


def test_buffer_overflow_evicts_oldest():
    h = ChatHistory(maxlen=3)
    chat = _room()
    h.record(chat, "1", "a", "msg1")
    h.record(chat, "2", "b", "msg2")
    h.record(chat, "3", "c", "msg3")
    h.record(chat, "4", "d", "msg4")  # evicts msg1
    result = h.since_last_llm(chat, "999")
    assert "msg1" not in result
    assert "msg2" in result
    assert "msg3" in result
    assert "msg4" in result


def test_serialized_overflow_truncates_oldest():
    h = ChatHistory(maxlen=100, max_serialized_chars=50)
    chat = _room()
    h.record(chat, "1", "a", "X" * 30)  # 33 chars: "a: XXX..."
    h.record(chat, "2", "b", "Y" * 30)
    h.record(chat, "3", "c", "Z" * 30)
    result = h.since_last_llm(chat, "999")
    # Truncation marker is prepended; oldest dropped.
    assert _TRUNCATION_PREFIX in result
    # The most recent message survives.
    assert "Z" * 30 in result


def test_mark_evicted_falls_back_to_buffer_head():
    """If last_llm_msg_id was evicted from the ring, walk from head."""
    h = ChatHistory(maxlen=2)
    chat = _room()
    h.record(chat, "1", "a", "first")
    h.mark_llm_call(chat, "1")  # mark on msg 1
    h.record(chat, "2", "b", "second")  # buffer: [1, 2]
    h.record(chat, "3", "c", "third")   # evicts msg 1 → buffer: [2, 3]
    # mark "1" no longer in buffer; walk falls back to head.
    result = h.since_last_llm(chat, "999")
    assert "second" in result
    assert "third" in result


def test_default_constants_match_gitlab():
    assert _DEFAULT_MAXLEN == 200
    assert _DEFAULT_MAX_SERIALIZED_CHARS == 4000


def test_serialized_format_matches_gitlab():
    """[Recent discussion]\n<sender>: <text>\n<sender>: <text>\n\n"""
    h = ChatHistory()
    chat = _room()
    h.record(chat, "1", "alice", "hi")
    h.record(chat, "2", "bob", "there")
    result = h.since_last_llm(chat, "999")
    assert result == "[Recent discussion]\nalice: hi\nbob: there\n\n"


def test_record_idempotent_on_same_msg_id():
    """Re-recording the same msg_id is a no-op — guards against dispatch
    chains that record in multiple handlers for the same incoming."""
    h = ChatHistory()
    chat = _room()
    h.record(chat, "100", "alice", "hello")
    h.record(chat, "100", "alice", "hello")  # duplicate
    h.record(chat, "100", "alice", "hello")  # triplicate
    result = h.since_last_llm(chat, "999")
    assert result.count("alice: hello") == 1


def test_record_different_msg_id_after_dup_still_appends():
    """Idempotency only skips the LATEST msg_id; subsequent records work."""
    h = ChatHistory()
    chat = _room()
    h.record(chat, "100", "alice", "first")
    h.record(chat, "100", "alice", "first")  # skipped
    h.record(chat, "101", "bob", "second")
    result = h.since_last_llm(chat, "999")
    assert "first" in result
    assert "second" in result


def test_last_msg_id_returns_tail():
    h = ChatHistory()
    chat = _room()
    assert h.last_msg_id(chat) is None
    h.record(chat, "1", "alice", "hello")
    assert h.last_msg_id(chat) == "1"
    h.record(chat, "2", "bob", "world")
    assert h.last_msg_id(chat) == "2"


def test_last_msg_id_returns_none_for_unknown_chat():
    h = ChatHistory()
    other = _room(native="999")
    assert h.last_msg_id(other) is None
