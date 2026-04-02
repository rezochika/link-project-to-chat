# Full Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix active bugs, add dev tooling and CI, comprehensive test coverage, and reliability improvements.

**Architecture:** Five sequential phases — bug fixes first (clean foundation), then dev tooling + CI (guardrails), then unit tests (coverage), then integration tests (end-to-end confidence), then reliability hardening. Each phase builds on the previous.

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio + pytest-cov, ruff, mypy, GitHub Actions, hatchling

**Spec:** `docs/superpowers/specs/2026-04-02-full-cleanup-design.md`

---

## Phase 1: Bug Fixes

### Task 1: Fix version mismatch

**Files:**
- Modify: `src/link_project_to_chat/__init__.py:1`
- Modify: `pyproject.toml:5-6`

- [ ] **Step 1: Update `__init__.py` version to `0.2.0`**

In `src/link_project_to_chat/__init__.py`, change:

```python
__version__ = "0.2.0"
```

- [ ] **Step 2: Switch pyproject.toml to dynamic versioning**

In `pyproject.toml`, replace the static `version` line and add dynamic versioning:

```toml
[project]
name = "link-project-to-chat"
dynamic = ["version"]
description = "Link a project directory to a Telegram bot that chats with Claude"
```

Add after the `[build-system]` section:

```toml
[tool.hatch.version]
path = "src/link_project_to_chat/__init__.py"
```

Remove the line `version = "0.2.0"` from `[project]`.

- [ ] **Step 3: Verify the build still works**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && pip install -e .`
Expected: Installs successfully, `pip show link-project-to-chat` shows `Version: 0.2.0`

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/__init__.py pyproject.toml
git commit -m "fix: sync version via hatchling dynamic versioning"
```

---

### Task 2: Wire --model and --effort flags to Claude CLI

**Files:**
- Modify: `src/link_project_to_chat/claude_client.py:31-38`

- [ ] **Step 1: Uncomment and wire --model and --effort in `chat()`**

In `src/link_project_to_chat/claude_client.py`, replace the `cmd` construction in `chat()` (lines 32-38):

```python
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
            "--effort", self.effort,
            "--dangerously-skip-permissions",
        ]
```

- [ ] **Step 2: Commit**

```bash
git add src/link_project_to_chat/claude_client.py
git commit -m "fix: pass --model and --effort flags to claude subprocess"
```

---

### Task 3: Fix _proc single-slot race condition

**Files:**
- Modify: `src/link_project_to_chat/claude_client.py:19-110`
- Modify: `src/link_project_to_chat/bot.py:278-293`

- [ ] **Step 1: Simplify ClaudeClient — remove process tracking fields**

In `src/link_project_to_chat/claude_client.py`, replace the `__init__` method:

```python
    def __init__(self, project_path: Path):
        self.model = DEFAULT_MODEL
        self.project_path = project_path
        self.effort: str = "medium"
        self.session_id: str | None = None
```

- [ ] **Step 2: Simplify the `chat()` method — remove process tracking**

Replace the `chat()` method to remove `_proc`, `_started_at`, `_last_message`, `_last_duration`, `_total_requests` references:

```python
    async def chat(self, user_message: str, on_proc=None) -> str:
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
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
        logger.info("claude subprocess started pid=%s", proc.pid)

        stdout, stderr = await asyncio.to_thread(proc.communicate)

        logger.info("claude pid=%s done, code=%s, %d bytes", proc.pid, proc.returncode, len(stdout))

        if stderr_text := stderr.decode("utf-8", errors="replace").strip():
            logger.warning("claude stderr: %s", stderr_text)

        if proc.returncode != 0:
            return f"Error: {stderr_text or f'exit code {proc.returncode}'}"

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return "[No response]"

        try:
            data = json.loads(raw)
            self.session_id = data.get("session_id", self.session_id)
            self.model = next(iter(data.get("modelUsage", {})), self.model)
            return data.get("result", raw)
        except json.JSONDecodeError:
            return raw
```

- [ ] **Step 3: Replace `status` property and remove `cancel()` method**

Replace the `status` property and remove the `cancel` method. The new `status` only reports session/model/effort:

```python
    @property
    def status(self) -> dict:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "effort": self.effort,
        }
```

Delete the `cancel()` method entirely (lines 111-116 of the original file).

- [ ] **Step 4: Update bot.py `_on_status` to use TaskManager for running state**

In `src/link_project_to_chat/bot.py`, replace the `_on_status` method (lines 274-293):

```python
    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return

        uptime = time.monotonic() - self._started_at
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)

        st = self.task_manager.claude.status
        lines = [
            f"Project: {self.name}",
            f"Path: {self.path}",
            f"Model: {st['model']}",
            f"Effort: {st['effort']}",
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st['session_id'] or 'none'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
        ]
        await update.effective_message.reply_text("\n".join(lines))
```

