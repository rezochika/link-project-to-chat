"""Tests for :mod:`link_project_to_chat.conversation_log`.

The conversation log is a per-chat append-only SQLite store that lets the
bot inject prior conversational turns into the next agent prompt — the
mechanism that makes ``/backend codex`` see what the user already said to
Claude (and vice versa). These tests cover the storage primitive only;
bot wiring + cross-backend behaviour live in ``test_context_command.py``.
"""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.conversation_log import (
    ASSISTANT_ROLE,
    USER_ROLE,
    ConversationLog,
    format_history_block,
)
from link_project_to_chat.transport import ChatKind, ChatRef


def _chat(native_id: str = "100", transport_id: str = "telegram") -> ChatRef:
    return ChatRef(
        transport_id=transport_id, native_id=native_id, kind=ChatKind.DM,
    )


def _log(tmp_path: Path, name: str = "proj") -> ConversationLog:
    return ConversationLog(tmp_path / "conversations" / f"{name}.db")


def test_append_then_recent_round_trips_in_chronological_order(tmp_path):
    log = _log(tmp_path)
    chat = _chat()
    log.append(chat, USER_ROLE, "hello", backend="claude")
    log.append(chat, ASSISTANT_ROLE, "hi there", backend="claude")
    log.append(chat, USER_ROLE, "how are you", backend="codex")
    log.append(chat, ASSISTANT_ROLE, "good", backend="codex")

    turns = log.recent(chat, limit=10)
    assert turns == [
        (USER_ROLE, "hello"),
        (ASSISTANT_ROLE, "hi there"),
        (USER_ROLE, "how are you"),
        (ASSISTANT_ROLE, "good"),
    ]


def test_recent_limit_returns_only_last_n(tmp_path):
    log = _log(tmp_path)
    chat = _chat()
    for i in range(6):
        log.append(chat, USER_ROLE, f"msg-{i}")

    turns = log.recent(chat, limit=3)
    assert turns == [
        (USER_ROLE, "msg-3"),
        (USER_ROLE, "msg-4"),
        (USER_ROLE, "msg-5"),
    ]


def test_recent_limit_zero_returns_empty(tmp_path):
    log = _log(tmp_path)
    chat = _chat()
    log.append(chat, USER_ROLE, "anything")
    assert log.recent(chat, limit=0) == []


def test_recent_per_chat_isolation_keeps_chats_distinct(tmp_path):
    log = _log(tmp_path)
    chat_a = _chat(native_id="A")
    chat_b = _chat(native_id="B")
    log.append(chat_a, USER_ROLE, "a-message")
    log.append(chat_b, USER_ROLE, "b-message")

    assert log.recent(chat_a) == [(USER_ROLE, "a-message")]
    assert log.recent(chat_b) == [(USER_ROLE, "b-message")]


def test_recent_per_transport_isolation(tmp_path):
    """Same native_id but different transports are separate chats — Telegram
    chat 42 and Web chat 42 are unrelated rooms."""
    log = _log(tmp_path)
    chat_tg = _chat(native_id="42", transport_id="telegram")
    chat_web = _chat(native_id="42", transport_id="web")
    log.append(chat_tg, USER_ROLE, "from telegram")
    log.append(chat_web, USER_ROLE, "from web")

    assert log.recent(chat_tg) == [(USER_ROLE, "from telegram")]
    assert log.recent(chat_web) == [(USER_ROLE, "from web")]


def test_clear_only_target_chat(tmp_path):
    log = _log(tmp_path)
    chat_a = _chat(native_id="A")
    chat_b = _chat(native_id="B")
    log.append(chat_a, USER_ROLE, "a1")
    log.append(chat_a, ASSISTANT_ROLE, "a2")
    log.append(chat_b, USER_ROLE, "b1")

    deleted = log.clear(chat_a)

    assert deleted == 2
    assert log.recent(chat_a) == []
    assert log.recent(chat_b) == [(USER_ROLE, "b1")]


def test_append_skips_blank_text(tmp_path):
    """Blank user/assistant text must not pollute the log — it would show
    up as empty 'user:' lines in the prepended history block."""
    log = _log(tmp_path)
    chat = _chat()
    log.append(chat, USER_ROLE, "")
    log.append(chat, USER_ROLE, "   \n\t")

    assert log.recent(chat) == []


def test_append_invalid_role_raises(tmp_path):
    log = _log(tmp_path)
    chat = _chat()
    try:
        log.append(chat, "system", "ignored")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown role")


def test_format_history_block_empty_returns_empty_string():
    assert format_history_block([]) == ""


def test_format_history_block_renders_turns_in_order():
    turns = [
        (USER_ROLE, "hi"),
        (ASSISTANT_ROLE, "hello"),
        (USER_ROLE, "how are you"),
    ]
    block = format_history_block(turns)
    # Header announces count.
    assert "last 3 turns" in block
    # Roles + texts appear in order.
    assert block.index("user: hi") < block.index("assistant: hello")
    assert block.index("assistant: hello") < block.index("user: how are you")
    # Trailing marker so the agent can tell the new prompt from the history.
    assert block.endswith("[Current message]\n")


def test_format_history_block_truncates_oversized_turns():
    """A 50KB user paste must NOT inject 50KB into every subsequent prompt.
    Per-turn content is capped at HISTORY_TURN_CHAR_CAP with an ellipsis
    suffix; what's stored on disk is unchanged so /reset and any future
    export still see the full text."""
    from link_project_to_chat.conversation_log import HISTORY_TURN_CHAR_CAP

    huge = "x" * (HISTORY_TURN_CHAR_CAP * 5)
    block = format_history_block([(USER_ROLE, huge)])
    # The rendered user line is bounded.
    user_line = next(line for line in block.splitlines() if line.startswith("user:"))
    assert len(user_line) <= HISTORY_TURN_CHAR_CAP + len("user: ")
    # And carries a marker so the agent knows it was truncated.
    assert "truncated" in user_line


def test_clear_on_empty_log_returns_zero(tmp_path):
    log = _log(tmp_path)
    assert log.clear(_chat()) == 0


def test_log_persists_across_instances(tmp_path):
    """The DB file lives on disk — re-opening the same path must surface
    prior turns. Prevents accidental in-memory state."""
    db_path = tmp_path / "conversations" / "proj.db"
    log = ConversationLog(db_path)
    chat = _chat()
    log.append(chat, USER_ROLE, "persisted")

    log2 = ConversationLog(db_path)
    assert log2.recent(chat) == [(USER_ROLE, "persisted")]
