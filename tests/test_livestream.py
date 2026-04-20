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


@pytest.mark.asyncio
async def test_overflow_rotates_to_new_message():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05, max_chars=50)
    await live.start()
    first_mid = live.message_id
    # 60 chars, above the 50-char cap.
    await live.append("x" * 60)
    await asyncio.sleep(0.15)
    # Rotation produced: placeholder send, seal-edit on first msg, new send with tail.
    assert len(bot.sent) == 2, f"expected 2 sends, got {len(bot.sent)}"
    assert bot.sent[1]["chat_id"] == 1
    assert live.message_id != first_mid
    # Seal-edit lands on the first message with exactly max_chars worth of data.
    seal_edits = [e for e in bot.edits if e["message_id"] == first_mid]
    assert seal_edits, "expected at least one edit on the original message"
    assert seal_edits[-1]["text"] == "x" * 50
    # New message starts with the 10-char tail.
    assert bot.sent[1]["text"] == "x" * 10
    # Buffer reflects only the tail now.
    assert live._buffer == "x" * 10


@pytest.mark.asyncio
async def test_rotated_seal_renders_markdown_as_html():
    """Sealed intermediate messages must have markdown rendered, not stay plain.

    Regression: long replies that rotated across multiple messages showed
    literal '**Step 1:**' in every message except the last, because seal_text
    was raw buffer.
    """
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05, max_chars=100)
    await live.start()
    placeholder_mid = live.message_id
    # 150 chars of content with markdown bold, exceeds 100 → rotation.
    payload = "**Step 1:** do thing " * 10  # ~200 chars, bold scattered throughout
    await live.append(payload)
    await asyncio.sleep(0.25)  # let flush + rotation complete

    # First (sealed) message must be rendered HTML: contains <b> tags.
    sealed = [e for e in bot.edits if e["message_id"] == placeholder_mid]
    assert sealed, "placeholder was never edited"
    assert "<b>Step 1:</b>" in sealed[-1]["text"], (
        f"sealed message is still plain markdown: {sealed[-1]['text'][:80]!r}"
    )
    assert sealed[-1].get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_rotated_seal_falls_back_to_plain_when_html_rejected():
    """If HTML-rendered seal is rejected by Telegram, fall back to plain text
    (no dropped seal, no Message_too_long crash).
    """

    @dataclass
    class RejectHtmlBot(FakeBot):
        async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, **kw):
            if parse_mode == "HTML":
                raise RuntimeError("simulated HTML parse failure")
            return await super().edit_message_text(chat_id, message_id, text, **kw)

    bot = RejectHtmlBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05, max_chars=50)
    await live.start()
    placeholder_mid = live.message_id
    await live.append("**hello** " * 20)  # ~200 chars
    await asyncio.sleep(0.25)

    sealed = [e for e in bot.edits if e["message_id"] == placeholder_mid]
    assert sealed, "placeholder was never sealed"
    # HTML attempt rejected; fallback edit is plain text (parse_mode None).
    assert sealed[-1].get("parse_mode") is None
    assert "**hello**" in sealed[-1]["text"]


@pytest.mark.asyncio
async def test_finalize_rotates_when_buffer_over_max_chars():
    """Bug: if the pre-flush buffer is > max_chars at finalize time (e.g. Claude
    emitted the final chunk faster than the throttle window), finalize must
    split via rotation instead of failing with Message_too_long.
    """
    bot = FakeBot()
    # Huge throttle so the streaming flush never fires — forces finalize to
    # handle the whole overflow itself.
    live = LiveMessage(bot=bot, chat_id=1, throttle=60.0, max_chars=50)
    await live.start()
    placeholder_mid = live.message_id
    await live.append("y" * 120)  # 120 chars, well over 50-char cap

    await live.finalize(render=False)

    # All sends/edits combined must contain the full 120 chars. No single text
    # may exceed max_chars. Placeholder message got sealed with the first slice.
    texts = [s["text"] for s in bot.sent] + [e["text"] for e in bot.edits]
    assert all(len(t) <= 50 for t in texts), (
        f"some message exceeded max_chars=50: {[(len(t), t[:20]) for t in texts]}"
    )
    # The first message (placeholder) must have been sealed with 50 y's (not
    # left at the "…" placeholder).
    final_first = [e["text"] for e in bot.edits if e["message_id"] == placeholder_mid]
    assert final_first and final_first[-1] == "y" * 50
    # All 120 characters show up somewhere.
    combined = "".join(
        t for t in (
            [e["text"] for e in bot.edits if e["message_id"] == placeholder_mid][-1:]
            + [s["text"] for s in bot.sent[1:]]
            + [e["text"] for e in bot.edits if e["message_id"] != placeholder_mid]
        )
    )
    assert combined.count("y") >= 120


@dataclass
class RejectTooLongBot(FakeBot):
    """Mirrors the real Telegram hard cap: editing > 4096 chars raises."""

    hard_cap: int = 4096

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if len(text) > self.hard_cap:
            raise RuntimeError(f"Message_too_long: {len(text)} > {self.hard_cap}")
        return await super().edit_message_text(chat_id, message_id, text, **kw)

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        if len(text) > self.hard_cap:
            raise RuntimeError(f"Message_too_long: {len(text)} > {self.hard_cap}")
        return await super().send_message(
            chat_id, text, reply_to_message_id=reply_to_message_id, **kw
        )


