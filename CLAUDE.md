# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. Keep in sync with [AGENTS.md](AGENTS.md); both are pointers to [docs/TODO.md](docs/TODO.md) for live status.

## Project Overview

A Python CLI that connects a local project directory to a chat platform (Telegram, or a local web UI) for chat-based interactions with an LLM agent. Two backends ship today: Claude (via the `claude` CLI, default) and Codex (via the `codex` CLI, opt-in via `/backend codex`). Features: streaming responses, background shell execution, task management, skills/personas, voice transcription, multi-project management via a manager bot, and team/group chat support.

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
`cli.py` → `ProjectBot` (bot.py) → `Transport` (telegram or web) → user messages → `TaskManager` → `AgentBackend` (backends/) → streaming response → `StreamingMessage` (transport/streaming.py)

### Key modules
- **bot.py** — Main ProjectBot handler. Zero direct Telegram imports (enforced by `tests/test_transport_lockout.py`). All platform calls go through Transport.
- **transport/** — `Transport` Protocol (base.py), `TelegramTransport` (telegram.py), `FakeTransport` (fake.py) for tests, `StreamingMessage` (streaming.py) for rate-limited edits. Telegram-specific helpers (`_telegram_relay.py`, `_telegram_group.py`) live alongside but are private.
- **web/** — `WebTransport` (transport.py), FastAPI app + SSE (app.py), SQLite store (store.py). First non-Telegram transport, shipped under spec #1.
- **backends/** — `AgentBackend` Protocol + `BaseBackend` env-policy helper (base.py), `ClaudeBackend` (claude.py) and Claude JSONL parser (claude_parser.py), `CodexBackend` (codex.py) and Codex JSONL parser (codex_parser.py), `factory.py`. The bot constructs a backend via the factory; backend extraction landed under backend phase 1, and Codex landed under phase 3 as an opt-in via `/backend codex`. Per-backend env scrub/keep allowlists run through `BaseBackend._prepare_env`. Live Codex coverage gates behind `RUN_CODEX_LIVE=1` and the `codex_live` pytest marker.
- **stream.py** — Event types: TextDelta, ThinkingDelta, ToolUse, AskQuestion, Result, Error.
- **task_manager.py** — Tracks concurrent agent tasks and shell commands with lifecycle callbacks.
- **manager/** — `ManagerBot` (ported to `TelegramTransport` under spec #0c, with a 7-name allowlist for `Update`/`ConversationHandler` family enforced by `tests/test_manager_lockout.py`). `ProcessManager` handles project-bot subprocess lifecycle. Wizard state lives in `conversation.py` above the transport layer.
- **_auth.py** — AuthMixin: username-based auth, user ID locking, brute-force protection, rate limiting.
- **skills.py** — Skill/persona loading with priority: project > global > Claude Code user > bundled.
- **config.py** — Dataclass-based config with backward-compat migrations. Files written with `0o600`. Includes transport-agnostic `BotPeerRef` and `RoomBinding` types for team routing.
- **formatting.py** — Markdown-to-Telegram HTML conversion, message chunking at ~4096 chars.

### Transport abstraction
The `Transport` Protocol decouples bot logic from Telegram. `bot.py` only communicates through transport primitives (`ChatRef`, `MessageRef`, `Identity`, `IncomingMessage`, `PromptSpec`). New transports must implement the Protocol and pass the parametrized contract test in `tests/transport/test_contract.py` (currently runs against `[fake, telegram, web]`).

### Manager & team support
- `manager/bot.py` — Multi-project orchestration bot, runs through `TelegramTransport` (spec #0c). Residual `telegram` imports are limited to `Update` + `ConversationHandler` family.
- `transport/_telegram_relay.py` — Telegram team relay; lifecycle owned by `TelegramTransport.enable_team_relay`.
- `transport/_telegram_group.py` — Telethon TL helpers used by the manager for supergroup creation/deletion.
- `group_filters.py` / `group_state.py` — Group chat filtering and persistent state. `group_state` is keyed by `ChatRef`; `group_filters` consumes `IncomingMessage.mentions` (structured mentions, not regex parsing).

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

Active branch `feat/transport-abstraction` carries the transport-abstraction track plus the first non-Telegram transport.

- Shipped: spec #0 (core, v0.13.0), #0b (voice, v0.14.0), #0a (group/team, v0.15.0), #0c (manager, v0.16.0), #1 (Web UI). Backend abstraction phases 1 (Claude extraction behind `AgentBackend`), 2 (backend-aware config + `/backend` command), 3 (Codex adapter behind `/backend codex`), and 4 (capability expansion: Codex `/model`/`/effort`/`/permissions`, `/backend` picker, provider-aware `/status`, `/context`) also shipped.
- Designed but not yet implemented: Discord (#2), Slack (#3), Google Chat (#4).

[docs/TODO.md](docs/TODO.md) is the live status source — update there first; this section is a pointer.
