# Backend Phase 1 Claude Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the current Claude-specific runtime behind an `AgentBackend` interface without changing user-visible behavior.

**Architecture:** Shared stream event dataclasses move into a transport-neutral `events.py`; Claude-specific parsing and subprocess behavior move into `src/link_project_to_chat/backends/`; `TaskManager` stops constructing `ClaudeClient` directly and instead receives an injected backend created by a factory in `bot.py`.

**Tech Stack:** Python 3.11+, asyncio, subprocess, pytest

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/events.py` | **NEW**: shared `StreamEvent` dataclasses moved out of `stream.py` |
| `src/link_project_to_chat/stream.py` | Replace with temporary shim that re-exports events + Claude parser |
| `src/link_project_to_chat/backends/__init__.py` | **NEW**: package marker and explicit exports |
| `src/link_project_to_chat/backends/base.py` | **NEW**: `AgentBackend` Protocol, `BackendCapabilities`, `HealthStatus` |
| `src/link_project_to_chat/backends/factory.py` | **NEW**: registry-based backend factory |
| `src/link_project_to_chat/backends/claude_parser.py` | **NEW**: `parse_stream_line()` moved out of `stream.py` |
| `src/link_project_to_chat/backends/claude.py` | **NEW**: `ClaudeBackend` moved from `claude_client.py`, plus `probe_health()` and factory registration |
| `src/link_project_to_chat/claude_client.py` | Temporary re-export shim in mid-phase; deleted at end of phase |
| `src/link_project_to_chat/task_manager.py` | Inject backend, rename Claude-specific identifiers to agent-neutral names |
| `src/link_project_to_chat/bot.py` | Build backend through the factory; add `_claude` tier-2 accessor; stop constructing `ClaudeClient` directly |
| `src/link_project_to_chat/skills.py` | Add backend-agnostic skills docstring |
| `tests/test_stream.py` | Update imports to prefer `events.py` / `backends.claude_parser.py` |
| `tests/test_task_manager.py` | Update for injected backend and renamed task manager API |
| `tests/backends/fakes.py` | **NEW**: `FakeBackend` for unit and contract tests |
| `tests/backends/test_factory.py` | **NEW**: factory registration / duplicate registration coverage |
| `tests/backends/test_contract.py` | **NEW**: backend contract test for Claude + fake backend |
| `tests/backends/test_claude_backend.py` | **NEW**: Claude backend tests moved from `test_claude_client.py` plus `probe_health()` coverage |
| `tests/test_backend_lockout.py` | **NEW**: task manager may not import `claude_client` or `backends.claude` |
| `tests/test_bot_backend_lockout.py` | **NEW**: `bot.py` may not construct `ClaudeClient` or reach through `task_manager.claude` |

---

### Task 1: Split Shared Events From The Claude Parser

**Files:**
- Create: `src/link_project_to_chat/events.py`
- Create: `src/link_project_to_chat/backends/claude_parser.py`
- Modify: `src/link_project_to_chat/stream.py`
- Modify: `tests/test_stream.py`
- Create: `tests/test_events_module.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_events_module.py
from link_project_to_chat.events import Error, Result, TextDelta, ThinkingDelta, ToolUse
from link_project_to_chat.stream import parse_stream_line


def test_events_module_exports_shared_types():
    assert TextDelta(text="hi").text == "hi"
    assert ThinkingDelta(text="reasoning").text == "reasoning"
    assert ToolUse(tool="Read", path="/tmp/x").tool == "Read"
    assert Result(text="done", session_id="s1", model="claude-3").session_id == "s1"
    assert Error(message="boom").message == "boom"


def test_stream_shim_still_exports_parse_stream_line():
    line = '{"type":"result","result":"done","session_id":"s1","modelUsage":{"claude-3":1}}'
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], Result)
```

- [ ] **Step 2: Run the focused tests to confirm the failures**

```bash
pytest tests/test_events_module.py tests/test_stream.py -v
```

Expected: `ModuleNotFoundError: No module named 'link_project_to_chat.events'`.

- [ ] **Step 3: Create `events.py` with the shared dataclasses**

```python
# src/link_project_to_chat/events.py
from __future__ import annotations

from dataclasses import dataclass


class StreamEvent:
    pass


@dataclass
class TextDelta(StreamEvent):
    text: str


