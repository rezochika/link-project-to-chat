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


@pytest.mark.asyncio
async def test_first_group_message_captures_chat_id(tmp_path, monkeypatch):
    """When group_chat_id=0 (sentinel), a trusted-user message captures the actual chat_id."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_999
    update.effective_message.text = "@acme_manager hi"
    update.effective_message.from_user = MagicMock(is_bot=False, username="rezoc666")
    update.effective_message.reply_to_message = None
    update.effective_message.parse_entities = MagicMock(return_value={})
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    update.effective_chat = MagicMock(id=-100_999)
    ctx = MagicMock()
    ctx.user_data = {}

    await bot._on_text(update, ctx)

    # Capture happened
    assert captured == [("acme", {"group_chat_id": -100_999})]
    assert bot.group_chat_id == -100_999


@pytest.mark.asyncio
async def test_unauth_user_does_not_trigger_capture(tmp_path, monkeypatch):
    """An unauthenticated message must NOT capture the chat_id."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=False)  # unauthorized

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_999
    update.effective_message.text = "@acme_manager hi"
    update.effective_message.from_user = MagicMock(is_bot=False, username="randoc")
    update.effective_message.reply_to_message = None
    update.effective_message.parse_entities = MagicMock(return_value={})
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=99999)
    update.effective_chat = MagicMock(id=-100_999)
    ctx = MagicMock()
    ctx.user_data = {}

    await bot._on_text(update, ctx)

    assert captured == []
    assert bot.group_chat_id == 0  # unchanged


@pytest.mark.asyncio
async def test_second_message_after_capture_routes_normally(tmp_path, monkeypatch):
    """After chat_id is captured, subsequent messages from the same group should NOT re-trigger capture."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    def _make_update(chat_id):
        u = MagicMock()
        u.effective_message = MagicMock()
        u.effective_message.chat_id = chat_id
        u.effective_message.text = "@acme_manager hi"
        u.effective_message.from_user = MagicMock(is_bot=False, username="rezoc666")
        u.effective_message.reply_to_message = None
        u.effective_message.parse_entities = MagicMock(return_value={})
        u.effective_message.reply_text = AsyncMock()
        u.effective_user = MagicMock(id=12345)
        u.effective_chat = MagicMock(id=chat_id)
        return u, MagicMock(user_data={})

    # First message captures.
    u1, c1 = _make_update(-100_999)
    await bot._on_text(u1, c1)
    assert captured == [("acme", {"group_chat_id": -100_999})]
    assert bot.group_chat_id == -100_999

    # Second message: must NOT re-trigger capture.
    u2, c2 = _make_update(-100_999)
    await bot._on_text(u2, c2)
    assert captured == [("acme", {"group_chat_id": -100_999})]  # still only one entry


@pytest.mark.asyncio
async def test_message_from_other_group_after_capture_rejected(tmp_path, monkeypatch):
    """After chat_id is captured, a message from a DIFFERENT group is silently rejected."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,  # already captured
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)

    captured = []
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", lambda *a, **k: captured.append(a))

    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.chat_id = -100_222  # wrong group
    update.effective_message.text = "@acme_manager hi"
    update.effective_message.from_user = MagicMock(is_bot=False, username="rezoc666")
    update.effective_message.reply_to_message = None
    update.effective_message.parse_entities = MagicMock(return_value={})
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    update.effective_chat = MagicMock(id=-100_222)
    ctx = MagicMock(user_data={})

    await bot._on_text(update, ctx)

    # No capture should happen, no reply should be sent
    assert captured == []
    update.effective_message.reply_text.assert_not_called()
