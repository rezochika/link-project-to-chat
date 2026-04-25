from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.group_state import GroupStateRegistry
from link_project_to_chat.transport import (
    ChatKind, ChatRef, CommandInvocation, Identity, IncomingMessage, MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _halt_ci(chat: ChatRef, *, sender_uid: int = 12345, handle: str = "rezo") -> CommandInvocation:
    """Build a CommandInvocation for /halt + /resume tests."""
    sender = Identity(
        transport_id=chat.transport_id, native_id=str(sender_uid),
        display_name=handle, handle=handle, is_bot=False,
    )
    return CommandInvocation(
        chat=chat,
        sender=sender,
        name="halt",
        args=[],
        raw_text="/halt",
        message=MessageRef(transport_id=chat.transport_id, native_id="1", chat=chat),
    )


def _team_bot_with_fake_transport(bot: ProjectBot) -> ProjectBot:
    """Replace a team ProjectBot's _transport with a FakeTransport for assertion."""
    bot._transport = FakeTransport()
    return bot


def _group_chat(chat_id: int) -> ChatRef:
    # transport_id="telegram" because the tests model Telegram-bound bots
    # (constructed with the legacy `group_chat_id: int` kwarg, which synthesizes
    # a Telegram-flavored RoomBinding). The bot's _transport is FakeTransport
    # for assertion convenience; ChatRef.transport_id is independent metadata.
    return ChatRef(transport_id="telegram", native_id=str(chat_id), kind=ChatKind.ROOM)


def _telegram_group_chat(chat_id: int) -> ChatRef:
    """Mirror the ChatRef that chat_ref_from_telegram() builds from a MagicMock chat.

    The /halt and /resume command handlers run `chat_ref_from_telegram(update.effective_chat)`
    which produces transport_id="telegram". Setup code that pre-halts the registry for
    those tests must use the same key.
    """
    return ChatRef(transport_id="telegram", native_id=str(chat_id), kind=ChatKind.ROOM)


def _sender_identity(uid: int, handle: str, is_bot: bool) -> Identity:
    return Identity(
        transport_id="telegram", native_id=str(uid),
        display_name=handle, handle=handle, is_bot=is_bot,
    )


def _group_incoming(
    chat: ChatRef,
    text: str,
    *,
    sender_uid: int = 1,
    sender_handle: str = "rezo",
    sender_is_bot: bool = False,
    is_relayed: bool = False,
    reply_to_bot_username: str | None = None,
) -> IncomingMessage:
    reply_to = None
    reply_to_sender = None
    if reply_to_bot_username:
        reply_to = MessageRef(transport_id="telegram", native_id="0", chat=chat)
        reply_to_sender = Identity(
            transport_id="telegram", native_id="0",
            display_name=reply_to_bot_username,
            handle=reply_to_bot_username, is_bot=True,
        )
    return IncomingMessage(
        chat=chat,
        sender=_sender_identity(uid=sender_uid, handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=None,
        is_relayed_bot_to_bot=is_relayed,
        message=MessageRef(transport_id="telegram", native_id="1", chat=chat),
        reply_to_sender=reply_to_sender,
    )


def test_cap_message_fires_only_once():
    """Once halted, subsequent bot-to-bot messages should be ignored silently."""
    reg = GroupStateRegistry(max_bot_rounds=2)
    chat = _group_chat(-1)
    reg.note_bot_to_bot(chat)
    reg.note_bot_to_bot(chat)  # halts
    assert reg.get(chat).halted is True
    reg.note_bot_to_bot(chat)
    assert reg.get(chat).halted is True


def test_resume_via_user_message_clears_halt():
    """A user message after halt should clear it and reset the counter (uses resume())."""
    reg = GroupStateRegistry(max_bot_rounds=3)
    chat = _group_chat(-1)
    for _ in range(3):
        reg.note_bot_to_bot(chat)
    assert reg.get(chat).halted is True
    reg.resume(chat)
    s = reg.get(chat)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def _mk_bot(tmp_path: Path, max_rounds: int = 3) -> ProjectBot:
    """Construct a ProjectBot in team mode and rewire its registry to the requested cap."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"
    bot._group_state = GroupStateRegistry(max_bot_rounds=max_rounds)
    return bot


@pytest.mark.asyncio
async def test_on_text_emits_cap_message_when_round_limit_tripped(tmp_path):
    """When the bot-to-bot counter trips the cap, the transport should send the
    auto-pause message exactly once."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()
    chat = _group_chat(-100_111)

    # Two bot-to-bot messages — the second trips the cap.
    incoming1 = _group_incoming(
        chat, "@acme_manager hello",
        sender_handle="acme_dev", sender_is_bot=True,
    )
    incoming2 = _group_incoming(
        chat, "@acme_manager hello again",
        sender_handle="acme_dev", sender_is_bot=True,
    )
    await bot._on_text_from_transport(incoming1)
    await bot._on_text_from_transport(incoming2)

    # Exactly one transport-side message — the cap notice.
    assert len(bot._transport.sent_messages) == 1
    sent = bot._transport.sent_messages[0]
    assert "Auto-paused" in sent.text
    assert "2 bot-to-bot rounds" in sent.text


@pytest.mark.asyncio
async def test_on_text_silently_drops_bot_messages_after_cap(tmp_path):
    """Once halted, additional bot-to-bot messages must not produce any reply."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()
    chat = _group_chat(-100_111)

    # Trip the cap.
    for _ in range(2):
        incoming = _group_incoming(
            chat, "@acme_manager x",
            sender_handle="acme_dev", sender_is_bot=True,
        )
        await bot._on_text_from_transport(incoming)
    # One auto-paused notice already sent.
    assert len(bot._transport.sent_messages) == 1

    # Now send a third bot message — must be silently dropped (no new transport sends).
    incoming3 = _group_incoming(
        chat, "@acme_manager x",
        sender_handle="acme_dev", sender_is_bot=True,
    )
    await bot._on_text_from_transport(incoming3)
    assert len(bot._transport.sent_messages) == 1  # unchanged


@pytest.mark.asyncio
async def test_on_text_human_message_resumes_halted_group(tmp_path):
    """A trusted-user message in a halted group should clear the halt (group_state.resume)."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    _team_bot_with_fake_transport(bot)
    chat = _group_chat(-100_111)
    # Manually halt
    bot._group_state.halt(chat)
    assert bot._group_state.get(chat).halted is True
    # Trusted-user message arrives (not a bot). Auth path will likely reject
    # (no allowed_username configured), but resume() must still happen first.
    incoming = _group_incoming(
        chat, "@acme_manager hi",
        sender_handle="rezoc666", sender_is_bot=False,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._group_state.get(chat).halted is False
    assert bot._group_state.get(chat).bot_to_bot_rounds == 0


@pytest.mark.asyncio
async def test_halt_command_sets_halted_in_registry(tmp_path):
    bot = _mk_bot(tmp_path, max_rounds=20)
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True

    chat = _group_chat(-100_111)
    await bot._on_halt(_halt_ci(chat))

    assert bot._group_state.get(chat).halted is True
    assert len(bot._transport.sent_messages) == 1
    assert "Halted" in bot._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_resume_command_clears_halt(tmp_path):
    bot = _mk_bot(tmp_path, max_rounds=20)
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True

    chat = _group_chat(-100_111)
    bot._group_state.halt(chat)
    bot._group_state.get(chat).bot_to_bot_rounds = 5

    await bot._on_resume(_halt_ci(chat))

    s = bot._group_state.get(chat)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0
    assert len(bot._transport.sent_messages) == 1
    assert "Resumed" in bot._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_halt_in_solo_mode_rejects(tmp_path):
    """Solo (non-team) bots should reject /halt with a helpful message."""
    bot = ProjectBot(name="solo", path=tmp_path, token="t")  # no team_name → group_mode=False
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True
    chat = _group_chat(-100_111)

    await bot._on_halt(_halt_ci(chat))

    assert len(bot._transport.sent_messages) == 1
    assert "group mode" in bot._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_resume_in_solo_mode_rejects(tmp_path):
    """Solo (non-team) bots should reject /resume with a helpful message."""
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True
    chat = _group_chat(-100_111)

    await bot._on_resume(_halt_ci(chat))

    assert len(bot._transport.sent_messages) == 1
    assert "group mode" in bot._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_halt_from_wrong_group_silently_ignored(tmp_path):
    """A /halt sent from a chat_id != self.group_chat_id should be silently ignored."""
    bot = _mk_bot(tmp_path, max_rounds=20)  # bot.group_chat_id == -100_111
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True

    wrong_chat = _group_chat(-100_222)
    await bot._on_halt(_halt_ci(wrong_chat))

    assert bot._group_state.get(_group_chat(-100_111)).halted is False
    assert bot._group_state.get(wrong_chat).halted is False
    assert bot._transport.sent_messages == []


@pytest.mark.asyncio
async def test_resume_from_wrong_group_silently_ignored(tmp_path):
    """A /resume sent from wrong chat_id should be silently ignored."""
    bot = _mk_bot(tmp_path, max_rounds=20)  # bot.group_chat_id == -100_111
    _team_bot_with_fake_transport(bot)
    bot._auth_identity = lambda _s: True
    correct_chat = _group_chat(-100_111)
    bot._group_state.halt(correct_chat)

    wrong_chat = _group_chat(-100_222)
    await bot._on_resume(_halt_ci(wrong_chat))

    assert bot._group_state.get(correct_chat).halted is True
    assert bot._transport.sent_messages == []


# ─────────────────────────────────────────────────────────────────────────────
# Q4-C regression tests
#
# Before the Q4-C fix, the relayed bot-to-bot path (is_relayed_bot_to_bot=True,
# sender = the trusted user posting on behalf of the peer bot) was treated as a
# user message, which RESET the round counter — so a runaway relay loop would
# never auto-halt. Q4-C makes both relayed bot-to-bot AND native bot senders
# (peer bots posting directly on non-Telegram transports) increment the counter
# and trip the cap at max_bot_rounds.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relayed_bot_to_bot_increments_round_counter(tmp_path):
    """Q4-C fix: relayed bot-to-bot messages (is_relayed_bot_to_bot=True)
    increment the round counter (previously reset it, per the v1 tradeoff)."""
    bot = _mk_bot(tmp_path, max_rounds=20)
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()
    chat = _group_chat(int(bot.group_chat_id))

    for _ in range(20):
        incoming = _group_incoming(
            chat,
            text="@acme_manager please continue",
            sender_handle="rezo",  # trusted user — that's who the relay posts as
            sender_is_bot=False,
            is_relayed=True,
        )
        await bot._on_text_from_transport(incoming)

    assert bot._group_state.get(chat).halted is True
    assert any("Auto-paused" in m.text for m in bot._transport.sent_messages), (
        "Q4-C: cap-tripping should emit the auto-pause message via transport"
    )


@pytest.mark.asyncio
async def test_native_bot_sender_increments_round_counter(tmp_path):
    """For non-Telegram transports: a sender with is_bot=True also increments."""
    bot = _mk_bot(tmp_path, max_rounds=20)
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()
    chat = _group_chat(int(bot.group_chat_id))

    for _ in range(20):
        incoming = _group_incoming(
            chat,
            text="@acme_manager please continue",
            sender_handle="acme_dev",  # peer bot
            sender_is_bot=True,
            is_relayed=False,
        )
        await bot._on_text_from_transport(incoming)

    assert bot._group_state.get(chat).halted is True
    assert any("Auto-paused" in m.text for m in bot._transport.sent_messages), (
        "Q4-C: cap-tripping should emit the auto-pause message via transport"
    )
