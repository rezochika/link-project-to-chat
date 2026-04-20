# Live Streaming and Thinking Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream Claude's text output to Telegram in real time, and add a persistent per-project `show_thinking` toggle that also streams thinking deltas to a separate live message when enabled.

**Architecture:** A new `LiveMessage` helper (in `src/link_project_to_chat/livestream.py`) owns a single updating Telegram message: it accepts deltas, throttles edits, handles overflow by rotating to a new message, and finalises with optional Markdown-to-HTML rendering. The bot creates one `LiveMessage` per task for the answer, and a second one for thinking when `show_thinking` is on. The old accumulate-only buffers are replaced; the post-completion "Thinking" button stays as a fallback for the toggle-off path.

**Tech Stack:** Python 3, `python-telegram-bot` (async), `asyncio`, `pytest` + `pytest-asyncio`, existing `md_to_telegram` + `split_html` helpers.

**Reference spec:** `docs/superpowers/specs/2026-04-20-live-stream-and-thinking-toggle-design.md`

---

## File Structure

**Create:**
- `src/link_project_to_chat/livestream.py` — `LiveMessage` class.
- `tests/test_livestream.py` — unit tests for `LiveMessage`.
- `tests/test_bot_streaming.py` — integration tests for bot stream event wiring.

**Modify:**
- `src/link_project_to_chat/config.py` — add `show_thinking` field + load/save round-trip.
- `src/link_project_to_chat/bot.py` — wire in `LiveMessage`, add `/thinking` command + callback, update `_finalize_claude_task` and `_on_waiting_input`.
- `tests/test_config.py` — add `show_thinking` round-trip test.
- `README.md` — document `/thinking` in the commands table.

---

## Task 1: Add `show_thinking` field to `ProjectConfig`

**Files:**
- Modify: `src/link_project_to_chat/config.py` (dataclass at line 24; `_load_project` block at lines 118-133; `_save_config_unlocked` project block at lines 207-247)
- Test: `tests/test_config.py`

- [ ] **Step 1.1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_project_show_thinking_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={
            "proj": ProjectConfig(
                path="/x",
                telegram_bot_token="T",
                show_thinking=True,
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is True


def test_project_show_thinking_defaults_false(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {"proj": {"path": "/x", "telegram_bot_token": "T"}}
    }))
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is False
```

- [ ] **Step 1.2: Run the test and confirm failure**

Run: `pytest tests/test_config.py::test_project_show_thinking_roundtrip tests/test_config.py::test_project_show_thinking_defaults_false -v`
Expected: FAIL — `ProjectConfig.__init__() got an unexpected keyword argument 'show_thinking'`.

- [ ] **Step 1.3: Add the dataclass field**

In `src/link_project_to_chat/config.py`, inside `ProjectConfig` (line 24 area), add the field immediately after `active_persona`:

```python
    active_persona: str | None = None
    show_thinking: bool = False
```

- [ ] **Step 1.4: Load the field**

In `_load_project` where each project is built (around lines 118-133), add the parameter:

```python
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                allowed_usernames=_migrate_usernames(proj, "allowed_usernames", "username"),
                trusted_user_ids=_migrate_user_ids(proj, "trusted_user_ids", "trusted_user_id"),
                model=proj.get("model"),
                effort=proj.get("effort"),
                permissions=_load_permissions(proj),
                session_id=proj.get("session_id"),
                autostart=proj.get("autostart", False),
                group_mode=proj.get("group_mode", False),
                group_chat_id=proj.get("group_chat_id"),
                role=proj.get("role"),
                active_persona=proj.get("active_persona"),
                show_thinking=proj.get("show_thinking", False),
            )
```

- [ ] **Step 1.5: Save the field**

In `_save_config_unlocked`, inside the `for name, p in config.projects.items()` loop (around lines 208-244), add just before `existing_projects[name] = proj`:

```python
        if p.show_thinking:
            proj["show_thinking"] = True
        else:
            proj.pop("show_thinking", None)
```

- [ ] **Step 1.6: Run the tests and confirm pass**

Run: `pytest tests/test_config.py -v`
Expected: all tests pass, including the two new ones.

- [ ] **Step 1.7: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add per-project show_thinking flag"
```