@dataclass
class ThinkingDelta(StreamEvent):
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
class QuestionOption:
    label: str
    description: str


@dataclass
class Question:
    question: str
    header: str
    options: list[QuestionOption]
    multi_select: bool = False


@dataclass
class AskQuestion(StreamEvent):
    questions: list[Question]


@dataclass
class Error(StreamEvent):
    message: str
```

- [ ] **Step 4: Move the Claude parser into `backends/claude_parser.py` and turn `stream.py` into a shim**

```python
# src/link_project_to_chat/backends/claude_parser.py
from __future__ import annotations

import json
import logging

from ..events import (
    AskQuestion,
    Error,
    Question,
    QuestionOption,
    Result,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUse,
)

logger = logging.getLogger(__name__)


def parse_stream_line(line: str) -> list[StreamEvent]:
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
        return [
            Result(
                text=data.get("result", ""),
                session_id=data.get("session_id"),
                model=model,
            )
        ]

    if event_type == "stream_event":
        sub = data.get("event", {})
        if sub.get("type") != "content_block_delta":
            return []
        delta = sub.get("delta", {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text", "")
            return [TextDelta(text=text)] if text else []
        if delta_type == "thinking_delta":
            text = delta.get("thinking", "")
            return [ThinkingDelta(text=text)] if text else []
        return []

    if event_type == "assistant":
        message = data.get("message", {})
        content = message.get("content", [])
        events: list[StreamEvent] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "tool_use":
                tool_name = item.get("name", "unknown")
                tool_input = item.get("input", {})
                if tool_name == "AskUserQuestion":
                    raw_qs = tool_input.get("questions", [])
                    questions = []
                    for rq in raw_qs:
                        opts = [
                            QuestionOption(
                                label=o.get("label", ""),
                                description=o.get("description", ""),
                            )
                            for o in rq.get("options", [])
                        ]
                        questions.append(
                            Question(
                                question=rq.get("question", ""),
                                header=rq.get("header", ""),
                                options=opts,
                                multi_select=rq.get("multiSelect", False),
                            )
                        )
                    if questions:
                        events.append(AskQuestion(questions=questions))
                else:
                    file_path = tool_input.get("file_path")
                    events.append(ToolUse(tool=tool_name, path=file_path))
        return events

    return []
```

```python
# src/link_project_to_chat/stream.py
from .events import *  # noqa: F401,F403
from .backends.claude_parser import parse_stream_line  # noqa: F401
```

When moving the parser body, keep the logic byte-for-byte equivalent to the current implementation in `stream.py` so this task stays behavior-neutral.

- [ ] **Step 5: Update `tests/test_stream.py` to import event types from `events.py`**

```python
from link_project_to_chat.backends.claude_parser import parse_stream_line
from link_project_to_chat.events import Error, Result, TextDelta, ThinkingDelta, ToolUse
```

- [ ] **Step 6: Run the event/parser tests**

```bash
pytest tests/test_events_module.py tests/test_stream.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/events.py src/link_project_to_chat/backends/claude_parser.py src/link_project_to_chat/stream.py tests/test_events_module.py tests/test_stream.py
git commit -m "refactor: split shared stream events from claude parser"
```

---

### Task 2: Introduce The Backend Protocol And Factory

**Files:**
- Create: `src/link_project_to_chat/backends/__init__.py`
- Create: `src/link_project_to_chat/backends/base.py`
- Create: `src/link_project_to_chat/backends/factory.py`
- Create: `tests/backends/test_factory.py`

- [ ] **Step 1: Write the failing factory tests**

```python
# tests/backends/test_factory.py
from pathlib import Path

import pytest

from link_project_to_chat.backends.factory import available, create, register


class _DummyBackend:
    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.state = state


def test_register_and_create_backend():
    register("dummy", lambda project_path, state: _DummyBackend(project_path, state))

    backend = create("dummy", Path("/tmp/project"), {"model": "x"})

    assert backend.project_path == Path("/tmp/project")
    assert backend.state == {"model": "x"}
    assert "dummy" in available()


def test_duplicate_registration_fails():
    register("duplicate", lambda project_path, state: _DummyBackend(project_path, state))

    with pytest.raises(ValueError, match="already registered"):
        register("duplicate", lambda project_path, state: _DummyBackend(project_path, state))


def test_unknown_backend_fails():
    with pytest.raises(KeyError, match="Unknown backend"):
        create("missing", Path("/tmp/project"), {})
```

- [ ] **Step 2: Run the tests to confirm failure**

```bash
pytest tests/backends/test_factory.py -v
```

Expected: `ModuleNotFoundError: No module named 'link_project_to_chat.backends.factory'`.

- [ ] **Step 3: Create `backends/base.py` with the backend contract**

```python
# src/link_project_to_chat/backends/base.py
from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..events import StreamEvent


@dataclass(frozen=True)
class BackendCapabilities:
    models: Sequence[str]
    supports_thinking: bool
    supports_permissions: bool
    supports_resume: bool
    supports_compact: bool
    supports_allowed_tools: bool
    supports_usage_cap_detection: bool


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    usage_capped: bool
    error_message: str | None = None


class AgentBackend(Protocol):
    name: str
    capabilities: BackendCapabilities
    project_path: Path
    model: str | None
    session_id: str | None

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        pass

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> str:
        pass

    async def probe_health(self) -> HealthStatus:
        pass

    def close_interactive(self) -> None:
        pass

    def cancel(self) -> bool:
        pass

    @property
    def status(self) -> dict:
        pass
```

- [ ] **Step 4: Create the backend registry**

```python
# src/link_project_to_chat/backends/factory.py
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .base import AgentBackend

BackendFactory = Callable[[Path, dict], AgentBackend]

_registry: dict[str, BackendFactory] = {}


def register(name: str, factory: BackendFactory) -> None:
    if name in _registry:
        raise ValueError(f"Backend {name!r} already registered")
    _registry[name] = factory


def create(name: str, project_path: Path, state: dict) -> AgentBackend:
    if name not in _registry:
        raise KeyError(f"Unknown backend {name!r}; available: {sorted(_registry)}")
    return _registry[name](project_path, state)


def available() -> list[str]:
    return sorted(_registry)
```

- [ ] **Step 5: Add a minimal `__init__.py` for the package**

```python
# src/link_project_to_chat/backends/__init__.py
from .base import AgentBackend, BackendCapabilities, HealthStatus
from .factory import available, create, register

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "HealthStatus",
    "available",
    "create",
    "register",
]
```

- [ ] **Step 6: Run the factory tests**

```bash
pytest tests/backends/test_factory.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/backends/__init__.py src/link_project_to_chat/backends/base.py src/link_project_to_chat/backends/factory.py tests/backends/test_factory.py
git commit -m "refactor: add backend protocol and registry factory"
```

---

### Task 3: Move `ClaudeClient` Into `ClaudeBackend` And Add `probe_health()`

**Files:**
- Create: `src/link_project_to_chat/backends/claude.py`
- Modify: `src/link_project_to_chat/claude_client.py`
- Create: `tests/backends/test_claude_backend.py`
- Modify: `tests/test_claude_client.py`

- [ ] **Step 1: Write the failing backend tests**

```python
# tests/backends/test_claude_backend.py
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.backends.claude import ClaudeBackend, DEFAULT_MODEL
from link_project_to_chat.backends.base import HealthStatus


def test_claude_backend_declares_name_and_capabilities():
    backend = ClaudeBackend(project_path=Path("/tmp/project"))
    assert backend.name == "claude"
    assert backend.model == DEFAULT_MODEL
    assert backend.capabilities.supports_thinking is True
    assert backend.capabilities.supports_usage_cap_detection is True


@pytest.mark.asyncio
async def test_probe_health_returns_ok_when_chat_succeeds(monkeypatch):
    backend = ClaudeBackend(project_path=Path("/tmp/project"))
    monkeypatch.setattr(backend, "chat", lambda message, on_proc=None: "pong")

    status = await backend.probe_health()

    assert status == HealthStatus(ok=True, usage_capped=False, error_message=None)
```

- [ ] **Step 2: Run the focused tests**

```bash
pytest tests/backends/test_claude_backend.py tests/test_claude_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'link_project_to_chat.backends.claude'`.

- [ ] **Step 3: Move the Claude implementation into `backends/claude.py` and rename the class**

```python
# src/link_project_to_chat/backends/claude.py
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import Error, Result, StreamEvent
from .base import BackendCapabilities, HealthStatus
from .claude_parser import parse_stream_line
from .factory import register

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODELS = ("haiku", "sonnet", "opus", "opus[1m]", "sonnet[1m]")
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")
DEFAULT_MODEL = "sonnet"


class ClaudeBackend:
    name = "claude"
    capabilities = BackendCapabilities(
        models=MODELS,
        supports_thinking=True,
        supports_permissions=True,
        supports_resume=True,
        supports_compact=True,
        supports_allowed_tools=True,
        supports_usage_cap_detection=True,
    )

    def __init__(
        self,
        project_path: Path,
        model: str = DEFAULT_MODEL,
        skip_permissions: bool = True,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        self.project_path = project_path
        self.model = model
        self.model_display: str | None = None
        self.effort: str = "medium"
        self.skip_permissions: bool = skip_permissions
        self.permission_mode: str | None = permission_mode
        self.allowed_tools: list[str] = allowed_tools or []
        self.disallowed_tools: list[str] = disallowed_tools or []
        self.append_system_prompt: str | None = None
        self.team_system_note: str | None = None
        self.session_id: str | None = None
        self.show_thinking: bool = False
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_duration: float | None = None
        self._total_requests: int = 0

    async def probe_health(self) -> HealthStatus:
        try:
            result = await self.chat("ping")
        except ClaudeStreamError as exc:
            message = str(exc)
            return HealthStatus(
                ok=False,
                usage_capped=is_usage_cap_error(message),
                error_message=message,
            )
        return HealthStatus(ok=not is_usage_cap_error(result), usage_capped=False)
```

Keep the rest of the current `ClaudeClient` implementation intact while moving it over; the intent here is relocation plus the added `name`, `capabilities`, and `probe_health()` surface, not a behavioral rewrite.

- [ ] **Step 4: Register Claude in the factory and add the temporary compatibility shim**

```python
def _make_claude(project_path: Path, state: dict) -> ClaudeBackend:
    permissions = state.get("permissions")
    backend = ClaudeBackend(
        project_path=project_path,
        model=state.get("model") or DEFAULT_MODEL,
        skip_permissions=(permissions == "dangerously-skip-permissions"),
        permission_mode=permissions if permissions != "dangerously-skip-permissions" else None,
    )
    backend.session_id = state.get("session_id")
    backend.show_thinking = bool(state.get("show_thinking"))
    backend.effort = state.get("effort") or "medium"
    return backend


register("claude", _make_claude)
```

```python
# src/link_project_to_chat/claude_client.py
from .backends.claude import (
    ClaudeBackend as ClaudeClient,
    ClaudeStreamError,
    ClaudeUsageCapError,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    MODELS,
    PERMISSION_MODES,
    is_usage_cap_error,
)
```

- [ ] **Step 5: Update the old Claude tests to import from the new backend module**

```python
# tests/test_claude_client.py
from link_project_to_chat.backends.claude import ClaudeBackend as ClaudeClient, _sanitize_error
```

Do not rename the file yet; keep it green while the compatibility shim still exists.

- [ ] **Step 6: Run the Claude-specific tests**

```bash
pytest tests/backends/test_claude_backend.py tests/test_claude_client.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/backends/claude.py src/link_project_to_chat/claude_client.py tests/backends/test_claude_backend.py tests/test_claude_client.py
git commit -m "refactor: move claude client behind ClaudeBackend"
```

---

### Task 4: Inject The Backend Into `TaskManager` And Rename Claude-Specific Internals

**Files:**
- Modify: `src/link_project_to_chat/task_manager.py`
- Create: `tests/backends/fakes.py`
- Modify: `tests/test_task_manager.py`
- Create: `tests/backends/test_contract.py`
- Create: `tests/test_backend_lockout.py`

- [ ] **Step 1: Write the failing tests for backend injection and lockout**

```python
# tests/backends/fakes.py
from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from link_project_to_chat.backends.base import BackendCapabilities, HealthStatus
from link_project_to_chat.events import Result, StreamEvent


class FakeBackend:
    name = "fake"
    capabilities = BackendCapabilities(
        models=("fake",),
        supports_thinking=False,
        supports_permissions=False,
        supports_resume=False,
        supports_compact=False,
        supports_allowed_tools=False,
        supports_usage_cap_detection=False,
    )

    def __init__(self, project_path: Path, turns: list[list[StreamEvent]] | None = None):
        self.project_path = project_path
        self.model = "fake"
        self.session_id = None
        self.turns = list(turns or [[Result(text="ok", session_id=None, model=None)]])
        self.inputs: list[str] = []

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[object], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        self.inputs.append(user_message)
        for event in self.turns.pop(0):
            yield event

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[object], None] | None = None,
    ) -> str:
        self.inputs.append(user_message)
        return "ok"

    async def probe_health(self) -> HealthStatus:
        return HealthStatus(ok=True, usage_capped=False)

    def close_interactive(self) -> None:
        return None

    def cancel(self) -> bool:
        return False

    @property
    def status(self) -> dict:
        return {"running": False, "session_id": self.session_id}
