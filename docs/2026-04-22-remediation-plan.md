# Remediation Plan — 2026-04-22

Source: `docs/issues-2026-04-22.md`
Status: awaiting implementation

---

## Batch 1 — Critical + High (security, implement first)

### C1 — `/run` resource exhaustion
- **File:** `task_manager.py:329–336`
- **Fix:** Cap concurrent shell subprocesses to 3. Track child PIDs in a set; reject new `/run` if cap reached. Do NOT remove the feature.

### H1 — Unsanitized error messages
- **File:** `task_manager.py:241`
- **Fix:** Before raising `RuntimeError(event.message)`, scrub the message for patterns matching API keys (40+ char alphanumeric), file paths (`/home/…`, `/root/…`), and tokens. Replace matches with `[REDACTED]`.

### H2 — Path traversal (`str.startswith`)
- **File:** `bot.py:1562`
- **Fix:** Replace `str(resolved).startswith(str(self.path.resolve()))` with `resolved.is_relative_to(self.path.resolve())`. **Confirm with PM before editing `bot.py`.**

### H3 — Env var leakage to Claude subprocess
- **File:** `claude_client.py:254–256`
- **Fix:** After `env = os.environ.copy()`, scrub keys matching any of: `*_TOKEN`, `*_KEY`, `*_SECRET`, `AWS_*`, `OPENAI_*`, `GITHUB_*`, `DATABASE_*`, `PASSWORD*`. Use `fnmatch` for pattern matching. **Confirm with PM before editing `claude_client.py`.**

### H4 — Session file chmod race
- **File:** `botfather.py:110`
- **Fix:** Move `chmod(0o600)` to before `client.start(...)` populates the session file. If the file doesn't exist yet, create it empty with correct permissions first, then let Telethon write into it.

### H5 — Missing path traversal tests
- **File:** `tests/`
- **Fix:** Add unit tests for `_send_image` path validation. Test cases: `../outside`, symlink pointing outside project dir, exact project dir boundary (should pass).

### H6 — Missing env var scrubbing tests
- **File:** `tests/`
- **Fix:** Add unit test: mock `os.environ` with `AWS_ACCESS_KEY_ID`, `GITHUB_TOKEN`, `OPENAI_API_KEY` set, call the subprocess env-building code, assert none of those keys are present in the resulting env dict.

---

## Batch 2 — Medium (after Batch 1 reviewed and approved)

| ID | File | Line | Fix |
|----|------|------|-----|
| M1 | `bot.py` | 564–570 | Wrap `find_by_message` + cancel in a single lock or use `asyncio.shield` to make the cancel atomic. Confirm with PM before editing. |
| M2 | `claude_client.py` | 234–241 | Refactor `chat()` to raise a typed exception (e.g. `ClaudeStreamError`) instead of returning `"Error:…"` strings. Update callers. Confirm with PM before editing. |
| M4 | `config.py` | 227 | Low priority; document the predictable lock path in a comment. No code change required unless shared-fs deployment is planned. |
| M5 | `config.py` | 293–348 | Replace O(n) per-project loop with a dict-keyed update. |
| M6 | `task_manager.py` | 400–404 | Use `heapq.nlargest(limit, tasks, key=lambda t: t.id)` instead of full sort. |
| M8 | `bot.py` | 1392 | Replace `/tmp/link-project-to-chat` with `Path(tempfile.gettempdir()) / "link-project-to-chat"`. Confirm with PM before editing. |
| M10 | `tests/` | — | Add auth tests: concurrent attempts, rate-limit boundary at exactly 30 msg/min, multi-user mode field precedence. |
| M11 | `tests/` | — | Add config I/O tests: malformed JSON, permission errors, concurrent access. |
| M12 | `tests/` | — | Add `livestream._rotate_once` boundary tests: overflow, HTML render failure, iteration cap. |
| M13 | `src/` + `docs/` | — | Add docstring to `_auth()` explaining username/ID locking and multi-user field precedence. Add `docs/auth-migration.md` for single→multi-user transition guide. |

---

## Batch 3 — Low (after Batch 2)

| ID | File | Line | Fix |
|----|------|------|-----|
| L1 | `config.py` | 180–182 | Replace `print(..., file=sys.stderr)` with `logger.warning(...)`. |
| L2 | `livestream.py` | 150–161 | Add fallback: if all 5 iterations fail to fit, hard-truncate at `_DEFAULT_MAX_CHARS` with a `…` suffix. |
| L3 | `group_state.py` | 29–30 | Add LRU eviction (max 500 entries) to `GroupStateRegistry._states`. |
| L4 | `livestream.py` | 18–20 | Move magic numbers to config or at least to named constants with comments explaining their purpose. |
| L5 | `_auth.py` | 73 | Change `(user.username or "").lower()` to `(user.username or "").strip().lower()`. |
| L6 | `task_manager.py` | 304–311 | Add a one-line comment explaining what `COMPACT_PROMPT` is used for. |
| L7 | `docs/` | — | Add `CHANGELOG.md` with auth refactor entry (single-user → multi-user). |

---

## Review gates

- Batch 1: PM reads every modified file before approving. `bot.py` and `claude_client.py` edits require PM confirmation before implementation.
- Batch 2: Same protocol; M1/M2/M8 need PM confirmation.
- Batch 3: Dev can proceed autonomously; ping PM when done.

---

_PM: @lptc_mgr_claude_bot | Dev: @lptc_dev_claude_bot_
