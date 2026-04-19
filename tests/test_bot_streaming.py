from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from link_project_to_chat.livestream import LiveMessage
from link_project_to_chat.stream import TextDelta, ThinkingDelta
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType


@dataclass
class FakeMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    next_id: int = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        mid = self.next_id
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id, "mid": mid, **kw})
        return FakeMessage(message_id=mid)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kw})


def _fake_task(task_id: int = 1) -> Task:
    t = Task.__new__(Task)
    t.id = task_id
    t.chat_id = 99
    t.message_id = 7
    t.status = TaskStatus.RUNNING
    t.type = TaskType.CLAUDE
    t.result = ""
    t.error = None
    t.pending_questions = []
    t._compact = False
    return t


async def _stub_bot(show_thinking: bool = False):
    """Construct a minimal ProjectBot-like object just for the stream event tests."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot.__new__(ProjectBot)
    bot._app = SimpleNamespace(bot=FakeBot())
    bot._typing_tasks = {}
    bot._live_text = {}
    bot._live_thinking = {}
    bot._thinking_buf = {}
    bot._thinking_store = {}
    bot._voice_tasks = set()
    bot.show_thinking = show_thinking
    return bot


@pytest.mark.asyncio
async def test_text_delta_starts_live_message():
    bot = await _stub_bot()
    task = _fake_task()
    await bot._on_stream_event(task, TextDelta(text="hello "))
    await bot._on_stream_event(task, TextDelta(text="world"))
    # A LiveMessage exists for the task.
    assert task.id in bot._live_text
    live = bot._live_text[task.id]
    # The first delta triggered start() which sent the placeholder.
    assert len(bot._app.bot.sent) == 1
    # The buffer contains both deltas.
    assert live._buffer == "hello world"


@pytest.mark.asyncio
async def test_thinking_delta_with_toggle_on_streams_separate_message():
    bot = await _stub_bot(show_thinking=True)
    task = _fake_task(task_id=2)
    await bot._on_stream_event(task, ThinkingDelta(text="first thought"))
    assert task.id in bot._live_thinking
    # The first thinking delta produces its own separate placeholder send.
    assert len(bot._app.bot.sent) == 1
    assert bot._app.bot.sent[0]["text"].startswith("💭 ")
    # `_thinking_buf` is NOT used when live thinking is on.
    assert task.id not in bot._thinking_buf


@pytest.mark.asyncio
async def test_thinking_delta_with_toggle_off_uses_buffer():
    bot = await _stub_bot(show_thinking=False)
    task = _fake_task(task_id=3)
    await bot._on_stream_event(task, ThinkingDelta(text="step 1"))
    await bot._on_stream_event(task, ThinkingDelta(text="step 2"))
    assert task.id not in bot._live_thinking
    assert bot._thinking_buf[task.id] == "step 1\n\nstep 2"
    # No Telegram messages were sent for thinking.
    assert len(bot._app.bot.sent) == 0