---

## Task 2: `LiveMessage.start` — initial placeholder send

**Files:**
- Create: `src/link_project_to_chat/livestream.py`
- Create: `tests/test_livestream.py`

- [ ] **Step 2.1: Write the failing test**

Create `tests/test_livestream.py`:

```python
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
```

- [ ] **Step 2.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'link_project_to_chat.livestream'`.

- [ ] **Step 2.3: Create the module with minimal implementation**

Create `src/link_project_to_chat/livestream.py`:

```python
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_THROTTLE = 1.2  # seconds between edits per message
_DEFAULT_MAX_CHARS = 3800  # Telegram hard cap is 4096; leave room for prefix + ellipsis
_MAX_THROTTLE = 5.0  # cap when backing off from 429


class LiveMessage:
    """A single Telegram message that is edited in place as deltas arrive."""

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        *,
        reply_to_message_id: int | None = None,
        prefix: str = "",
        throttle: float = _DEFAULT_THROTTLE,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._bot = bot
        self.chat_id = chat_id
        self._reply_to = reply_to_message_id
        self._prefix = prefix
        self._throttle = throttle
        self._effective_throttle = throttle
        self._max_chars = max_chars
        self._buffer: str = ""
        self._last_rendered: str = ""
        self._last_edit_ts: float = 0.0
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._finalized = False
        self.message_id: int | None = None

    async def start(self, initial: str = "…") -> None:
        msg = await self._bot.send_message(
            self.chat_id, self._prefix + initial, reply_to_message_id=self._reply_to
        )
        self.message_id = msg.message_id
        self._last_rendered = self._prefix + initial
        self._last_edit_ts = time.monotonic()
```

- [ ] **Step 2.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py::test_start_sends_placeholder -v`
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): add LiveMessage.start with placeholder send"
```

---

## Task 3: `LiveMessage.append` with throttled flush

**Files:**
- Modify: `src/link_project_to_chat/livestream.py`
- Modify: `tests/test_livestream.py`

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/test_livestream.py`:

```python
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
```

Also ensure `pytest-asyncio` auto-mode is on. If the project doesn't already have it, add a `tests/conftest.py` (or rely on existing). Check `pyproject.toml` / `pytest.ini` — if neither enables asyncio auto-mode, add this to `tests/test_livestream.py`:

```python
pytestmark = pytest.mark.asyncio
```

Verify by running the test file; if the `@pytest.mark.asyncio` decorators are already picked up, this is unnecessary.

- [ ] **Step 3.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py -v`
Expected: FAIL on the new tests — `AttributeError: 'LiveMessage' object has no attribute 'append'`.

- [ ] **Step 3.3: Implement `append` + internal flush**

In `src/link_project_to_chat/livestream.py`, add these methods to `LiveMessage`:

```python
    async def append(self, delta: str) -> None:
        if self._finalized:
            logger.debug("append after finalize ignored (mid=%s)", self.message_id)
            return
        if not delta:
            return
        self._buffer += delta
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        try:
            # Sleep until we're allowed to edit again.
            wait = self._effective_throttle - (time.monotonic() - self._last_edit_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._edit_current()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LiveMessage flush failed (mid=%s)", self.message_id)

    async def _edit_current(self) -> None:
        async with self._lock:
            if self._finalized or self.message_id is None:
                return
            text = self._prefix + self._buffer
            if text == self._last_rendered:
                return
            if not text.strip():
                return
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self._last_rendered = text
            self._last_edit_ts = time.monotonic()
```

- [ ] **Step 3.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py -v`
Expected: all four tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): throttle edits and coalesce rapid deltas"
```

---

## Task 4: `LiveMessage.finalize` with render modes

**Files:**
- Modify: `src/link_project_to_chat/livestream.py`
- Modify: `tests/test_livestream.py`

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_livestream.py`:

```python
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
```

