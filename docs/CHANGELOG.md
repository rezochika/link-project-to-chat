# Changelog

## 1.1.0 — 2026-05-16

### Added
- **`ProjectConfig.respond_in_groups`** (default False). When True, a
  standard project bot responds in Telegram groups to `@bot_username`
  mentions and replies to its own prior messages; ignores everything
  else. CLI surface: `projects add --respond-in-groups`,
  `projects edit NAME respond_in_groups true|false`. Manager bot
  exposes the field via the project-edit keyboard. The PTB filter is
  set once at startup, so flipping the flag requires a bot restart.
  Restores the GitLab fork's solo-bot-in-group behavior that was
  scoped out during the transport-abstraction track.
- **`TelegramTransport.attach_telegram_routing(..., respond_in_groups=False)`**
  kwarg. 3-way chat-type filter: team-mode (GROUPS only),
  solo+respond_in_groups (PRIVATE | GROUPS), default (PRIVATE only).
- **`ProjectBot._strip_self_mention`** helper. Pure function;
  word-bounded case-insensitive `@bot_username` removal via regex.

### Notes
- Default off — pre-v1.1.0 deployments behave identically until the
  flag is flipped.
- Peer-bot loop defense: solo project bots in groups ignore all other
  bots' messages, including `@<bot_username>` from a peer bot.