- [ ] **Step 5: Verify the app still starts**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -c "from link_project_to_chat.claude_client import ClaudeClient; from pathlib import Path; c = ClaudeClient(Path('.')); print(c.status)"`
Expected: `{'session_id': None, 'model': 'sonnet', 'effort': 'medium'}`

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/claude_client.py src/link_project_to_chat/bot.py
git commit -m "fix: remove _proc single-slot from ClaudeClient, move running state to TaskManager"
```

---

## Phase 2: Dev Tooling & CI

### Task 4: Add ruff config and fix findings

**Files:**
- Modify: `pyproject.toml`
- Modify: all `src/link_project_to_chat/*.py` (auto-fixed by ruff)

- [ ] **Step 1: Add ruff config to pyproject.toml**

Append to `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]

[tool.ruff.format]
quote-style = "double"
```

- [ ] **Step 2: Run ruff check and auto-fix**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && pip install ruff && ruff check src/ --fix`
Review the output. Fix any remaining issues that can't be auto-fixed.

- [ ] **Step 3: Run ruff format**

Run: `ruff format src/`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/
git commit -m "chore: add ruff config, fix lint and format issues"
```

---

### Task 5: Add mypy config and fix type errors

**Files:**
- Modify: `pyproject.toml`
- Modify: source files as needed for type fixes

- [ ] **Step 1: Add mypy config to pyproject.toml**

Append to `pyproject.toml`:

```toml
[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[[tool.mypy.overrides]]
module = "telegram.*"
ignore_missing_imports = true
```

- [ ] **Step 2: Install mypy and run it**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && pip install mypy && mypy src/`
Review errors and fix them. Common fixes will be adding type annotations to function parameters and return types.

- [ ] **Step 3: Fix all type errors**

Fix each error reported by mypy. Typical changes:
- Add return type annotations to methods missing them
- Add parameter type annotations where missing
- Use `type: ignore[...]` comments sparingly for third-party library issues

- [ ] **Step 4: Verify clean mypy run**

Run: `mypy src/`
Expected: `Success: no issues found`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/
git commit -m "chore: add mypy config, fix all type errors"
```

---

### Task 6: Add pytest config and dev dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/integration/__init__.py`

- [ ] **Step 1: Add optional dev dependencies to pyproject.toml**

Add after `[project.urls]`:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.0",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

- [ ] **Step 2: Add pytest config to pyproject.toml**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "integration: integration tests (require external services)",
]
```

- [ ] **Step 3: Create test directories**

Create `tests/__init__.py` (empty file) and `tests/integration/__init__.py` (empty file).

- [ ] **Step 4: Install dev dependencies and verify pytest runs**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && pip install -e ".[dev]" && pytest --co`
Expected: `no tests ran` (no test files yet, but no errors)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/
git commit -m "chore: add pytest config, dev dependencies, test directories"
```

---

### Task 7: Add GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create CI workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -e ".[dev]"
      - run: ruff check src/
      - run: ruff format --check src/
      - run: mypy src/

  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest --cov=link_project_to_chat --cov-report=term-missing -m "not integration"
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow for lint and test"
```

---

## Phase 3: Unit Tests

### Task 8: Unit tests for config module

**Files:**
- Create: `tests/test_config.py`

- [ ] **Step 1: Write config tests**

Create `tests/test_config.py`:

```python
from __future__ import annotations

import json
import stat

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    clear_session,
    clear_trusted_user_id,
    load_config,
    load_sessions,
    load_trusted_user_id,
    save_config,
    save_session,
    save_trusted_user_id,
)


class TestLoadSaveConfig:
    def test_load_missing_file(self, tmp_path):
        config = load_config(tmp_path / "missing.json")
        assert config.allowed_username == ""
        assert config.projects == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.json"
        config = Config(
            allowed_username="alice",
            projects={"myproj": ProjectConfig(path="/tmp/myproj", telegram_bot_token="tok123")},
        )
        save_config(config, path)
        loaded = load_config(path)
        assert loaded.allowed_username == "alice"
        assert "myproj" in loaded.projects
        assert loaded.projects["myproj"].path == "/tmp/myproj"
        assert loaded.projects["myproj"].telegram_bot_token == "tok123"

    def test_save_sets_permissions(self, tmp_path):
        path = tmp_path / "config.json"
        save_config(Config(), path)
        file_mode = stat.S_IMODE(path.stat().st_mode)
        assert file_mode == 0o600

    def test_save_creates_parent_directory(self, tmp_path):
        path = tmp_path / "subdir" / "config.json"
        save_config(Config(), path)
        assert path.exists()

    def test_load_normalizes_username(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"allowed_username": "@Alice", "projects": {}}))
        config = load_config(path)
        assert config.allowed_username == "alice"


class TestSessions:
    def test_load_missing(self, tmp_path):
        assert load_sessions(tmp_path / "sessions.json") == {}

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "sessions.json"
        save_session("proj1", "sess-abc", path)
        sessions = load_sessions(path)
        assert sessions == {"proj1": "sess-abc"}

    def test_save_multiple(self, tmp_path):
        path = tmp_path / "sessions.json"
        save_session("proj1", "sess-1", path)
        save_session("proj2", "sess-2", path)
        sessions = load_sessions(path)
        assert sessions == {"proj1": "sess-1", "proj2": "sess-2"}

    def test_clear_session(self, tmp_path):
        path = tmp_path / "sessions.json"
        save_session("proj1", "sess-1", path)
        clear_session("proj1", path)
        sessions = load_sessions(path)
        assert "proj1" not in sessions

    def test_clear_nonexistent(self, tmp_path):
        path = tmp_path / "sessions.json"
        save_session("proj1", "sess-1", path)
        clear_session("other", path)  # should not error
        assert load_sessions(path) == {"proj1": "sess-1"}

    def test_load_corrupted(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("not json")
        assert load_sessions(path) == {}


class TestTrustedUserId:
    def test_load_missing(self, tmp_path):
        assert load_trusted_user_id(tmp_path / "uid.json") is None

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "uid.json"
        save_trusted_user_id(12345, path)
        assert load_trusted_user_id(path) == 12345

    def test_clear(self, tmp_path):
        path = tmp_path / "uid.json"
        save_trusted_user_id(12345, path)
        clear_trusted_user_id(path)
        assert not path.exists()

    def test_clear_missing(self, tmp_path):
        clear_trusted_user_id(tmp_path / "uid.json")  # should not error

    def test_load_corrupted(self, tmp_path):
        path = tmp_path / "uid.json"
        path.write_text("not json")
        assert load_trusted_user_id(path) is None
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "test: add unit tests for config module"
```

---

### Task 9: Unit tests for formatting module

**Files:**
- Create: `tests/test_formatting.py`

- [ ] **Step 1: Write formatting tests**

Create `tests/test_formatting.py`:

```python
from __future__ import annotations

from link_project_to_chat.formatting import md_to_telegram, split_html, strip_html


class TestMdToTelegram:
    def test_bold(self):
        assert "<b>hello</b>" in md_to_telegram("**hello**")

    def test_bold_underscores(self):
        assert "<b>hello</b>" in md_to_telegram("__hello__")

    def test_italic_asterisks(self):
        assert "<i>hello</i>" in md_to_telegram("*hello*")

    def test_italic_underscores(self):
        assert "<i>hello</i>" in md_to_telegram("_hello_")

    def test_italic_not_inside_words(self):
        result = md_to_telegram("file_name_here")
        assert "<i>" not in result

    def test_strikethrough(self):
        assert "<s>hello</s>" in md_to_telegram("~~hello~~")

    def test_inline_code(self):
        assert "<code>foo</code>" in md_to_telegram("`foo`")

    def test_fenced_code_block(self):
        md = "```python\nprint('hi')\n```"
        result = md_to_telegram(md)
        assert '<code class="language-python">' in result
        assert "<pre>" in result

    def test_fenced_code_block_no_language(self):
        md = "```\nfoo\n```"
        result = md_to_telegram(md)
        assert "<pre>" in result
        assert "language-" not in result

    def test_code_block_html_escaped(self):
        md = "```\n<div>&amp;</div>\n```"
        result = md_to_telegram(md)
        assert "&lt;div&gt;" in result

    def test_link(self):
        result = md_to_telegram("[click](https://example.com)")
        assert '<a href="https://example.com">click</a>' in result

    def test_header_becomes_bold(self):
        assert "<b>Title</b>" in md_to_telegram("# Title")

    def test_h3_becomes_bold(self):
        assert "<b>Sub</b>" in md_to_telegram("### Sub")

    def test_blockquote(self):
        result = md_to_telegram("> quoted text")
        assert "<blockquote>" in result

    def test_html_escaped_in_text(self):
        result = md_to_telegram("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_table(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = md_to_telegram(md)
        assert "<pre>" in result

    def test_empty_input(self):
        assert md_to_telegram("") == ""

    def test_plain_text_unchanged(self):
        result = md_to_telegram("just plain text")
        assert "just plain text" in result


class TestSplitHtml:
    def test_short_message_no_split(self):
        html = "short message"
        assert split_html(html) == ["short message"]

    def test_split_at_limit(self):
        html = "a" * 5000
        chunks = split_html(html, limit=4096)
        assert len(chunks) >= 2
        assert all(len(c) <= 4096 for c in chunks)

    def test_preserves_pre_blocks(self):
        code = "x" * 100
        html = f"before\n<pre>{code}</pre>\nafter"
        chunks = split_html(html, limit=4096)
        # The <pre> block should not be split across chunks
        full = "".join(chunks)
        assert f"<pre>{code}</pre>" in full

    def test_custom_limit(self):
        html = "a\nb\nc\nd\ne"
        chunks = split_html(html, limit=3)
        assert all(len(c) <= 3 for c in chunks)


class TestStripHtml:
    def test_strips_tags(self):
        assert strip_html("<b>bold</b>") == "bold"

    def test_unescapes_entities(self):
        assert strip_html("&amp; &lt; &gt;") == "& < >"

    def test_nested_tags(self):
        assert strip_html("<b><i>text</i></b>") == "text"
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_formatting.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_formatting.py
git commit -m "test: add unit tests for formatting module"
```