```

```python
# tests/test_backend_lockout.py
from pathlib import Path


def test_task_manager_does_not_import_claude_modules_directly():
    source = Path("src/link_project_to_chat/task_manager.py").read_text(encoding="utf-8")
    assert "from .claude_client import ClaudeClient" not in source
    assert "from .backends.claude import" not in source
```

- [ ] **Step 2: Run the task manager tests to confirm failure**

```bash
pytest tests/test_task_manager.py tests/test_backend_lockout.py -v
```

Expected: the lockout test fails immediately because `task_manager.py` still imports `ClaudeClient`.

- [ ] **Step 3: Refactor `TaskManager` to accept a backend and rename the Claude identifiers**

```python
# src/link_project_to_chat/task_manager.py
from .backends.base import AgentBackend
from .events import AskQuestion, Error, Question, Result, StreamEvent, TextDelta


class TaskType(enum.Enum):
    AGENT = "agent"
    COMMAND = "command"


class TaskManager:
    def __init__(
        self,
        project_path: Path,
        backend: AgentBackend,
        on_complete: OnTaskEvent,
        on_task_started: OnTaskEvent,
        on_stream_event: Callable[[Task, StreamEvent], Awaitable[None]] | None = None,
        on_waiting_input: OnTaskEvent | None = None,
    ):
        self.project_path = project_path
        self._backend = backend
        self._backend_owner_task_id: int | None = None
        self._tasks: dict[int, Task] = {}
        self._waiting: list[tuple[float, int]] = []
        self._running_commands: set[int] = set()
        self._next_id = 1
        self._on_complete = on_complete
        self._on_task_started = on_task_started
        self._on_stream_event = on_stream_event
        self._on_waiting_input = on_waiting_input

    @property
    def backend(self) -> AgentBackend:
        return self._backend
