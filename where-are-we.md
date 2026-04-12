# State

## Done
- Streaming responses with rate-limited edits (2s throttle)
- Inline keyboard buttons on `/tasks` (click for detail view with Cancel/Log/Back) and `/reset`
- File upload support (photos + documents) — saved to `{project}/uploads/`
- Unsupported message type replies (voice, sticker, video, location, contact, audio)
- `/help`, `/model`, `/effort`, `/permissions` commands
- Session persistence across restarts
- `/compact` for context compression
- `/reset` with confirmation dialog (stale button after restart handled gracefully)
- Image detection and auto-send on Claude tool use
- Username-based auth with trusted user_id locking (numeric ID stored globally)
- Brute-force protection: user blocked after 5 failed auth attempts
- Rate limiting: 30 messages/minute per user
- Bot refuses to start without a configured username
- Fail-closed auth: empty username denies all access
- httpx logs suppressed to prevent Telegram token leaking
- Permission configuration: `--permission-mode`, `--allowed-tools`, `--disallowed-tools`, `--dangerously-skip-permissions`
- Startup message sent to trusted user on bot start
- Shared `AuthMixin` for auth + rate limiting (no duplication between bot and manager)
- **Manager subpackage** (`link_project_to_chat.manager`):
  - `ProcessManager` — starts/stops/monitors project bots as subprocesses with log capture
  - `ManagerBot` — Telegram bot to control all projects from one chat
  - State persistence: restores running projects after manager restart
  - Config at `~/.link-project-to-chat/manager/config.json`
  - Button-based UI: `/projects` lists all with per-project Start/Stop/Logs/Edit/Remove buttons
  - Inline edit flow for project fields (no ConversationHandler — plain user_data + MessageHandler)
- File permissions: `0o600` on all sensitive files (config, sessions, manager state)
- Per-project `username` and `trusted_user_id` in config — projects with own username get isolated auth
- `trusted_user_id` stored in `config.json` (not a separate file)
- Per-project trusted_user_id only falls back to global when project has no own username
- **CLI restructured** (v0.5.0):
  - `projects` subgroup: `list`, `add`, `remove`, `edit`
  - `configure [--username USER] [--manager-token TOKEN]` — merged, no wizard prompts
  - `start`, `start-manager` unchanged
  - All old flat commands removed (`link`, `unlink`, `list`, `add-project`, `remove-project`, `edit-project`, `configure-manager`)
  - `projects add`: `--name`, `--path`, `--token` required; optional `--username`, `--model`, `--permission-mode`, `--dangerously-skip-permissions`
- **Per-project config fields** (v0.6.0): `model`, `permission_mode`, `dangerously_skip_permissions` in `ProjectConfig` — `start --project NAME` uses them as fallbacks when CLI flags are absent
- **Refactors** (v0.6.0–v0.7.0):
  - `formatting.py`: extracted `_split_pre_block` and `_merge_segments`; fixed hard-slice for oversized plain-text segments
  - `task_manager.py`: extracted `_submit` helper used by `submit_claude` and `submit_compact`
  - `bot.py`: extracted `_CMD_HELP`, `_parse_task_id`, `_send_html`, `_send_stream_result`; split `_on_task_complete` into `_finalize_claude_task` and `_finalize_command_task`
  - `manager/config.py`: extracted `_load_json` helper; `resolve_flags` uses `vars(defaults)` to auto-include all `PermissionDefaults` fields

## Coding Style
- Single-purpose functions
- Functions over 100 lines must be split
- Avoid nesting — extract helpers instead
- Fail early: no defensive error handling for internal code, only at system boundaries (user input, external APIs)
- No magic patterns
- Distinct, named inputs and outputs
- No duplicate logic — extract shared helpers
- No over-engineering: minimum complexity for the current task

## Pending
- File uploads stored permanently in project dir — consider `/tmp/{project_name}/` for temp files
- Manager bot `/add_project` wizard allows skipping token — inconsistent with CLI requirement

## Resolved (v0.10.0)
- Stream state (`_stream_text`) now cleaned up on cancel via `_on_task_complete`
- `_proc` on `ClaudeClient` now has concurrency guard (raises RuntimeError)
- `chmod 0o600` applied consistently via `_patch_json()` for all config writes
- Open file handles in `_send_image` — already properly closed via context manager
- Protocol interfaces added (ProcessRunner, TelegramUser, OnTaskEvent)
- Dependency injection: ClaudeClient, TaskManager, ProjectBot all accept dependencies
- mypy type checking and ruff linting configured
- py.typed marker for PEP 561 compliance