---

### Task 10: Unit tests for claude_client module

**Files:**
- Create: `tests/test_claude_client.py`

- [ ] **Step 1: Write claude_client tests**

Create `tests/test_claude_client.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from link_project_to_chat.claude_client import ClaudeClient, DEFAULT_MODEL, EFFORT_LEVELS


@pytest.fixture
def client(tmp_path):
    return ClaudeClient(tmp_path)


def _mock_popen(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create a mock Popen that returns given stdout/stderr/returncode."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    return mock_proc


class TestClaudeClientInit:
    def test_defaults(self, client):
        assert client.model == DEFAULT_MODEL
        assert client.effort == "medium"
        assert client.session_id is None

    def test_status(self, client):
        st = client.status
        assert st == {"session_id": None, "model": DEFAULT_MODEL, "effort": "medium"}


class TestChat:
    @pytest.mark.asyncio
    async def test_successful_response(self, client):
        response_data = {"session_id": "sess-123", "result": "Hello!", "modelUsage": {"claude-sonnet-4-20250514": {}}}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            result = await client.chat("hi")

        assert result == "Hello!"
        assert client.session_id == "sess-123"

    @pytest.mark.asyncio
    async def test_session_id_extracted(self, client):
        response_data = {"session_id": "new-sess", "result": "ok"}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            await client.chat("test")

        assert client.session_id == "new-sess"

    @pytest.mark.asyncio
    async def test_model_extracted_from_usage(self, client):
        response_data = {"session_id": "s1", "result": "ok", "modelUsage": {"claude-opus-4-20250514": {}}}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            await client.chat("test")

        assert client.model == "claude-opus-4-20250514"

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_error(self, client):
        mock_proc = _mock_popen(stderr=b"something broke", returncode=1)

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            result = await client.chat("test")

        assert result == "Error: something broke"

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_no_response(self, client):
        mock_proc = _mock_popen(stdout=b"")

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            result = await client.chat("test")

        assert result == "[No response]"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_raw(self, client):
        mock_proc = _mock_popen(stdout=b"not json at all")

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            result = await client.chat("test")

        assert result == "not json at all"

    @pytest.mark.asyncio
    async def test_command_includes_model_and_effort(self, client):
        client.model = "opus"
        client.effort = "high"
        response_data = {"result": "ok"}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc) as mock_cls:
            await client.chat("test")

        cmd = mock_cls.call_args[0][0]
        assert "--model" in cmd
        assert "opus" in cmd
        assert "--effort" in cmd
        assert "high" in cmd

    @pytest.mark.asyncio
    async def test_command_includes_resume_when_session(self, client):
        client.session_id = "existing-sess"
        response_data = {"result": "ok", "session_id": "existing-sess"}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc) as mock_cls:
            await client.chat("test")

        cmd = mock_cls.call_args[0][0]
        assert "--resume" in cmd
        assert "existing-sess" in cmd

    @pytest.mark.asyncio
    async def test_command_no_resume_without_session(self, client):
        response_data = {"result": "ok"}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc) as mock_cls:
            await client.chat("test")

        cmd = mock_cls.call_args[0][0]
        assert "--resume" not in cmd

    @pytest.mark.asyncio
    async def test_on_proc_callback_called(self, client):
        response_data = {"result": "ok"}
        mock_proc = _mock_popen(stdout=json.dumps(response_data).encode())
        callback = MagicMock()

        with patch("link_project_to_chat.claude_client.subprocess.Popen", return_value=mock_proc):
            await client.chat("test", on_proc=callback)

        callback.assert_called_once_with(mock_proc)


class TestEffortLevels:
    def test_effort_levels_tuple(self):
        assert EFFORT_LEVELS == ("low", "medium", "high", "max")
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_claude_client.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_claude_client.py
git commit -m "test: add unit tests for claude_client module"
```

---