```

Rename the rest of the Claude-named helpers in the same commit:

```python
async def _acquire_backend_slot(self, task_id: int) -> None:
    while self._backend_owner_task_id not in (None, task_id):
        await asyncio.sleep(0.05)
    self._backend_owner_task_id = task_id


def _release_backend_slot(self, task_id: int) -> None:
    if self._backend_owner_task_id == task_id:
        self._backend_owner_task_id = None


async def _close_backend_interactive(self) -> None:
    await asyncio.to_thread(self._backend.close_interactive)


def submit_agent(self, chat_id: int, message_id: int, prompt: str) -> Task:
    task = Task(
        id=self._next_id,
        chat_id=chat_id,
        message_id=message_id,
        type=TaskType.AGENT,
        input=prompt,
        name=prompt[:40],
    )
    self._next_id += 1
    return self._submit(task)
```

- [ ] **Step 4: Update the task-manager tests to use `FakeBackend`**

```python
# tests/test_task_manager.py
from link_project_to_chat.events import AskQuestion, Question, QuestionOption, Result, TextDelta
from link_project_to_chat.task_manager import Task, TaskManager, TaskStatus, TaskType
from tests.backends.fakes import FakeBackend


def _noop_manager(tmp_path) -> TaskManager:
    async def _noop(task):
        pass

    return TaskManager(
        project_path=tmp_path,
        backend=FakeBackend(tmp_path),
        on_complete=_noop,
        on_task_started=_noop,
    )
