# UX Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add streaming output, file uploads, image responses, unsupported message handling, and command UX polish to the Telegram bot.

**Architecture:** The core change is rewriting `ClaudeClient` to use `--output-format stream-json --verbose` instead of `--output-format json`, yielding incremental `StreamEvent` objects via an async generator. A new `stream.py` module handles parsing. The bot layer receives stream events for progressive message edits and image detection. Input handling (file uploads, unsupported types) and command UX improvements are additive changes to `bot.py`.

**Tech Stack:** Python 3.11+, python-telegram-bot 22.0+, Claude CLI `stream-json` format

**Spec:** `docs/superpowers/specs/2026-04-02-ux-improvements-design.md`

**Important — Claude CLI `stream-json` format (verified):**

The CLI requires `--verbose` with `--output-format stream-json`. Output is JSON lines with these event types:

- `{"type": "system", "subtype": "init", ...}` — session init
- `{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}, {"type": "tool_use", "name": "Write", "input": {"file_path": "..."}}]}, "session_id": "..."}` — assistant output (text chunks and tool uses)
- `{"type": "result", "result": "...", "session_id": "...", "modelUsage": {...}}` — final result
- `{"type": "system", ...}` / `{"type": "rate_limit_event", ...}` — metadata (ignore)

Text and tool_use are nested inside `assistant.message.content[]` as array items.

---

## Phase 1: Streaming Foundation

### Task 1: Create stream.py — StreamEvent types and parser

**Files:**
- Create: `src/link_project_to_chat/stream.py`
- Create: `tests/test_stream.py`

- [ ] **Step 1: Write tests for StreamEvent types and parser**

Create `tests/test_stream.py`:

```python
from __future__ import annotations

import json

from link_project_to_chat.stream import (
    Error,
    Result,
    StreamEvent,
    TextDelta,
    ToolUse,
    parse_stream_line,
)


class TestStreamEventTypes:
    def test_text_delta(self):
        e = TextDelta(text="hello")
        assert e.text == "hello"

    def test_tool_use(self):
        e = ToolUse(tool="Write", path="/tmp/foo.png")
        assert e.tool == "Write"
        assert e.path == "/tmp/foo.png"

    def test_tool_use_no_path(self):
        e = ToolUse(tool="Bash", path=None)
        assert e.path is None

    def test_result(self):
        e = Result(text="done", session_id="sess-1", model=None)
        assert e.text == "done"
        assert e.session_id == "sess-1"

    def test_error(self):
        e = Error(message="fail")
        assert e.message == "fail"


class TestParseStreamLine:
    def test_text_content(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hello world"}],
            },
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hello world"

    def test_tool_use_with_file_path(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "/tmp/image.png", "content": "..."},
                }],
            },
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ToolUse)
        assert events[0].tool == "Write"
        assert events[0].path == "/tmp/image.png"

    def test_tool_use_without_file_path(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }],
            },
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ToolUse)
        assert events[0].tool == "Bash"
        assert events[0].path is None

    def test_result_event(self):
        line = json.dumps({
            "type": "result",
            "result": "final answer",
            "session_id": "sess-abc",
            "modelUsage": {"claude-opus-4-6": {}},
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], Result)
        assert events[0].text == "final answer"
        assert events[0].session_id == "sess-abc"
        assert events[0].model == "claude-opus-4-6"

    def test_result_event_no_model(self):
        line = json.dumps({
            "type": "result",
            "result": "ok",
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert isinstance(events[0], Result)
        assert events[0].model is None

    def test_system_event_ignored(self):
        line = json.dumps({"type": "system", "subtype": "init"})
        events = parse_stream_line(line)
        assert events == []

    def test_rate_limit_event_ignored(self):
        line = json.dumps({"type": "rate_limit_event"})
        events = parse_stream_line(line)
        assert events == []

    def test_multiple_content_items(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll create a file."},
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/tmp/out.txt"},
                    },
                ],
            },
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert len(events) == 2
        assert isinstance(events[0], TextDelta)
        assert isinstance(events[1], ToolUse)

    def test_invalid_json_returns_empty(self):
        events = parse_stream_line("not json at all")
        assert events == []

    def test_unknown_type_returns_empty(self):
        line = json.dumps({"type": "unknown_future_type"})
        events = parse_stream_line(line)
        assert events == []

    def test_is_error_result(self):
        line = json.dumps({
            "type": "result",
            "is_error": True,
            "result": "something went wrong",
            "session_id": "s1",
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], Error)
        assert events[0].message == "something went wrong"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_stream.py -v`
