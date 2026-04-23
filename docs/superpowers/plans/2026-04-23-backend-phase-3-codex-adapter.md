# Backend Phase 3 Codex Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `codex` backend that works through the existing backend abstraction, persists its own session state, and stays conservative about unsupported capabilities.

**Architecture:** Start with a real CLI validation pass and save the results in-repo. Then extract shared env-prep logic into `BaseBackend`, implement a non-interactive `CodexBackend` that shells out to `codex exec --json` / `codex exec resume --json`, and translate Codex JSONL output into shared stream events. Keep the capability surface narrow: no `/thinking`, `/permissions`, `/compact`, or `/model` exposure until the CLI behavior is explicitly validated.

**Tech Stack:** Python 3.11+, asyncio, subprocess, pytest, Markdown docs

---

## File Map

| File | Change |
|------|--------|
| `docs/superpowers/specs/2026-04-23-codex-cli-findings.md` | **NEW**: actual Codex CLI findings recorded from this environment |
| `tests/fixtures/codex_exec_ok.jsonl` | **NEW**: captured JSONL example for first-turn `codex exec --json` |
| `tests/fixtures/codex_resume_ok.jsonl` | **NEW**: captured JSONL example for `codex exec resume --json` |
| `tests/fixtures/codex_stderr_warning.txt` | **NEW**: representative stderr warning fixture that should not fail successful runs |
| `src/link_project_to_chat/backends/base.py` | Add `BaseBackend` env helper while keeping `AgentBackend` Protocol |
| `src/link_project_to_chat/backends/claude.py` | Inherit `BaseBackend` and stop inlining env scrubbing |
| `src/link_project_to_chat/backends/codex_parser.py` | **NEW**: parse Codex JSONL into shared events + lightweight metadata |
| `src/link_project_to_chat/backends/codex.py` | **NEW**: `CodexBackend`, `probe_health()`, cancel/cleanup, factory registration |
| `src/link_project_to_chat/backends/__init__.py` | Import `codex` module for registration side effects and export `CodexBackend` |
| `tests/backends/test_base_backend.py` | **NEW**: env keep/scrub behavior for `BaseBackend` |
| `tests/backends/test_codex_parser.py` | **NEW**: parser behavior against captured JSONL fixtures |
| `tests/backends/test_codex_backend.py` | **NEW**: command building, stream handling, resume, cancel, probe health |
| `tests/backends/test_env_policy.py` | **NEW**: Claude/Codex cross-contamination regression tests |
| `tests/backends/test_capability_declaration.py` | **NEW**: conservative Codex capability lock |
| `tests/backends/test_contract.py` | Extend backend contract coverage to include Codex |
| `tests/backends/test_codex_live.py` | **NEW**: real-CLI integration tests, skipped by default |
| `pyproject.toml` | Add `codex_live` pytest marker |
| `CLAUDE.md` | Update architecture notes to mention backend abstraction and opt-in Codex |

---

### Task 1: Capture Codex CLI Findings And Freeze Fixtures

**Files:**
- Create: `docs/superpowers/specs/2026-04-23-codex-cli-findings.md`
- Create: `tests/fixtures/codex_exec_ok.jsonl`
- Create: `tests/fixtures/codex_resume_ok.jsonl`
- Create: `tests/fixtures/codex_stderr_warning.txt`

- [ ] **Step 1: Run the validation commands and save the observed session id**

```powershell
$stderr = Join-Path $env:TEMP "codex-phase3-stderr.txt"
if (Test-Path $stderr) { Remove-Item $stderr }

$first = codex exec --json --sandbox read-only "Reply with exactly OK and do not run any commands." 2> $stderr
$sessionId = (
    $first |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.type -eq "thread.started" } |
    Select-Object -First 1
).thread_id

$resume = codex exec resume --json $sessionId "Reply with exactly AGAIN and do not run any commands." 2>> $stderr

$first
$resume
Get-Content $stderr | Select-Object -First 20
```

