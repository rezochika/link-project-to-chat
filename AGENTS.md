# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

A Python CLI that connects a local project directory to a Telegram bot for chat-based interactions with Codex. Features: streaming Codex responses, background shell execution, task management, skills/personas, voice transcription, multi-project management via a manager bot, and team/group chat support.

## Commands

```bash
# Install (editable dev mode)
pip install -e "."            # core only
pip install -e ".[all]"       # all optional deps (httpx, telethon, openai)

# Run tests
pytest                        # all tests (async auto-mode)
pytest tests/test_bot_streaming.py  # single file
pytest tests/transport/       # transport tests only

# Run the bot
link-project-to-chat start --project NAME
link-project-to-chat start-manager
```

No Makefile, tox, or linter configured. Build system is hatchling. Entry point: `link_project_to_chat.cli:main`.

## Architecture

### Core flow
`cli.py` → `ProjectBot` (bot.py) → `TelegramTransport` (transport/telegram.py) → user messages → `TaskManager` → `ClaudeClient` → streaming response → `StreamingMessage` (transport/streaming.py)

### Key modules
- **bot.py** — Main ProjectBot handler. Zero direct Telegram imports (enforced by `tests/test_transport_lockout.py`). All platform calls go through Transport.
- **transport/** — `Transport` Protocol (base.py), `TelegramTransport` (telegram.py), `FakeTransport` (fake.py) for tests, `StreamingMessage` (streaming.py) for rate-limited edits.
- **claude_client.py** — Wraps `Codex` CLI as subprocess, reads streaming events from stderr.
- **stream.py** — Event types: TextDelta, ThinkingDelta, ToolUse, AskQuestion, Result, Error.
- **task_manager.py** — Tracks concurrent Codex tasks and shell commands with lifecycle callbacks.
- **manager/** — ManagerBot controls multiple project bots as subprocesses. ProcessManager handles lifecycle.
- **_auth.py** — AuthMixin: username-based auth, user ID locking, brute-force protection, rate limiting.
- **skills.py** — Skill/persona loading with priority: project > global > Codex user > bundled.
- **config.py** — Dataclass-based config with backward-compat migrations. Files written with `0o600`.
- **formatting.py** — Markdown-to-Telegram HTML conversion, message chunking at ~4096 chars.

### Transport abstraction
The `Transport` Protocol decouples bot logic from Telegram. `bot.py` only communicates through transport primitives (`ChatRef`, `MessageRef`, `Identity`, `IncomingMessage`). New transports must implement the Protocol and pass the parametrized contract test in `tests/transport/test_contract.py`.

### Manager & team support
- `manager/bot.py` — Multi-project orchestration bot (still Telegram-specific, pending port)
- `manager/team_relay.py` — Syncs messages between group chat and project bot DMs
- `group_filters.py` / `group_state.py` — Group chat filtering and persistent state

## Coding Conventions

- Single-purpose functions; split anything over ~100 lines
- Avoid nesting — extract helpers
- Fail early; no defensive error handling for internal code, only at system boundaries
- No duplicate logic — extract shared helpers
- Minimum complexity for the current task; no over-engineering
- All async I/O; tests use `asyncio_mode = "auto"`
- `FakeTransport` for testing bot logic without network calls

## Testing

- pytest with `asyncio_mode = "auto"` — no need to mark individual async tests
- Transport contract test (`tests/transport/test_contract.py`) parametrized across all Transport implementations
- Import lockout test (`tests/test_transport_lockout.py`) ensures bot.py has zero telegram imports
- Test doubles: `FakeTransport`, `FakeBot` (inline in test files)

## Current Development

Active branch `feat/transport-abstraction` is porting remaining Telegram-specific code to the Transport Protocol. Completed: core bot (spec #0), voice (spec #0b). Pending: group/team (spec #0a), manager (spec #0c).
