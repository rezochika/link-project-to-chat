from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# --- pure helpers ---


def test_find_peer_mention_returns_peer_when_addressed():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    text = "@acme_dev_bot please implement models.py"
    result = find_peer_mention(text, self_username="acme_mgr_bot", team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"})
    assert result == "acme_dev_bot"


def test_find_peer_mention_case_insensitive():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    result = find_peer_mention(
        "Hey @ACME_Dev_Bot ready to go",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result == "acme_dev_bot"


def test_find_peer_mention_returns_none_when_self_mention():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    result = find_peer_mention(
        "I, @acme_mgr_bot, have finished my review",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result is None


def test_find_peer_mention_returns_none_when_no_peer_mention():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    result = find_peer_mention(
        "Here is my update",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result is None


def test_is_relayed_text_detects_prefix():
    from link_project_to_chat.manager.team_relay import is_relayed_text

    assert is_relayed_text("[auto-relay from acme_mgr_bot]\n\nHello @acme_dev_bot") is True
    assert is_relayed_text("@acme_dev_bot ready") is False
    assert is_relayed_text("") is False


def test_relay_prefix_has_no_at_sign_to_avoid_self_mention():
    """The sender's name in the prefix must NOT be @mention-parseable.

    Telegram parses any `@handle` in plain text as a mention entity. If the
    prefix included `@sender`, that bot would read its own relayed message as
    "addressed to me" and reply to itself — a self-reply feedback loop.
    Regression for the bug captured in the 2026-04-20 chat export.
    """
    from link_project_to_chat.manager.team_relay import _RELAY_PREFIX

    assert "@" not in _RELAY_PREFIX


# --- TeamRelay routing ---


def _fake_sender(username: str, is_bot: bool):
    s = MagicMock()
    s.username = username
    s.bot = is_bot
    return s


async def _mk_event(text: str, sender_username: str, sender_is_bot: bool, chat_id: int | None = -100_111):
    event = MagicMock()
    event.message = MagicMock()
    event.message.message = text
    event.message.chat_id = chat_id
    event.get_sender = AsyncMock(return_value=_fake_sender(sender_username, sender_is_bot))
    return event


@pytest.mark.asyncio
async def test_relay_ignores_user_messages():
    """A real user's message is not relayed — only bot-to-bot traffic needs the bridge."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.add_event_handler = MagicMock(return_value="handler")
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement X",
        sender_username="rezoc666",
        sender_is_bot=False,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_ignores_bot_messages_not_addressing_peer():
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "PRD is drafted — standby for handoff",  # no @ mention
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_ignores_already_relayed_text():
    """A relayed message must not be re-relayed (loop guard)."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "[auto-relay from acme_mgr_bot]\n\n@acme_dev_bot implement X",
        sender_username="some_other_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_forwards_bot_to_bot_handoff():
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement src/models.py per docs/architecture.md §2",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_awaited_once()
    args, _ = client.send_message.call_args
    chat_id, text = args
    assert chat_id == -100_111
    assert text.startswith("[auto-relay from acme_mgr_bot]")
    # Regression: the prefix must not contain '@sender' — that re-entered the
    # sending bot's mention filter and caused a self-reply loop.
    assert "@acme_mgr_bot" not in text.split("\n\n", 1)[0]
    assert "@acme_dev_bot" in text
    assert "src/models.py" in text


@pytest.mark.asyncio
async def test_relay_ignores_message_from_unknown_bot():
    """A third-party bot's message shouldn't trigger a relay even if it @mentions a team bot."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot try this",
        sender_username="random_3rd_party_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()