@pytest.mark.asyncio
async def test_finalize_does_not_strand_buffer_over_telegram_hard_cap():
    """Regression: previously if the un-flushed buffer exceeded Telegram's 4096
    hard cap at finalize time, both the HTML edit and the plain fallback raised
    Message_too_long. The placeholder was stranded at '…' and the whole Claude
    response was silently lost. finalize must rotate/split so the content lands.
    """
    bot = RejectTooLongBot(hard_cap=4096)
    live = LiveMessage(bot=bot, chat_id=1, throttle=60.0)  # default max_chars=3800
    await live.start()
    placeholder_mid = live.message_id
    # Worst-case: a single delta that is ~2x the max.
    await live.append("z" * 7500)
    await live.finalize(render=False)

    # Every send/edit must be within the bot's hard cap (no Message_too_long).
    texts = [s["text"] for s in bot.sent] + [e["text"] for e in bot.edits]
    assert all(len(t) <= 4096 for t in texts), (
        f"a message exceeded 4096 chars: {[(len(t), t[:30]) for t in texts]}"
    )
    # Placeholder must have been replaced with real content, not left at '…'.
    placeholder_edits = [e["text"] for e in bot.edits if e["message_id"] == placeholder_mid]
    assert placeholder_edits, "placeholder was never edited — buffer stranded at '…'"
    assert placeholder_edits[-1] != "…", "placeholder still shows '…' after finalize"
    # All 7500 z's must be distributed across the sent/edited messages.
    total_z = sum(t.count("z") for t in texts)
    assert total_z >= 7500, f"only {total_z} of 7500 z's reached Telegram"


@pytest.mark.asyncio
async def test_prefix_longer_than_max_chars_raises():
    bot = FakeBot()
    with pytest.raises(ValueError):
        LiveMessage(bot=bot, chat_id=1, prefix="💭 very long prefix", max_chars=5)


class FakeRetryAfter(Exception):
    """Stand-in for telegram.error.RetryAfter — matches on class name."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after


@dataclass
class FlakeyBot(FakeBot):
    fail_first_edits: int = 1
    _edit_fail_count: int = 0

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self._edit_fail_count < self.fail_first_edits:
            self._edit_fail_count += 1
            raise FakeRetryAfter(retry_after=0.05)
        return await super().edit_message_text(chat_id, message_id, text, **kw)


@pytest.mark.asyncio
async def test_retry_after_backs_off_then_succeeds(monkeypatch):
    import link_project_to_chat.livestream as ls_mod
    # Patch the RetryAfter class the module recognises.
    monkeypatch.setattr(ls_mod, "RetryAfter", FakeRetryAfter, raising=False)

    bot = FlakeyBot(fail_first_edits=1)
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("hello")
    # Wait for initial flush (which fails) + retry after backoff.
    await asyncio.sleep(0.4)
    # Exactly one successful edit of "hello" lands — not two or more accidental flushes.
    hello_edits = [e for e in bot.edits if e["text"] == "hello"]
    assert len(hello_edits) == 1, f"expected exactly one 'hello' edit; got edits={bot.edits}"
    # After the retry succeeded, the elevated throttle decayed back to normal.
    assert live._effective_throttle == live._throttle


@dataclass
class VeryFlakeyBot(FakeBot):
    fail_first_edits: int = 2
    _edit_fail_count: int = 0

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self._edit_fail_count < self.fail_first_edits:
            self._edit_fail_count += 1
            raise FakeRetryAfter(retry_after=0.02)
        return await super().edit_message_text(chat_id, message_id, text, **kw)


@pytest.mark.asyncio
async def test_double_retry_after_does_not_strand_buffer(monkeypatch):
    import link_project_to_chat.livestream as ls_mod
    monkeypatch.setattr(ls_mod, "RetryAfter", FakeRetryAfter, raising=False)

    bot = VeryFlakeyBot(fail_first_edits=2)
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.02)
    await live.start()
    await live.append("payload")
    # Two 0.02s backoffs + subsequent dirty-buffer reschedule — give it headroom.
    await asyncio.sleep(0.6)
    # Even after two RetryAfter hits, the dirty-buffer reschedule eventually lands the edit.
    assert any(e["text"] == "payload" for e in bot.edits), \
        f"buffer was stranded after double RetryAfter; edits={bot.edits}"


@pytest.mark.asyncio
async def test_cancel_appends_note_and_seals():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("partial answer")
    await live.cancel()
    # Final edit carries a cancellation marker.
    assert "(cancelled)" in bot.edits[-1]["text"]
    # No literal markdown underscores leak through (finalize runs render=False).
    assert "_(cancelled)_" not in bot.edits[-1]["text"]
    # Subsequent appends are dropped.
    edits_before = len(bot.edits)
    await live.append("late")
    await asyncio.sleep(0.15)
    assert len(bot.edits) == edits_before