Expected: FAIL — `stream` module does not exist.

- [ ] **Step 3: Implement stream.py**

Create `src/link_project_to_chat/stream.py`:

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class StreamEvent:
    """Base class for stream events."""


@dataclass
class TextDelta(StreamEvent):
    text: str


@dataclass
class ToolUse(StreamEvent):
    tool: str
    path: str | None


@dataclass
class Result(StreamEvent):
    text: str
    session_id: str | None
    model: str | None


@dataclass
class Error(StreamEvent):
    message: str


def parse_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single JSON line from Claude CLI stream-json output.

    Returns a list of StreamEvent objects (usually 0 or 1, but an assistant
    message with multiple content items can produce several).
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Ignoring non-JSON stream line: %s", line[:100])
        return []

    event_type = data.get("type")

    if event_type == "result":
        if data.get("is_error"):
            return [Error(message=data.get("result", "Unknown error"))]
        model_usage = data.get("modelUsage", {})
        model = next(iter(model_usage), None)
        return [Result(
            text=data.get("result", ""),
            session_id=data.get("session_id"),
            model=model,
        )]

    if event_type == "assistant":
        message = data.get("message", {})
        content = message.get("content", [])
        events: list[StreamEvent] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "")
                if text:
                    events.append(TextDelta(text=text))
            elif item_type == "tool_use":
                tool_name = item.get("name", "unknown")
                tool_input = item.get("input", {})
                file_path = tool_input.get("file_path")
                events.append(ToolUse(tool=tool_name, path=file_path))
        return events

    # system, rate_limit_event, and unknown types are ignored
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_stream.py -v`
Expected: All tests pass.

- [ ] **Step 5: Run ruff and format**

Run: `python3 -m ruff check src/link_project_to_chat/stream.py tests/test_stream.py --fix && python3 -m ruff format src/link_project_to_chat/stream.py tests/test_stream.py`

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/stream.py tests/test_stream.py
git commit -m "feat: add stream.py — StreamEvent types and stream-json parser"
```

---

### Task 2: Add chat_stream() to ClaudeClient

**Files:**
- Modify: `src/link_project_to_chat/claude_client.py`
- Modify: `tests/test_claude_client.py`

- [ ] **Step 1: Write tests for chat_stream()**

Add to `tests/test_claude_client.py`:

