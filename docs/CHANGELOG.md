# Changelog

## Unreleased

### Security
- **C1** — Cap concurrent `/run` subprocesses at 3; excess commands fail immediately with a user-visible error (`task_manager.py`)
- **H1** — Scrub API keys (40+ char tokens) and home/root paths from stream Error messages before raising (`task_manager.py`)
- **H2** — Replace `str.startswith` path traversal check with `Path.is_relative_to`; closes sibling-dir prefix bypass (`bot.py`)
- **H3** — Strip sensitive env vars (`*_TOKEN`, `*_KEY`, `*_SECRET`, `AWS_*`, `OPENAI_*`, `GITHUB_*`, `DATABASE_*`, `PASSWORD*`) before passing environment to Claude subprocess (`claude_client.py`)
- **H4** — Move `chmod(0o600)` to before `client.start()` on Telethon session file; eliminates race window where credentials were world-readable (`botfather.py`)
- **H5/H6** — Add security regression tests for path traversal (`_send_image`) and env var scrubbing (`tests/test_security.py`)

### Fixed
- **Team relay** — Disable per-delta livestreaming for team bots; send single finalized message to avoid partial-message relay (`bot.py`, `team_relay.py`)
- **Team relay** — Coalesce split messages (Telegram 4096-char fragmentation) using `(sender, reply_to_msg_id)` buffer with 3s window (`team_relay.py`)
- **Team relay** — Early placeholder on task start so relay auto-delete fires before 60s fallback; retry without `reply_to` on `BadRequest` (`bot.py`)
- **M1** — Swap cancel order: `task_manager.cancel()` (sync) before `await _cancel_live_for()`; closes race window in superseded-task handling (`bot.py`)
- **M2** — `ClaudeStreamError` exception replaces `"Error:"` string returns from `chat()`; callers updated (`claude_client.py`, `bot.py`)
- **M8** — Replace hardcoded `/tmp/link-project-to-chat` uploads dir with `tempfile.gettempdir()` for portability (`bot.py`)
- **L1** — Replace `print(..., file=sys.stderr)` with `logger.warning()` in config loader (`config.py`)
- **L2** — Hard-truncate with `…` when HTML binary-search exhausts 5 iterations (`livestream.py`)
- **L3** — LRU eviction (max 500 entries) on `GroupStateRegistry` to prevent unbounded memory growth (`group_state.py`)
- **L5** — Add `.strip()` to username comparison to prevent whitespace-bypass of allowlist (`_auth.py`)

### Improved
- **M5** — Extract `_merge_project_entry` helper; replace O(n) mutation loop with dict comprehension in config save (`config.py`)
- **M6** — Replace full sort in `list_tasks` with `heapq.nlargest` for O(n log k) performance (`task_manager.py`)
- **M13** — Add docstring to `_auth()` explaining fail-closed behaviour, brute-force lockout, trusted-ID fast path, and multi-user field precedence (`_auth.py`)
- **L4** — Add explanatory comments on `_DEFAULT_THROTTLE`, `_DEFAULT_MAX_CHARS`, `_MAX_THROTTLE` constants (`livestream.py`)
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
