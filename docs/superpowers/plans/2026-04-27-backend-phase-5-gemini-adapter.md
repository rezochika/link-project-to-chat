# Backend Phase 5 Gemini Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `gemini` backend that works through the existing backend abstraction, persists its own session state if Task 0 confirms session-id support, and stays conservative about every other capability.

**Architecture:** Mirror the Phase 3 (Codex) adapter shape. Begin with a real `gemini` CLI validation pass and save the results in-repo as a findings doc + JSONL fixtures. Then implement `GeminiBackend` (per-turn subprocess) and `gemini_parser.py` (JSONL → shared `StreamEvent` taxonomy), register with the factory, and lock conservative capability declarations behind a pinning test. Ship the corrected `chat_stream` lifecycle from day 1 (terminate proc before clearing `_proc`) so Gemini doesn't inherit the P4-C2 zombie-proc bug Codex still has.

**Tech Stack:** Python 3.11+, asyncio, subprocess, pytest, Markdown docs.

**Precondition:** Plan execution requires the official `gemini-cli` (`npm install -g @google/gemini-cli`) installed locally and authenticated (`gemini auth login` or `GEMINI_API_KEY` env var). Task 1 will fail without it.

**Reference spec:** [docs/superpowers/specs/2026-04-27-backend-phase-5-gemini-adapter-design.md](../specs/2026-04-27-backend-phase-5-gemini-adapter-design.md). Phase 3 reference plan: [2026-04-23-backend-phase-3-codex-adapter.md](2026-04-23-backend-phase-3-codex-adapter.md).

**Substitution-marker convention:** Tokens shown as `<SUB>`, `<JSON_FLAG>`, `<extracted-id>` etc. are values **Task 1 captures** by running the real CLI. Tasks 2–6 reference them as placeholders only because the plan itself is written before Task 1 has access to a local Gemini CLI (per spec Q1=B). After Task 1 completes, return to this plan, find every `<SUB>` / `<JSON_FLAG>` token, and substitute the literal Task-1-captured values inline. The same applies to the example Gemini event-type strings (`text_delta`, `session_started`, `turn_completed`, etc.) — substitute the actual event-type strings the findings doc captured. Do not proceed to Task 2 with placeholders unsubstituted.

---

## File Map

| File | Change |
|------|--------|
| `docs/superpowers/specs/2026-04-27-gemini-cli-findings.md` | **NEW**: actual Gemini CLI findings recorded from this environment (output of Task 1) |
| `tests/fixtures/gemini_exec_ok.jsonl` | **NEW**: captured JSONL example for first-turn `gemini exec --json` (or equivalent — exact form pinned by Task 1) |
| `tests/fixtures/gemini_resume_ok.jsonl` | **NEW**: captured JSONL example for second-turn resume (only if Task 1 confirms `supports_resume=True`) |
| `tests/fixtures/gemini_stderr_noise.txt` | **NEW**: representative benign stderr fixture that must not fail successful runs (only created if Task 1 captures any) |
| `src/link_project_to_chat/backends/gemini_parser.py` | **NEW**: parse Gemini JSONL into shared events + lightweight metadata |
| `src/link_project_to_chat/backends/gemini.py` | **NEW**: `GeminiBackend`, `probe_health()`, cancel/cleanup with the P4-C2 lifecycle fix, factory registration |
| `src/link_project_to_chat/backends/__init__.py` | Import `gemini` module for registration side effects and export `GeminiBackend` |
| `tests/backends/test_gemini_parser.py` | **NEW**: parser behavior against captured JSONL fixtures |
| `tests/backends/test_gemini_backend.py` | **NEW**: command building, stream handling, lifecycle (incl. early-cancel), cancel, probe health |
| `tests/backends/test_gemini_live.py` | **NEW**: real-CLI integration tests, skipped by default |
| `tests/backends/test_env_policy.py` | Add Gemini env keep/scrub regression test |
| `tests/backends/test_capability_declaration.py` | Add Gemini conservative capability lock |
| `tests/backends/test_contract.py` | Extend backend-contract coverage to include Gemini |
| `tests/test_backend_command.py` | Add Gemini-active rejection tests for `/model` / `/effort` / `/permissions` / `/compact` / `/thinking` |
| `pyproject.toml` | Register `gemini_live` pytest marker |
| `tests/conftest.py` | Exempt `gemini_live` marker from `_isolate_home` (so the spawned process can read real `~/.gemini/` auth) |
| `CLAUDE.md` | Mention Gemini in the backends architecture line |
| `AGENTS.md` | Mirror the same line |
| `docs/CHANGELOG.md` | Phase 5 changelog entry citing the commit range |
| `docs/TODO.md` | Flip Phase 5 status from 📋 to ✅ once shipped |

---

### Task 1: Capture Gemini CLI Findings And Freeze Fixtures

**Blocking:** No subsequent task may proceed until Task 1 has produced a committed findings doc that answers all 10 questions in the spec §4.1. If `gemini-cli` reveals a structurally incompatible JSONL surface (e.g., no per-turn process exit, no streaming events), STOP and re-spec per spec §12 rollback plan.

**Files:**
- Create: `docs/superpowers/specs/2026-04-27-gemini-cli-findings.md`
- Create: `tests/fixtures/gemini_exec_ok.jsonl`
- Create: `tests/fixtures/gemini_resume_ok.jsonl` (only if resume is supported)
- Create: `tests/fixtures/gemini_stderr_noise.txt` (only if benign stderr is observed)

- [ ] **Step 1: Verify the binary is installed and authenticated**

```bash
gemini --version
gemini auth status   # or whatever the CLI's auth-check subcommand is
```

Expected: version string printed, auth check exits 0. If it fails, install via `npm install -g @google/gemini-cli` and run `gemini auth login` (or set `GEMINI_API_KEY`), then retry.

- [ ] **Step 2: Discover the one-shot turn invocation**

```bash
gemini --help
gemini chat --help 2>&1 || true
gemini exec --help 2>&1 || true
gemini run --help 2>&1 || true
```

Expected: identify the subcommand (likely `gemini`, `gemini chat`, or `gemini exec`) that takes a prompt argument and exits. Record the full flag list — JSON output flag, model flag, sandbox flag, resume flag.

- [ ] **Step 3: Run a smoke turn and capture stdout + stderr**