```python
import asyncio
from unittest.mock import MagicMock, patch, PropertyMock
from link_project_to_chat.stream import TextDelta, Result, ToolUse, Error


def _mock_stream_popen(lines: list[str], returncode: int = 0):
    """Create a mock Popen whose stdout yields JSON lines."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = returncode
    # stdout is an iterable of bytes lines
    mock_proc.stdout.__iter__ = lambda self: iter(
        (line + "\n").encode() for line in lines
    )
    mock_proc.stderr.read.return_value = b""
    mock_proc.wait.return_value = returncode
    return mock_proc


class TestChatStream:
    async def test_yields_text_and_result(self, client):
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]},"session_id":"s1"}',
            '{"type":"result","result":"hello","session_id":"s1","modelUsage":{"claude-sonnet-4-20250514":{}}}',
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("hi"):
                events.append(event)

        assert len(events) == 2
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hello"
        assert isinstance(events[1], Result)
        assert events[1].session_id == "s1"

    async def test_yields_tool_use(self, client):
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":"/tmp/img.png"}}]},"session_id":"s1"}',
            '{"type":"result","result":"done","session_id":"s1"}',
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("create image"):
                events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_events) == 1
        assert tool_events[0].path == "/tmp/img.png"

    async def test_updates_session_id_from_result(self, client):
        lines = [
            '{"type":"result","result":"ok","session_id":"new-sess","modelUsage":{"sonnet":{}}}',
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            async for _ in client.chat_stream("test"):
                pass

        assert client.session_id == "new-sess"
        assert client.model == "sonnet"

    async def test_yields_error_on_nonzero_exit(self, client):
        lines = []
        mock_proc = _mock_stream_popen(lines, returncode=1)
        mock_proc.stderr.read.return_value = b"something broke"

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("test"):
                events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Error)
        assert "something broke" in events[0].message

    async def test_command_uses_stream_json(self, client):
        lines = [
            '{"type":"result","result":"ok","session_id":"s1"}',
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_cls:
            async for _ in client.chat_stream("test"):
                pass

        cmd = mock_cls.call_args[0][0]
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd


class TestChatWrapsStream:
    async def test_chat_collects_text(self, client):
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hello "}]},"session_id":"s1"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"world"}]},"session_id":"s1"}',
            '{"type":"result","result":"hello world","session_id":"s1"}',
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            result = await client.chat("test")

        assert result == "hello world"
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `venv/bin/python -m pytest tests/test_claude_client.py::TestChatStream -v`
Expected: FAIL — `chat_stream` doesn't exist yet.

- [ ] **Step 3: Implement chat_stream() and update chat()**

In `src/link_project_to_chat/claude_client.py`, add imports at top:

```python
from collections.abc import AsyncGenerator
from .stream import StreamEvent, TextDelta, Result, Error, parse_stream_line
```

Add `chat_stream()` method to `ClaudeClient`:

```python
    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "stream-json",
            "--verbose",
            "--effort", self.effort,
            "--dangerously-skip-permissions",
        ]

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        cmd.extend(["--", user_message])

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if on_proc:
            on_proc(proc)
        logger.info("claude stream subprocess started pid=%s", proc.pid)

        def _read_lines():
            lines = []
            for raw_line in proc.stdout:
                lines.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
            return lines

        try:
            all_lines = await asyncio.to_thread(_read_lines)
            for line in all_lines:
                if not line.strip():
                    continue
                for event in parse_stream_line(line):
                    if isinstance(event, Result):
                        self.session_id = event.session_id or self.session_id
                        if event.model:
                            self.model = event.model
                    yield event

            stderr_text = await asyncio.to_thread(proc.stderr.read)
            await asyncio.to_thread(proc.wait)

            if proc.returncode != 0:
                err = stderr_text.decode("utf-8", errors="replace").strip()
                yield Error(message=err or f"exit code {proc.returncode}")
        finally:
            logger.info("claude stream pid=%s done, code=%s", proc.pid, proc.returncode)
```

Replace the existing `chat()` method to wrap `chat_stream()`:

```python
    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> str:
        result_text = ""
        async for event in self.chat_stream(user_message, on_proc=on_proc):
            if isinstance(event, Result):
                result_text = event.text
            elif isinstance(event, Error):
                return f"Error: {event.message}"
        return result_text or "[No response]"
```

Remove the old `chat()` implementation entirely (the one using `--output-format json` and `proc.communicate()`).

- [ ] **Step 4: Run all claude_client tests**

Run: `venv/bin/python -m pytest tests/test_claude_client.py -v`
Expected: All tests pass (existing tests may need minor updates since `chat()` now delegates to `chat_stream()`).

- [ ] **Step 5: Run ruff and format**

Run: `python3 -m ruff check src/link_project_to_chat/claude_client.py tests/test_claude_client.py --fix && python3 -m ruff format src/link_project_to_chat/claude_client.py tests/test_claude_client.py`

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/claude_client.py tests/test_claude_client.py
git commit -m "feat: add chat_stream() async generator with stream-json parsing"
```

