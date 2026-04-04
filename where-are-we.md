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

## Pending
- Stream state (`_stream_messages`, `_stream_text`) not cleaned up on cancel
- Open file handles in `_send_image` (`open(path, "rb")` passed directly without closing)
- File uploads stored permanently in project dir — consider `/tmp/{project_name}/` for temp files
- `_proc` on `ClaudeClient` is a single slot — concurrent Claude tasks could overwrite it
- `chmod 0o600` missing from `clear_session()` write path
- No `chmod 0o600` on `save_trusted_user_id()` in main config.py
- Manager bot `/add_project` wizard allows skipping token — inconsistent with CLI requirement