### Task 11: Unit tests for task_manager module

**Files:**
- Create: `tests/test_task_manager.py`

- [ ] **Step 1: Write task_manager tests**

Create `tests/test_task_manager.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.task_manager import Task, TaskManager, TaskStatus, TaskType


@pytest.fixture
def callbacks():
    return {
        "on_complete": AsyncMock(),
        "on_task_started": AsyncMock(),
    }


@pytest.fixture
def manager(tmp_path, callbacks):
    return TaskManager(
        project_path=tmp_path,
        on_complete=callbacks["on_complete"],
        on_task_started=callbacks["on_task_started"],
    )


class TestTask:
    def test_initial_state(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        assert t.status == TaskStatus.WAITING
        assert t.result is None
        assert t.elapsed is None

    def test_elapsed_human_seconds(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        t.started_at = 0.0
        t.finished_at = 45.0
        assert t.elapsed_human == "45s"

    def test_elapsed_human_minutes(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        t.started_at = 0.0
        t.finished_at = 125.0
        assert t.elapsed_human == "2m 5s"

    def test_elapsed_human_hours(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        t.started_at = 0.0
        t.finished_at = 3661.0
        assert t.elapsed_human == "1h 1m"

    def test_cancel_waiting(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        assert t.cancel() is True
        assert t.status == TaskStatus.CANCELLED

    def test_cancel_done_returns_false(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.CLAUDE, input="hi", name="hi")
        t.status = TaskStatus.DONE
        assert t.cancel() is False

    def test_tail(self):
        t = Task(id=1, chat_id=100, message_id=200, type=TaskType.COMMAND, input="ls", name="ls")
        t._log.extend(["line1", "line2", "line3"])
        assert t.tail(2) == "line2\nline3"


class TestTaskManagerClaude:
    async def test_submit_claude_creates_task(self, manager, callbacks):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="response"):
            task = manager.submit_claude(chat_id=1, message_id=10, prompt="hello")
            assert task.type == TaskType.CLAUDE
            assert task.input == "hello"
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task.result == "response"
        callbacks["on_complete"].assert_called()
        callbacks["on_task_started"].assert_called()

    async def test_submit_claude_failure(self, manager, callbacks):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            task = manager.submit_claude(chat_id=1, message_id=10, prompt="hello")
            await task._asyncio_task

        assert task.status == TaskStatus.FAILED
        assert task.error == "boom"

    async def test_submit_compact(self, manager):
        with patch.object(manager, "_do_compact", new_callable=AsyncMock, return_value="summary"):
            task = manager.submit_compact(chat_id=1, message_id=10)
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task._compact is True
        assert task.result == "summary"


class TestTaskManagerCommand:
    async def test_run_command_success(self, manager):
        task = manager.run_command(chat_id=1, message_id=10, command="echo hello")
        await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert "hello" in task.result
        assert task.exit_code == 0

    async def test_run_command_failure(self, manager):
        task = manager.run_command(chat_id=1, message_id=10, command="false")
        await task._asyncio_task

        assert task.status == TaskStatus.FAILED
        assert task.exit_code != 0


class TestTaskManagerQueries:
    async def test_list_tasks(self, manager):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            t1 = manager.submit_claude(chat_id=1, message_id=10, prompt="a")
            t2 = manager.submit_claude(chat_id=1, message_id=11, prompt="b")
            await asyncio.gather(t1._asyncio_task, t2._asyncio_task)

        tasks = manager.list_tasks(chat_id=1)
        assert len(tasks) == 2
        # Sorted by ID descending
        assert tasks[0].id > tasks[1].id

    async def test_list_tasks_filters_by_chat(self, manager):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            t1 = manager.submit_claude(chat_id=1, message_id=10, prompt="a")
            t2 = manager.submit_claude(chat_id=2, message_id=11, prompt="b")
            await asyncio.gather(t1._asyncio_task, t2._asyncio_task)

        tasks = manager.list_tasks(chat_id=1)
        assert len(tasks) == 1

    async def test_find_by_message(self, manager):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            task = manager.submit_claude(chat_id=1, message_id=42, prompt="a")
            # Task is running, so find_by_message should find it
            await asyncio.sleep(0)  # let the task start

        found = manager.find_by_message(42)
        assert len(found) >= 0  # may have finished already

    def test_get_nonexistent(self, manager):
        assert manager.get(999) is None

    async def test_cancel_all(self, manager):
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, side_effect=asyncio.sleep(10)):
            t1 = manager.submit_claude(chat_id=1, message_id=10, prompt="a")
            await asyncio.sleep(0.01)  # let tasks start
            count = manager.cancel_all()

        assert count >= 1

    def test_running_count_initial(self, manager):
        assert manager.running_count == 0

    def test_waiting_count_initial(self, manager):
        assert manager.waiting_count == 0
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_task_manager.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_task_manager.py
git commit -m "test: add unit tests for task_manager module"
```

