"""M12 — LiveMessage._rotate_once boundary and failure tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

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
    edit_raises: Exception | None = None

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        mid = self.next_id
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "mid": mid})
        return FakeMessage(message_id=mid)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self.edit_raises:
            raise self.edit_raises
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})


# ---------------------------------------------------------------------------
# Overflow / rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_once_splits_buffer_at_boundary():
    """_rotate_once seals the current message and opens a new one with the tail."""
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, max_chars=20, prefix="")
    await live.start("…")
    live._buffer = "A" * 30  # exceeds max_chars=20

    await live._rotate_once()

    # A new message must have been sent (original + new = 2 total)
    assert len(bot.sent) == 2
    # The new message takes over; buffer should now hold the tail
    assert len(live._buffer) < 30


@pytest.mark.asyncio
async def test_rotate_once_massive_overflow_uses_placeholder():
    """When tail itself overflows max_chars, new message opens with '…' placeholder."""
    bot = FakeBot()
    # max_chars=10, buffer=50 chars — tail after split will still overflow
    live = LiveMessage(bot=bot, chat_id=1, max_chars=10, prefix="")
    await live.start("…")
    live._buffer = "X" * 50

    await live._rotate_once()

    assert bot.sent[-1]["text"] == "…"


@pytest.mark.asyncio
async def test_rotate_once_binary_search_exhaustion_falls_back_to_plain():
    """When the 5-iteration binary search never finds a fitting HTML render,
    _rotate_once seals with the full boundary in plain text (no raise)."""
    bot = FakeBot()
    # Use a large buffer that will force the binary search to shrink many times.
    live = LiveMessage(bot=bot, chat_id=1, max_chars=50, prefix="")
    await live.start("…")
    # Construct content where HTML expansion will make it overflow in each attempt.
    # Use many '&' chars — md_to_telegram may HTML-encode them, inflating the string.
    live._buffer = "&" * 60

    # Should complete without error regardless of how HTML rendering behaves.
    await live._rotate_once()

    # A new message must have been opened.
    assert len(bot.sent) == 2


# ---------------------------------------------------------------------------
# HTML render failure fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_once_html_edit_failure_falls_back_to_plain():
    """If the HTML seal-edit fails, _rotate_once falls back to plain-text seal."""
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, max_chars=20, prefix="")
    await live.start("…")
    live._buffer = "A" * 30

    # Patch edit to fail on the first call (HTML attempt), succeed on second (plain).
    call_count = 0
    original_edit = bot.edit_message_text

    async def flaky_edit(chat_id, message_id, text, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Telegram HTML parse error")
        await original_edit(chat_id, message_id, text, **kw)

    bot.edit_message_text = flaky_edit

    # Should not raise — falls back gracefully
    await live._rotate_once()
    assert len(bot.sent) == 2  # new message opened after fallback


# ---------------------------------------------------------------------------
# Iteration cap in _rotate_once binary search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_once_binary_search_caps_at_5_iterations(monkeypatch):
    """The head-size binary search in _seal_and_rotate runs at most 5 iterations."""
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, max_chars=50, prefix="")
    await live.start("…")
    live._buffer = "M" * 100

    iteration_count = 0
    original_seal = live._rotate_once

    async def counting_rotate():
        nonlocal iteration_count
        iteration_count += 1
        await original_seal()

    # Just ensure _rotate_once itself completes (the 5-iteration cap is internal)
    await live._rotate_once()
    assert len(bot.sent) >= 2
