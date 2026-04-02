# UX Improvements Design: link-project-to-chat

**Date**: 2026-04-02
**Scope**: Full UX pass — input handling, output handling, command UX
**Depends on**: Full cleanup (phases 1-5) should be completed first

---

## Overview

Three tracks of improvements to the Telegram bot UX:

- **A. Input handling** — file uploads, unsupported message type replies
- **B. Output handling** — streaming responses with progressive edits, image detection and sending
- **C. Command UX** — `/help`, `/log` truncation, `/reset` confirmation, `/run` exit codes, inline keyboards on `/tasks`

---

## Section 1: Streaming Output — ClaudeClient Rewrite

### New interface

`ClaudeClient` gains a new primary method:

```python
async def chat_stream(self, user_message: str, on_proc=None) -> AsyncGenerator[StreamEvent, None]:
    """Yields StreamEvent objects as Claude produces output."""
```

### StreamEvent types

Defined in a new `src/link_project_to_chat/stream.py` module:

- `TextDelta(text: str)` — partial text chunk from Claude's response
- `ToolUse(tool: str, path: str | None)` — Claude used a tool (file write, edit, etc.)
- `Result(text: str, session_id: str | None)` — final result with session ID
- `Error(message: str)` — error from subprocess

### How it works

1. Subprocess is launched with `--output-format stream-json` instead of `--output-format json`
2. Stdout is read line-by-line in a thread via `asyncio.to_thread`
3. Each line is a JSON object — parsed and yielded as the appropriate `StreamEvent`
4. The existing `chat()` method becomes a thin wrapper that collects all `TextDelta` events and returns the concatenated result (backward-compatible for `/compact` and other internal callers)

### stream-json event format

The Claude CLI `stream-json` output includes events like:

```json
{"type": "assistant", "subtype": "tool_use", "tool": {"name": "Write", "params": {"file_path": "/abs/path/to/file.png"}}}
```

The exact field names must be verified during implementation by inspecting actual `stream-json` output. The parsing logic should be defensive and log unrecognized event shapes rather than crashing.

### Impact on TaskManager

`_exec_claude` changes from calling `await self._claude.chat(...)` to iterating over `chat_stream()`. It accumulates text and forwards events to a new callback `on_stream_event` provided by the bot layer for progressive message edits.

`TaskManager.__init__` gains a new callback parameter: `on_stream_event: Callable[[Task, StreamEvent], Awaitable[None]]`.

### Impact on bot.py

The bot receives stream events via callback and:

