"""Solo project bot in Telegram group — routing gate tests.

Verifies the `respond_in_groups=True` elif branch in
ProjectBot._on_text_from_transport:
  - mention → process
  - reply-to-bot → process
  - drive-by → silent
  - self → silent
  - peer bot → silent
  - mention-strip happens before _on_text
  - DMs still work (filter is PRIVATE | GROUPS, not GROUPS only)
  - captioned file with @mention → process
  - DM behavior unchanged when flag is False
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot(*, respond_in_groups: bool, bot_username: str = "MyBot"):
    """Build a minimal ProjectBot suitable for routing-gate tests.

    Bypasses __init__ via __new__ and sets only the fields the gate touches.
    Stubs _on_text so tests can assert called-with cleanly.
    """
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = bot_username
    bot._respond_in_groups = respond_in_groups
    bot.group_mode = False
    bot.team_name = None
    bot.role = None
    bot._allowed_users = [AllowedUser(username="alice", role="executor",
                                       locked_identities=["fake:42"])]
    bot._auth_dirty = False
    bot._on_text = AsyncMock()
    bot._transport = FakeTransport()
    return bot


def _make_group_incoming(
    text: str,
    *,
    sender_handle: str = "alice",
    sender_id: str = "42",
    sender_is_bot: bool = False,
    mentions: list[Identity] | None = None,
    reply_to_sender: Identity | None = None,
    files: list[IncomingFile] | None = None,
) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="100", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id=sender_id,
        display_name=sender_handle, handle=sender_handle, is_bot=sender_is_bot,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=files or [],
        reply_to=None, message=msg,
        reply_to_sender=reply_to_sender,
        mentions=mentions or [],
    )


def _make_dm_incoming(text: str) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
    )


def _bot_mention(handle: str = "MyBot") -> Identity:
    return Identity(
        transport_id="fake", native_id="bot-self",
        display_name=handle, handle=handle, is_bot=True,
    )


@pytest.mark.asyncio
async def test_group_mention_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # Mention stripped before reaching _on_text.
    assert forwarded.text == " do X"


@pytest.mark.asyncio
async def test_group_reply_to_bot_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "follow-up question",
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # No mention to strip; text unchanged.
    assert forwarded.text == "follow-up question"


@pytest.mark.asyncio
async def test_group_drive_by_message_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming("chatter between humans")
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_self_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "any text",
        sender_handle="MyBot",  # same as bot_username
        sender_is_bot=True,
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_peer_bot_with_mention_is_silent():
    """Peer-bot defense: solo bot in group must NEVER respond to another bot,
    even when @mentioned. Avoids bot-to-bot loops; team workflows are
    explicitly opt-in via team mode."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot ping",
        sender_handle="OtherBot",
        sender_is_bot=True,
        mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_captioned_file_with_mention_reaches_on_text():
    """Captioned files: caption rides on incoming.text per
    TelegramTransport._dispatch_message. Same @mention gate applies."""
    bot = _make_bot(respond_in_groups=True)
    files = [IncomingFile(
        path=Path("/tmp/x.png"),
        original_name="x.png",
        mime_type="image/png",
        size_bytes=1024,
    )]
    incoming = _make_group_incoming(
        "@MyBot analyze this",
        mentions=[_bot_mention("MyBot")],
        files=files,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.files == files
    assert forwarded.text == " analyze this"


@pytest.mark.asyncio
async def test_group_text_when_flag_is_false_is_silent_via_filter():
    """With respond_in_groups=False, group messages don't reach _on_text
    even if they would have been addressed-at-me. (In production this is
    enforced by the PTB filter — here we verify the bot-side gate also
    refuses to process them defensively.)"""
    bot = _make_bot(respond_in_groups=False)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_message_unaffected_by_flag():
    """DM messages reach _on_text regardless of respond_in_groups setting."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_dm_incoming("just a DM")
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # DM text isn't mention-stripped (no @mention typically).
    assert forwarded.text == "just a DM"


@pytest.mark.asyncio
async def test_group_reply_to_bot_with_other_mention_is_silent():
    """Reply to bot + simultaneously @-mentions someone else → silent.
    Matches team-mode semantics in is_directed_at_me."""
    bot = _make_bot(respond_in_groups=True)
    other = Identity(
        transport_id="fake", native_id="999",
        display_name="OtherUser", handle="OtherUser", is_bot=False,
    )
    incoming = _make_group_incoming(
        "@OtherUser the bot said X",
        mentions=[other],  # mentions OtherUser, NOT MyBot
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_empty_text_after_strip_falls_through_silently():
    """User types just '@MyBot' with nothing else. After stripping the bot
    is responsible for whatever _on_text does with empty text — typically
    early-returns. Verify _on_text receives the empty-text incoming and
    handles it (early-return semantics are owned by _on_text itself)."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    # _on_text IS called — it's _on_text's responsibility to early-return
    # on empty text. The gate's job is only to filter not-addressed-at-me.
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.text == ""
