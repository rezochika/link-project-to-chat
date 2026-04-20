from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.transport import ChatKind, ChatRef


def _mk_task(chat_id: int, error: str) -> Task:
    """Build a finalized Task in error state with the given error message."""
    t = MagicMock(spec=Task)
    t.id = 1
    t.chat_id = chat_id
    t.message_id = 100
    t.status = TaskStatus.FAILED
    t.error = error
    t.result = ""
    t.type = TaskType.CLAUDE
    t._compact = False
    return t


def _chat_ref(chat_id: int) -> ChatRef:
    """Build a Telegram ChatRef matching what _chat_ref_for_task produces in group mode."""
    return ChatRef(transport_id="telegram", native_id=str(chat_id), kind=ChatKind.ROOM)


@pytest.mark.asyncio
async def test_cap_marker_halts_group_and_announces(tmp_path):
    """When a Claude task fails with USAGE_CAP: marker, the bot halts the group and posts a pause message."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot._send_to_chat = AsyncMock()
    bot._schedule_cap_probe = MagicMock()  # don't actually start a probe
    task = _mk_task(chat_id=-100_111, error="USAGE_CAP: Rate limit exceeded.")
    await bot._finalize_claude_task(task)
    # Group is halted
    assert bot._group_state.get(_chat_ref(-100_111)).halted is True
    # Pause message sent
    bot._send_to_chat.assert_called_once()
    sent_text = bot._send_to_chat.call_args[0][1]
    assert "usage cap" in sent_text.lower() or "pausing" in sent_text.lower()
    # Probe scheduled
    bot._schedule_cap_probe.assert_called_once_with(_chat_ref(-100_111))


@pytest.mark.asyncio
async def test_cap_marker_in_solo_mode_falls_through_to_normal_error(tmp_path):
    """In solo (non-team) mode, the cap marker is treated as a normal error (no halt, no probe)."""
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    bot._send_to_chat = AsyncMock()
    bot._schedule_cap_probe = MagicMock()
    task = _mk_task(chat_id=12345, error="USAGE_CAP: Rate limit exceeded.")
    await bot._finalize_claude_task(task)
    # No halt (no group_state for solo bot interaction)
    bot._send_to_chat.assert_called_once()
    sent_text = bot._send_to_chat.call_args[0][1]
    assert sent_text.startswith("Error:")
    bot._schedule_cap_probe.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_error_falls_through_normally(tmp_path):
    """Non-cap errors should follow the existing 'Error: ...' path."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot._send_to_chat = AsyncMock()
    bot._schedule_cap_probe = MagicMock()
    task = _mk_task(chat_id=-100_111, error="Something else broke")
    await bot._finalize_claude_task(task)
    assert bot._group_state.get(_chat_ref(-100_111)).halted is False
    bot._send_to_chat.assert_called_once()
    sent_text = bot._send_to_chat.call_args[0][1]
    assert sent_text.startswith("Error:")
    bot._schedule_cap_probe.assert_not_called()


def test_is_usage_cap_error_detects_marker():
    from link_project_to_chat.claude_client import is_usage_cap_error
    assert is_usage_cap_error("USAGE_CAP: rate limit") is True
    assert is_usage_cap_error("Error: USAGE_CAP: rate limit") is False  # only prefix-matched
    assert is_usage_cap_error("you've reached your usage") is True  # via _detect_usage_cap
    assert is_usage_cap_error("ordinary error") is False
    assert is_usage_cap_error(None) is False
    assert is_usage_cap_error("") is False


@pytest.mark.asyncio
async def test_schedule_cap_probe_retains_task_reference(tmp_path, monkeypatch):
    """Verifies the probe task is retained on self._probe_tasks (GC safety)."""
    # Capture the real sleep before patching so the test itself can yield to the loop.
    real_sleep = asyncio.sleep

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot._group_state.halt(_chat_ref(-100_111))

    # Make _probe exit immediately by patching asyncio.sleep to raise out.
    async def fake_sleep(_): raise asyncio.CancelledError
    monkeypatch.setattr("link_project_to_chat.bot.asyncio.sleep", fake_sleep)

    bot._schedule_cap_probe(_chat_ref(-100_111), interval_s=1)
    # Immediately after scheduling, exactly one task is held.
    assert len(bot._probe_tasks) == 1
    # Wait for it to die from CancelledError; done_callback should clean it up.
    await real_sleep(0)  # let event loop run
    # Now it's removed via add_done_callback
    # (May still be in set briefly; allow one more loop step)
    for _ in range(5):
        if not bot._probe_tasks:
            break
        await real_sleep(0)
    assert len(bot._probe_tasks) == 0