Expected:
- First command exits `0` and prints four JSONL records: `thread.started`, `turn.started`, `item.completed`, `turn.completed`
- `$sessionId` is a UUID-like thread id
- Resume command exits `0` and reuses the same thread id
- Warning noise appears on stderr even though the command succeeded

- [ ] **Step 2: Create the findings doc with the validated command surface**

````markdown
# Codex CLI Findings

**Captured on:** 2026-04-23
**Binary:** `codex`
**Version:** `codex-cli 0.122.0`

## Commands validated

```text
codex --version
codex --help
codex exec --help
codex exec resume --help
codex login --help
codex exec --json --sandbox read-only "Reply with exactly OK and do not run any commands."
codex exec resume --json 019db9de-2ad5-7110-8d11-d96e6617cc0f "Reply with exactly AGAIN and do not run any commands."
```

## Observed behavior

- Non-interactive execution works through `codex exec --json`.
- Resume works through the validated `codex exec resume --json 019db9de-2ad5-7110-8d11-d96e6617cc0f "Reply with exactly AGAIN and do not run any commands."` form.
- `codex exec resume` does **not** accept `--sandbox`; its option surface differs from `codex exec`.
- Stdout is JSONL with top-level event types:
  - `thread.started`
  - `turn.started`
  - `item.completed` with `item.type == "agent_message"`
  - `turn.completed` with `usage`
- Successful runs can still emit noisy stderr warnings about plugin sync and analytics failures; stderr alone is not a failure signal.
- No thinking delta stream was observed.
- No tool-use event stream was observed.
- Model selection exists as `--model`, but the CLI help does not enumerate a fixed supported model list.

## Initial capability conclusions

- `supports_resume = True`
- `supports_thinking = False`
- `supports_permissions = False`
- `supports_compact = False`
- `supports_allowed_tools = False`
- `supports_usage_cap_detection = False`
- `models = ()` for now because the CLI advertises `--model` but not a validated fixed list
````

- [ ] **Step 3: Save deterministic JSONL fixtures from the observed output**

```text
# tests/fixtures/codex_exec_ok.jsonl
{"type":"thread.started","thread_id":"019db9de-2ad5-7110-8d11-d96e6617cc0f"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}
{"type":"turn.completed","usage":{"input_tokens":24298,"cached_input_tokens":3456,"output_tokens":36}}
```

```text
# tests/fixtures/codex_resume_ok.jsonl
{"type":"thread.started","thread_id":"019db9de-2ad5-7110-8d11-d96e6617cc0f"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"AGAIN"}}
{"type":"turn.completed","usage":{"input_tokens":48773,"cached_input_tokens":27648,"output_tokens":55}}
```

```text
# tests/fixtures/codex_stderr_warning.txt
2026-04-23T10:23:58.058856Z  WARN codex_core::plugins::startup_sync: startup remote plugin sync failed
2026-04-23T10:24:07.229439Z  WARN codex_analytics::client: events failed with status 403 Forbidden
```

- [ ] **Step 4: Commit the findings and fixtures**

```bash
git add docs/superpowers/specs/2026-04-23-codex-cli-findings.md tests/fixtures/codex_exec_ok.jsonl tests/fixtures/codex_resume_ok.jsonl tests/fixtures/codex_stderr_warning.txt
git commit -m "docs: capture codex cli findings and parser fixtures"
```

---

### Task 2: Extract `BaseBackend` Env Policy And Move Claude To It

**Files:**
- Modify: `src/link_project_to_chat/backends/base.py`
- Modify: `src/link_project_to_chat/backends/claude.py`
- Create: `tests/backends/test_base_backend.py`

- [ ] **Step 1: Write the failing env-policy tests**