---

### Task 3: Update TaskManager to forward stream events

**Files:**
- Modify: `src/link_project_to_chat/task_manager.py`
- Modify: `tests/test_task_manager.py`

- [ ] **Step 1: Write test for stream event forwarding**

Add to `tests/test_task_manager.py`:

```python
from link_project_to_chat.stream import TextDelta, Result


class TestStreamEventForwarding:
    async def test_stream_events_forwarded(self, callbacks):
        stream_events_received = []

        async def on_stream(task, event):
            stream_events_received.append((task.id, event))

        manager = TaskManager(
            project_path=Path("/tmp"),
            on_complete=callbacks["on_complete"],
            on_task_started=callbacks["on_task_started"],
            on_stream_event=on_stream,
        )

        async def fake_stream(*args, **kwargs):
            yield TextDelta(text="hello ")
            yield TextDelta(text="world")
            yield Result(text="hello world", session_id="s1", model=None)

        with patch.object(
            manager.claude, "chat_stream", side_effect=fake_stream
        ):
            task = manager.submit_claude(
                chat_id=1, message_id=10, prompt="hi"
            )
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task.result == "hello world"
        assert len(stream_events_received) == 3
        assert isinstance(stream_events_received[0][1], TextDelta)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_task_manager.py::TestStreamEventForwarding -v`
Expected: FAIL — `on_stream_event` parameter doesn't exist.

- [ ] **Step 3: Update TaskManager to accept and use on_stream_event**

In `src/link_project_to_chat/task_manager.py`:

Add import at top:

```python
from .stream import StreamEvent, TextDelta, Result, Error
```

Update `__init__`:

```python
    def __init__(self, project_path: Path,
                 on_complete: OnTaskEvent, on_task_started: OnTaskEvent,
                 on_stream_event: Callable[[Task, StreamEvent], Awaitable[None]] | None = None):
        self.project_path = project_path
        self._on_complete = on_complete
        self._on_task_started = on_task_started
        self._on_stream_event = on_stream_event
        self._next_id = 1
        self._tasks: dict[int, Task] = {}
        self._claude = ClaudeClient(project_path)
```

Replace `_exec_claude`:

```python
    async def _exec_claude(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = time.monotonic()
        await self._safe_callback(self._on_task_started, task)
        try:
            if task._compact:
                task.result = await self._do_compact()
            else:
                collected_text: list[str] = []
                async for event in self._claude.chat_stream(
                    task.input,
                    on_proc=lambda p: setattr(task, '_proc', p),
                ):
                    if self._on_stream_event:
                        await self._safe_callback(
                            lambda t: self._on_stream_event(t, event), task
                        )
                    if isinstance(event, TextDelta):
                        collected_text.append(event.text)
                    elif isinstance(event, Result):
                        task.result = event.text
                    elif isinstance(event, Error):
                        raise RuntimeError(event.message)
                if task.result is None:
                    task.result = "".join(collected_text) or "[No response]"
            task.status = TaskStatus.DONE
        except asyncio.CancelledError:
            if task._proc and task._proc.poll() is None:
                task._proc.kill()
            task.status = TaskStatus.CANCELLED
        except Exception as e:
            logger.exception("Claude task #%d failed", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
        finally:
            task.finished_at = time.monotonic()
        if task.status != TaskStatus.CANCELLED:
            await self._safe_callback(self._on_complete, task)
```

- [ ] **Step 4: Run all task_manager tests**