```

Then rename every `submit_claude` call in the file to `submit_agent`, and every `TaskType.CLAUDE` assertion to `TaskType.AGENT`.

- [ ] **Step 5: Add a backend contract test**

```python
# tests/backends/test_contract.py
from pathlib import Path

import pytest

from link_project_to_chat.backends.claude import ClaudeBackend
from link_project_to_chat.events import Result
from tests.backends.fakes import FakeBackend


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda tmp_path: FakeBackend(tmp_path, turns=[[Result(text="ok", session_id=None, model=None)]]),
        lambda tmp_path: ClaudeBackend(tmp_path),
    ],
)
@pytest.mark.asyncio
async def test_backend_contract_chat_returns_string(tmp_path, backend_factory):
    backend = backend_factory(tmp_path)
    if backend.name == "claude":
        pytest.skip("ClaudeBackend contract is covered via focused tests without spawning the real CLI here")
    result = await backend.chat("hello")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_backend_contract_probe_health(tmp_path):
    backend = FakeBackend(tmp_path)
    status = await backend.probe_health()
    assert status.ok is True
    assert status.usage_capped is False
```

Keep the contract lightweight at this stage; the important part is establishing the shape and reusing `FakeBackend` everywhere the core flow needs an injected backend.

- [ ] **Step 6: Run the backend and task-manager tests**

```bash
pytest tests/test_task_manager.py tests/backends/test_contract.py tests/test_backend_lockout.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/task_manager.py tests/backends/fakes.py tests/backends/test_contract.py tests/test_task_manager.py tests/test_backend_lockout.py
git commit -m "refactor: inject AgentBackend into TaskManager"
```

---

### Task 5: Switch `bot.py` To The Factory, Route Cap Probe Through The Backend, And Remove The Shim

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `src/link_project_to_chat/skills.py`
- Delete: `src/link_project_to_chat/claude_client.py`
- Create: `tests/test_bot_backend_lockout.py`
- Modify: `tests/test_bot_streaming.py`
- Modify: `tests/test_claude_client.py`

- [ ] **Step 1: Write the failing lockout tests for `bot.py`**

```python
# tests/test_bot_backend_lockout.py
from pathlib import Path