```python
# tests/backends/test_base_backend.py
from pathlib import Path

from link_project_to_chat.backends.base import BaseBackend, BackendCapabilities
from link_project_to_chat.backends.claude import ClaudeBackend


class _DummyBackend(BaseBackend):
    name = "dummy"
    capabilities = BackendCapabilities(
        models=(),
        supports_thinking=False,
        supports_permissions=False,
        supports_resume=False,
        supports_compact=False,
        supports_allowed_tools=False,
        supports_usage_cap_detection=False,
    )
    _env_keep_patterns = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns = (
        "*_TOKEN",
        "*_KEY",
        "*_SECRET",
        "ANTHROPIC_*",
        "AWS_*",
        "GITHUB_*",
        "DATABASE_*",
        "PASSWORD*",
    )

    def __init__(self) -> None:
        self.project_path = Path("/tmp/project")
        self.model = None
        self.session_id = None


def test_keep_patterns_override_scrub_patterns(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CODEX_SESSION_TOKEN", "codex-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    env = _DummyBackend()._prepare_env()

    assert env["OPENAI_API_KEY"] == "openai-secret"
    assert env["CODEX_SESSION_TOKEN"] == "codex-secret"
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_claude_backend_still_scrubs_openai_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = ClaudeBackend(project_path=tmp_path)._prepare_env()

    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
```

- [ ] **Step 2: Run the env-policy tests to confirm failure**

```bash
pytest tests/backends/test_base_backend.py -v
```

Expected: import or attribute failures because `BaseBackend` and `_prepare_env()` do not exist yet.

- [ ] **Step 3: Add `BaseBackend` to `backends/base.py` without changing the `AgentBackend` caller contract**

```python
# src/link_project_to_chat/backends/base.py
from __future__ import annotations

import fnmatch
import os
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


class BaseBackend:
    _env_keep_patterns: Sequence[str] = ()
    _env_scrub_patterns: Sequence[str] = ()

    def _matches(self, key: str, patterns: Sequence[str]) -> bool:
        return any(fnmatch.fnmatch(key, pattern) for pattern in patterns)

    def _prepare_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        for key in list(env):
            if self._matches(key, self._env_keep_patterns):
                continue
            if self._matches(key, self._env_scrub_patterns):
                del env[key]
        return env


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

- [ ] **Step 4: Move Claude env scrubbing onto the shared helper**

```python
# src/link_project_to_chat/backends/claude.py
class ClaudeBackend(BaseBackend):
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
    _env_keep_patterns = ()
    _env_scrub_patterns = (
        "*_TOKEN",
        "*_KEY",
        "*_SECRET",
        "AWS_*",
        "OPENAI_*",
        "GITHUB_*",
        "DATABASE_*",
        "PASSWORD*",
    )

    def _start_proc(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> subprocess.Popen:
        cmd = self._build_cmd()
        env = self._prepare_env()
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._proc = proc
        if on_proc:
            on_proc(proc)
        self._send_stdin(proc, user_message)
        return proc
```

- [ ] **Step 5: Run the env-policy tests**

```bash
pytest tests/backends/test_base_backend.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/backends/base.py src/link_project_to_chat/backends/claude.py tests/backends/test_base_backend.py
git commit -m "refactor: add shared backend env policy helper"
```

---

### Task 3: Implement The Codex JSONL Parser

**Files:**
- Create: `src/link_project_to_chat/backends/codex_parser.py`
- Create: `tests/backends/test_codex_parser.py`

- [ ] **Step 1: Write the failing parser tests against the captured fixtures**

```python
# tests/backends/test_codex_parser.py
from pathlib import Path

from link_project_to_chat.backends.codex_parser import parse_codex_line
from link_project_to_chat.events import TextDelta

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_thread_started_yields_session_metadata():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[0])

    assert parsed.thread_id == "019db9de-2ad5-7110-8d11-d96e6617cc0f"
    assert parsed.turn_completed is False
    assert parsed.events == []


def test_agent_message_yields_text_delta():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[2])

    assert parsed.events == [TextDelta(text="OK")]
    assert parsed.thread_id is None


def test_turn_completed_preserves_usage():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[3])

    assert parsed.turn_completed is True
    assert parsed.usage == {
        "input_tokens": 24298,
        "cached_input_tokens": 3456,
        "output_tokens": 36,
    }


def test_non_json_stderr_line_is_ignored():
    parsed = parse_codex_line(
        "2026-04-23T10:23:58.058856Z  WARN codex_core::plugins::startup_sync: startup remote plugin sync failed"
    )

    assert parsed.events == []
    assert parsed.thread_id is None
    assert parsed.turn_completed is False
