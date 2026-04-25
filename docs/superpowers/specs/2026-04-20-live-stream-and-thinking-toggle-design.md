# Live Streaming and Optional Thinking Display — Design

**Date:** 2026-04-20
**Status:** Shipped. See [docs/TODO.md §3](../../TODO.md#3-earlier-feature-tracks-shipped) for current status.

## Context

Today, `_on_stream_event` in `src/link_project_to_chat/bot.py` accumulates both
`TextDelta` and `ThinkingDelta` events in in-memory buffers (`_stream_text`,
`_thinking_buf`). Nothing is shown to the Telegram user until
`_finalize_claude_task` runs — the answer is sent as one block, and thinking is
only reachable after completion via an inline **Thinking** button on
`/tasks → task info`. For long tasks this feels dead.

## Goals

- Text output streams to Telegram as `TextDelta` events arrive. Always on.
- Thinking streams to a separate Telegram message when the project-level toggle
  `show_thinking` is enabled.
- A persistent per-project setting `show_thinking`, controllable via a
  `/thinking on|off` command (same pattern as `/effort`).
- When the toggle is off, behaviour is identical to today — including the
  post-completion **Thinking** button.

## Non-goals

- No changes to voice / TTS. Voice replies still synthesise from the final
  `task.result`, unchanged.
- No changes to tool use or image attachments.
- `ToolUse` events remain handled as today — no live streaming of them.
- No changes to `/run` command task output.

## Data model

Add one field to `ProjectConfig` (`src/link_project_to_chat/config.py`):

```python
show_thinking: bool = False
```

- Loaded in `_load_project` alongside existing fields.
- Persisted via the existing `patch_project(name, {"show_thinking": bool})`.
- Default `False` ⇒ zero behaviour change for existing users until they opt in.

## Architecture

### New module: `src/link_project_to_chat/livestream.py`

A small helper class `LiveMessage` encapsulates one live-updating Telegram
message. One file, focused responsibility, independently testable.

**Responsibilities:**
- Owns: `chat_id`, current `message_id`, the plain-text buffer, a monotonic
  `last_flush_ts`, an optional prefix (e.g. `💭 `), a `reply_to_message_id`,
  and a reference to the `telegram.Bot` instance.
- `append(delta: str) -> None`: appends to the buffer and schedules a debounced
  flush. Idempotent scheduling — at most one pending flush task at a time.
- Flush task: edits the Telegram message with `buffer` at most once every
  **~1.2 seconds** (safely below Telegram's ~1 edit/sec ceiling). Skips if the
  buffer is unchanged since the last successful edit.
- **Overflow handling:** when `len(prefix) + len(buffer) >= 3800`, seal the
  current message (one last edit, plain text), send a new placeholder message
  in the same chat, rotate `message_id`, reset buffer to an empty string.
  Subsequent deltas continue in the new message.
- `finalize(final_text: str | None = None, *, render: bool = True) -> None`:
  cancels any pending flush, performs one last edit. If `render=True`, applies
  `md_to_telegram(...)` + HTML parse mode. If `render=False` (thinking), keeps
  the message as plain text. If `final_text` is provided it replaces the
  buffered text (used for answer finalisation with `task.result`).
- Rate-limit resilience: on `telegram.error.RetryAfter` (or equivalent 429)
  during flush, double the effective interval temporarily (cap at 5 s) and
  retry on the next tick. Never crash the task.
- During streaming, always sends **plain text** (no `parse_mode`). Partial
  markdown renders badly and often errors on tag mid-breaks.

**Interface (rough):**

```python
class LiveMessage:
    async def start(self, initial: str = "…") -> None: ...
    async def append(self, delta: str) -> None: ...
    async def finalize(
        self, final_text: str | None = None, *, render: bool = True
    ) -> None: ...
    async def cancel(self, note: str = "(cancelled)") -> None: ...
```

### Wiring in `bot.py`

Two dicts keyed by `task.id`:

```python
self._live_text: dict[int, LiveMessage] = {}
self._live_thinking: dict[int, LiveMessage] = {}
```

`self._stream_text` is removed — `LiveMessage` owns the buffer now.

`self._thinking_buf` is retained for the **toggle-off** path (feeds the
post-completion **Thinking** button as today).

**`_on_stream_event` becomes:**

- `TextDelta`:
  - If `_live_text[task.id]` does not exist, create one replying to
    `task.message_id` and `await live.start()`.
  - `await live.append(event.text)`.
- `ThinkingDelta`:
  - If `show_thinking` is true:
    - If `_live_thinking[task.id]` does not exist, create one with the `💭 `
      prefix, replying to `task.message_id`, and `await live.start()`.
    - `await live.append(event.text)`.
  - Else (toggle off): accumulate in `_thinking_buf[task.id]` as today.
- `ToolUse`: unchanged.

**`_finalize_claude_task` becomes:**

- If `_live_text[task.id]` exists:
  - On `DONE`: `await live.finalize(task.result or None, render=True)`. When
    `task.result` is falsy, `finalize` keeps the buffered stream text rather
    than overwriting with empty — prevents the rare "nothing appears" bug when
    the agent emits deltas but no final `result`.
  - On error: `await live.finalize(f"Error: {task.error}", render=True)`.
  - Don't re-send `task.result` with `_send_to_chat` — the live message is the
    canonical answer now.
- Else (no text streamed — e.g. tool-only response, very fast failure): fall
  back to today's `_send_to_chat(task.result)`.
- If `_live_thinking[task.id]` exists: `await live.finalize(render=False)`.
- If toggle was off and `_thinking_buf[task.id]` is populated: store into
  `_thinking_store[task.id]` as today (keeps the **Thinking** button working).
- Pop both `_live_*` dicts for the task.

**Voice path:** unchanged — still calls `_send_voice_response(task.chat_id,
task.result, ...)` after text finalisation.

**Compact path (`task._compact`):** unchanged — no streaming expected; the
existing one-line status message stays.

**`_on_waiting_input` (model pauses for a user question)
— `bot.py:266`:** today this flushes any accompanying text via
`self._stream_text.pop(task.id, "")`. New behaviour:

- If `_live_text[task.id]` exists: `await live.finalize(task.result or None,
  render=True)` and pop it. The sealed message stands as the "accompanying
  text"; the question buttons are then sent as a separate message.
- If `_live_thinking[task.id]` exists: `await live.finalize(render=False)` and
  pop it. Subsequent deltas after the user answers will start a fresh live
  message.
- Remove the `_stream_text.pop(...)` calls — nothing else references
  `_stream_text` after its removal.

### Commands and UX

**`/thinking` command** (new), mirroring `/effort`:

- No args → shows current state and inline `On` / `Off` buttons.
- `/thinking on` / `/thinking off` → persist via `patch_project(self.name,
  {"show_thinking": bool})` and confirm.
- Callback data: `thinking_set_on` / `thinking_set_off`, handled in the
  existing callback-query dispatcher.
- Register in `BOT_COMMANDS` and in the command handler map
  (`self._handler_map` around `bot.py:1262`).

**`/tasks` → task info:** the **Thinking** inline button stays. It's still
useful for:
- Tasks that ran while the toggle was off.
- Reviewing the thinking transcript after the live message has scrolled away.

**Visual:**
- Answer live message: plain text while streaming; final edit uses
  `md_to_telegram(...)` with HTML parse mode (matches today's `_send_to_chat`).
- Thinking live message: `💭 ` prefix, plain text, stays plain on finalize.

## Error handling

- Telegram 429 / `RetryAfter`: catch in the flush task, exponential backoff
  capped at 5 s, retry on next tick. Never surface to the user; never cancel
  the task.
- Telegram `BadRequest` (e.g. "message is not modified"): ignore and continue.
- Stream event delivered after `_finalize_claude_task` ran (race): log a
  warning and drop — `_live_*` dicts will have been popped.
- Task cancellation: `await live.cancel()` before popping; the message ends
  with `_(cancelled)_` appended so the user sees why it stopped.

## Testing

**Unit (new tests in `tests/test_livestream.py`):**
- `LiveMessage.append` + flush schedules at most one edit per throttle window
  (mock `telegram.Bot`; advance `asyncio` clock; assert edit-call cadence).
- Overflow (buffer > 3800) seals and rotates to a new `message_id`.
- `finalize(final_text, render=True)` issues exactly one final edit with the
  rendered HTML and cancels any pending flush.
- `finalize(render=False)` does not apply markdown rendering.
- Rate-limit `RetryAfter` doubles the interval and the flush eventually
  succeeds.

**Unit (extend `tests/test_config.py` if present; otherwise add):**
- Round-trip `show_thinking` through `patch_project` and `_load_project`.
- Default is `False` when the field is absent from an existing config file.

**Integration (extend the existing bot/stream tests):**
- Feed a sequence of `TextDelta` events into `_on_stream_event` → assert a
  `start` + ≥1 `append` + final `finalize(task.result)` are issued, and that
  `_send_to_chat(task.result)` is NOT called.
- Same with `ThinkingDelta` events and `show_thinking=True` → thinking
  `LiveMessage` created and finalised; `_thinking_store` is not populated
  (we already have the live message).
- Same with `show_thinking=False` → no thinking `LiveMessage`;
  `_thinking_store[task.id]` populated as today.
- Error finalisation appends `Error: …` to the live message.

## Rollout

- Default `show_thinking=False` — existing users see no change except that
  the answer now streams in real time.
- Migration: none needed. The new config field defaults cleanly on load.

## Open questions

_None at design time. Any that surface during planning are captured in the
implementation plan._