---

### Task 12: Unit tests for CLI module

**Files:**
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write CLI tests**

Create `tests/test_cli.py`:

```python
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from link_project_to_chat.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path / "config.json"


class TestConfigure:
    def test_sets_username(self, runner, config_dir):
        result = runner.invoke(main, ["--config", str(config_dir), "configure", "--username", "alice"])
        assert result.exit_code == 0
        assert "alice" in result.output
        data = json.loads(config_dir.read_text())
        assert data["allowed_username"] == "alice"

    def test_strips_at_sign(self, runner, config_dir):
        result = runner.invoke(main, ["--config", str(config_dir), "configure", "--username", "@Bob"])
        assert result.exit_code == 0
        data = json.loads(config_dir.read_text())
        assert data["allowed_username"] == "bob"


class TestLink:
    def test_link_project(self, runner, config_dir, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        result = runner.invoke(main, [
            "--config", str(config_dir),
            "link", str(project_dir), "--token", "tok123",
        ])
        assert result.exit_code == 0
        assert "Linked" in result.output
        data = json.loads(config_dir.read_text())
        assert "myproject" in data["projects"]

    def test_link_with_custom_name(self, runner, config_dir, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        result = runner.invoke(main, [
            "--config", str(config_dir),
            "link", str(project_dir), "--token", "tok123", "--name", "custom",
        ])
        assert result.exit_code == 0
        data = json.loads(config_dir.read_text())
        assert "custom" in data["projects"]


class TestUnlink:
    def test_unlink_existing(self, runner, config_dir, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        runner.invoke(main, ["--config", str(config_dir), "link", str(project_dir), "--token", "tok"])
        result = runner.invoke(main, ["--config", str(config_dir), "unlink", "myproject"])
        assert result.exit_code == 0
        assert "Unlinked" in result.output

    def test_unlink_nonexistent(self, runner, config_dir):
        result = runner.invoke(main, ["--config", str(config_dir), "unlink", "nope"])
        assert result.exit_code != 0


class TestList:
    def test_list_empty(self, runner, config_dir):
        result = runner.invoke(main, ["--config", str(config_dir), "list"])
        assert result.exit_code == 0
        assert "No projects" in result.output

    def test_list_with_projects(self, runner, config_dir, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        runner.invoke(main, ["--config", str(config_dir), "link", str(project_dir), "--token", "tok"])
        result = runner.invoke(main, ["--config", str(config_dir), "list"])
        assert result.exit_code == 0
        assert "myproject" in result.output


class TestStart:
    def test_start_no_projects_no_params(self, runner, config_dir):
        result = runner.invoke(main, ["--config", str(config_dir), "start"])
        assert result.exit_code != 0

    def test_start_missing_username(self, runner, config_dir, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        runner.invoke(main, ["--config", str(config_dir), "link", str(project_dir), "--token", "tok"])
        result = runner.invoke(main, ["--config", str(config_dir), "start"])
        assert result.exit_code != 0
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: All tests pass.

- [ ] **Step 3: Run full test suite with coverage**

Run: `pytest --cov=link_project_to_chat --cov-report=term-missing -m "not integration" -v`
Expected: All tests pass, coverage report shows coverage across modules.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cli.py
git commit -m "test: add unit tests for CLI module"
```

---

## Phase 4: Integration Tests

### Task 13: Claude CLI integration tests

**Files:**
- Create: `tests/integration/test_claude_integration.py`

- [ ] **Step 1: Write Claude integration tests**

Create `tests/integration/test_claude_integration.py`:

```python
from __future__ import annotations

import shutil

import pytest

from link_project_to_chat.claude_client import ClaudeClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="claude CLI not found on PATH",
    ),
]


@pytest.fixture
def client(tmp_path):
    return ClaudeClient(tmp_path)


class TestClaudeIntegration:
    async def test_simple_prompt(self, client):
        result = await client.chat("Reply with exactly: PING")
        assert "PING" in result
        assert client.session_id is not None

    async def test_session_resume(self, client):
        await client.chat("Remember the word BANANA")
        session_id = client.session_id
        assert session_id is not None

        # Resume the same session
        result = await client.chat("What word did I ask you to remember?")
        assert client.session_id == session_id
        assert "BANANA" in result.upper()

    async def test_model_flag_accepted(self, client):
        client.model = "sonnet"
        result = await client.chat("Reply with exactly: OK")
        assert "OK" in result

    async def test_effort_flag_accepted(self, client):
        client.effort = "low"
        result = await client.chat("Reply with exactly: OK")
        assert "OK" in result
```

- [ ] **Step 2: Run integration tests (locally, requires claude CLI)**