- On first `TextDelta`: sends a new message with the partial text
- On subsequent `TextDelta`s: edits the message (batched at ~2-3 second intervals to stay under Telegram's ~30 edits/min rate limit)
- On `ToolUse` with an image path: sends the image via `send_photo()`
- On `Result`: final edit with complete text, extracts session_id

---

## Section 2: File Upload Handling

### Upload flow

1. New `MessageHandler` registered for `filters.Document.ALL | filters.PHOTO` in `build()`
2. Handler method `_on_file` downloads the file to `{project_path}/uploads/{filename}`
3. Creates the `uploads/` directory on first use
4. For photos (which have no filename), generates one: `photo_{timestamp}.jpg`
5. For documents, preserves the original filename. If a file with the same name exists, appends a counter: `report.pdf` -> `report_2.pdf`
6. Constructs a prompt: `"[User uploaded uploads/{filename}]\n\n{caption or ''}"` and submits it as a normal Claude task via `task_manager.submit_claude()`

### Why `uploads/` subdirectory

Keeps uploaded files separate from project source. Claude can still access them via relative path. Easy to `.gitignore` if desired.

### Security considerations

- Max file size: 20MB (Telegram's own limit handles this, but we check and reply with an error if exceeded)
- Filename sanitization: strip path separators, limit length, replace unsafe characters
- No symlink following when creating the upload path

### Photo handling specifics

Telegram sends photos as multiple resolutions. The handler picks the largest resolution (`message.photo[-1]`) and downloads it.

---

## Section 3: Image Response Detection

### How it works with streaming

As the bot's `on_stream_event` callback receives `StreamEvent` objects during a Claude task:

1. When a `ToolUse` event is received where the tool indicates a file write (e.g., `tool="Write"` or `tool="Edit"`), extract the file path
2. Check if the path extension matches the image set: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`
3. If it's an image and the file exists at `{project_path}/{path}`, send it via `bot.send_photo(chat_id, open(path, 'rb'))` with the filename as caption
4. If the file is larger than 10MB (Telegram's photo limit), send as document instead via `send_document()`
5. Images are sent as separate messages, interleaved with the text response — they appear in the chat right when Claude creates them, not at the end

### SVG special case

Telegram doesn't render SVG as photos. SVGs are always sent via `send_document()` regardless of size.

---

## Section 4: Unsupported Message Types

### Handled types and responses

| Message type | Filter | Response |
|---|---|---|
| Voice message | `filters.VOICE \| filters.VIDEO_NOTE` | "Voice messages aren't supported yet. Please type your message." |
| Sticker | `filters.Sticker.ALL` | "Stickers aren't supported. Please type your message." |
| Video | `filters.VIDEO` | "Video messages aren't supported. Please type your message." |
| Other | Catch-all for remaining non-text | "This message type isn't supported. Please type your message or send a file." |

### Implementation

A single handler with a broad filter covering everything not already handled (text, commands, documents, photos). The handler checks the message type and replies with the appropriate message. One method, one handler registration.

Auth check runs first — unauthorized users get "Unauthorized." regardless of message type.

---

## Section 5: Command UX Improvements

### 5a. `/help` command

Same output as `/start` but without the project name/path header. Just the command list. Added to `COMMANDS` list and handler map.

### 5b. `/log` truncation

Currently `/log` dumps the entire `task.result` with no limit. Fix: truncate output to 3000 characters (matching the existing truncation in `_on_task_complete`). When truncated, append `"\n... (truncated, {total_len} chars total)"`. Full output remains accessible via `/run cat` or direct file access.

### 5c. `/run` exit code on success

Currently successful commands show raw output, failed ones show `[exit N]`. Change: always show exit code. Successful commands get `\n[exit 0]` appended inside the `<pre>` block after the command output (not as a prefix, to keep the output scannable).

### 5d. `/reset` confirmation

Replace the immediate reset with an inline keyboard:

```
Are you sure? This will clear the Claude session.
[Yes, reset] [Cancel]
```

Uses Telegram's `InlineKeyboardMarkup` with `CallbackQueryHandler`. "Yes" triggers the existing reset logic. "Cancel" edits the message to "Reset cancelled." Callback data is prefixed with `reset_` to namespace it.

### 5e. Inline keyboards on `/tasks`

Each task in the `/tasks` output gets inline buttons based on its status:

- Running tasks: `[Cancel] [Log]`
- Completed/failed tasks: `[Log]`
- Waiting tasks: `[Cancel]`

Each button's callback data encodes the action and task ID, e.g., `task_cancel_5`, `task_log_5`. A single `CallbackQueryHandler` with pattern `^task_` routes these. The handler calls the existing `cancel()` or formats log output.

---

## Section 6: File Organization

### New files

- `src/link_project_to_chat/stream.py` — `StreamEvent` dataclasses (`TextDelta`, `ToolUse`, `Result`, `Error`) and the stream-json line parser

### Modified files

- `claude_client.py` — Add `chat_stream()` async generator. Existing `chat()` becomes a wrapper. Removes `proc.communicate()` path.
- `task_manager.py` — `_exec_claude()` iterates stream events. New `on_stream_event` callback parameter on `TaskManager.__init__`.
- `bot.py` — Largest changes:
  - New handler: `_on_file` (document/photo uploads)
  - New handler: `_on_unsupported` (voice/sticker/video/other)
  - New handler: `_on_callback` (inline keyboard callbacks for reset + task actions)
  - Modified: `_on_task_started` / `_on_task_complete` adapt to streaming
  - New helper: `_send_image` (send photo or document based on size/type)
  - Modified: `build()` registers new handlers + `CallbackQueryHandler`
  - Modified: `/tasks`, `/reset`, `/run` output per sections above
  - New: `/help` handler
- `formatting.py` — No changes
- `config.py` — No changes
- `cli.py` — No changes

### Testing considerations

- `stream.py` is pure parsing — highly testable with fixture JSON lines
- `chat_stream()` can be tested with a mock subprocess that writes stream-json lines
- Inline keyboard callbacks need mock `CallbackQuery` objects
- File upload needs mock `Document`/`PhotoSize` objects with `get_file()` stubs