Use the subcommand and JSON flag identified in Step 2. Substitute `<SUB>` and `<JSON_FLAG>` below.

```bash
mkdir -p /tmp/gemini-phase5-tmp && cd /tmp/gemini-phase5-tmp && git init -q
gemini <SUB> <JSON_FLAG> "Reply with exactly OK and do not run any commands." \
    > /tmp/gemini-phase5-stdout.jsonl 2> /tmp/gemini-phase5-stderr.txt
echo "exit=$?"
cat /tmp/gemini-phase5-stdout.jsonl
echo '---'
head -20 /tmp/gemini-phase5-stderr.txt
```

Expected: exit 0, stdout is JSONL with one event per line (capture the exact event types — likely `text_delta`, `tool_use`, `result`/`done`/`turn_complete` equivalents), stderr may have benign noise.

- [ ] **Step 4: Test session resume (skip if Step 2 found no resume flag)**

Extract the session/thread id from the Step 3 stdout (whatever field name Gemini uses), then:

```bash
SESSION=<extracted-id>
gemini <SUB> <JSON_FLAG> --resume "$SESSION" "Reply with exactly AGAIN and do not run any commands." \
    > /tmp/gemini-phase5-resume.jsonl 2>> /tmp/gemini-phase5-stderr.txt
echo "exit=$?"
cat /tmp/gemini-phase5-resume.jsonl
```

Expected: exit 0, resume stdout's session/thread id matches `$SESSION`. If the resume flag form is different (e.g., `--session`, `--thread`, `gemini exec resume`, etc.), use the discovered form.

If no resume mechanism exists, document that and `supports_resume=False` survives.

- [ ] **Step 5: Test failure modes — invalid model, invalid prompt path, missing auth**

```bash
gemini <SUB> <JSON_FLAG> --model bogus-model-name "test" 2>&1 | head -20 || true
unset GEMINI_API_KEY && gemini <SUB> <JSON_FLAG> "test" 2>&1 | head -20 || true
```

Expected: capture the stderr/stdout shape for each. Used by Task 3's error-path tests.

- [ ] **Step 6: Write the findings doc**

Create `docs/superpowers/specs/2026-04-27-gemini-cli-findings.md` modeled on `2026-04-23-codex-cli-findings.md`. Required sections — every section must contain verbatim CLI output, no speculation:

````markdown
# Gemini CLI Findings

**Captured on:** <today's date>
**Binary:** `gemini`
**Version:** `<from Step 1>`

## Commands validated

```text
gemini --version
gemini --help
gemini <SUB> --help
gemini <SUB> <JSON_FLAG> "Reply with exactly OK..."
gemini <SUB> <JSON_FLAG> --resume <session> "Reply with exactly AGAIN..."   # if resume exists
```

## Observed behavior

- Non-interactive execution works through `gemini <SUB> <JSON_FLAG>`.
- Stdout is JSONL with these top-level event types: <list, with one example line each>.
- The session-id field is named `<exact field>`. Resume <does | does not> work via `<exact flag>`.
- Authentication: <env var name> OR `<credentials file path>`.
- Successful runs <do | do not> emit benign stderr noise. <If yes, document the patterns.>
- Sandbox / cwd: <does | does not> require git-init'd cwd. Has flags: <list>.
- Model selection: `--model <slug>` <accepted | unknown | unverified>. Visible models: <list or "no fixed list documented">.
- No thinking-stream event was observed. (Or: a thinking event with type `<X>` was observed.)
- No tool-use event stream was observed. (Or: tool events with type `<Y>` were observed.)

## Initial capability conclusions

- `supports_resume = <True | False>`  — evidence: <Step 4 output line>
- `supports_thinking = False`         — no thinking event observed
- `supports_permissions = False`      — sandbox flags exist but no `/permissions` mapping in v1
- `supports_compact = False`          — no compact subcommand observed
- `supports_allowed_tools = False`    — no allowed-tools flag observed
- `supports_usage_cap_detection = False` — no rate-limit signal observed
- `supports_effort = False`           — no reasoning-effort flag observed
- `models = ()`                       — promotion deferred to a future Phase 6

## Auth & env

- Required env var(s): <list, e.g. GEMINI_API_KEY>
- Credentials file (if any): <path, e.g. ~/.gemini/oauth_creds.json>

## Failure-mode capture

### Invalid model
<stderr/stdout from Step 5, verbatim>

### Missing auth
<stderr/stdout from Step 5, verbatim>
````

- [ ] **Step 7: Save the deterministic JSONL fixtures from the captured output**

Copy the captured stdout into `tests/fixtures/gemini_exec_ok.jsonl` (and `gemini_resume_ok.jsonl` if applicable). If the captured output contains volatile fields (timestamps, telemetry IDs), redact them to deterministic values. Document any redactions in the findings doc.

If Step 3 produced benign stderr lines, save up to 5 representative lines to `tests/fixtures/gemini_stderr_noise.txt`.

- [ ] **Step 8: Commit the findings and fixtures**

```bash
git add docs/superpowers/specs/2026-04-27-gemini-cli-findings.md \
        tests/fixtures/gemini_exec_ok.jsonl
# Add the optional fixtures only if they were created
[ -f tests/fixtures/gemini_resume_ok.jsonl ] && git add tests/fixtures/gemini_resume_ok.jsonl
[ -f tests/fixtures/gemini_stderr_noise.txt ] && git add tests/fixtures/gemini_stderr_noise.txt
git commit -m "docs: capture gemini cli findings and parser fixtures"
```

- [ ] **Step 9: Update spec §10 with resolved open questions**

Edit `docs/superpowers/specs/2026-04-27-backend-phase-5-gemini-adapter-design.md` §10 ("Open questions") and check off each item with the answer captured in the findings doc. Commit:

```bash
git add docs/superpowers/specs/2026-04-27-backend-phase-5-gemini-adapter-design.md
git commit -m "docs(phase-5): resolve open questions from cli findings"
```

---

### Task 2: Implement `gemini_parser.py`

**Files:**
- Create: `src/link_project_to_chat/backends/gemini_parser.py`
- Create: `tests/backends/test_gemini_parser.py`

The parser maps Gemini JSONL lines to the shared `StreamEvent` taxonomy. Mirror `backends/codex_parser.py`'s shape: a single `parse_gemini_line(line: str) -> GeminiParseResult` function, side-effect-free.