- [ ] **Step 4.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py -v`
Expected: FAIL on the new tests — `finalize` not defined.

- [ ] **Step 4.3: Implement `finalize`**

In `src/link_project_to_chat/livestream.py`:

Add near the top of the file, just below the existing imports:

```python
from .formatting import md_to_telegram
```

Add these methods to `LiveMessage`:

```python
    async def finalize(
        self,
        final_text: str | None = None,
        *,
        render: bool = True,
    ) -> None:
        if self._finalized or self.message_id is None:
            return
        # Cancel any pending throttled flush — we're about to replace it.
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            try:
                await self._pending
            except (asyncio.CancelledError, Exception):
                pass
        self._pending = None

        if final_text is not None and final_text != "":
            self._buffer = final_text

        text = self._prefix + self._buffer
        if not text.strip():
            self._finalized = True
            return

        parse_mode: str | None = None
        rendered = text
        if render:
            rendered = self._prefix + md_to_telegram(self._buffer)
            parse_mode = "HTML"

        if rendered != self._last_rendered:
            try:
                await self._bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=rendered,
                    parse_mode=parse_mode,
                )
                self._last_rendered = rendered
            except Exception:
                logger.warning(
                    "LiveMessage.finalize edit failed (mid=%s); falling back to plain",
                    self.message_id,
                    exc_info=True,
                )
                # Final HTML edit failed (bad tags mid-stream etc). Retry plain.
                try:
                    await self._bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=text,
                    )
                    self._last_rendered = text
                except Exception:
                    logger.exception("LiveMessage.finalize plain fallback failed")

        self._finalized = True
```

- [ ] **Step 4.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): add finalize with optional HTML rendering"
```

---

## Task 5: `LiveMessage` overflow into a new Telegram message

**Files:**
- Modify: `src/link_project_to_chat/livestream.py`
- Modify: `tests/test_livestream.py`

- [ ] **Step 5.1: Write the failing test**

Append to `tests/test_livestream.py`:

```python
@pytest.mark.asyncio
async def test_overflow_rotates_to_new_message():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05, max_chars=50)
    await live.start()
    first_mid = live.message_id
    # 60 chars, well above the 50-char cap.
    await live.append("x" * 60)
    await asyncio.sleep(0.12)
    # The buffer should have rotated: a new message was sent after the seal.
    assert len(bot.sent) == 2
    assert live.message_id != first_mid
    # The first message was sealed with the prefix of the overflowed content
    # (content may have been split); the new message_id becomes the active one.
    assert bot.sent[1]["chat_id"] == 1
```

- [ ] **Step 5.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py::test_overflow_rotates_to_new_message -v`
Expected: FAIL — current flush does a single edit without splitting.

- [ ] **Step 5.3: Implement overflow rotation**

Replace the `_edit_current` method in `src/link_project_to_chat/livestream.py` with:

```python
    async def _edit_current(self) -> None:
        async with self._lock:
            if self._finalized or self.message_id is None:
                return
            # Overflow: seal the current message and rotate to a new one.
            while len(self._prefix + self._buffer) > self._max_chars:
                await self._rotate_once()
            text = self._prefix + self._buffer
            if text == self._last_rendered:
                return
            if not text.strip():
                return
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self._last_rendered = text
            self._last_edit_ts = time.monotonic()

    async def _rotate_once(self) -> None:
        """Seal the current message at the max-char boundary and open a new one."""
        # Compute how much of the current buffer fits in the current message.
        room = self._max_chars - len(self._prefix)
        if room <= 0:
            room = 0
        head = self._buffer[:room]
        tail = self._buffer[room:]
        seal_text = self._prefix + head
        try:
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=seal_text,
            )
        except Exception:
            logger.warning("LiveMessage seal-edit failed (mid=%s)", self.message_id, exc_info=True)
        # Open a new message with the tail as the initial content (or a placeholder if empty).
        initial = tail or "…"
        msg = await self._bot.send_message(
            self.chat_id,
            self._prefix + initial,
            reply_to_message_id=self._reply_to,
        )
        self.message_id = msg.message_id
        self._buffer = tail
        self._last_rendered = self._prefix + initial
        self._last_edit_ts = time.monotonic()
```

- [ ] **Step 5.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): rotate to a new message on overflow"
```

---

## Task 6: Rate-limit backoff on `RetryAfter`