```

- [ ] **Step 2: Run the parser tests to confirm failure**

```bash
pytest tests/backends/test_codex_parser.py -v
```

Expected: `ModuleNotFoundError: No module named 'link_project_to_chat.backends.codex_parser'`.

- [ ] **Step 3: Implement a lightweight parser result object and line parser**

```python
# src/link_project_to_chat/backends/codex_parser.py
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..events import Error, StreamEvent, TextDelta


@dataclass
class CodexParseResult:
    events: list[StreamEvent] = field(default_factory=list)
    thread_id: str | None = None
    turn_completed: bool = False
    usage: dict[str, int] | None = None


def parse_codex_line(line: str) -> CodexParseResult:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return CodexParseResult()

    event_type = data.get("type")

    if event_type == "thread.started":
        return CodexParseResult(thread_id=data.get("thread_id"))

    if event_type == "item.completed":
        item = data.get("item", {})
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            return CodexParseResult(
                events=[TextDelta(text=text)] if text else []
            )
        if item.get("type") == "error":
            message = item.get("text") or item.get("message") or "Unknown error"
            return CodexParseResult(events=[Error(message=message)])

    if event_type == "turn.completed":
        usage = data.get("usage")
        return CodexParseResult(
            turn_completed=True,
            usage=usage if isinstance(usage, dict) else None,
        )

    if event_type == "error":
        message = data.get("message") or data.get("error") or "Unknown error"
        return CodexParseResult(events=[Error(message=message)])

    return CodexParseResult()
```

- [ ] **Step 4: Run the parser tests**

```bash
pytest tests/backends/test_codex_parser.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/codex_parser.py tests/backends/test_codex_parser.py
git commit -m "feat: add codex jsonl parser"
```

---

### Task 4: Implement `CodexBackend` And Register It

**Files:**
- Create: `src/link_project_to_chat/backends/codex.py`
- Modify: `src/link_project_to_chat/backends/__init__.py`
- Create: `tests/backends/test_codex_backend.py`

- [ ] **Step 1: Write the failing backend tests**

```python
# tests/backends/test_codex_backend.py
import io
from pathlib import Path

import pytest

from link_project_to_chat.backends.codex import CodexBackend
from link_project_to_chat.events import Result, TextDelta

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _FakeProc:
    def __init__(self, stdout_lines: list[str], stderr_text: str = "", returncode: int = 0):
        payload = "".join(line + "\n" for line in stdout_lines).encode("utf-8")
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr_text.encode("utf-8"))
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            return self.returncode
        return None

    def wait(self, timeout=None):
        return -9 if self.killed else self.returncode

    def kill(self):
        self.killed = True


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_build_cmd_for_new_turn_uses_exec_json(tmp_path):
    backend = CodexBackend(tmp_path, {"model": "gpt-5.4"})

    assert backend._build_cmd("hello") == [
        "codex",
        "exec",
        "--json",
        "--model",
        "gpt-5.4",
        "hello",
    ]


def test_build_cmd_for_resume_uses_exec_resume_json(tmp_path):
    backend = CodexBackend(tmp_path, {"session_id": "sess-1"})

    assert backend._build_cmd("again") == [
        "codex",
        "exec",
        "resume",
        "--json",
        "sess-1",
        "again",
    ]


@pytest.mark.asyncio
async def test_chat_stream_emits_text_delta_then_result(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: _FakeProc(_lines("codex_exec_ok.jsonl")))

    events = [event async for event in backend.chat_stream("hello")]

    assert events[0] == TextDelta(text="OK")
    assert events[-1] == Result(
        text="OK",
        session_id="019db9de-2ad5-7110-8d11-d96e6617cc0f",
        model=None,
    )
    assert backend.session_id == "019db9de-2ad5-7110-8d11-d96e6617cc0f"


@pytest.mark.asyncio
async def test_probe_health_returns_ok(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})

    async def _fake_chat(user_message: str, on_proc=None) -> str:
        return "PONG"

    monkeypatch.setattr(backend, "chat", _fake_chat)
    status = await backend.probe_health()

    assert status.ok is True
    assert status.usage_capped is False
    assert status.error_message is None