Run: `venv/bin/python -m pytest tests/test_task_manager.py -v`
Expected: All tests pass. Existing tests use `patch.object(manager.claude, "chat", ...)` which still works since `chat()` wraps `chat_stream()`.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/task_manager.py tests/test_task_manager.py
git commit -m "feat: forward stream events from TaskManager to bot layer"
```

---

### Task 4: Progressive message edits in bot.py

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Add stream event handler and progressive edit logic**

In `src/link_project_to_chat/bot.py`, add imports:

```python
import time as time_mod
from .stream import StreamEvent, TextDelta, ToolUse, Result
```

Add a dict to track in-progress streaming messages in `__init__`:

```python
        self._stream_messages: dict[int, tuple[int, float]] = {}  # task_id -> (message_id, last_edit_time)
        self._stream_text: dict[int, str] = {}  # task_id -> accumulated text
```

Update `TaskManager` construction to pass the new callback:

```python
        self.task_manager = TaskManager(
            project_path=self.path,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
        )
```

Add the stream event handler:

```python
    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, TextDelta):
            self._stream_text.setdefault(task.id, "")
            self._stream_text[task.id] += event.text

            if task.id not in self._stream_messages:
                # First text — send a new message
                text = self._stream_text[task.id]
                html = md_to_telegram(text).replace("\x00", "")
                try:
                    msg = await self._app.bot.send_message(
                        task.chat_id, html or "...",
                        parse_mode="HTML",
                        reply_to_message_id=task.message_id,
                    )
                    self._stream_messages[task.id] = (msg.message_id, time_mod.time())
                except Exception:
                    logger.warning("Failed to send initial stream message", exc_info=True)
            else:
                # Subsequent text — edit message (rate-limited)
                msg_id, last_edit = self._stream_messages[task.id]
                now = time_mod.time()
                if now - last_edit >= 2.0:  # batch edits every 2 seconds
                    text = self._stream_text[task.id]
                    html = md_to_telegram(text).replace("\x00", "")
                    try:
                        await self._app.bot.edit_message_text(
                            html or "...",
                            chat_id=task.chat_id,
                            message_id=msg_id,
                            parse_mode="HTML",
                        )
                        self._stream_messages[task.id] = (msg_id, now)
                    except Exception:
                        logger.debug("Stream edit failed", exc_info=True)

        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(task.chat_id, event.path, reply_to=task.message_id)
```

Update `_on_task_complete` to do a final edit instead of sending a new message when streaming was active:

```python
    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.CLAUDE:
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            if task._compact:
                text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
                await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            elif task.id in self._stream_messages:
                # Streaming was active — do final edit
                msg_id, _ = self._stream_messages.pop(task.id)
                self._stream_text.pop(task.id, None)
                if task.status == TaskStatus.DONE:
                    text = task.result or "[No output]"
                    html = md_to_telegram(text).replace("\x00", "")
                    for i, chunk in enumerate(split_html(html)):
                        try:
                            if i == 0:
                                await self._app.bot.edit_message_text(
                                    chunk, chat_id=task.chat_id,
                                    message_id=msg_id, parse_mode="HTML",
                                )
                            else:
                                await self._app.bot.send_message(
                                    task.chat_id, chunk, parse_mode="HTML",
                                    reply_to_message_id=task.message_id,
                                )
                        except Exception:
                            logger.warning("Final stream edit failed", exc_info=True)
                            plain = strip_html(chunk).replace("\x00", "")
                            if plain.strip():
                                await self._app.bot.send_message(
                                    task.chat_id, plain[:4096],
                                    reply_to_message_id=task.message_id,
                                )
                else:
                    await self._app.bot.edit_message_text(
                        f"Error: {task.error}",
                        chat_id=task.chat_id, message_id=msg_id,
                    )
            else:
                # No streaming (fallback)
                text = task.result if task.status == TaskStatus.DONE else f"Error: {task.error}"
                await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
        else:
            output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + "\n... (truncated, use /log)"
            if task.status != TaskStatus.DONE:
                await self._send_raw(task.chat_id, f"[exit {task.exit_code}]\n\n{output}")
            else:
                await self._send_raw(task.chat_id, f"{output}\n[exit 0]")