> **Note:** The exact Gemini event-type strings (`text_delta`, `result`, `error`, etc.) below are placeholders. Replace them with the actual event names from your Task 1 findings doc when implementing. Examples in this task assume Gemini-style names; substitute Codex-style if Task 1 reveals Gemini uses different conventions.

- [ ] **Step 1: Write the failing parser tests**

```python
# tests/backends/test_gemini_parser.py
"""Parser tests against captured fixtures.

Replaces the literal event-type strings below with the names captured in
docs/superpowers/specs/2026-04-27-gemini-cli-findings.md when implementing.
"""
from pathlib import Path

from link_project_to_chat.backends.gemini_parser import parse_gemini_line
from link_project_to_chat.events import Error, TextDelta


FIXTURE_OK = Path("tests/fixtures/gemini_exec_ok.jsonl")


def test_parse_text_delta_yields_text_delta_event():
    # Substitute the actual text-event line from your fixture.
    line = '{"type":"text_delta","text":"OK"}'
    parsed = parse_gemini_line(line)
    assert parsed.events == [TextDelta(text="OK")]
    assert parsed.turn_completed is False
    assert parsed.session_id is None


def test_parse_session_started_records_session_id():
    line = '{"type":"session_started","session_id":"abc-123"}'
    parsed = parse_gemini_line(line)
    assert parsed.session_id == "abc-123"
    assert parsed.events == []


def test_parse_turn_completed_marks_turn_done_and_captures_usage():
    line = '{"type":"turn_completed","usage":{"input_tokens":100,"output_tokens":20}}'
    parsed = parse_gemini_line(line)
    assert parsed.turn_completed is True
    assert parsed.usage == {"input_tokens": 100, "output_tokens": 20}


def test_parse_error_event_yields_error_event():
    line = '{"type":"error","message":"rate limit exceeded"}'
    parsed = parse_gemini_line(line)
    assert parsed.events == [Error(message="rate limit exceeded")]


def test_parse_unknown_type_returns_empty_result():
    line = '{"type":"some_future_event","data":42}'
    parsed = parse_gemini_line(line)
    assert parsed.events == []
    assert parsed.turn_completed is False
    assert parsed.session_id is None


def test_parse_invalid_json_returns_empty_result():
    parsed = parse_gemini_line("not json at all")
    assert parsed.events == []
    assert parsed.turn_completed is False


def test_parse_full_fixture_yields_text_then_turn_completed():
    """Drive the parser over every line in the OK fixture and assert
    the expected aggregate shape: at least one TextDelta and exactly
    one turn-completed marker."""
    text_count = 0
    completions = 0
    for raw in FIXTURE_OK.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        result = parse_gemini_line(raw)
        text_count += sum(1 for e in result.events if isinstance(e, TextDelta))
        if result.turn_completed:
            completions += 1
    assert text_count >= 1
    assert completions == 1
```

- [ ] **Step 2: Run the parser tests to confirm they fail**

```bash
pytest tests/backends/test_gemini_parser.py -v
```

Expected: ImportError because `link_project_to_chat.backends.gemini_parser` doesn't exist yet.

- [ ] **Step 3: Write the parser**

Match the exact event-type strings from your findings doc. The structure below mirrors `codex_parser.py`:

```python
# src/link_project_to_chat/backends/gemini_parser.py
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..events import Error, StreamEvent, TextDelta


@dataclass
class GeminiParseResult:
    events: list[StreamEvent] = field(default_factory=list)
    session_id: str | None = None
    turn_completed: bool = False
    usage: dict[str, int] | None = None


def parse_gemini_line(line: str) -> GeminiParseResult:
    """Translate one JSONL line from `gemini ... --json` into shared events.

    Returns an empty result for invalid JSON or unknown event types so the
    caller can keep iterating without try/except. Mirrors codex_parser.py.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return GeminiParseResult()

    event_type = data.get("type")

    # === Substitute these branches with the exact event types from the
    # findings doc. The Codex equivalents are thread.started, turn.started,
    # item.completed (agent_message), turn.completed, error. ===

    if event_type == "session_started":
        return GeminiParseResult(session_id=data.get("session_id"))

    if event_type == "text_delta":
        text = data.get("text", "")
        return GeminiParseResult(events=[TextDelta(text=text)] if text else [])

    if event_type == "turn_completed":
        usage = data.get("usage")
        return GeminiParseResult(
            turn_completed=True,
            usage=usage if isinstance(usage, dict) else None,
        )

    if event_type == "error":
        message = data.get("message") or data.get("error") or "Unknown error"
        return GeminiParseResult(events=[Error(message=message)])

    return GeminiParseResult()
```

- [ ] **Step 4: Run the parser tests and verify they pass**

```bash
pytest tests/backends/test_gemini_parser.py -v
```