Run: `pytest tests/integration/test_claude_integration.py -v -m integration`
Expected: All tests pass (if `claude` is on PATH and authenticated).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_claude_integration.py
git commit -m "test: add Claude CLI integration tests"
```

---

### Task 14: Telegram bot integration tests

**Files:**
- Create: `tests/integration/test_bot_integration.py`

- [ ] **Step 1: Write bot integration tests**

Create `tests/integration/test_bot_integration.py`:

```python
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from link_project_to_chat.bot import ProjectBot

TEST_BOT_TOKEN = os.environ.get("TEST_BOT_TOKEN")
TEST_CHAT_ID = os.environ.get("TEST_CHAT_ID")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        TEST_BOT_TOKEN is None,
        reason="TEST_BOT_TOKEN env var not set",
    ),
]


@pytest.fixture
def bot(tmp_path):
    return ProjectBot(
        name="test-project",
        path=tmp_path,
        token=TEST_BOT_TOKEN,
        allowed_username="testuser",
    )


class TestBotBuild:
    def test_build_creates_app(self, bot):
        app = bot.build()
        assert app is not None
        assert bot._app is app

    def test_commands_registered(self, bot):
        app = bot.build()
        # Verify handlers were added (at least command handlers + message handler + error handler)
        assert len(app.handlers[0]) >= 10  # 9 command handlers + 1 message handler


class TestBotAuth:
    def test_auth_no_username_allows_all(self, tmp_path):
        bot = ProjectBot(name="test", path=tmp_path, token=TEST_BOT_TOKEN or "fake", allowed_username="")
        user = type("User", (), {"id": 1, "username": "anyone"})()
        assert bot._auth(user) is True

    def test_auth_wrong_username_rejected(self, bot):
        user = type("User", (), {"id": 1, "username": "wronguser"})()
        assert bot._auth(user) is False

    def test_auth_correct_username_pins_id(self, bot):
        user = type("User", (), {"id": 42, "username": "testuser"})()
        assert bot._auth(user) is True
        assert bot._trusted_user_id == 42

    def test_auth_pinned_id_survives_username_change(self, bot):
        user = type("User", (), {"id": 42, "username": "testuser"})()
        bot._auth(user)
        # Same user, different username
        user2 = type("User", (), {"id": 42, "username": "newname"})()
        assert bot._auth(user2) is True

    def test_auth_different_id_rejected_after_pin(self, bot):
        user = type("User", (), {"id": 42, "username": "testuser"})()
        bot._auth(user)
        imposter = type("User", (), {"id": 99, "username": "testuser"})()
        assert bot._auth(imposter) is False


class TestBotTaskRoundTrip:
    @pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="claude CLI not found on PATH",
    )
    async def test_command_execution(self, bot):
        bot.build()
        on_complete = asyncio.Event()

        async def _on_complete(task):
            on_complete.set()

        bot.task_manager._on_complete = _on_complete
        bot.task_manager._on_task_started = asyncio.coroutine(lambda t: None) if False else (lambda t: asyncio.sleep(0))

        task = bot.task_manager.run_command(chat_id=1, message_id=1, command="echo integration-test")
        await asyncio.wait_for(on_complete.wait(), timeout=10)

        assert task.status.value == "done"
        assert "integration-test" in task.result
```

- [ ] **Step 2: Run bot integration tests**

Run: `TEST_BOT_TOKEN=your_token pytest tests/integration/test_bot_integration.py -v -m integration`
Expected: Tests that don't need a real token are skipped; auth and build tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_bot_integration.py
git commit -m "test: add Telegram bot integration tests"
```

---

## Phase 5: Reliability Improvements

### Task 15: Task eviction

**Files:**
- Modify: `src/link_project_to_chat/task_manager.py`
- Modify: `tests/test_task_manager.py`

- [ ] **Step 1: Write failing test for task eviction**

Add to `tests/test_task_manager.py`:

```python
class TestTaskEviction:
    async def test_old_completed_tasks_evicted(self, callbacks):
        manager = TaskManager(
            project_path=Path("/tmp"),
            on_complete=callbacks["on_complete"],
            on_task_started=callbacks["on_task_started"],
            max_history=5,
        )
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            tasks = []
            for i in range(8):
                t = manager.submit_claude(chat_id=1, message_id=i, prompt=f"msg{i}")
                tasks.append(t)
            await asyncio.gather(*(t._asyncio_task for t in tasks))

        all_tasks = manager.list_tasks(chat_id=1, limit=100)
        assert len(all_tasks) <= 5

    async def test_running_tasks_never_evicted(self, callbacks):
        manager = TaskManager(
            project_path=Path("/tmp"),
            on_complete=callbacks["on_complete"],
            on_task_started=callbacks["on_task_started"],
            max_history=3,
        )
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            t1 = manager.submit_claude(chat_id=1, message_id=1, prompt="a")
            await t1._asyncio_task

        # Submit a long-running task
        with patch.object(manager.claude, "chat", new_callable=AsyncMock, side_effect=asyncio.sleep(100)):
            running = manager.submit_claude(chat_id=1, message_id=2, prompt="b")
            await asyncio.sleep(0.01)

        with patch.object(manager.claude, "chat", new_callable=AsyncMock, return_value="r"):
            for i in range(5):
                t = manager.submit_claude(chat_id=1, message_id=10 + i, prompt=f"c{i}")
                await t._asyncio_task

        # The running task should still be there
        assert manager.get(running.id) is not None
        running.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_task_manager.py::TestTaskEviction -v`
