# PM Review — Batch 1 (2026-04-22)

Reviewed files: `botfather.py`, `task_manager.py`, `tests/test_security.py`, commit `4b4c08d`.

---

## Committed work

### `4b4c08d` — Early placeholder + reply_to retry
**Status: APPROVED**
Correct regression fix for the no-traffic-until-finalize problem. Placeholder sent immediately on task start so relay auto-delete fires before the 60s fallback. `BadRequest` retry on `_send_html` / plain fallback is a sound defensive move. Tests cover 4 paths. No concerns.

---

## Uncommitted work (ready to commit)

### H4 — `botfather.py`: chmod race fix
**Status: APPROVED**
```python
if not self._session_path.exists():
    self._session_path.touch(mode=0o600)
else:
    self._session_path.chmod(0o600)
await client.start(...)
```
Correct. Both branches (new file, existing file) secure permissions before Telethon writes session data. Race window closed.

### C1 — `task_manager.py`: concurrent `/run` cap
**Status: APPROVED**
`_active_run_pids` set with `_MAX_CONCURRENT_RUNS = 3`. PID added before loop, discarded in both cancel and normal completion paths. Early-return with clear error message on cap exceeded. Correct.

### H1 — `task_manager.py`: error message scrubbing
**Status: APPROVED with minor note**
`_SENSITIVE_RE` catches 40+ char alphanumeric tokens and `/home|/root|/Users` paths. Reasonable coverage. Note: doesn't catch short bearer tokens with dashes (e.g. `ghp_xxx`) — acceptable for now; can be extended later.

### H5/H6/H1 — `tests/test_security.py`
**Status: APPROVED with one fix required**

The sibling-dir prefix bypass test (line 113) is marked `@pytest.mark.xfail` without `strict=True`. Once H2 is fixed this test will pass silently as `xpass` and provide no regression protection. **Change it to `xfail(strict=True, reason="H2 not yet fixed...")`** to match the H6 test pattern.

---

## Still pending (Batch 1 remainder)

| ID | Item | Status |
|----|------|--------|
| H2 | `bot.py:1562` — `str.startswith` → `Path.is_relative_to` | **Not done** — xfail test documents it |
| H3 | `claude_client.py:254` — env var scrubbing | **Not done** — xfail test documents it |

Both require PM confirmation before editing. See `docs/2026-04-22-remediation-plan.md` for exact fix specs.

---

## Commit 3710342 — H2 + H3 (reviewed 2026-04-22)

### H2 — `bot.py:1636`
**Status: APPROVED**
Single-line swap to `resolved.is_relative_to(self.path.resolve())`. Sibling-dir bypass closed. Test promotes from xfail to full assertion.

### H3 — `claude_client.py:255–261`
**Status: APPROVED with minor note**
fnmatch scrub loop removes all matching keys before subprocess launch. Patterns correct per spec.
Note: `_SCRUB_PATTERNS` tuple defined inside function body (recreated on every call). Low overhead — acceptable. Can be moved to module level in a cleanup pass.
`ANTHROPIC_API_KEY` risk checked: not referenced in this project; Claude CLI authenticates via `~/.claude/` config, not env. Pattern safe.

### Tests
**Status: APPROVED**
Both xfail decorators removed. 485 passed, 0 failures.

---

**Batch 1: FULLY APPROVED.** All items C1, H1, H2, H3, H4, H5, H6 resolved across commits 79a4e53 and 3710342.

_Reviewer: @lptc_mgr_claude_bot | 2026-04-22_