Expected: all 7 tests PASS. If a test fails because the fixture's actual event types don't match the parser, update the parser to match the findings — NOT the test (the test asserts captured behavior).

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/gemini_parser.py tests/backends/test_gemini_parser.py
git commit -m "feat(gemini): add JSONL parser translating to shared stream events"
```

---

### Task 3: Implement `GeminiBackend`

**Files:**
- Create: `src/link_project_to_chat/backends/gemini.py`
- Create: `tests/backends/test_gemini_backend.py`

The backend is a per-turn-subprocess shape (Codex pattern, not Claude's persistent REPL). It must ship the **P4-C2 lifecycle fix** from day 1 — `chat_stream`'s `finally` terminates the proc before clearing `self._proc`.

- [ ] **Step 1: Write the failing backend tests**

This is the largest test file in the plan. Each test exercises one behavior; do not bundle.

```python
# tests/backends/test_gemini_backend.py
"""GeminiBackend unit tests.

Each test runs against a deterministic fake subprocess so we never spawn
the real `gemini` CLI here. The live CLI is exercised in
`tests/backends/test_gemini_live.py` (gated behind RUN_GEMINI_LIVE=1).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from link_project_to_chat.backends.gemini import (
    GEMINI_CAPABILITIES,
    GeminiBackend,
    GeminiStreamError,
)
from link_project_to_chat.events import Error, Result, TextDelta


FIXTURE_OK = Path("tests/fixtures/gemini_exec_ok.jsonl")


# --- _build_cmd ----------------------------------------------------------

def test_build_cmd_emits_minimum_args(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    cmd = backend._build_cmd("hello world")
    # Exact subcommand and JSON flag from findings — substitute when implementing.
    assert cmd[0] == "gemini"
    assert "hello world" == cmd[-1]
    # No model/effort/permission flags in the conservative v1.
    assert "--model" not in cmd or backend.model is not None
    assert "--effort" not in cmd
    assert "--permission" not in cmd
    assert "--allowed-tools" not in cmd


def test_build_cmd_appends_resume_when_session_id_set(tmp_path):
    """Skip if Task 1 found no resume mechanism — delete this test then."""
    if not GEMINI_CAPABILITIES.supports_resume:
        pytest.skip("supports_resume=False — resume isn't part of v1")
    backend = GeminiBackend(tmp_path, {"session_id": "abc-123"})
    cmd = backend._build_cmd("follow up")
    assert "--resume" in cmd or "--session" in cmd or "resume" in cmd
    assert "abc-123" in cmd


# --- chat_stream success path -------------------------------------------

class _FakeProc:
    """Stand-in for subprocess.Popen used by chat_stream's stdout loop."""

    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0):
        self._lines = list(lines)
        self._stderr_data = stderr.encode()
        self._returncode = returncode
        self._waited = False
        self.pid = 12345
        self.stderr = self  # we'll satisfy .read() below

    def _readline(self) -> bytes:
        if self._lines:
            return (self._lines.pop(0) + "\n").encode()
        return b""

    @property
    def stdout(self):  # noqa: D401
        return self

    def readline(self) -> bytes:
        return self._readline()

    def read(self) -> bytes:
        return self._stderr_data

    def poll(self) -> int | None:
        return self._returncode if self._waited else None

    def kill(self) -> None:
        self._waited = True

    def wait(self, timeout: float | None = None) -> int:
        self._waited = True
        return self._returncode

    @property
    def returncode(self) -> int:
        return self._returncode


@pytest.mark.asyncio
async def test_chat_stream_emits_text_delta_then_result(tmp_path, monkeypatch):
    backend = GeminiBackend(tmp_path, {})
    fixture_lines = FIXTURE_OK.read_text(encoding="utf-8").splitlines()
    fake = _FakeProc(fixture_lines, returncode=0)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    events = []
    async for ev in backend.chat_stream("hi"):
        events.append(ev)

    text_events = [e for e in events if isinstance(e, TextDelta)]
    result_events = [e for e in events if isinstance(e, Result)]
    assert len(text_events) >= 1
    assert len(result_events) == 1
    assert result_events[0].text  # joined collected text, never empty


@pytest.mark.asyncio
async def test_chat_stream_drains_proc_after_turn_completed(tmp_path, monkeypatch):
    """After a clean turn, stderr must be drained and proc.wait() awaited
    before the finally clears _proc. Regression for the P4-C2-class bug."""
    backend = GeminiBackend(tmp_path, {})
    fixture_lines = FIXTURE_OK.read_text(encoding="utf-8").splitlines()
    fake = _FakeProc(fixture_lines, stderr="", returncode=0)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    async for _ in backend.chat_stream("hi"):
        pass

    assert fake._waited is True
    assert backend._proc is None


@pytest.mark.asyncio
async def test_chat_stream_eof_without_turn_completed_raises(tmp_path, monkeypatch):
    """If stdout EOFs before a turn-completed event arrives AND the proc exited
    non-zero, the generator yields one Error and raises GeminiStreamError."""
    backend = GeminiBackend(tmp_path, {})
    # Empty stdout (EOF immediately) + non-zero exit + stderr explanation.
    fake = _FakeProc([], stderr="auth required", returncode=2)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    error_events = []
    with pytest.raises(GeminiStreamError):
        async for ev in backend.chat_stream("hi"):
            if isinstance(ev, Error):
                error_events.append(ev)
    assert len(error_events) == 1
    assert "auth required" in error_events[0].message


@pytest.mark.asyncio
async def test_chat_stream_logs_post_turn_nonzero_exit(tmp_path, monkeypatch, caplog):
    backend = GeminiBackend(tmp_path, {})
    fixture_lines = FIXTURE_OK.read_text(encoding="utf-8").splitlines()
    fake = _FakeProc(fixture_lines, stderr="something went wrong", returncode=1)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    with caplog.at_level("WARNING"):
        async for _ in backend.chat_stream("hi"):
            pass

    assert any("exited 1" in r.message for r in caplog.records)


# --- chat_stream early-cancel path (P4-C2 regression) -------------------

@pytest.mark.asyncio
async def test_chat_stream_kills_proc_on_generator_close(tmp_path, monkeypatch):
    """Closing the async generator before turn_completed must terminate
    the subprocess. This is the test missing for Codex (P4-T4); Gemini
    ships it from day 1 so a future Codex backfill has a template."""
    backend = GeminiBackend(tmp_path, {})
    # Many fixture lines so the consumer can break mid-stream.
    long_lines = ['{"type":"text_delta","text":"x"}'] * 100
    fake = _FakeProc(long_lines, returncode=0)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    gen = backend.chat_stream("hi")
    # Advance the generator once, then close it before turn_completed.
    await gen.__anext__()
    await gen.aclose()

    # The finally must have killed and reaped the proc.
    assert fake._waited is True
    assert backend._proc is None


# --- benign stderr ------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_stderr_warning_does_not_fail_turn(tmp_path, monkeypatch):
    backend = GeminiBackend(tmp_path, {})
    fixture_lines = FIXTURE_OK.read_text(encoding="utf-8").splitlines()
    benign_stderr = "WARN benign telemetry log\n"
    fake = _FakeProc(fixture_lines, stderr=benign_stderr, returncode=0)
    monkeypatch.setattr(backend, "_popen", lambda cmd: fake)

    events = []
    async for ev in backend.chat_stream("hi"):
        events.append(ev)
    result_events = [e for e in events if isinstance(e, Result)]
    error_events = [e for e in events if isinstance(e, Error)]
    assert len(result_events) == 1
    assert error_events == []


# --- cancel / close -----------------------------------------------------