def test_bot_does_not_construct_claude_client_directly():
    source = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    assert "ClaudeClient(" not in source


def test_bot_does_not_reach_through_task_manager_claude_property():
    source = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    assert ".claude." not in source
```

- [ ] **Step 2: Run the lockout test to confirm failure**

```bash
pytest tests/test_bot_backend_lockout.py -v
```

Expected: both assertions fail against the current `bot.py`.

- [ ] **Step 3: Build the backend in `ProjectBot.__init__` via the factory**

```python
# src/link_project_to_chat/bot.py
from .backends.claude import (
    EFFORT_LEVELS,
    MODELS,
    PERMISSION_MODES,
    ClaudeBackend,
    ClaudeStreamError,
    is_usage_cap_error,
)
from .backends.factory import create


class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_username: str = "",
        trusted_user_id: int | None = None,
        on_trust: Callable[[int, str], None] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        allowed_usernames: list[str] | None = None,
        trusted_users: dict[str, int] | None = None,
        trusted_user_ids: list[int] | None = None,
        transcriber: "Transcriber | None" = None,
        synthesizer: "Synthesizer | None" = None,
        active_persona: str | None = None,
        show_thinking: bool = False,
        team_name: str | None = None,
        group_chat_id: int | None = None,
        role: str | None = None,
        peer_bot_username: str = "",
        config_path: Path | None = None,
    ):
        backend_state = {
            "permissions": (
                "dangerously-skip-permissions"
                if skip_permissions
                else permission_mode
            ),
            "show_thinking": show_thinking,
            "allowed_tools": allowed_tools or [],
            "disallowed_tools": disallowed_tools or [],
        }
        backend = create("claude", self.path, backend_state)
        self.task_manager = TaskManager(
            project_path=self.path,
            backend=backend,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            on_waiting_input=self._on_waiting_input,
        )
```

Use the existing constructor body in `bot.py` as-is around this change; the only intended behavior shift in this task is that the backend instance is created explicitly before `TaskManager` is constructed.

- [ ] **Step 4: Add the explicit tier-2 Claude accessor and update backend references**

```python
@property
def _claude(self) -> ClaudeBackend:
    backend = self.task_manager.backend
    assert isinstance(backend, ClaudeBackend), "Tier-2 Claude-only access requires ClaudeBackend"
    return backend
```

Then update the two access tiers consistently:

```python
st = self.task_manager.backend.status
model = self.task_manager.backend.model
session_id = self.task_manager.backend.session_id