def test_cancel_terminates_running_process(tmp_path):
    backend = CodexBackend(tmp_path, {})
    proc = _FakeProc([], returncode=0)
    backend._proc = proc

    assert backend.cancel() is True
    assert proc.killed is True
```

- [ ] **Step 2: Run the backend tests to confirm failure**

```bash
pytest tests/backends/test_codex_backend.py -v
```

Expected: import failure because `backends/codex.py` does not exist yet.

- [ ] **Step 3: Implement `CodexBackend` as a non-interactive backend that resumes by spawning a new process**

```python
# src/link_project_to_chat/backends/codex.py
from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import Error, Result, StreamEvent, TextDelta
from ..task_manager import _terminate_process_tree
from .base import BaseBackend, BackendCapabilities, HealthStatus
from .codex_parser import parse_codex_line
from .factory import register

CODEX_CAPABILITIES = BackendCapabilities(
    models=(),
    supports_thinking=False,
    supports_permissions=False,
    supports_resume=True,
    supports_compact=False,
    supports_allowed_tools=False,
    supports_usage_cap_detection=False,
)


class CodexStreamError(Exception):
    """Raised when Codex produces an Error event instead of a final result."""


class CodexBackend(BaseBackend):
    name = "codex"
    capabilities = CODEX_CAPABILITIES
    _env_keep_patterns = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns = (
        "*_TOKEN",
        "*_KEY",
        "*_SECRET",
        "ANTHROPIC_*",
        "AWS_*",
        "GITHUB_*",
        "DATABASE_*",
        "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.model = state.get("model")
        self.session_id = state.get("session_id")
        self._proc: subprocess.Popen[bytes] | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_usage: dict[str, int] | None = None
        self._total_requests = 0

    def _build_cmd(self, user_message: str) -> list[str]:
        if self.session_id:
            cmd = ["codex", "exec", "resume", "--json"]
            if self.model:
                cmd.extend(["--model", self.model])
            cmd.extend([self.session_id, user_message])
            return cmd

        cmd = ["codex", "exec", "--json"]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append(user_message)
        return cmd

    def _popen(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._prepare_env(),
        )

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd = self._build_cmd(user_message)
        proc = self._popen(cmd)
        self._proc = proc
        self._started_at = time.monotonic()
        self._last_message = user_message[:80]
        self._total_requests += 1
        if on_proc:
            on_proc(proc)

        collected_text: list[str] = []
        thread_id = self.session_id

        try:
            while True:
                raw_line = await asyncio.to_thread(proc.stdout.readline)
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                parsed = parse_codex_line(line)
                if parsed.thread_id:
                    thread_id = parsed.thread_id
                for event in parsed.events:
                    if isinstance(event, TextDelta):
                        collected_text.append(event.text)
                    yield event
                    if isinstance(event, Error):
                        raise CodexStreamError(event.message)
                if parsed.turn_completed:
                    self.session_id = thread_id or self.session_id
                    self._last_usage = parsed.usage
                    yield Result(
                        text="".join(collected_text) or "[No response]",
                        session_id=self.session_id,
                        model=None,
                    )
                    return

            stderr = (await asyncio.to_thread(proc.stderr.read)).decode("utf-8", errors="replace").strip()
            await asyncio.to_thread(proc.wait)
            if proc.returncode != 0:
                raise CodexStreamError(stderr or f"codex exit code {proc.returncode}")
        finally:
            if self._proc is proc:
                self._proc = None
            self._started_at = None

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> str:
        final_text = ""
        async for event in self.chat_stream(user_message, on_proc=on_proc):
            if isinstance(event, Result):
                final_text = event.text
            elif isinstance(event, Error):
                raise CodexStreamError(event.message)
        return final_text or "[No response]"

    async def probe_health(self) -> HealthStatus:
        try:
            await self.chat("Reply with exactly PONG and do not run any commands.")
        except CodexStreamError as exc:
            return HealthStatus(ok=False, usage_capped=False, error_message=str(exc))
        return HealthStatus(ok=True, usage_capped=False, error_message=None)

    def close_interactive(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            _terminate_process_tree(proc)
        if self._proc is proc:
            self._proc = None
            self._started_at = None

    def cancel(self) -> bool:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return False
        _terminate_process_tree(proc)
        if self._proc is proc:
            self._proc = None
            self._started_at = None
        return True

    @property
    def status(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        return {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_usage": self._last_usage,
        }


def _make_codex(project_path: Path, state: dict) -> CodexBackend:
    return CodexBackend(project_path, state)


register("codex", _make_codex)
```

The key behavior here is intentional:
- First turn uses `codex exec --json`
- Follow-up turns use `codex exec resume --json` plus the backend's current `session_id` and the new user message
- Prompt is passed as a positional argument, not over stdin
- Successful stderr warnings are ignored
- Final `Result` is emitted when `turn.completed` arrives

- [ ] **Step 4: Import Codex in `backends/__init__.py` so the factory sees it**

```python
# src/link_project_to_chat/backends/__init__.py
from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from .base import AgentBackend, BackendCapabilities, BaseBackend, HealthStatus
from .claude import ClaudeBackend
from .codex import CodexBackend
from .factory import available, create, register

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "BaseBackend",
    "ClaudeBackend",
    "CodexBackend",
    "HealthStatus",
    "available",
    "create",
    "register",
]
```

- [ ] **Step 5: Run the Codex backend tests**

```bash
pytest tests/backends/test_codex_backend.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/backends/codex.py src/link_project_to_chat/backends/__init__.py tests/backends/test_codex_backend.py
git commit -m "feat: add conservative codex backend"
```

---

### Task 5: Lock Capabilities, Cross-Backend Env Policy, And Live Coverage

**Files:**
- Create: `tests/backends/test_env_policy.py`
- Create: `tests/backends/test_capability_declaration.py`
- Modify: `tests/backends/test_contract.py`
- Create: `tests/backends/test_codex_live.py`
- Modify: `pyproject.toml`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the failing capability and env regression tests**

```python
# tests/backends/test_env_policy.py
from link_project_to_chat.backends.claude import ClaudeBackend
from link_project_to_chat.backends.codex import CodexBackend


def test_claude_scrubs_openai_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = ClaudeBackend(project_path=tmp_path)._prepare_env()

    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_codex_keeps_openai_but_scrubs_anthropic(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("CODEX_SESSION_TOKEN", "codex-secret")

    env = CodexBackend(tmp_path, {})._prepare_env()

    assert env["OPENAI_API_KEY"] == "openai-secret"
    assert env["CODEX_SESSION_TOKEN"] == "codex-secret"
    assert "ANTHROPIC_API_KEY" not in env
```

```python
# tests/backends/test_capability_declaration.py
from link_project_to_chat.backends.codex import CODEX_CAPABILITIES


def test_codex_capabilities_match_validated_findings():
    assert tuple(CODEX_CAPABILITIES.models) == ()
    assert CODEX_CAPABILITIES.supports_thinking is False
    assert CODEX_CAPABILITIES.supports_permissions is False
    assert CODEX_CAPABILITIES.supports_resume is True
    assert CODEX_CAPABILITIES.supports_compact is False
    assert CODEX_CAPABILITIES.supports_allowed_tools is False
    assert CODEX_CAPABILITIES.supports_usage_cap_detection is False
```

- [ ] **Step 2: Extend the backend contract test to include Codex without requiring a live subprocess**

```python
# tests/backends/test_contract.py
import pytest

from link_project_to_chat.backends.codex import CodexBackend
from tests.backends.fakes import FakeBackend


@pytest.mark.asyncio
async def test_codex_backend_contract_chat_returns_string(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})

    async def _fake_chat(user_message: str, on_proc=None) -> str:
        return "ok"

    monkeypatch.setattr(backend, "chat", _fake_chat)

    assert isinstance(await backend.chat("hello"), str)


@pytest.mark.asyncio
async def test_fake_backend_contract_probe_health(tmp_path):
    backend = FakeBackend(tmp_path)
    status = await backend.probe_health()
    assert status.ok is True
    assert status.usage_capped is False
```

- [ ] **Step 3: Add live Codex integration tests and register the marker**

```python
# tests/backends/test_codex_live.py
import shutil
import subprocess

import pytest

from link_project_to_chat.backends.codex import CodexBackend
from link_project_to_chat.events import Result, TextDelta

pytestmark = pytest.mark.codex_live


def _require_codex() -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is not installed")
    status = subprocess.run(
        ["codex", "login", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        pytest.skip("codex CLI is not authenticated")


@pytest.mark.asyncio
async def test_codex_live_round_trip(tmp_path):
    _require_codex()
    backend = CodexBackend(tmp_path, {})

    events = [
        event
        async for event in backend.chat_stream(
            "Reply with exactly OK and do not run any commands."
        )
    ]

    assert any(isinstance(event, TextDelta) and event.text.strip() == "OK" for event in events)
    assert isinstance(events[-1], Result)
    assert events[-1].text.strip() == "OK"
    assert backend.session_id


@pytest.mark.asyncio
async def test_codex_live_resume_reuses_session(tmp_path):
    _require_codex()
    backend = CodexBackend(tmp_path, {})

    await backend.chat("Reply with exactly OK and do not run any commands.")
    first_session = backend.session_id
    reply = await backend.chat("Reply with exactly AGAIN and do not run any commands.")

    assert reply.strip() == "AGAIN"
    assert backend.session_id == first_session
```

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "codex_live: requires a real codex CLI installation and local authentication",
]
```

- [ ] **Step 4: Update `CLAUDE.md` to reflect the backend abstraction and opt-in Codex**

```markdown
## Architecture

### Core flow
`cli.py` → `ProjectBot` (bot.py) → `TaskManager` → `AgentBackend` → backend-specific subprocess adapter → streaming response → `StreamingMessage` (transport/streaming.py)

### Key modules
- **backends/** — Backend abstraction layer. `base.py` defines `AgentBackend` and `BaseBackend`; `claude.py` wraps Claude CLI; `codex.py` wraps Codex CLI; `factory.py` registers available backends.
- **events.py** — Shared backend-agnostic stream events: `TextDelta`, `ThinkingDelta`, `ToolUse`, `AskQuestion`, `Result`, `Error`.
- **skills.py** — Skill/persona loading shared across backends.

## Current Development

Backend abstraction is rolling out in phases. Claude remains the default backend. Codex is opt-in and experimental through `/backend codex`.
```

- [ ] **Step 5: Run the unit suites**

```bash
pytest tests/backends/test_base_backend.py tests/backends/test_codex_parser.py tests/backends/test_codex_backend.py tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_contract.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Run the manual live suite**

```bash
pytest tests/backends/test_codex_live.py -m codex_live -v -s
```

Expected:
- On a machine with `codex` installed and authenticated, both tests PASS
- On CI or an unauthenticated machine, the tests SKIP cleanly

- [ ] **Step 7: Commit**

```bash
git add tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_contract.py tests/backends/test_codex_live.py pyproject.toml CLAUDE.md
git commit -m "test: lock codex backend capabilities and live coverage"
```

---

## Phase 3 Self-Review Checklist

- [ ] `docs/superpowers/specs/2026-04-23-codex-cli-findings.md` exists and reflects the real installed CLI.
- [ ] `BaseBackend` owns env preparation; Claude no longer hardcodes its own scrub loop.
- [ ] `CodexBackend` uses `codex exec --json` for new turns and `codex exec resume --json` for resumed turns.
- [ ] Prompt text is passed as a positional argument, not streamed over stdin.
- [ ] Successful stderr warnings do not fail a turn.
- [ ] `supports_resume` is `True` because resume was validated; other unsupported features remain `False`.
- [ ] `OPENAI_*` is preserved for Codex and scrubbed for Claude.
- [ ] Factory registration exposes `"codex"` without changing the default backend.
- [ ] Live tests are marked `codex_live` and skipped cleanly when Codex is unavailable.