def test_cancel_kills_proc(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    fake = _FakeProc(["{}"])
    backend._proc = fake
    assert backend.cancel() is True
    assert fake._waited is True


def test_cancel_returns_false_when_no_proc(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    assert backend.cancel() is False


def test_close_interactive_no_proc_is_noop(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    backend.close_interactive()  # must not raise


# --- status ------------------------------------------------------------

def test_status_shape(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    st = backend.status
    for key in ("running", "session_id", "total_requests",
                "last_message", "last_error", "permission"):
        assert key in st, f"status missing required key: {key}"
    assert st["running"] is False
    assert st["total_requests"] == 0


def test_current_permission_is_default(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    assert backend.current_permission() == "default"


def test_set_permission_is_noop_when_unsupported(tmp_path):
    backend = GeminiBackend(tmp_path, {})
    backend.set_permission("plan")  # supports_permissions=False; ignored
    # The internal storage does not affect _build_cmd output.
    assert backend.current_permission() == "default"
```

- [ ] **Step 2: Run the backend tests to confirm they fail**

```bash
pytest tests/backends/test_gemini_backend.py -v
```

Expected: ImportError because `link_project_to_chat.backends.gemini` does not exist yet.

- [ ] **Step 3: Implement `GeminiBackend`**

```python
# src/link_project_to_chat/backends/gemini.py
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import Error, Result, StreamEvent, TextDelta
from .base import BackendCapabilities, BaseBackend, HealthStatus
from .factory import register
from .gemini_parser import parse_gemini_line

logger = logging.getLogger(__name__)


class GeminiStreamError(Exception):
    """Raised by GeminiBackend.chat() when the stream returns an Error event."""


GEMINI_CAPABILITIES = BackendCapabilities(
    models=(),
    supports_thinking=False,
    supports_permissions=False,
    supports_resume=False,           # Flip True ONLY if Task 1 confirmed
    supports_compact=False,
    supports_allowed_tools=False,
    supports_usage_cap_detection=False,
    supports_effort=False,
    effort_levels=(),
)


class GeminiBackend(BaseBackend):
    name = "gemini"
    capabilities = GEMINI_CAPABILITIES
    MODEL_OPTIONS: list[tuple[str, str, str, tuple[str, ...]]] = []
    _env_keep_patterns = ("GEMINI_*", "GOOGLE_*")
    _env_scrub_patterns = (
        "*_TOKEN", "*_KEY", "*_SECRET",
        "ANTHROPIC_*", "OPENAI_*", "CODEX_*",
        "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.model: str | None = state.get("model")
        self.model_display: str | None = None
        self.session_id: str | None = state.get("session_id")
        self.effort: str | None = None              # supports_effort=False
        self.permissions: str | None = None         # supports_permissions=False
        self.team_system_note: str | None = None
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_usage: dict | None = None
        self._last_error: str | None = None
        self._total_requests: int = 0

    # --- command building ------------------------------------------------

    def _build_cmd(self, user_message: str) -> list[str]:
        # Replace this with the exact subcommand + JSON flag form from
        # 2026-04-27-gemini-cli-findings.md. The shape below is a template.
        cmd = ["gemini"]
        # cmd += ["<SUB>", "<JSON_FLAG>"]    # e.g. ["chat", "--json"]
        if self.session_id and GEMINI_CAPABILITIES.supports_resume:
            cmd += ["--resume", self.session_id]   # substitute exact flag form
        if self.model:
            cmd += ["--model", self.model]
        prompt = self._build_prompt(user_message)
        cmd.append(prompt)
        return cmd

    def _build_prompt(self, user_message: str) -> str:
        if not self.team_system_note:
            return user_message
        return (
            "<system-reminder>\n"
            f"{self.team_system_note}\n"
            "</system-reminder>\n\n"
            f"{user_message}"
        )

    # --- process spawn ---------------------------------------------------

    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        # Lazy-import: task_manager imports backends.base; importing it at
        # module load creates a circular path through backends/__init__.
        from ..task_manager import _command_popen_kwargs
        kwargs = _command_popen_kwargs()
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._prepare_env(),
            **kwargs,
        )
        if kwargs.get("start_new_session"):
            proc._kill_process_tree = True  # type: ignore[attr-defined]
        return proc

    # --- streaming -------------------------------------------------------

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd = self._build_cmd(user_message)
        proc = self._popen(cmd)
        self._proc = proc
        if on_proc:
            on_proc(proc)
        logger.info("gemini stream subprocess started pid=%s", proc.pid)

        self._last_message = user_message[:80]
        self._started_at = time.monotonic()
        self._total_requests += 1

        collected_text: list[str] = []
        usage: dict | None = None
        try:
            while True:
                raw_line = await asyncio.to_thread(proc.stdout.readline)
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                parsed = parse_gemini_line(line)
                if parsed.session_id:
                    self.session_id = parsed.session_id
                for event in parsed.events:
                    if isinstance(event, TextDelta):
                        collected_text.append(event.text)
                    yield event
                if parsed.usage is not None:
                    usage = parsed.usage
                if parsed.turn_completed:
                    self._last_error = None
                    yield Result(
                        text="".join(collected_text) or "[No response]",
                        session_id=self.session_id,
                        model=self.model_display,
                    )
                    if usage is not None:
                        self._last_usage = usage
                    stderr_bytes = await asyncio.to_thread(proc.stderr.read)
                    await asyncio.to_thread(proc.wait)
                    if proc.returncode != 0:
                        err = stderr_bytes.decode("utf-8", errors="replace").strip()
                        logger.warning(
                            "gemini pid=%s exited %s after turn.completed; stderr=%s",
                            proc.pid,
                            proc.returncode,
                            err[:200] or "(empty)",
                        )
                    return

            # stdout EOF without turn_completed
            stderr_bytes = await asyncio.to_thread(proc.stderr.read)
            await asyncio.to_thread(proc.wait)
            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace").strip()
                self._last_error = err or f"exit code {proc.returncode}"
                yield Error(message=self._last_error)
                raise GeminiStreamError(self._last_error)
        finally:
            # P4-C2 fix: terminate before clearing _proc. An early generator
            # close (CancelledError, exception, early return) must not orphan
            # the subprocess. When P4-C2 lands in Codex later, the two
            # backends end up identical here.
            if proc.poll() is None:
                try:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 5)
                except Exception:
                    logger.exception("gemini chat_stream cleanup failed pid=%s", proc.pid)
            if self._proc is proc:
                self._proc = None
                self._started_at = None
            logger.info("gemini stream pid=%s turn done", proc.pid)

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
                raise GeminiStreamError(event.message)
        return result_text or "[No response]"

    async def probe_health(self) -> HealthStatus:
        try:
            await self.chat("Reply with exactly PONG and do not run any commands.")
        except GeminiStreamError as exc:
            self._last_error = str(exc)
            return HealthStatus(ok=False, usage_capped=False, error_message=self._last_error)
        self._last_error = None
        return HealthStatus(ok=True, usage_capped=False, error_message=None)

    # --- process control -------------------------------------------------

    def close_interactive(self) -> None:
        from ..task_manager import _terminate_process_tree
        proc = self._proc
        if proc and proc.poll() is None:
            _terminate_process_tree(proc)
        if self._proc is proc:
            self._proc = None
            self._started_at = None

    def cancel(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        proc.kill()
        return True

    def current_permission(self) -> str:
        return "default"

    def set_permission(self, mode: str | None) -> None:
        # supports_permissions=False — capability gate prevents this from
        # being called via UI, but the Protocol method must exist.
        if mode not in (None, "default"):
            logger.warning("gemini ignoring set_permission(%r); supports_permissions=False", mode)

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
            "permission": self.current_permission(),
            "last_error": self._last_error,
        }


def _make_gemini(project_path: Path, state: dict) -> GeminiBackend:
    return GeminiBackend(project_path, state)


register("gemini", _make_gemini)
```

- [ ] **Step 4: Run the backend tests and verify they pass**

```bash
pytest tests/backends/test_gemini_backend.py -v
```

Expected: all tests PASS. If `test_chat_stream_kills_proc_on_generator_close` fails, the `finally` block is wrong — DO NOT clear `_proc` before the `proc.kill()` step.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/gemini.py tests/backends/test_gemini_backend.py
git commit -m "feat(gemini): add conservative GeminiBackend with P4-C2 lifecycle fix"
```

---

### Task 4: Register Gemini, Lock Capabilities, Extend Env Policy

**Files:**
- Modify: `src/link_project_to_chat/backends/__init__.py:1-18` (import gemini for registration side effects + export `GeminiBackend`)
- Modify: `tests/backends/test_capability_declaration.py:1-19` (add Gemini lock test)
- Modify: `tests/backends/test_env_policy.py:1-21` (add Gemini env keep/scrub test)
- Modify: `pyproject.toml:56-58` (register `gemini_live` marker)
- Modify: `tests/conftest.py:25-29` (exempt `gemini_live` from `_isolate_home`)

- [ ] **Step 1: Modify `backends/__init__.py` to register gemini**

```python
# src/link_project_to_chat/backends/__init__.py
from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from . import gemini as _gemini  # noqa: F401
from .base import AgentBackend, BackendCapabilities, BaseBackend, HealthStatus
from .claude import ClaudeBackend
from .codex import CodexBackend
from .gemini import GeminiBackend
from .factory import available, create, register

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "BaseBackend",
    "ClaudeBackend",
    "CodexBackend",
    "GeminiBackend",
    "HealthStatus",
    "available",
    "create",
    "register",
]
```

- [ ] **Step 2: Add the Gemini capability lock test**

Append to `tests/backends/test_capability_declaration.py`:

```python
# Append below the existing test_codex_capabilities_match_validated_findings.

from link_project_to_chat.backends.gemini import GEMINI_CAPABILITIES


def test_gemini_capabilities_match_validated_findings():
    """Phase 5 conservative declaration. Promotion lives in a future Phase 6
    once real-usage gaps surface (the Phase 4 trigger pattern).

    If Task 1 (CLI findings) confirms session-id support, supports_resume
    flips to True here AND in backends/gemini.py:GEMINI_CAPABILITIES.
    """
    assert tuple(GEMINI_CAPABILITIES.models) == ()
    assert GEMINI_CAPABILITIES.supports_thinking is False
    assert GEMINI_CAPABILITIES.supports_permissions is False
    # Replace `False` below with `True` IFF the findings doc confirms resume.
    assert GEMINI_CAPABILITIES.supports_resume is False
    assert GEMINI_CAPABILITIES.supports_compact is False
    assert GEMINI_CAPABILITIES.supports_allowed_tools is False
    assert GEMINI_CAPABILITIES.supports_usage_cap_detection is False
    assert GEMINI_CAPABILITIES.supports_effort is False
    assert tuple(GEMINI_CAPABILITIES.effort_levels) == ()
```

- [ ] **Step 3: Add the Gemini env-policy test**

Append to `tests/backends/test_env_policy.py`:

```python
from link_project_to_chat.backends.gemini import GeminiBackend


def test_gemini_keeps_google_but_scrubs_anthropic_and_openai(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/svc.json")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    env = GeminiBackend(tmp_path, {})._prepare_env()
    assert env["GEMINI_API_KEY"] == "gemini-secret"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/tmp/svc.json"
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
```

- [ ] **Step 4: Register the `gemini_live` pytest marker**

Edit `pyproject.toml:56-58`:

```toml
markers = [
    "codex_live: requires RUN_CODEX_LIVE=1 plus a real codex CLI installation and local authentication",
    "gemini_live: requires RUN_GEMINI_LIVE=1 plus a real gemini CLI installation and local authentication",
]
```

- [ ] **Step 5: Exempt `gemini_live` from `_isolate_home`**

Edit `tests/conftest.py:25-29`:

```python
@pytest.fixture(autouse=True)
def _isolate_home(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("codex_live"):
        return
    if request.node.get_closest_marker("gemini_live"):
        return
    monkeypatch.setenv("HOME", str(tmp_path))
```

- [ ] **Step 6: Run the modified test suites**

```bash
pytest tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_contract.py -v
```

Expected:
- 3 env-policy tests pass (Claude, Codex, Gemini).
- 3 capability-lock tests pass (Codex, Gemini; Claude is implicit).
- The contract test parametrizations now include `gemini` automatically — verify `test_backend_contract_declares_name_and_capabilities` runs once per registered backend and passes.

If a contract-test parametrization fails for Gemini because `chat()` actually spawns a subprocess in CI, mirror the Codex pattern at `tests/backends/test_contract.py:32-41` — add a `test_gemini_backend_contract_chat_returns_string` that monkeypatches `backend.chat` to return `"ok"` rather than spawning the real CLI.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/backends/__init__.py \
        tests/backends/test_capability_declaration.py \
        tests/backends/test_env_policy.py \
        pyproject.toml tests/conftest.py
git commit -m "feat(gemini): register backend, lock conservative capabilities, exempt live marker"
```

---

### Task 5: Add Live (Gated) Tests

**Files:**
- Create: `tests/backends/test_gemini_live.py`

The live suite mirrors `tests/backends/test_codex_live.py`. Skipped by default; runs only when `RUN_GEMINI_LIVE=1` AND the `gemini_live` marker is selected.

- [ ] **Step 1: Write the live tests**

```python
# tests/backends/test_gemini_live.py
"""Live integration tests against the real `gemini` CLI.

Skipped unless RUN_GEMINI_LIVE=1 AND the `gemini_live` marker is selected:

    RUN_GEMINI_LIVE=1 pytest tests/backends/test_gemini_live.py -m gemini_live -v -s

Spawns a real `gemini` subprocess in a fresh git-init'd tmp_path so the cwd
mirrors a production project. Requires local authentication (GEMINI_API_KEY
or `gemini auth login` credentials in ~/.gemini/).
"""
from __future__ import annotations

import os
import subprocess

import pytest

from link_project_to_chat.backends.gemini import GEMINI_CAPABILITIES, GeminiBackend
from link_project_to_chat.events import Result, TextDelta


pytestmark = [
    pytest.mark.gemini_live,
    pytest.mark.skipif(
        os.environ.get("RUN_GEMINI_LIVE") != "1",
        reason="set RUN_GEMINI_LIVE=1 to enable live Gemini tests",
    ),
]


def _trusted_project(tmp_path):
    """Init tmp_path as a git repo so gemini-cli is happy if it requires
    a git-tracked cwd (Codex 0.125 did)."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    return tmp_path


@pytest.mark.asyncio
async def test_gemini_live_round_trip(tmp_path):
    project = _trusted_project(tmp_path)
    backend = GeminiBackend(project, {})
    text_count = 0
    final: Result | None = None
    async for event in backend.chat_stream(
        "Reply with exactly OK and do not run any commands."
    ):
        if isinstance(event, TextDelta):
            text_count += 1
        if isinstance(event, Result):
            final = event
    assert text_count >= 1, "expected at least one TextDelta from the real CLI"
    assert final is not None, "expected a closing Result from the real CLI"
    assert "OK" in (final.text or "")


@pytest.mark.asyncio
async def test_gemini_live_resume_reuses_session(tmp_path):
    if not GEMINI_CAPABILITIES.supports_resume:
        pytest.skip("supports_resume=False — resume isn't part of v1")
    project = _trusted_project(tmp_path)
    backend = GeminiBackend(project, {})

    async for _ in backend.chat_stream(
        "Reply with exactly OK and do not run any commands."
    ):
        pass
    first_session = backend.session_id
    assert first_session is not None, "expected a session id from the first turn"

    async for _ in backend.chat_stream(
        "Reply with exactly AGAIN and do not run any commands."
    ):
        pass
    assert backend.session_id == first_session, "resume must reuse the original session id"
```

- [ ] **Step 2: Verify the live suite skips cleanly without RUN_GEMINI_LIVE**

```bash
pytest tests/backends/test_gemini_live.py -v
```

Expected: tests reported as skipped with reason "set RUN_GEMINI_LIVE=1 to enable live Gemini tests".

- [ ] **Step 3: If `gemini-cli` is locally installed and authenticated, run the live suite**

```bash
RUN_GEMINI_LIVE=1 pytest tests/backends/test_gemini_live.py -m gemini_live -v -s
```

Expected: both tests pass (the resume test skips if Task 1 disabled `supports_resume`).

If the round-trip test fails because `gemini` requires extra arguments, update `_build_cmd` in `backends/gemini.py` to match what the live invocation requires AND update `gemini_exec_ok.jsonl` if the captured event types changed.

- [ ] **Step 4: Commit**

```bash
git add tests/backends/test_gemini_live.py
git commit -m "test(gemini): add gated live integration suite"
```

---

### Task 6: Bot-Level Rejection Tests And Documentation

**Files:**
- Modify: `tests/test_backend_command.py` (add Gemini-active rejection tests)
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/TODO.md` (flip Phase 5 status from 📋 to ✅)

- [ ] **Step 1: Add bot-level capability-rejection tests for Gemini**

The existing test file uses two helpers — `_make_bot(tmp_path)` (line 58) which builds a fresh `ProjectBot` with a `FakeTransport`, and `_switch_to_codex(bot)` (line 260) which sends `/backend codex` to flip the active backend. Append a parallel `_switch_to_gemini` helper, then five rejection tests + one picker-content test. All test bodies use the existing `_ci([args])` invocation helper (line 46) and `bot._transport.sent_messages` to read FakeTransport output.

Append to `tests/test_backend_command.py`:

```python
# Append at the end of the file.

from link_project_to_chat.backends.gemini import GEMINI_CAPABILITIES


async def _switch_to_gemini(bot: ProjectBot) -> None:
    from link_project_to_chat.backends import gemini as _gemini  # noqa: F401

    await bot._on_backend(_ci(["gemini"]))
    bot._transport.sent_messages.clear()


async def test_model_command_rejected_when_gemini_active(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_gemini(bot)
    await bot._on_model(_ci([]))
    sent = bot._transport.sent_messages
    assert any(
        "doesn't support /model" in (m.text or "") for m in sent
    ), [m.text for m in sent]


async def test_effort_command_rejected_when_gemini_active(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_gemini(bot)
    await bot._on_effort(_ci([]))
    sent = bot._transport.sent_messages
    assert any("doesn't support /effort" in (m.text or "") for m in sent)


async def test_permissions_command_rejected_when_gemini_active(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_gemini(bot)
    await bot._on_permissions(_ci([]))
    sent = bot._transport.sent_messages
    assert any("doesn't support /permissions" in (m.text or "") for m in sent)


async def test_compact_command_rejected_when_gemini_active(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_gemini(bot)
    await bot._on_compact(_ci([]))
    sent = bot._transport.sent_messages
    assert any("doesn't support /compact" in (m.text or "") for m in sent)


async def test_thinking_command_rejected_when_gemini_active(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_gemini(bot)
    await bot._on_thinking(_ci([]))
    sent = bot._transport.sent_messages
    assert any("doesn't support /thinking" in (m.text or "") for m in sent)


def test_backend_picker_includes_gemini_row(tmp_path):
    bot = _make_bot(tmp_path)
    buttons = bot._backend_buttons()
    button_values = {b.value for row in buttons.rows for b in row}
    assert "backend_set_gemini" in button_values
```

The file's `pyproject.toml` is configured with `asyncio_mode = "auto"` (per `CLAUDE.md`), so `async def test_*` is sufficient — no `@pytest.mark.asyncio` decorator is needed.

- [ ] **Step 2: Run the new bot-level tests**

```bash
pytest tests/test_backend_command.py -v -k "gemini"
```

Expected: 6 new tests pass. The capability-gate code in `bot.py` already handles `False` flags — no bot.py changes are needed for these to pass.

- [ ] **Step 3: Update top-level docs**

Edit `CLAUDE.md` — find the `### Key modules` block referencing backends and append Gemini:

```markdown
- **backends/** — `AgentBackend` Protocol + `BaseBackend` env-policy helper (base.py), `ClaudeBackend` (claude.py) and Claude JSONL parser (claude_parser.py), `CodexBackend` (codex.py) and Codex JSONL parser (codex_parser.py), **`GeminiBackend` (gemini.py) and Gemini JSONL parser (gemini_parser.py)**, `factory.py`. The bot constructs a backend via the factory; backend extraction landed under backend phase 1, Codex landed under phase 3 as an opt-in via `/backend codex`, and **Gemini landed under phase 5 as an opt-in via `/backend gemini` with conservative capabilities (no `/model`, `/effort`, `/permissions`, etc.)**. Per-backend env scrub/keep allowlists run through `BaseBackend._prepare_env`. Live Codex coverage gates behind `RUN_CODEX_LIVE=1` and the `codex_live` pytest marker; **live Gemini coverage gates behind `RUN_GEMINI_LIVE=1` and the `gemini_live` marker**.
```

Edit `AGENTS.md` similarly — locate the equivalent backend-architecture line and apply the same Gemini addendum.

- [ ] **Step 4: Add a CHANGELOG entry**

Prepend to `docs/CHANGELOG.md` (under the next-version header — use the same heading style as the most recent entries; add a new heading if shipping a new version):

```markdown
## Backend Phase 5 — Gemini adapter (opt-in, conservative)

- Added `GeminiBackend` wrapping the official `gemini-cli` (`@google/gemini-cli`).
- Selectable via `/backend gemini`. Defaults remain Claude.
- Conservative capability declaration: `supports_resume` only (if Task 1 confirmed); `/model`, `/effort`, `/permissions`, `/compact`, `/thinking`, `/allowed_tools` rejected via the existing capability gate.
- Env policy keeps `GEMINI_*` and `GOOGLE_*`, scrubs `ANTHROPIC_*` / `OPENAI_*` / `CODEX_*` / generic token patterns.
- Ships the corrected subprocess lifecycle (terminate before clearing `_proc`) — Gemini does NOT inherit the P4-C2 zombie-proc bug Codex still has.
- Live tests: `RUN_GEMINI_LIVE=1 pytest -m gemini_live`.

Commits: <list, populated after final commit>.
```

- [ ] **Step 5: Update `docs/TODO.md`**

Edit the `§2` phase table — flip Phase 5 status from `📋` to `✅` and link the new plan:

```markdown
| Phase 5 — Gemini adapter (conservative) | [spec](superpowers/specs/2026-04-27-backend-phase-5-gemini-adapter-design.md) | [plan](superpowers/plans/2026-04-27-backend-phase-5-gemini-adapter.md) | ✅ |
```

Append a Phase 5 evidence paragraph after the existing Phase 5 scope paragraph, listing the actual commit SHAs and `RUN_GEMINI_LIVE=1` test results.

Update the `§Summary by Status` line to remove "Backend Phase 5 (Gemini adapter)" from "Designed, not started" and bump the "Shipped" count.

- [ ] **Step 6: Run the full suite to confirm nothing regressed**

```bash
PYTHONPATH=src pytest -q 2>&1 | tail -10
```

Expected: 880+ passed, 30+ skipped, 0 new failures (the 2 pre-existing `tests/test_cli_transport.py::test_start_*` failures are documented in `docs/TODO.md` §7 as known and are NOT caused by this plan).

If any other test fails, diagnose the root cause — do NOT skip or weaken the failing test.

- [ ] **Step 7: Final commit**

```bash
git add tests/test_backend_command.py CLAUDE.md AGENTS.md docs/CHANGELOG.md docs/TODO.md
git commit -m "feat(gemini): bot-level capability-gate tests + docs"
```

---

## Phase 5 Self-Review Checklist

- [ ] Task 1's findings doc has a captured answer for each of the 10 questions in spec §4.1 (no "unknown").
- [ ] Spec §10 open questions are marked resolved with the Task 1 answers.
- [ ] `GEMINI_CAPABILITIES` declarations match the findings doc's "Initial capability conclusions" section, line-for-line.
- [ ] `chat_stream`'s `finally` block terminates the proc BEFORE clearing `self._proc` (P4-C2 fix shipped from day 1, not deferred).
- [ ] `tests/backends/test_gemini_backend.py::test_chat_stream_kills_proc_on_generator_close` passes — proves the lifecycle fix.
- [ ] `RUN_GEMINI_LIVE=1 pytest -m gemini_live` passes on at least one authenticated machine OR skips cleanly on machines without `gemini-cli`.
- [ ] Full suite (`pytest -q`) shows the same pass count as before plan execution + 20–30 new passes (parser + backend + bot-level tests), with NO new failures.
- [ ] Bot-level commands `/model`, `/effort`, `/permissions`, `/compact`, `/thinking` all reject cleanly when Gemini is active — verified by the new tests.
- [ ] No `bot.py` changes were needed (the registry-driven pickers and capability gates handle Gemini automatically). If a `bot.py` change WAS needed, it indicates a Phase 4 design gap and should be tracked separately in TODO §2.1.
- [ ] `docs/TODO.md` Phase 5 row flipped to ✅ and the evidence paragraph cites real commit SHAs.