self._claude.effort = effort
self._claude.permission_mode = new_mode
self._claude.append_system_prompt = skill_prompt
self._claude.team_system_note = team_note
```

- [ ] **Step 5: Route the cap probe through `backend.probe_health()` and document skills as backend-agnostic**

```python
def _schedule_cap_probe(self, chat: ChatRef, interval_s: int = 1800) -> None:
    async def _probe() -> None:
        while self._group_state.get(chat).halted:
            await asyncio.sleep(interval_s)
            if not self._group_state.get(chat).halted:
                return
            status = await self.task_manager.backend.probe_health()
            if status.ok and not status.usage_capped:
                self._group_state.resume(chat)
                await self._send_to_chat(int(chat.native_id), "Usage cap cleared. Resumed.")
                return
```

```python
# src/link_project_to_chat/skills.py
"""Skill and persona loading.

Skills are backend-agnostic prompt text. The Claude-named fallback path
(`~/.claude/skills`) is a convenience source only; the loaded markdown is
shared across backends and not tied to Claude-specific runtime behavior.
"""
```

- [ ] **Step 6: Delete the compatibility shim and update the remaining imports**

After every import site has moved to `link_project_to_chat.backends.claude`, delete `src/link_project_to_chat/claude_client.py`.

Also update any tests that still import `link_project_to_chat.claude_client` to import from `link_project_to_chat.backends.claude` instead.

- [ ] **Step 7: Run the bot/backend regression tests**

```bash
pytest tests/test_bot_backend_lockout.py tests/test_bot_streaming.py tests/test_claude_client.py tests/test_skills.py -v
```

Expected: all tests PASS.

- [ ] **Step 8: Run the broader smoke suite for this phase**

```bash
pytest tests/test_stream.py tests/test_task_manager.py tests/test_bot_streaming.py tests/test_claude_client.py tests/test_skills.py tests/backends -v
```

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/skills.py tests/test_bot_backend_lockout.py tests/test_bot_streaming.py tests/test_claude_client.py
git rm src/link_project_to_chat/claude_client.py
git commit -m "refactor: route project bot through backend factory and remove claude shim"
```

---

## Phase 1 Self-Review Checklist

- [ ] `task_manager.py` no longer imports `claude_client` or `backends.claude` directly.
- [ ] `TaskType.CLAUDE`, `submit_claude`, `self._claude`, and other Claude-specific task-manager identifiers are gone.
- [ ] `bot.py` constructs the runtime through `backends.factory.create("claude", self.path, backend_state)`.
- [ ] `bot.py` no longer contains `ClaudeClient(` or `task_manager.claude`.
- [ ] `stream.py` is only a temporary shim; shared events live in `events.py`.
- [ ] `skills.py` explicitly documents that skills are shared across backends.
- [ ] `claude_client.py` is deleted at the end of the phase.

## Phase 1 Smoke Test (blocking exit criterion)

A green pytest run proves the Protocol wiring compiles and unit-level behavior is preserved; it does NOT prove that the refactored path still spawns a real `claude` subprocess and streams events end-to-end. Before Phase 1 is considered complete, the dev must execute and record the following manual smoke test:

**Setup:** local checkout of `feat/transport-abstraction` on a machine with the `claude` CLI installed and authenticated.

**Procedure:**
1. Start a project bot: `link-project-to-chat start --project <name>`
2. From an authorized Telegram account, send a trivial prompt to the bot (e.g. "say hello in one word")
3. Observe that:
   - The bot streams a live response (text deltas render mid-generation)
   - The final message is finalized cleanly (no `RuntimeError`, no hung typing indicator)
   - `/tasks` shows the task as `COMPLETED`
4. Send `/run echo hi` and confirm the shell path still works (regression guard on `TaskType` rename)
5. Send `/compact` and confirm the session-summary round-trip still works (regression guard on resume semantics)

**Pass condition:** all three round-trips succeed; no tracebacks in bot logs; task statuses are correct.

**Record:** paste the bot log excerpt or screenshots under `docs/backend-phase-1-smoke-evidence.md` as part of the final commit.

_Rationale: Phase 1 moves the subprocess spawn path across module boundaries and renames 14+ call sites in `bot.py`. A unit-only test run can pass while the integration is subtly broken (wrong env, wrong working dir, stream parser regression). The smoke test is cheap and catches what tests can't._
