# Backend Phase 1 — Smoke Test Evidence

_Populated by @lptc_dev_claude_bot on Phase 1 completion. PM (@lptc_mgr_claude_bot) reviews before sign-off._

---

## Test environment

- **Branch:** `feat/transport-abstraction`
- **Commit:** _(final Phase 1 commit hash)_
- **Tested mode:** _one of:_
  - `PYTHONPATH` override against working tree (editable-install bypass), OR
  - Post-merge install against live service (after `feat/transport-abstraction` merges to `main`)
- **Claude CLI version:** _(output of `claude --version`)_

## Round-trip 1 — Streaming prompt

Prompt: `say hello in one word`

- [ ] Bot streams partial response (at least one mid-generation edit visible)
- [ ] Final message finalizes cleanly (no `RuntimeError`)
- [ ] Typing indicator stops after finalization
- [ ] `/tasks` shows task as `COMPLETED`
- [ ] No tracebacks in bot logs

**Bot log excerpt:**
```
(paste relevant log lines)
```

## Round-trip 2 — `/run echo hi`

- [ ] Command executes
- [ ] Output `hi` returned to chat
- [ ] Task status `COMPLETED`
- [ ] No regressions from `TaskType.CLAUDE` → `TaskType.AGENT` rename

**Bot log excerpt:**
```
(paste relevant log lines)
```

## Round-trip 3 — `/compact`

- [ ] Session summary prompt runs
- [ ] Resume semantics intact (new session id assigned)
- [ ] No regressions in Claude `--resume` handling

**Bot log excerpt:**
```
(paste relevant log lines)
```

## Pass condition

All three round-trips succeed; no tracebacks in logs; task statuses correct.

- [ ] **PASS** — Phase 1 ready to merge
- [ ] **FAIL** — issues listed below; Phase 1 not closed

## Issues observed (if any)

_List any anomalies, even non-blocking ones, for PM triage._
