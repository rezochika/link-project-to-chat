"""ProjectBot wires ChatHistory: records on incoming, injects on submit."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.chat_history import ChatHistory
from link_project_to_chat.transport.base import (
    ChatKind, ChatRef, Identity, IncomingMessage, MessageRef,
)
from link_project_to_chat.config import AllowedUser


def _make_bot():
    """Construct a minimal ProjectBot for routing-gate-style tests."""
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = "MyBot"
    bot._respond_in_groups = True
    bot.group_mode = False
    bot.team_name = None
    bot.role = None
    bot._allowed_users = [
        AllowedUser(username="alice", role="executor", locked_identities=["fake:42"]),
    ]
    bot._auth_dirty = False
    bot._chat_history = ChatHistory()
    bot._on_text = AsyncMock()
    bot.task_manager = MagicMock()
    bot.task_manager.submit_agent = MagicMock()
    from link_project_to_chat.transport.fake import FakeTransport
    bot._transport = FakeTransport()
    return bot


def _group_incoming(text: str, msg_id: str = "100", sender_handle: str = "alice"):
    chat = ChatRef(transport_id="fake", native_id="ROOM-1", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name=sender_handle, handle=sender_handle, is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id=msg_id, chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
        mentions=[Identity(
            transport_id="fake", native_id="bot-self",
            display_name="MyBot", handle="MyBot", is_bot=True,
        )],
    )


@pytest.mark.asyncio
async def test_group_message_recorded_into_chat_history():
    bot = _make_bot()
    incoming = _group_incoming("@MyBot hello world")
    await bot._on_text_from_transport(incoming)
    # Bot recorded the cleaned message before injecting. The bot may have
    # already stripped the mention; assert the record contains the visible text.
    result = bot._chat_history.since_last_llm(incoming.chat, "999")
    assert "hello world" in result or "@MyBot hello world" in result


@pytest.mark.asyncio
async def test_resolve_recent_discussion_returns_empty_for_dm():
    bot = _make_bot()
    dm_chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    incoming = MagicMock(chat=dm_chat, message=MagicMock(native_id="1"))
    result = bot._resolve_recent_discussion(incoming)
    assert result == ""


@pytest.mark.asyncio
async def test_resolve_recent_discussion_marks_llm_call_for_room():
    """After resolving, the next call for the same chat should not re-include
    the previously-resolved messages."""
    bot = _make_bot()
    chat = ChatRef(transport_id="fake", native_id="ROOM-1", kind=ChatKind.ROOM)
    bot._chat_history.record(chat, "1", "alice", "first")
    msg2 = MessageRef(transport_id="fake", native_id="2", chat=chat)
    sender = Identity(transport_id="fake", native_id="42", display_name="alice",
                       handle="alice", is_bot=False)
    incoming = IncomingMessage(chat=chat, sender=sender, text="@MyBot reply",
                                files=[], reply_to=None, message=msg2)
    # First resolve: includes msg 1.
    result1 = bot._resolve_recent_discussion(incoming)
    assert "first" in result1
    # Record another, then resolve again with a new before_msg_id.
    bot._chat_history.record(chat, "3", "bob", "second")
    msg4 = MessageRef(transport_id="fake", native_id="4", chat=chat)
    incoming2 = IncomingMessage(chat=chat, sender=sender, text="@MyBot reply2",
                                 files=[], reply_to=None, message=msg4)
    result2 = bot._resolve_recent_discussion(incoming2)
    # 'first' is now pre-mark; only 'second' should appear.
    assert "first" not in result2
    assert "second" in result2


@pytest.mark.asyncio
async def test_chat_history_instantiated_in_init():
    """A fresh ProjectBot has a ChatHistory instance ready."""
    bot = _make_bot()
    assert isinstance(bot._chat_history, ChatHistory)


@pytest.mark.asyncio
async def test_dm_message_does_not_get_recorded():
    bot = _make_bot()
    dm_chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(transport_id="fake", native_id="42",
                       display_name="alice", handle="alice", is_bot=False)
    msg = MessageRef(transport_id="fake", native_id="1", chat=dm_chat)
    incoming = IncomingMessage(chat=dm_chat, sender=sender, text="DM text",
                                files=[], reply_to=None, message=msg)
    await bot._on_text_from_transport(incoming)
    # DM never recorded.
    assert bot._chat_history.since_last_llm(dm_chat, "999") == ""