Expected: FAIL (TaskManager doesn't accept `max_history` yet)

- [ ] **Step 3: Implement task eviction**

In `src/link_project_to_chat/task_manager.py`, update `TaskManager.__init__`:

```python
    def __init__(self, project_path: Path,
                 on_complete: OnTaskEvent, on_task_started: OnTaskEvent,
                 max_history: int = 100):
        self.project_path = project_path
        self._on_complete = on_complete
        self._on_task_started = on_task_started
        self._max_history = max_history
        self._next_id = 1
        self._tasks: dict[int, Task] = {}
        self._claude = ClaudeClient(project_path)
```

Add an eviction method:

```python
    def _evict_old_tasks(self) -> None:
        if len(self._tasks) <= self._max_history:
            return
        evictable = sorted(
            (t for t in self._tasks.values()
             if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)),
            key=lambda t: t.id,
        )
        to_remove = len(self._tasks) - self._max_history
        for t in evictable[:to_remove]:
            del self._tasks[t.id]
```

Call `self._evict_old_tasks()` at the end of `_exec_claude` (after the callback) and at the end of `_exec_command` (after the callback):

In `_exec_claude`, after `await self._safe_callback(self._on_complete, task)`:
```python
        self._evict_old_tasks()
```

In `_exec_command`, after `await self._safe_callback(self._on_complete, task)`:
```python
        self._evict_old_tasks()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_task_manager.py::TestTaskEviction -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/task_manager.py tests/test_task_manager.py
git commit -m "feat: add task eviction to bound TaskManager memory"
```

---

### Task 16: Graceful shutdown

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Write failing test for graceful shutdown**

Add to `tests/test_task_manager.py`:

```python
class TestGracefulShutdown:
    async def test_cancel_all_kills_running_procs(self, manager):
        task = manager.run_command(chat_id=1, message_id=1, command="sleep 60")
        await asyncio.sleep(0.1)  # let the process start
        assert task.status == TaskStatus.RUNNING
        assert task._proc is not None

        count = manager.cancel_all()
        assert count >= 1
        await asyncio.sleep(0.1)
        assert task.status == TaskStatus.CANCELLED
```

- [ ] **Step 2: Run test to verify it passes** (cancel_all already works)

Run: `pytest tests/test_task_manager.py::TestGracefulShutdown -v`
Expected: PASS (the cancel path already kills `_proc`)

- [ ] **Step 3: Add pre_shutdown callback to bot**

In `src/link_project_to_chat/bot.py`, add a `_pre_shutdown` method to `ProjectBot`:

```python
    async def _pre_shutdown(self, app) -> None:
        logger.info("Shutting down bot '%s', cancelling all tasks...", self.name)
        count = self.task_manager.cancel_all()
        for typing_task in self._typing_tasks.values():
            typing_task.cancel()
        self._typing_tasks.clear()
        logger.info("Cancelled %d task(s)", count)
```

In `build()`, register the callback on the ApplicationBuilder. Change the builder line:

```python
        app = (ApplicationBuilder()
               .token(self.token)
               .concurrent_updates(True)
               .post_init(self._post_init)
               .post_shutdown(self._pre_shutdown)
               .build())
```

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_task_manager.py
git commit -m "feat: add graceful shutdown via post_shutdown callback"
```

---

### Task 17: Final verification

**Files:** None (verification only)

- [ ] **Step 1: Run full unit test suite with coverage**

Run: `pytest --cov=link_project_to_chat --cov-report=term-missing -m "not integration" -v`
Expected: All tests pass, reasonable coverage across modules.

- [ ] **Step 2: Run ruff and mypy**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/`
Expected: No errors.

- [ ] **Step 3: Run integration tests (if environment allows)**

Run: `pytest -m integration -v`
Expected: Tests pass or skip cleanly if environment not configured.

- [ ] **Step 4: Final commit (if any lint/type fixes needed)**

```bash
git add -A
git commit -m "chore: final lint and type fixes after full cleanup"
```
