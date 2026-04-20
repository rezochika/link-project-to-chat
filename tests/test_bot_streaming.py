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


@pytest.mark.asyncio
async def test_finalize_with_live_text_does_not_resend():
    """Live-text path: keeps the accumulated buffer, edits in place, no new message sent."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=10)
    task.status = TaskStatus.DONE
    # task.result contains only the LAST assistant text block; the streamed buffer
    # has every text delta (narration + final). The finalized message must preserve
    # the buffer's content, not clobber it with task.result.
    task.result = "final answer"
    await bot._on_stream_event(task, TextDelta(text="narration before tool use"))
    await bot._on_stream_event(task, TextDelta(text=" — final answer"))
    sent_before = len(bot._app.bot.sent)

    await bot._finalize_claude_task(task)

    # No new send_message call — the live message was edited in place.
    assert len(bot._app.bot.sent) == sent_before
    assert task.id not in bot._live_text
    # Full streamed buffer preserved (narration survives, not just task.result).
    assert any("narration before tool use" in e["text"] for e in bot._app.bot.edits)


@pytest.mark.asyncio
async def test_finalize_with_empty_buffer_falls_back_to_task_result():
    """If the stream dropped before any deltas arrived, use task.result as the message body."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=11)
    task.status = TaskStatus.DONE
    task.result = "fallback answer"

    # Create an empty-buffer LiveMessage (no TextDelta ever fired).
    from link_project_to_chat.livestream import LiveMessage
    lm = LiveMessage(bot=bot._app.bot, chat_id=task.chat_id, reply_to_message_id=task.message_id)
    await lm.start()
    bot._live_text[task.id] = lm

    await bot._finalize_claude_task(task)
    # With empty buffer, the fallback kicks in and the final edit contains task.result.
    assert any("fallback answer" in e["text"] for e in bot._app.bot.edits)


@pytest.mark.asyncio
async def test_finalize_without_live_text_falls_back_to_send_to_chat():
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    sent_chats: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, reply_to=None):
        sent_chats.append((chat_id, text))

    bot._send_to_chat = fake_send
    task = _fake_task(task_id=11)
    task.status = TaskStatus.DONE
    task.result = "tool-only answer"

    await bot._finalize_claude_task(task)

    assert sent_chats == [(task.chat_id, "tool-only answer")]


@pytest.mark.asyncio
async def test_finalize_with_toggle_off_stores_thinking_for_button():
    bot = await _stub_bot(show_thinking=False)
    bot._is_image = lambda p: False
    bot._synthesizer = None

    async def fake_send(chat_id, text, reply_to=None):
        pass

    bot._send_to_chat = fake_send
    task = _fake_task(task_id=12)
    task.status = TaskStatus.DONE
    task.result = "ok"
    await bot._on_stream_event(task, ThinkingDelta(text="hidden reasoning"))

    await bot._finalize_claude_task(task)

    assert bot._thinking_store[task.id] == "hidden reasoning"


@pytest.mark.asyncio
async def test_waiting_input_seals_live_text():
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None

    # We don't exercise the question rendering here — stub it.
    async def fake_send(chat_id, text, reply_to=None):
        pass

    bot._send_to_chat = fake_send

    async def fake_render_questions(task):
        pass

    # _on_waiting_input will try to render questions; give it an empty list so that path is a no-op.
    task = _fake_task(task_id=20)
    task.pending_questions = []
    task.result = ""

    await bot._on_stream_event(task, TextDelta(text="mid-stream"))
    assert task.id in bot._live_text

    await bot._on_waiting_input(task)

    # Live text was finalised and popped.
    assert task.id not in bot._live_text


@pytest.mark.asyncio
async def test_thinking_command_handlers_exist_and_register():
    # Sanity check that the ProjectBot class exposes the new command handler.
    from link_project_to_chat.bot import ProjectBot, COMMANDS
    assert any(c[0] == "thinking" for c in COMMANDS)
    assert hasattr(ProjectBot, "_on_thinking")


@pytest.mark.asyncio
async def test_cancel_live_for_seals_both_messages():
    bot = await _stub_bot(show_thinking=True)
    task = _fake_task(task_id=30)
    # Create both a live-text and a live-thinking message by feeding deltas.
    await bot._on_stream_event(task, TextDelta(text="answer so far"))
    await bot._on_stream_event(task, ThinkingDelta(text="reasoning"))
    assert task.id in bot._live_text
    assert task.id in bot._live_thinking

    await bot._cancel_live_for(task.id, "(cancelled)")

    assert task.id not in bot._live_text
    assert task.id not in bot._live_thinking
    # Both messages received a final edit containing the cancellation note.
    cancel_texts = [e["text"] for e in bot._app.bot.edits if "(cancelled)" in e["text"]]
    assert len(cancel_texts) >= 2, f"expected 2 cancel edits, got edits={bot._app.bot.edits}"