```

Note: the `else` branch for commands now always shows exit code (spec 5c — `[exit 0]` suffix on success).

- [ ] **Step 2: Add image helper methods**

Add to `ProjectBot`:

```python
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath
        return PurePosixPath(path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _send_image(self, chat_id: int, file_path: str,
                          reply_to: int | None = None) -> None:
        path = self.path / file_path if not file_path.startswith("/") else Path(file_path)
        if not path.exists():
            logger.warning("Image file not found: %s", path)
            return
        try:
            size = path.stat().st_size
            suffix = path.suffix.lower()
            if suffix == ".svg" or size > 10 * 1024 * 1024:
                await self._app.bot.send_document(
                    chat_id, open(path, "rb"),
                    filename=path.name,
                    reply_to_message_id=reply_to,
                )
            else:
                await self._app.bot.send_photo(
                    chat_id, open(path, "rb"),
                    caption=path.name,
                    reply_to_message_id=reply_to,
                )
        except Exception:
            logger.warning("Failed to send image %s", path, exc_info=True)
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `venv/bin/python -m pytest -m "not integration" -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: progressive message edits via streaming, image response detection"
```

---

## Phase 2: Input Handling

### Task 5: File upload handler

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Add file upload handler**

Add to `ProjectBot`:

```python
    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.") if msg else None

        uploads_dir = self.path / "uploads"
        uploads_dir.mkdir(exist_ok=True)

        if msg.photo:
            # Pick highest resolution
            photo = msg.photo[-1]
            file = await photo.get_file()
            filename = f"photo_{int(time.monotonic() * 1000)}.jpg"
        elif msg.document:
            file = await msg.document.get_file()
            raw_name = msg.document.file_name or f"file_{int(time.monotonic() * 1000)}"
            # Sanitize filename
            filename = "".join(
                c for c in raw_name.replace("/", "_").replace("\\", "_")
                if c.isalnum() or c in "._- "
            )[:200]
        else:
            return await msg.reply_text("Unsupported file type.")

        # Handle name collisions
        dest = uploads_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 2
            while dest.exists():
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        await file.download_to_drive(str(dest))

        caption = msg.caption or ""
        prompt = f"[User uploaded uploads/{filename}]"
        if caption:
            prompt += f"\n\n{caption}"

        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )
```

- [ ] **Step 2: Add unsupported message handler**

Add to `ProjectBot`:

```python
    async def _on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        if msg.voice or msg.video_note:
            text = "Voice messages aren't supported yet. Please type your message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        await msg.reply_text(text)
```

- [ ] **Step 3: Register new handlers in build()**

In `build()`, after the existing `MessageHandler` for text, add:

```python
        # File uploads (documents and photos)
        file_filter = filters.Document.ALL | filters.PHOTO
        app.add_handler(MessageHandler(file_filter, self._on_file))

        # Unsupported message types
        unsupported_filter = (
            filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL
            | filters.VIDEO | filters.LOCATION | filters.CONTACT
            | filters.AUDIO
        )
        app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))
```

- [ ] **Step 4: Verify tests still pass**

Run: `venv/bin/python -m pytest -m "not integration" -v`

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: add file upload handler and unsupported message replies"
```

---

## Phase 3: Command UX Polish

### Task 6: /help command and /log truncation

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Add /help command**

Add `("help", "Show available commands")` to the `COMMANDS` list.

Add handler:

```python
    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        cmd_list = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        await update.effective_message.reply_text(cmd_list)
```

Add `"help": self._on_help` to the handlers dict in `build()`.

- [ ] **Step 2: Truncate /log output**

In `_on_log`, replace the section where `task.result` is appended (around the `elif task.result:` block). Replace with:

```python
        if task.status == TaskStatus.RUNNING:
            tail = task.tail(10)
            if tail:
                lines.append(f"\n{tail}")
            else:
                lines.append(f"\nRunning for {task.elapsed_human}...")
        elif task.result:
            output = task.result
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result)} chars total)"
            lines.append(f"\n{output}")
        elif task.error:
            lines.append(f"\nError: {task.error}")
        elif task.status == TaskStatus.WAITING:
            lines.append("\nWaiting...")
