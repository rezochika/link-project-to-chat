# Full Cleanup Design: link-project-to-chat

**Date**: 2026-04-02
**Approach**: Bug fixes first, then dev tooling, then tests, then reliability
**Scope**: All non-feature improvements (phases 1-5 below)

---

## Phase 1: Bug Fixes

### 1a. Version mismatch

`src/link_project_to_chat/__init__.py` has `0.1.0` while `pyproject.toml` has `0.2.0`.

**Fix**: Use hatchling's dynamic versioning. Configure `[tool.hatch.version]` with `source = "code"` pointing at `__init__.py`. Update `__init__.py` to `0.2.0`. Remove the hardcoded version from `pyproject.toml` and add `version` to the `[project]` `dynamic` list.

### 1b. Inert --model and --effort flags

`claude_client.py:33-34` has `--model` and `--effort` commented out. The `/effort` command sets state that is never passed to Claude. The `/status` model field tracks the response model but never sends a preferred model.

**Fix**: Uncomment and wire through. `ClaudeClient.chat()` passes `--model self.model` and `--effort self.effort` to the subprocess command list.

### 1c. _proc single-slot race condition

`ClaudeClient._proc` is a single field. Concurrent Claude messages overwrite it, so `cancel()` and `status` only track the last-submitted process.

**Fix**: Remove `_proc`, `_started_at`, `_last_message`, `_last_duration`, and `_total_requests` tracking from `ClaudeClient`. Process tracking already exists on `Task._proc` — `_exec_claude` already sets it via the `on_proc` callback. `ClaudeClient.cancel()` is removed; cancellation goes through `Task.cancel()` which already kills `task._proc`. The `status` property on `ClaudeClient` is simplified to report only session/model/effort info. Running state and request tracking become the `TaskManager`'s responsibility — it already has `running_count` and `_tasks`. The bot's `/status` handler queries `TaskManager` for running state instead of `ClaudeClient`.

---

## Phase 2: Dev Tooling & CI

### 2a. Ruff

Add `[tool.ruff]` config to `pyproject.toml`:
- `target-version = "py311"`
- `select = ["E", "F", "I", "UP"]` (pycodestyle, pyflakes, isort, pyupgrade)
- `[tool.ruff.format]` section

Run ruff over the codebase and fix any findings.

### 2b. Mypy

Add `[tool.mypy]` to `pyproject.toml`:
- `python_version = "3.11"`
- `warn_return_any = true`
- `warn_unused_configs = true`
- `disallow_untyped_defs = true` for `src/` package
- Add type stubs or per-module `ignore` for third-party libraries as needed

Fix any type errors surfaced.

### 2c. Pytest config

Add `[tool.pytest.ini_options]` to `pyproject.toml`:
- `testpaths = ["tests"]`
- `asyncio_mode = "auto"`
- Register custom marker: `markers = ["integration: integration tests (require external services)"]`

Add dev dependencies under `[project.optional-dependencies]`:
```
dev = ["pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy"]
```

### 2d. GitHub Actions CI

Single `.github/workflows/ci.yml`:
- Trigger: push/PR to `main`
- Matrix: Python 3.11, 3.12, 3.13
- Steps: checkout, setup-python, `pip install -e .[dev]`, `ruff check`, `ruff format --check`, `mypy src/`, `pytest --cov -m "not integration"`
- Integration tests only when explicitly triggered or credentials are available

---

## Phase 3: Unit Tests

All tests in `tests/` directory, mirroring source layout.

### 3a. tests/test_config.py

Covers: `load_config`, `save_config`, `load_sessions`, `save_session`, `clear_session`, `load_trusted_user_id`, `save_trusted_user_id`, `clear_trusted_user_id`.

Uses `tmp_path` fixture. Tests round-trip serialization, missing file handling, permission bits on saved files.

### 3b. tests/test_formatting.py

Covers: `md_to_telegram`, `split_html`, `strip_html`, `_render_table`.

Pure functions, most testable module. Cases:
- Code blocks (fenced with/without language)
- Inline code
- Bold, italic, strikethrough
- Links
- Blockquotes
- Markdown tables (aligned output)
- HTML escaping
- Message splitting at 4096 boundary
- Nested formatting
- Edge cases: empty input, very long input

### 3c. tests/test_claude_client.py

Mock `subprocess.Popen` to test:
- JSON response parsing (session_id extraction, model extraction)
- Error handling (non-zero exit, empty stdout, invalid JSON, stderr output)
- Command construction (verify `--model`, `--effort`, `--resume` flags passed correctly)

### 3d. tests/test_task_manager.py

Async tests with mocked `ClaudeClient.chat`. Covers:
- Task lifecycle: waiting -> running -> done/failed/cancelled
- Concurrent task submission
- `cancel()` and `cancel_all()`
- `find_by_message`
- `list_tasks` ordering and limit
- `submit_compact` flow
- Callback invocation (`on_complete`, `on_task_started`)
- Command execution with mocked subprocess

### 3e. tests/test_cli.py

Uses Click's `CliRunner`. Covers:
- `configure` sets username
- `link` / `unlink` / `list` CRUD operations
- `start` validation errors (missing username, missing project)
- No live bot — CLI argument parsing and config side effects only

---

## Phase 4: Integration Tests

Live in `tests/integration/`, skipped by default.

### 4a. tests/integration/test_claude_integration.py

Skipped via `pytest.mark.skipif` when `claude` is not on PATH, plus `pytest.mark.integration` marker.

Tests:
- Send a simple prompt, verify valid JSON response with `session_id` and `result`
- Resume a session with `--resume`, verify continuity
- Verify `--model` and `--effort` flags are accepted without error
- Error case: malformed or edge-case inputs

### 4b. tests/integration/test_bot_integration.py

Skipped when no `TEST_BOT_TOKEN` env var is set, plus `pytest.mark.integration` marker.

Tests:
- Bot startup, webhook deletion, command registration
- `/run echo hello` produces output
- `/status` returns expected fields
- `/reset` clears session
- Auth rejection for wrong user
- Message -> Claude -> response round-trip (requires both Claude CLI and bot token)

### 4c. Pytest marker config

Custom `integration` marker registered in `pyproject.toml`. Default `pytest` invocation uses `-m "not integration"`. CI opts in with `pytest -m integration` when credentials are available.

---

## Phase 5: Reliability Improvements

### 5a. Task eviction

`TaskManager._tasks` grows unbounded.

**Fix**: Add `_max_history` parameter (default 100). After each task completion, evict oldest completed/failed/cancelled tasks when dict exceeds limit. Running and waiting tasks are never evicted.

### 5b. Graceful shutdown

**Fix**: Use `python-telegram-bot`'s `pre_shutdown` callback on `ApplicationBuilder` to call `task_manager.cancel_all()` before the event loop exits. This ensures all running tasks are cancelled and subprocesses killed cleanly.

### 5c. Claude process cleanup

On bot shutdown, ensure any in-flight `claude` subprocess is killed. Covered by 5b if `cancel_all()` properly kills `task._proc` (which it already does). Add a unit test verifying this path.
