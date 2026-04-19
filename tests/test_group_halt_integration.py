from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.group_state import GroupStateRegistry


def test_cap_message_fires_only_once():
    """Once halted, subsequent bot-to-bot messages should be ignored silently."""
    reg = GroupStateRegistry(max_bot_rounds=2)
    reg.note_bot_to_bot(-1)
    reg.note_bot_to_bot(-1)  # halts
    assert reg.get(-1).halted is True
    reg.note_bot_to_bot(-1)
    assert reg.get(-1).halted is True


def test_resume_via_user_message_clears_halt():
    """A user message after halt should clear it and reset the counter (uses resume())."""
    reg = GroupStateRegistry(max_bot_rounds=3)
    for _ in range(3):
        reg.note_bot_to_bot(-1)
    assert reg.get(-1).halted is True
    reg.resume(-1)
    s = reg.get(-1)
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


def _mk_update(chat_id: int, text: str, sender_username: str, sender_is_bot: bool):
    """Build a MagicMock update for _on_text. Returns (update, ctx)."""
    update = MagicMock()
    msg = update.effective_message
    msg.chat_id = chat_id
    msg.text = text
    msg.from_user = MagicMock(is_bot=sender_is_bot, username=sender_username)
    msg.reply_to_message = None
    # parse_entities returns dict[Entity, str]; build one mention entity that matches @acme_manager
    entity = MagicMock()
    entity.type = "mention"
    msg.parse_entities = MagicMock(return_value={entity: "@acme_manager"})
    msg.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    update.effective_chat = MagicMock(id=chat_id)
    ctx = MagicMock()
    ctx.user_data = {}
    return update, ctx


@pytest.mark.asyncio
async def test_on_text_emits_cap_message_when_round_limit_tripped(tmp_path):
    """When the bot-to-bot counter trips the cap, _on_text should reply with the auto-pause message exactly once."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    # Two bot-to-bot messages — the second trips the cap.
    update1, ctx1 = _mk_update(-100_111, "@acme_manager hello", sender_username="acme_dev", sender_is_bot=True)
    update2, ctx2 = _mk_update(-100_111, "@acme_manager hello again", sender_username="acme_dev", sender_is_bot=True)
    await bot._on_text(update1, ctx1)
    await bot._on_text(update2, ctx2)
    # Second update should have produced exactly one reply containing "Auto-paused".
    update2.effective_message.reply_text.assert_called_once()
    reply_text = update2.effective_message.reply_text.call_args[0][0]
    assert "Auto-paused" in reply_text
    assert "2 bot-to-bot rounds" in reply_text


@pytest.mark.asyncio
async def test_on_text_silently_drops_bot_messages_after_cap(tmp_path):
    """Once halted, additional bot-to-bot messages must not produce any reply."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    # Trip the cap.
    for _ in range(2):
        u, c = _mk_update(-100_111, "@acme_manager x", sender_username="acme_dev", sender_is_bot=True)
        await bot._on_text(u, c)
    # Now send a third bot message.
    u3, c3 = _mk_update(-100_111, "@acme_manager x", sender_username="acme_dev", sender_is_bot=True)
    await bot._on_text(u3, c3)
    u3.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_on_text_human_message_resumes_halted_group(tmp_path):
    """A trusted-user message in a halted group should clear the halt (group_state.resume)."""
    bot = _mk_bot(tmp_path, max_rounds=2)
    # Manually halt
    bot._group_state.halt(-100_111)
    assert bot._group_state.get(-100_111).halted is True
    # Trusted-user message arrives (not a bot)
    u, c = _mk_update(-100_111, "@acme_manager hi", sender_username="rezoc666", sender_is_bot=False)
    # Auth path will likely reject (no allowed_username configured), but resume() must still happen first.
    await bot._on_text(u, c)
    assert bot._group_state.get(-100_111).halted is False
    assert bot._group_state.get(-100_111).bot_to_bot_rounds == 0


@pytest.mark.asyncio
async def test_halt_command_sets_halted_in_registry(tmp_path):
    bot = _mk_bot(tmp_path, max_rounds=20)
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_chat = MagicMock(id=-100_111)
    update.effective_user = MagicMock(id=12345)
    update.effective_message.reply_text = AsyncMock()
    # Stub auth to allow this user
    bot._auth = MagicMock(return_value=True)
    await bot._on_halt(update, MagicMock())
    assert bot._group_state.get(-100_111).halted is True
    update.effective_message.reply_text.assert_called_once()
    assert "Halted" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_resume_command_clears_halt(tmp_path):
    bot = _mk_bot(tmp_path, max_rounds=20)
    bot._group_state.halt(-100_111)
    bot._group_state.get(-100_111).bot_to_bot_rounds = 5
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_chat = MagicMock(id=-100_111)
    update.effective_user = MagicMock(id=12345)
    update.effective_message.reply_text = AsyncMock()
    bot._auth = MagicMock(return_value=True)
    await bot._on_resume(update, MagicMock())
    s = bot._group_state.get(-100_111)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0
    update.effective_message.reply_text.assert_called_once()
    assert "Resumed" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_halt_in_solo_mode_rejects(tmp_path):
    """Solo (non-team) bots should reject /halt with a helpful message."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(name="solo", path=tmp_path, token="t")  # no team_name → group_mode=False
    bot._auth = MagicMock(return_value=True)
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    await bot._on_halt(update, MagicMock())
    update.effective_message.reply_text.assert_called_once()
    assert "group mode" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_resume_in_solo_mode_rejects(tmp_path):
    """Solo (non-team) bots should reject /resume with a helpful message."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    bot._auth = MagicMock(return_value=True)
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    await bot._on_resume(update, MagicMock())
    update.effective_message.reply_text.assert_called_once()
    assert "group mode" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_halt_from_wrong_group_silently_ignored(tmp_path):
    """A /halt sent from a chat_id != self.group_chat_id should be silently ignored."""
    bot = _mk_bot(tmp_path, max_rounds=20)  # bot.group_chat_id == -100_111
    bot._auth = MagicMock(return_value=True)
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_chat = MagicMock(id=-100_222)  # wrong group
    update.effective_user = MagicMock(id=12345)
    await bot._on_halt(update, MagicMock())
    # Should NOT have set halt; should NOT have replied
    assert bot._group_state.get(-100_111).halted is False
    assert bot._group_state.get(-100_222).halted is False
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_resume_from_wrong_group_silently_ignored(tmp_path):
    """A /resume sent from wrong chat_id should be silently ignored."""
    bot = _mk_bot(tmp_path, max_rounds=20)  # bot.group_chat_id == -100_111
    bot._auth = MagicMock(return_value=True)
    bot._group_state.halt(-100_111)  # halt the correct group first
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_chat = MagicMock(id=-100_222)
    update.effective_user = MagicMock(id=12345)
    await bot._on_resume(update, MagicMock())
    # The correct group's halt should still be set; no reply
    assert bot._group_state.get(-100_111).halted is True
    update.effective_message.reply_text.assert_not_called()
