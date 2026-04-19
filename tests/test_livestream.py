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
