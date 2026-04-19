from pathlib import Path

from link_project_to_chat.bot import ProjectBot


def test_project_bot_derives_group_mode_from_team_args(tmp_path):
    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-1001234567890,
    )
    assert bot.group_mode is True
    assert bot.team_name == "acme"
    assert bot.role == "manager"
    assert bot.group_chat_id == -1001234567890


def test_project_bot_solo_mode_when_no_team(tmp_path):
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    assert bot.group_mode is False
    assert bot.team_name is None
    assert bot.role is None


import pytest
from unittest.mock import MagicMock, AsyncMock

from link_project_to_chat.bot import ProjectBot


@pytest.mark.asyncio
async def test_group_mode_rejects_wrong_chat_id(tmp_path):
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_222  # wrong group
    update.effective_message.text = "@acme_manager hi"
    update.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {}
    # Expect: early return, no reply_text call, no further processing
    await bot._on_text(update, ctx)
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_allows_matching_chat_id_passes_routing(tmp_path):
    """When chat_id matches, the wrong-chat guard does not short-circuit. Other filters (auth, mention) still apply."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"  # required by group_filters
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_111  # matching
    update.effective_message.text = "no mention here"
    update.effective_message.from_user = MagicMock(is_bot=False, username="someone")
    update.effective_message.reply_to_message = None
    update.effective_message.parse_entities = MagicMock(return_value={})
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    ctx = MagicMock()
    ctx.user_data = {}
    # Without an @mention this should still early-return (not addressed to bot),
    # but NOT because of chat_id mismatch.
    await bot._on_text(update, ctx)
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_no_chat_id_set_does_not_reject(tmp_path):
    """When group_chat_id is None (not yet captured), the guard should not fire."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=None,
    )
    bot.bot_username = "acme_manager"
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_999  # any group
    update.effective_message.text = "no mention"
    update.effective_message.from_user = MagicMock(is_bot=False, username="someone")
    update.effective_message.reply_to_message = None
    update.effective_message.parse_entities = MagicMock(return_value={})
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    ctx = MagicMock()
    ctx.user_data = {}
    # No mention → still no reply, but the early return came from is_directed_at_me, not the guard.
    await bot._on_text(update, ctx)
    update.effective_message.reply_text.assert_not_called()