```

- [ ] **Step 3: Verify tests still pass**

Run: `venv/bin/python -m pytest -m "not integration" -v`

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: add /help command, truncate /log output"
```

---

### Task 7: /reset confirmation with inline keyboard

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Add inline keyboard imports**

Add to the telegram imports in `bot.py`:

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
```

- [ ] **Step 2: Replace _on_reset with confirmation prompt**

```python
    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, reset", callback_data="reset_confirm"),
            InlineKeyboardButton("Cancel", callback_data="reset_cancel"),
        ]])
        await update.effective_message.reply_text(
            "Are you sure? This will clear the Claude session.",
            reply_markup=keyboard,
        )
```

- [ ] **Step 3: Add callback handler for reset**

```python
    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._auth(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()

        if query.data == "reset_confirm":
            self.task_manager.cancel_all()
            self.task_manager.claude.session_id = None
            clear_session(self.name)
            await query.edit_message_text("Session reset.")
        elif query.data == "reset_cancel":
            await query.edit_message_text("Reset cancelled.")
        elif query.data.startswith("task_cancel_"):
            task_id = int(query.data.split("_")[-1])
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await query.edit_message_text(f"#{task_id} cancelled.")
            else:
                await query.edit_message_text(f"#{task_id} not found or already finished.")
        elif query.data.startswith("task_log_"):
            task_id = int(query.data.split("_")[-1])
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            await query.edit_message_text(f"Task #{task_id}:\n{output}")
```

- [ ] **Step 4: Register callback handler in build()**

Add after the error handler registration:

```python
        app.add_handler(CallbackQueryHandler(self._on_callback))
```

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: /reset confirmation dialog with inline keyboard"
```

---

### Task 8: Inline keyboards on /tasks

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Update _on_tasks to include inline buttons**

Replace `_on_tasks`:

```python
    async def _on_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return
        tasks = self.task_manager.list_tasks(chat_id=update.effective_chat.id)
        if not tasks:
            return await update.effective_message.reply_text("No tasks.")

        icons = {
            TaskStatus.WAITING: "~",
            TaskStatus.RUNNING: ">",
            TaskStatus.DONE: "+",
            TaskStatus.FAILED: "!",
            TaskStatus.CANCELLED: "x",
        }
        lines = []
        buttons = []
        for t in tasks:
            icon = icons.get(t.status, "?")
            elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:50]
            lines.append(f"{icon} #{t.id} [{t.type.value}]{elapsed} {label}")

            row = []
            if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                row.append(InlineKeyboardButton(
                    f"Cancel #{t.id}", callback_data=f"task_cancel_{t.id}"
                ))
            if t.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                row.append(InlineKeyboardButton(
                    f"Log #{t.id}", callback_data=f"task_log_{t.id}"
                ))
            if row:
                buttons.append(row)

        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=markup,
        )
```

- [ ] **Step 2: Verify tests still pass**

Run: `venv/bin/python -m pytest -m "not integration" -v`

- [ ] **Step 3: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: inline keyboard buttons on /tasks output"
```

---

### Task 9: Final verification and lint

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `venv/bin/python -m pytest --cov=link_project_to_chat --cov-report=term-missing -m "not integration" -v`
Expected: All tests pass.

- [ ] **Step 2: Run ruff and mypy**

Run: `python3 -m ruff check src/ tests/ --fix && python3 -m ruff format src/ tests/ && mypy src/`
Expected: Clean.

- [ ] **Step 3: Fix any issues found**

Fix lint/type errors if any.

- [ ] **Step 4: Commit if needed**

```bash
git add -A
git commit -m "chore: fix lint and type issues after UX improvements"
```
