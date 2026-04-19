from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from link_project_to_chat.livestream import LiveMessage


@dataclass
class FakeMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    next_id: int = 1000

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        mid = self.next_id
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id, "mid": mid, **kw})
        return FakeMessage(message_id=mid)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kw})


@pytest.mark.asyncio
async def test_start_sends_placeholder():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=42, reply_to_message_id=7, prefix="")
    await live.start()
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42
    assert bot.sent[0]["reply_to"] == 7
    assert bot.sent[0]["text"] == "…"
    assert live.message_id == 1000


@pytest.mark.asyncio
async def test_append_flushes_after_throttle():
    bot = FakeBot()
    # Tiny throttle so the test is fast.
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("hello")
    # Wait long enough for one throttle window to pass and the flush to fire.
    await asyncio.sleep(0.15)
    assert len(bot.edits) == 1
    assert bot.edits[0]["message_id"] == live.message_id
    assert bot.edits[0]["text"] == "hello"


@pytest.mark.asyncio
async def test_append_coalesces_rapid_deltas():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.1)
    await live.start()
    for chunk in ["a", "b", "c", "d", "e"]:
        await live.append(chunk)
    await asyncio.sleep(0.2)
    # All five deltas collapse into at most one edit (throttle window >> append loop).
    assert len(bot.edits) == 1
    assert bot.edits[0]["text"] == "abcde"


@pytest.mark.asyncio
async def test_append_skips_edit_when_buffer_unchanged():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("x")
    await asyncio.sleep(0.12)
    await asyncio.sleep(0.12)  # second window with no new delta
    assert len(bot.edits) == 1


@dataclass
class SlowBot(FakeBot):
    edit_delay: float = 0.05

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        # Simulate a slow network edit so that new deltas can land during the
        # in-flight edit_message_text await.
        await asyncio.sleep(self.edit_delay)
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kw})


@pytest.mark.asyncio
async def test_append_during_flush_is_not_stranded():
    bot = SlowBot(edit_delay=0.05)
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.02)
    await live.start()
    # First append triggers a flush; after the throttle wait it starts the
    # (slow) edit_message_text await.
    await live.append("first")
    # Give the flush task time to hit edit_message_text (past the throttle wait
    # but still inside the slow edit's sleep).
    await asyncio.sleep(0.05)
    # This append lands while the first edit is still in flight; _pending is
    # not yet done, so nothing new gets scheduled to drain it.
    asyncio.create_task(live.append("second"))
    # Allow plenty of time for both the in-flight edit to finish and any
    # follow-up flush to drain the tail.
    await asyncio.sleep(0.3)
    assert bot.edits, "expected at least one edit"
    # Some edit must eventually carry the full accumulated buffer.
    assert any(e["text"] == "firstsecond" for e in bot.edits), (
        f"'second' delta was stranded; edits were: {[e['text'] for e in bot.edits]}"
    )


@pytest.mark.asyncio
async def test_finalize_plain_keeps_buffer_when_final_is_none():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("streamed body")
    await live.finalize(None, render=False)
    # Last edit should carry the streamed body.
    assert bot.edits[-1]["text"] == "streamed body"
    # No parse_mode when render=False.
    assert bot.edits[-1].get("parse_mode") in (None, )


@pytest.mark.asyncio
async def test_finalize_overrides_buffer_with_final_text():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("partial")
    await live.finalize("the full answer", render=False)
    assert bot.edits[-1]["text"] == "the full answer"


@pytest.mark.asyncio
async def test_finalize_render_true_applies_html():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.finalize("**bold**", render=True)
    edit = bot.edits[-1]
    assert edit.get("parse_mode") == "HTML"
    # md_to_telegram turns **bold** into <b>bold</b>
    assert "<b>bold</b>" in edit["text"]


@pytest.mark.asyncio
async def test_finalize_is_idempotent():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.finalize("done", render=False)
    count_before = len(bot.edits)
    await live.finalize("done", render=False)
    assert len(bot.edits) == count_before


@pytest.mark.asyncio
async def test_append_after_finalize_is_ignored():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.finalize("done", render=False)
    edits_after_finalize = len(bot.edits)
    await live.append("late delta")
    await asyncio.sleep(0.12)
    assert len(bot.edits) == edits_after_finalize