- Plugins still only see addressed-at-me group messages (consistent
  with the existing "plugins see authorized + rate-limit-passing
  messages" pattern). Differs from the GitLab fork; future opt-in
  `Plugin.observe_unfiltered_group_messages` could change this.

## 1.0.0 — 2026-05-14

### BREAKING CHANGES
- **`config.json` schema:** `allowed_usernames`, `trusted_users`, and
  `trusted_user_ids` are removed. The new `allowed_users` field is the sole
  auth source. Legacy fields are read once on load (synthesized into
  `AllowedUser{role="executor"}` entries), then stripped on next save.
  Operators upgrading need to verify the migrated `allowed_users` list before
  exposing the bot to traffic. An empty `allowed_users` list now fails closed
  (every request denied) — pre-v1.0 ambiguous behavior is gone.

### Added
- **Plugin framework** (`plugin.py`) with `Plugin` base, `PluginContext`,
  `BotCommand`, entry-point discovery via `lptc.plugins`. Hooks: `start`,
  `stop`, `on_message`, `on_button`, `on_task_complete`, `on_tool_use`,
  `get_context`. Plugins are transport-portable: same plugin runs on
  Telegram, Web, and future Discord/Slack/Google Chat transports.
- **`Transport.on_stop(callback)`** Protocol method, fired during shutdown
  before the platform tears down. Implemented in TelegramTransport,
  FakeTransport, and WebTransport.
- **`plugin-call <project> <plugin_name> <tool_name> <args_json>`** CLI
  subcommand for invoking plugin tools (used by Claude via Bash).
- **Plugin toggle UI** in the manager bot (per-project, restart-required).
- **`AllowedUser` role model** (`viewer` / `executor`) is the sole auth +
  authority source. Per-user `locked_identities` list — first contact from each transport appends a new entry;
  subsequent requests validate by native ID, not username.
- **CLI flags** `--add-user USER[:ROLE]`, `--remove-user USER`,
  `--reset-user-identity USER` on `configure`. Legacy `--username` /
  `--remove-username` aliased for this release; removed in 1.1.
- **Manager bot user-management commands** `/promote_user`, `/demote_user`,
  `/reset_user_identity` added; `/add_user` accepts an optional `[viewer|executor]`
  role argument (defaults to `executor`).
- **Operational scripts** `scripts/restart.sh` and `scripts/stop.sh` for the
  manager process.

### Notes
- The plugin framework is in this repo; specific plugins (e.g.,
  `in-app-web-server`, `diff-reviewer`) live in a separate
  `link-project-to-chat-plugins` package.
- `get_context()` is Claude-only by design; Codex/Gemini turns ignore it.

## Unreleased

### Added
- **Backend abstraction phase 4 — capability expansion & hardening** (commits `93f8b9c`..`10c383d`, follow-ups through TODO hardening commit `b396b1e`) — completes the evidence-backed Claude + Codex polish pass. Codex now supports `/model`, `/effort`, and `/permissions` through validated CLI flags; `/backend` renders a button picker; `/status` reports friendly model labels, effort, permissions, Claude allowed/disallowed tools, request count, last duration, Codex token usage, Claude usage-cap state, and last backend error; per-chat `/context` history keeps conversational turns visible across backend switches.
- **Backend abstraction phase 3 — Codex adapter** (commits `da86be3`..`0886f6a`) — opt-in `codex` backend via `/backend codex`. Adds `BaseBackend` shared env-policy helper (`backends/base.py`), `CodexBackend` (`backends/codex.py`) shelling to `codex exec --json` / `codex exec resume --json`, and `codex_parser.py` translating Codex JSONL to shared stream events. Initial conservative capabilities were `supports_resume=True`, all others `False`, `models=()`; Phase 4 later promoted model, effort, and permission support after CLI validation. Per-backend env policy (Codex keeps `OPENAI_*`/`CODEX_*`, Claude scrubs them). `bot.py` guards Claude-only command paths (`/effort`, `/skills`, `_refresh_team_system_note`, several button callbacks) when Codex is active. Live integration tests behind `RUN_CODEX_LIVE=1` + `codex_live` pytest marker. 6 commits, 826 unit tests + 2 live tests pass.
- **Backend abstraction phase 2 — Backend-aware config + `/backend`** (commits `4828120`..`552df09`, follow-ups `45069fd`) — `ProjectConfig` and `TeamBotConfig` gain `backend: str` + `backend_state: dict[str, dict]`; `Config.default_backend` and `default_model_claude`. `load_config` migrates legacy flat fields (`model`, `effort`, `permissions`, `session_id`, `show_thinking`) into `backend_state["claude"]`; `save_config` dual-writes the new shape and mirrored legacy fields for one-release downgrade safety. Three new public helpers: `patch_backend_state`, `patch_team_bot_backend_state`, `patch_team_bot_backend`. `load_session`/`save_session`/`clear_session` rewritten for `backend_state[<active>]`. New `/backend` command (show + activate-first switch with live-task rejection); `/thinking`/`/permissions`/`/compact`/`/model` capability-gated. `model_display` promoted to the `AgentBackend` Protocol. Manager bot, CLI, and process-launch read backend-aware model defaults. Telegram-awareness preamble parameterized by capabilities. 6 commits + 1 follow-up, 75 new tests (config-migration, backend-command, capability-gating, manager-backend, naming-lockout).
- **Spec #1 — Web UI Transport** (commits `6c12b39`..`d24ef52`) — first non-Telegram transport. FastAPI + HTMX + SSE + SQLite. New `WebTransport` implements the full Protocol; new `web/store.py`, `web/app.py`, Jinja2 templates, browser composer + live-update timeline. 11 commits, 768 tests pass.
- **Spec #1 review-fix** (commits `77abcff`..`7b73b8d`) — closes 5 findings from external review:
  - **P1.1** `--transport [telegram|web]` and `--port` CLI flags; `ProjectBot.__init__` accepts `transport_kind`/`web_port`; `build()` branches accordingly. The smoke command `link-project-to-chat start --project NAME --transport web --port 8080` now actually runs.
  - **P1.2** Browser identity carries username through the auth gate. `WebTransport._dispatch_event` reads `sender_handle` from payload; `post_message` accepts optional `username` form field; `chat.html` renders a username input persisted via `localStorage`. Without this, every browser message was silently dropped by `_auth_identity`.
  - **P1.3** `pytest.importorskip` at module scope on every web test file; `tests/transport/test_contract.py` defers `WebTransport` import into the `web` fixture branch. Core-only installs now collect cleanly without `[web]` extras.
  - **P1.4** Multipart upload via `/chat/{id}/message`: `post_message` accepts `UploadFile`; saves to per-upload tempdir under `lp2c-web-*`; threads `files` array; `_dispatch_event` constructs `IncomingFile` list with `try/finally` cleanup. Composer template gains `enctype="multipart/form-data"` and a file input.
  - **P2** SSE notify moved from `post_message` → `_dispatch_event` *after* `save_message`. Eliminates the race where the user's just-posted message could be missing from an immediate `/messages` refresh.
- **Structured mentions** — `IncomingMessage.mentions: list[Identity]` field (`transport/base.py`); `group_filters.mentions_bot` prefers structured over regex; new `mentions_bot_by_id`. Foundational for non-Telegram transports.
- **Prompt primitives** — `PromptKind`, `PromptOption`, `PromptSpec`, `PromptRef`, `PromptSubmission`, `PromptHandler` types; 4 new Protocol methods (`open_prompt`, `update_prompt`, `close_prompt`, `on_prompt_submit`). Wizard state above transport.
- **Conversation sessions** — `ConversationSession` + `ConversationStore` in `manager/conversation.py` (transport-agnostic wizard state).
- **Transport-agnostic config types** — `BotPeerRef`, `RoomBinding` dataclasses in `config.py` with backward-compat synthesis from legacy `group_chat_id` / `bot_username` at load time.
- **A1 (closed)** — `_trusted_users` persistence accepts non-numeric ids; `_coerce_user_id` helper drops `int()` from `bind_trusted_user`/`bind_project_trusted_user` (closes A1 from spec0-followups).
- **Contract tests parametrized over `[fake, telegram, web]`** — 3 new contract tests (mentions, prompt open, prompt submit); existing PR #6 contracts (`set_authorizer`, `run`, `max_text_length`) verified across all 3 transports.

### Security
- **Web UI hardening** — Browser form usernames are display-only; Web identity now comes from server-issued session IDs plus configured server-side handles, POST routes require CSRF tokens, streamed/model text is escaped in templates, and Web buttons submit through a CSRF-protected route.
- **C1** — Cap concurrent `/run` subprocesses at 3; excess commands fail immediately with a user-visible error (`task_manager.py`)
- **H1** — Scrub API keys (40+ char tokens) and home/root paths from stream Error messages before raising (`task_manager.py`)
- **H2** — Replace `str.startswith` path traversal check with `Path.is_relative_to`; closes sibling-dir prefix bypass (`bot.py`)
- **H3** — Strip sensitive env vars (`*_TOKEN`, `*_KEY`, `*_SECRET`, `AWS_*`, `OPENAI_*`, `GITHUB_*`, `DATABASE_*`, `PASSWORD*`) before passing environment to Claude subprocess (`claude_client.py`)
- **H4** — Move `chmod(0o600)` to before `client.start()` on Telethon session file; eliminates race window where credentials were world-readable (`botfather.py`)
- **H5/H6** — Add security regression tests for path traversal (`_send_image`) and env var scrubbing (`tests/test_security.py`)

### Fixed
- **TODO hardening batch** (`b396b1e`) — Close Phase 4 audit follow-ups: full streamed assistant replies are logged after finalization, conversation-log SQLite work moves off the event loop with bounded history blocks, Codex validates permission modes and reaps early-closed subprocesses, backend switches are serialized with new task submissions, task completion persists sessions under the backend captured at submit time, and non-Telegram team `RoomBinding` values survive load/save/restart. Verified with `pytest -q` (`992 passed, 5 skipped`), `git diff --check`, and `python3 -m compileall -q src/link_project_to_chat`.
- **Web transport** — Persist/render button metadata, dispatch browser button clicks, release uvicorn listener cleanly on `stop()`, and add a ProjectBot + WebTransport auth/command integration test.
- **Manager setup** — `/add_project` no longer accepts `/skip` or a blank Telegram bot token.
- **Team relay** — Disable per-delta livestreaming for team bots; send single finalized message to avoid partial-message relay (`bot.py`, `team_relay.py`)
- **Team relay** — Coalesce split messages (Telegram 4096-char fragmentation) using `(sender, reply_to_msg_id)` buffer with 3s window (`team_relay.py`)
- **Team relay** — Early placeholder on task start so relay auto-delete fires before 60s fallback; retry without `reply_to` on `BadRequest` (`bot.py`)
- **M1** — Swap cancel order: `task_manager.cancel()` (sync) before `await _cancel_live_for()`; closes race window in superseded-task handling (`bot.py`)
- **M2** — `ClaudeStreamError` exception replaces `"Error:"` string returns from `chat()`; callers updated (`claude_client.py`, `bot.py`)
- **M8** — Replace hardcoded `/tmp/link-project-to-chat` uploads dir with `tempfile.gettempdir()` for portability (`bot.py`)
- **L1** — Replace `print(..., file=sys.stderr)` with `logger.warning()` in config loader (`config.py`)
- **L2** — Hard-truncate with `…` when HTML binary-search exhausts 5 iterations (`transport/streaming.py`)
- **L3** — LRU eviction (max 500 entries) on `GroupStateRegistry` to prevent unbounded memory growth (`group_state.py`)
- **L5** — Add `.strip()` to username comparison to prevent whitespace-bypass of allowlist (`_auth.py`)

### Improved
- **M5** — Extract `_merge_project_entry` helper; replace O(n) mutation loop with dict comprehension in config save (`config.py`)
- **M6** — Replace full sort in `list_tasks` with `heapq.nlargest` for O(n log k) performance (`task_manager.py`)
- **M13** — Add docstring to `_auth()` explaining fail-closed behaviour, brute-force lockout, trusted-ID fast path, and multi-user field precedence (`_auth.py`)
- **L4** — Add explanatory comments on `_DEFAULT_THROTTLE`, `_DEFAULT_MAX_CHARS`, `_MAX_THROTTLE` constants (`transport/streaming.py`)
- **L6** — Add one-line comment on `COMPACT_PROMPT` explaining its role in `/compact` flow (`task_manager.py`)

### Auth system migration note
The auth system was refactored from single-user to multi-user mode. Configuration field changes:

| Old field | New field | Notes |
|---|---|---|
| `allowed_username` (string) | `allowed_usernames` (list) | Legacy single-value field still accepted on load; written as list |
| `trusted_user_id` (int) | `trusted_user_ids` (list) | Legacy single-value field still accepted on load; written as list |
| `permission_mode` | `permissions` | Enum replaced by string list |
| `dangerously_skip_permissions` | removed | Replaced by `permissions` list |

If upgrading from a pre-multi-user config, the loader handles the field migration automatically on first save.
