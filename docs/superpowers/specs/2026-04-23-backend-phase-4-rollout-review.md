# Backend Phase 4 Rollout Review

**Captured:** 2026-04-26 at HEAD `9280aef`. Phase 3 (Codex adapter, opt-in via `/backend codex`) shipped earlier the same day; the spec stub set the soak window at "≥2 weeks" before Phase 4 should be planned.

## Trigger checklist

- [ ] Phase 3 has been opt-in for at least the agreed soak window
- [x] Known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or explicitly accepted as non-blocking
- [ ] At least one capability promotion has direct evidence and a clear implementation path
- [ ] At least one `/status` improvement has direct user or test evidence
- [x] At least one error-surface improvement has direct evidence from live or unit runs

### Notes against the checklist

- **Soak window — UNCHECKED.** Phase 3 shipped earlier today. The stub design (`docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md` §3) set the bar at two weeks of opt-in usage with concrete user-reported gaps. Today's evidence comes from automated tests and a single live smoke pair, not real dogfooding traffic.
- **P1/P2 fixes — CHECKED.** Five Phase-3-era findings landed with regression coverage at HEAD `9280aef`:
    1. Circular import in `backends/codex.py` (commit `7d216ec`) — `task_manager` helpers are now lazy-imported inside `_popen` and `close_interactive`.
    2. `model_display` `isinstance(ClaudeBackend)` leak (commit `f73b43e`) — promoted to the `AgentBackend` Protocol; `_compose_status` falls back to `backend.model` when `model_display` is `None`.
    3. Codex subprocess not reaped after `turn.completed` (commit `7bbbbd3`) — drains stderr, `await proc.wait()`, logs WARNING on non-zero post-turn exit. Regression: `tests/backends/test_codex_backend.py::test_chat_stream_drains_proc_after_turn_completed` and `::test_chat_stream_logs_post_turn_nonzero_exit`.
    4. Misleading `/use` rejection text under Codex (commit `7d216ec`) — corrected from "skills or personas yet" to "skills yet (personas still work)" because personas use prompt-prefix injection rather than `append_system_prompt`.
    5. `acme` partial team-entry crash in the manager loader (commit `ceca7ca`) — loader now skips+warns malformed teams; writers (`patch_team_bot_backend_state`, `patch_team_bot_backend`, `save_session(team)`, `clear_session(team)`) tolerate the same shape.
- **Capability promotion — UNCHECKED.** The capability matrix lists every `False` Codex flag and every `False`'s underlying CLI-surface reason. None of those reasons changed between Phase 3 capture (`docs/superpowers/specs/2026-04-23-codex-cli-findings.md`) and today's bash smoke run. No promotion candidate has direct evidence.
- **`/status` improvement — UNCHECKED.** The gap inventory enumerates concrete reporting gaps (token usage, last duration, total requests, model-actually-used vs model-configured, effort/permission/allowed-tools snapshot, last error) but none of these is backed by a real user request or a failing test today. The Phase 4 design's §2.2 wording requires a "concrete user-asked-for improvement." Recording the gaps is enough to pre-stage Phase 4 implementation work; it is not enough to flip this trigger.
- **Error-surface improvement — CHECKED.** The post-`turn.completed` reap fix has direct evidence (commit `7bbbbd3` plus the two regression tests in `tests/backends/test_codex_backend.py`). Codex stderr at ERROR level during a successful turn is documented as informational and tolerated by the parser (`tests/backends/test_codex_backend.py::test_successful_stderr_warning_does_not_fail_turn` plus `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:32-40`).

Two boxes checked total, but only **one** of those (error-surface) is a soak-evidence trigger box; the other (P1/P2 fixes) is the gating prerequisite per §3 of the rollout-review template. The plan's "fewer than two trigger checkboxes other than the P1/P2 fixes one" rule applies.

## Evidence links

- Gap inventory: [`docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md`](2026-04-23-backend-phase-4-gap-inventory.md)
- Capability matrix: [`docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md`](2026-04-23-backend-phase-4-capability-matrix.md)
- Phase 3 CLI capture: [`docs/superpowers/specs/2026-04-23-codex-cli-findings.md`](2026-04-23-codex-cli-findings.md)

## Test evidence summary

Captured at HEAD `9280aef`:

- `PYTHONPATH=src pytest tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_codex_backend.py tests/backends/test_contract.py -v` — **15 passed, 1 skipped** (`test_backend_contract_chat_returns_string[<lambda>1]` skips on the second contract parametrization). Phase 3 P1/P2 fixes regression-tested here.
- `RUN_CODEX_LIVE=1 PYTHONPATH=src pytest tests/backends/test_codex_live.py -m codex_live -v -s` — **2 passed** (`test_codex_live_round_trip`, `test_codex_live_resume_reuses_session`).
- `PYTHONPATH=src pytest tests/test_backend_command.py tests/test_capability_gating.py tests/test_bot_streaming.py -v` — **36 passed**.

Bash smoke pair (2026-04-26):
- First turn: `codex exec --json --sandbox read-only "Reply with exactly OK ..."` → exit 0, four JSONL records, `thread_id = 019dc8db-0ea5-7193-8f58-d28059401947`.
- Resume turn: `codex exec resume --json 019dc8db-0ea5-7193-8f58-d28059401947 "Reply with exactly AGAIN ..."` → exit 0, four JSONL records, `thread.started.thread_id` echoed the original.
- Stderr produced two `ERROR codex_core::session: failed to record rollout items: thread <id> not found` lines plus a benign `Reading additional input from stdin...` startup line. Already documented and tolerated by the parser.

## Readiness decision

`NOT READY`.

Phase 3 shipped earlier today. The stub design's trigger-write threshold is two trigger boxes other than the P1/P2-fixes prerequisite, and only the error-surface box is currently checked. The capability-promotion and `/status`-improvement boxes both require evidence Phase 3's smoke run cannot provide on its own — capability promotion would need a concrete CLI feature whose `False` declaration was over-conservative, and `/status` improvement would need a real user-asked-for field. Neither has surfaced. The soak-window prerequisite is also unmet by definition. Re-run this readiness pass after the agreed two-week soak window and append fresh evidence to the same three docs; do not flip the verdict from a fresh testing pass alone.