**Files:**
- Modify: `src/link_project_to_chat/livestream.py`
- Modify: `tests/test_livestream.py`

- [ ] **Step 6.1: Write the failing test**

Append to `tests/test_livestream.py`:

```python
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
    # A successful edit eventually lands.
    assert any(e["text"] == "hello" for e in bot.edits)
```

- [ ] **Step 6.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py::test_retry_after_backs_off_then_succeeds -v`
Expected: FAIL — the `FakeRetryAfter` leaks out of `_edit_current` and the edit never succeeds.

- [ ] **Step 6.3: Handle `RetryAfter` in flush**

In `src/link_project_to_chat/livestream.py`, add near the top (after the existing imports):

```python
try:
    from telegram.error import RetryAfter  # type: ignore
except Exception:  # pragma: no cover - test envs without full telegram install
    class RetryAfter(Exception):  # type: ignore
        retry_after: float = 0.0
```

Update `_flush_soon` to back off and reschedule on `RetryAfter`:

```python
    async def _flush_soon(self) -> None:
        try:
            wait = self._effective_throttle - (time.monotonic() - self._last_edit_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self._edit_current()
            except RetryAfter as e:
                backoff = min(max(float(getattr(e, "retry_after", 1.0)), self._throttle) * 2, _MAX_THROTTLE)
                self._effective_throttle = backoff
                logger.warning("Telegram RetryAfter; backing off to %.2fs (mid=%s)", backoff, self.message_id)
                # Re-schedule another flush after the backoff window.
                await asyncio.sleep(backoff)
                await self._edit_current()
                # Decay back toward the normal throttle on success.
                self._effective_throttle = self._throttle
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LiveMessage flush failed (mid=%s)", self.message_id)
```

- [ ] **Step 6.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): back off and retry on Telegram RetryAfter"
```

---

## Task 7: `LiveMessage.cancel`

**Files:**
- Modify: `src/link_project_to_chat/livestream.py`
- Modify: `tests/test_livestream.py`

- [ ] **Step 7.1: Write the failing test**

Append to `tests/test_livestream.py`:

```python
@pytest.mark.asyncio
async def test_cancel_appends_note_and_seals():
    bot = FakeBot()
    live = LiveMessage(bot=bot, chat_id=1, throttle=0.05)
    await live.start()
    await live.append("partial answer")
    await live.cancel()
    # Final edit carries a cancellation marker.
    assert "(cancelled)" in bot.edits[-1]["text"]
    # Subsequent appends are dropped.
    edits_before = len(bot.edits)
    await live.append("late")
    await asyncio.sleep(0.15)
    assert len(bot.edits) == edits_before
```

- [ ] **Step 7.2: Run and confirm failure**

Run: `pytest tests/test_livestream.py::test_cancel_appends_note_and_seals -v`
Expected: FAIL — `cancel` not defined.

- [ ] **Step 7.3: Implement `cancel`**

Add to `LiveMessage` in `src/link_project_to_chat/livestream.py`:

```python
    async def cancel(self, note: str = "(cancelled)") -> None:
        if self._finalized:
            return
        suffix = f"\n_{note}_" if self._buffer else note
        await self.finalize(self._buffer + suffix, render=False)
```

- [ ] **Step 7.4: Run and confirm pass**

Run: `pytest tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 7.5: Commit**

```bash
git add src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "feat(livestream): add cancel with cancellation note"
```

---

## Task 8: Wire live text streaming into `ProjectBot._on_stream_event`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_bot_streaming.py`

- [ ] **Step 8.1: Write the failing integration test**

Create `tests/test_bot_streaming.py`:

```python
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
```

- [ ] **Step 8.2: Run and confirm failure**

Run: `pytest tests/test_bot_streaming.py -v`
Expected: FAIL — `ProjectBot` doesn't have `_live_text` / `show_thinking` yet, and `_on_stream_event` still uses `_stream_text`.

- [ ] **Step 8.3: Rewire `_on_stream_event` and `__init__`**

In `src/link_project_to_chat/bot.py`:

1. Add the livestream import near the other local imports:

```python
from .livestream import LiveMessage
```

2. In `ProjectBot.__init__` around lines 107-113, replace `_stream_text` with `_live_text` and add `_live_thinking`:

```python
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._live_text: dict[int, LiveMessage] = {}
        self._live_thinking: dict[int, LiveMessage] = {}
        self._thinking_buf: dict[int, str] = {}   # task_id → accumulated thinking (toggle-off path)
        self._thinking_store: dict[int, str] = {}  # task_id → thinking text
```

3. Add `show_thinking` to the `__init__` parameter list (next to `active_persona`) and store it:

```python
        active_persona: str | None = None,
        group_mode: bool = False,
        show_thinking: bool = False,
    ):
        ...
        self._active_persona = active_persona
        self.show_thinking = show_thinking
```

4. Replace `_on_stream_event` body (around lines 146-159) with:

```python
    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, TextDelta):
            live = self._live_text.get(task.id)
            if live is None:
                live = LiveMessage(
                    bot=self._app.bot,
                    chat_id=task.chat_id,
                    reply_to_message_id=task.message_id,
                )
                self._live_text[task.id] = live
                await live.start()
            await live.append(event.text)
        elif isinstance(event, ThinkingDelta):
            if self.show_thinking:
                live = self._live_thinking.get(task.id)
                if live is None:
                    live = LiveMessage(
                        bot=self._app.bot,
                        chat_id=task.chat_id,
                        reply_to_message_id=task.message_id,
                        prefix="💭 ",
                    )
                    self._live_thinking[task.id] = live
                    await live.start()
                await live.append(event.text)
            else:
                buf = self._thinking_buf.setdefault(task.id, "")
                sep = "\n\n" if buf else ""
                self._thinking_buf[task.id] = buf + sep + event.text
        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(
                    task.chat_id, event.path, reply_to=task.message_id
                )
```

- [ ] **Step 8.4: Run and confirm pass**

Run: `pytest tests/test_bot_streaming.py::test_text_delta_starts_live_message -v`
Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "feat(bot): stream text deltas via LiveMessage"
```

---

## Task 9: Wire live thinking (toggle-aware)

**Files:**
- Modify: `tests/test_bot_streaming.py` (tests only — the implementation already landed in Task 8, this task validates both branches)

- [ ] **Step 9.1: Write the failing tests**

Append to `tests/test_bot_streaming.py`:

```python
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
```

- [ ] **Step 9.2: Run and confirm pass**

Run: `pytest tests/test_bot_streaming.py -v`
Expected: all three tests pass (the implementation was completed in Task 8).

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_bot_streaming.py
git commit -m "test(bot): cover both thinking toggle branches"
```

---

## Task 10: Rewire `_finalize_claude_task`

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (around lines 194-212)
- Modify: `tests/test_bot_streaming.py`

- [ ] **Step 10.1: Write the failing tests**

Append to `tests/test_bot_streaming.py`:

```python
@pytest.mark.asyncio
async def test_finalize_with_live_text_does_not_resend():
    bot = await _stub_bot()
    # Stub out the voice/compact helpers we don't exercise.
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=10)
    task.status = TaskStatus.DONE
    task.result = "final answer"
    await bot._on_stream_event(task, TextDelta(text="partial"))
    sent_before = len(bot._app.bot.sent)

    await bot._finalize_claude_task(task)

    # No new send_message call — the live message was edited to the final answer.
    assert len(bot._app.bot.sent) == sent_before
    assert task.id not in bot._live_text
    # A final edit landed carrying the final answer.
    assert any("final answer" in e["text"] for e in bot._app.bot.edits)


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
```

- [ ] **Step 10.2: Run and confirm failure**

Run: `pytest tests/test_bot_streaming.py -v`
Expected: FAIL on the new tests — current finalize still sends `task.result` via `_send_to_chat`.

- [ ] **Step 10.3: Rewrite `_finalize_claude_task`**

Replace the body of `_finalize_claude_task` (around lines 194-212) in `src/link_project_to_chat/bot.py`:

```python
    async def _finalize_claude_task(self, task: Task) -> None:
        live_text = self._live_text.pop(task.id, None)
        live_thinking = self._live_thinking.pop(task.id, None)
        thinking = self._thinking_buf.pop(task.id, None)
        is_voice = task.id in self._voice_tasks
        self._voice_tasks.discard(task.id)

        if task._compact:
            text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
            # Compact tasks don't stream, but clean up defensively.
            if live_text is not None:
                await live_text.cancel("(compacted)")
            if live_thinking is not None:
                await live_thinking.cancel("(compacted)")
            await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            return

        if task.status == TaskStatus.DONE:
            if live_text is not None:
                await live_text.finalize(task.result or None, render=True)
            else:
                await self._send_to_chat(task.chat_id, task.result, reply_to=task.message_id)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)
            elif thinking:
                self._thinking_store[task.id] = thinking
            if is_voice and self._synthesizer and task.result:
                await self._send_voice_response(task.chat_id, task.result, reply_to=task.message_id)
        else:
            error_text = f"Error: {task.error}"
            if live_text is not None:
                await live_text.finalize(error_text, render=False)
            else:
                await self._send_to_chat(task.chat_id, error_text, reply_to=task.message_id)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)
```

- [ ] **Step 10.4: Run and confirm pass**

Run: `pytest tests/test_bot_streaming.py tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 10.5: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "feat(bot): finalize live messages instead of re-sending result"
```

---

## Task 11: Fix `_on_waiting_input` for live messages

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (around lines 266-276)
- Modify: `tests/test_bot_streaming.py`

- [ ] **Step 11.1: Write the failing test**

Append to `tests/test_bot_streaming.py`:

```python
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
```

- [ ] **Step 11.2: Run and confirm failure**

Run: `pytest tests/test_bot_streaming.py::test_waiting_input_seals_live_text -v`
Expected: FAIL — `_on_waiting_input` still references `_stream_text` and leaves `_live_text` untouched.

- [ ] **Step 11.3: Update `_on_waiting_input`**

In `src/link_project_to_chat/bot.py` replace the "Flush any accompanying text" block (around lines 272-276) with:

```python
        # Finalize any live messages so the question buttons appear after the sealed stream.
        live_text = self._live_text.pop(task.id, None)
        if live_text is not None:
            await live_text.finalize(task.result or None, render=True)
        elif task.result and task.result.strip():
            await self._send_to_chat(task.chat_id, task.result, reply_to=task.message_id)
        live_thinking = self._live_thinking.pop(task.id, None)
        if live_thinking is not None:
            await live_thinking.finalize(render=False)
```

- [ ] **Step 11.4: Run and confirm pass**

Run: `pytest tests/test_bot_streaming.py tests/test_livestream.py -v`
Expected: all tests pass.

- [ ] **Step 11.5: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "feat(bot): seal live messages when the model asks a question"
```

---

## Task 12: `/thinking` command and callback

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (multiple locations: `COMMANDS` at line 45, callback-query dispatcher around line 764, handler map at line 1257)
- Modify: `tests/test_bot_streaming.py`

- [ ] **Step 12.1: Write the failing test**

Append to `tests/test_bot_streaming.py`:

```python
@pytest.mark.asyncio
async def test_thinking_command_handlers_exist_and_register():
    # Sanity check that the ProjectBot class exposes the new command handler.
    from link_project_to_chat.bot import ProjectBot, COMMANDS
    assert any(c[0] == "thinking" for c in COMMANDS)
    assert hasattr(ProjectBot, "_on_thinking")
```

- [ ] **Step 12.2: Run and confirm failure**

Run: `pytest tests/test_bot_streaming.py::test_thinking_command_handlers_exist_and_register -v`
Expected: FAIL.

- [ ] **Step 12.3: Register the command metadata**

In `src/link_project_to_chat/bot.py`, add to `COMMANDS` (around line 45-66) immediately after the `effort` entry:

```python
    ("effort", "Set thinking depth (low/medium/high/max)"),
    ("thinking", "Toggle live thinking display (on/off)"),
    ("permissions", "Set permission mode"),
```

- [ ] **Step 12.4: Add the command handler and markup helpers**

In `src/link_project_to_chat/bot.py`, add the following methods inside `ProjectBot`, placed just after `_on_effort` (~line 467):

```python
    def _thinking_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("On", callback_data="thinking_set_on"),
                InlineKeyboardButton("Off", callback_data="thinking_set_off"),
            ]
        ])

    def _current_thinking(self) -> str:
        return "on" if self.show_thinking else "off"

    async def _on_thinking(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        args = (ctx.args or []) if ctx else []
        if args:
            arg = args[0].lower()
            if arg in ("on", "off"):
                self.show_thinking = arg == "on"
                patch_project(self.name, {"show_thinking": self.show_thinking})
                return await update.effective_message.reply_text(
                    f"Live thinking: {self._current_thinking()}"
                )
            return await update.effective_message.reply_text(
                "Usage: /thinking on|off"
            )
        await update.effective_message.reply_text(
            f"Live thinking: {self._current_thinking()}",
            reply_markup=self._thinking_markup(),
        )
```

- [ ] **Step 12.5: Handle the callback**

In `src/link_project_to_chat/bot.py` inside the callback-query dispatcher (the `if query.data.startswith("model_set_")...elif...` chain around lines 764-794), add a new branch immediately after the `effort_set_` branch:

```python
        elif query.data.startswith("thinking_set_"):
            value = query.data[len("thinking_set_"):]
            if value in ("on", "off"):
                self.show_thinking = value == "on"
                patch_project(self.name, {"show_thinking": self.show_thinking})
            await query.edit_message_text(
                f"Live thinking: {self._current_thinking()}",
                reply_markup=self._thinking_markup(),
            )
```

- [ ] **Step 12.6: Register the handler**

In the `handlers = {...}` map inside `build()` (around line 1257), add after `"effort": self._on_effort,`:

```python
            "effort": self._on_effort,
            "thinking": self._on_thinking,
            "permissions": self._on_permissions,
```

- [ ] **Step 12.7: Run and confirm pass**

Run: `pytest tests/test_bot_streaming.py::test_thinking_command_handlers_exist_and_register -v`
Expected: PASS.

- [ ] **Step 12.8: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "feat(bot): add /thinking command and on/off callback"
```

---

## Task 13: Plumb `show_thinking` from config through `run_bot` / `run_bots`

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (`run_bot` signature around line 1322; `ProjectBot(...)` call around line 1350; the one `run_bot(...)` call in `run_bots` at line 1397)
- Modify: `src/link_project_to_chat/cli.py` (the `run_bot(...)` call at line 314 — the `--project NAME` branch)

**Note:** There are three `run_bot` call sites in total. The ad-hoc `--path/--token` call at `cli.py:260` has no `ProjectConfig` in scope, so it doesn't pass `show_thinking` — it defaults to `False`. Only the two call sites that have `proj: ProjectConfig` in scope need updating.

- [ ] **Step 13.1: Add the arg to `run_bot`**

In `run_bot` parameters (around line 1322-1342), add `show_thinking` just before the closing `) -> None:`:

```python
    group_mode: bool = False,
    active_persona: str | None = None,
    show_thinking: bool = False,
) -> None:
```

- [ ] **Step 13.2: Pass it into `ProjectBot`**

In the `ProjectBot(...)` constructor call inside `run_bot` (around line 1350-1363), add `show_thinking=show_thinking` just after `group_mode=group_mode`:

```python
    bot = ProjectBot(
        name, path, token,
        allowed_usernames=effective_usernames,
        trusted_user_ids=trusted_user_ids or ([trusted_user_id] if trusted_user_id else []),
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        transcriber=transcriber,
        synthesizer=synthesizer,
        active_persona=active_persona,
        group_mode=group_mode,
        show_thinking=show_thinking,
    )
```

- [ ] **Step 13.3: Pass it from `run_bots`**

In `src/link_project_to_chat/bot.py`, inside `run_bots` the `run_bot(...)` call at around line 1397-1414 ends with `active_persona=proj.active_persona,`. Add `show_thinking=proj.show_thinking,` as the final keyword argument:

```python
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            on_trust=on_trust,
            allowed_usernames=effective_usernames,
            trusted_user_ids=effective_trusted_ids,
            transcriber=transcriber,
            synthesizer=synthesizer,
            group_mode=proj.group_mode,
            active_persona=proj.active_persona,
            show_thinking=proj.show_thinking,
        )
```

- [ ] **Step 13.4: Pass it from the CLI `--project` branch**

In `src/link_project_to_chat/cli.py`, the `run_bot(...)` call at around line 314-332 currently ends with `active_persona=proj.active_persona,`. Add `show_thinking=proj.show_thinking,` as the final keyword argument:

```python
        run_bot(
            project,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_usernames=effective_usernames if not username else [username.lower().lstrip("@")],
            trusted_user_ids=effective_trusted_ids,
            session_id=session_id,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            on_trust=lambda uid: add_project_trusted_user_id(project, uid, cfg_path),
            transcriber=transcriber,
            synthesizer=synthesizer,
            group_mode=proj.group_mode,
            active_persona=proj.active_persona,
            show_thinking=proj.show_thinking,
        )
```

The ad-hoc `run_bot(...)` call at `cli.py:260` (inside the `if project_path and token:` branch) does NOT take a `ProjectConfig` — leave it alone. `show_thinking` defaults to `False` there, which is correct.

- [ ] **Step 13.5: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 13.6: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/cli.py
git commit -m "feat(bot): plumb show_thinking from config to ProjectBot"
```

---

## Task 14: Document `/thinking` in the README

**Files:**
- Modify: `README.md` (commands table around line 75)

- [ ] **Step 14.1: Add the row**

In `README.md`, inside the commands table, add a new row immediately after the `/effort` row (around line 77):

```markdown
| `/effort low/medium/high/max` | Set Claude thinking depth |
| `/thinking on/off` | Stream Claude's internal reasoning live to chat |
| `/permissions <mode>` | Set permission mode |
```

- [ ] **Step 14.2: Commit**

```bash
git add README.md
git commit -m "docs: document /thinking command"
```

---

## Task 15: End-to-end manual smoke test

**Files:** none — manual verification only.

- [ ] **Step 15.1: Run the bot locally**

```bash
python -m link_project_to_chat
```

- [ ] **Step 15.2: Send a prompt that produces a long streamed answer**

Send any prompt that takes several seconds and produces multi-paragraph output. Expected:
- The answer message appears as `…` immediately and is edited in place as text arrives.
- The final edit renders Markdown (bold/italic/code) correctly.

- [ ] **Step 15.3: Toggle thinking on and repeat**

```
/thinking on
```

Send another prompt. Expected:
- A separate `💭 …` message appears and updates as thinking deltas arrive.
- The answer message remains separate and also streams.
- Both messages are retained in chat after completion.

- [ ] **Step 15.4: Toggle thinking off and repeat**

```
/thinking off
```

Send another prompt. Expected:
- No `💭` message is created.
- The answer message still streams live.
- The **Thinking** button still works under `/tasks → task info` (confirms fallback path).

- [ ] **Step 15.5: Restart bot and verify persistence**

Stop the bot (Ctrl-C) and start it again. Run `/thinking` with no args — it should report the last-set state (confirming `patch_project` persisted).

---

## Self-review checklist (for the author)

Before handing this plan to execution, verify:

- [ ] Every spec requirement (live text; optional live thinking; `/thinking` command; persisted per-project flag; live-thinking fallback to the button when off; `_on_waiting_input` path fixed; voice path unchanged) maps to at least one task.
- [ ] No step contains `TBD`, `add appropriate X`, or similar placeholders.
- [ ] Method signatures used in tests match those defined earlier: `LiveMessage(bot=, chat_id=, reply_to_message_id=, prefix=, throttle=, max_chars=)`, `start(initial="…")`, `append(delta)`, `finalize(final_text=None, *, render=True)`, `cancel(note="(cancelled)")`.
- [ ] Dict names used in the bot tests match the init: `_live_text`, `_live_thinking`, `_thinking_buf`, `_thinking_store`, attribute `show_thinking`.
- [ ] Commit messages follow the repository style (lowercase prefix, scope, short summary) seen in `git log --oneline`.
