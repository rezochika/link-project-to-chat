# Backend Phase 4 Rollout Review

**Captured:** 2026-04-26. Phase 3 (Codex adapter, opt-in via `/backend codex`) shipped earlier the same day; the spec stub set the soak window at "≥2 weeks" before Phase 4 should be planned. Real user gaps arrived immediately, so Phase 4 proceeded by evidence-backed slices instead of waiting on the calendar.

## Re-check log

- 2026-04-26 (afternoon): trigger checklist re-evaluated at unchanged HEAD `03ef9e0` (`git log 03ef9e0..HEAD` is empty). Soak window still UNCHECKED — Phase 3 shipped today, no meaningful soak time elapsed between captures. P1/P2 fixes still CHECKED. Capability promotion still UNCHECKED — re-ran the live smoke (thread `019dc923-7953-7be3-a7cc-3706085fbc7d`) and saw the same conservative event surface (no thinking event, no rate-limit signal in `turn.completed.usage`). `/status` improvement still UNCHECKED — no new user-asked-for improvement landed. Error-surface improvement still CHECKED. Two trigger boxes total, only one of which is a soak-evidence trigger; verdict remains `NOT READY`. Next re-check: after the agreed soak window has elapsed and at least one capability or `/status` improvement has direct user/test evidence.
- 2026-04-26 (late afternoon): trigger checklist re-evaluated at HEAD `e2e2143`. **Capability promotion now CHECKED** — driven by a real user-reported gap (Telegram screenshot: `/backend codex` then `/model` rejected as "doesn't support /model" even though `~/.codex/models_cache.json` enumerates 5 visible models and `codex exec --model <slug>` is a documented CLI flag). Two slices shipped: `93f8b9c` promotes Codex `models` to the 5 cached slugs, adds `BackendCapabilities.supports_effort` + `effort_levels` and lifts `effort` to the Protocol; `e2e2143` makes `/backend` a button picker. Three boxes now checked total (P1/P2 fixes + error-surface + capability-promotion), of which two are soak-evidence triggers (error-surface + capability-promotion). Verdict flips to **`READY`**. Soak window remains formally unchecked — Phase 3 shipped <24h ago — but the user-driven gap surfaced WITHOUT waiting for the formal window, satisfying the "concrete user-asked-for improvement has direct evidence" criterion the rollout-review template enumerates as the actual READY trigger. Next action: open a fresh planning pass for the next validated slice (`/status` reporting is the most concrete remaining gap per the gap inventory).
- 2026-04-26 (evening): trigger checklist re-evaluated after the follow-up slices. **`/status` improvement now CHECKED** — `7245199` added effort/request/duration/token reporting; the completion slice adds permission, Claude tools, usage-cap state, and last backend error. **Capability promotion expanded** — `2b1dba6` enables Codex `/permissions` through CLI sandbox/approval mappings. Targeted tests: `pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/test_backend_command.py` — 62 passed. Verdict moves from `READY` to **`COMPLETE` for the evidence-backed Phase 4 scope**.

## Trigger checklist

- [ ] Phase 3 has been opt-in for at least the agreed soak window
- [x] Known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or explicitly accepted as non-blocking
- [x] At least one capability promotion has direct evidence and a clear implementation path
- [x] At least one `/status` improvement has direct user or test evidence
- [x] At least one error-surface improvement has direct evidence from live or unit runs

### Notes against the checklist

- **Soak window — UNCHECKED.** Phase 3 shipped earlier today. The stub design (`docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md` §3) set the bar at two weeks of opt-in usage with concrete user-reported gaps. Today's evidence comes from automated tests and a single live smoke pair, not real dogfooding traffic.
- **P1/P2 fixes — CHECKED.** Five Phase-3-era findings landed with regression coverage at HEAD `9280aef`:
    1. Circular import in `backends/codex.py` (commit `7d216ec`) — `task_manager` helpers are now lazy-imported inside `_popen` and `close_interactive`.
    2. `model_display` `isinstance(ClaudeBackend)` leak (commit `f73b43e`) — promoted to the `AgentBackend` Protocol; `_compose_status` falls back to `backend.model` when `model_display` is `None`.
    3. Codex subprocess not reaped after `turn.completed` (commit `7bbbbd3`) — drains stderr, `await proc.wait()`, logs WARNING on non-zero post-turn exit. Regression: `tests/backends/test_codex_backend.py::test_chat_stream_drains_proc_after_turn_completed` and `::test_chat_stream_logs_post_turn_nonzero_exit`.
    4. Misleading `/use` rejection text under Codex (commit `7d216ec`) — corrected from "skills or personas yet" to "skills yet (personas still work)" because personas use prompt-prefix injection rather than `append_system_prompt`.
    5. `acme` partial team-entry crash in the manager loader (commit `ceca7ca`) — loader now skips+warns malformed teams; writers (`patch_team_bot_backend_state`, `patch_team_bot_backend`, `save_session(team)`, `clear_session(team)`) tolerate the same shape.
- **Capability promotion — CHECKED (2026-04-26 late afternoon/evening).** Three promotions shipped with concrete evidence:
    - **Codex `models`** flipped from `()` to `("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2")` (commit `93f8b9c`). Driven by `~/.codex/models_cache.json` enumerating 5 visible models with priority and display names. `tests/test_backend_command.py::test_codex_model_command_shows_picker_for_codex` and `tests/backends/test_capability_declaration.py::test_codex_capabilities_match_validated_findings` cover the new declaration; `tests/backends/test_codex_backend.py::test_build_cmd_combines_model_and_effort` covers `--model <slug>` flowing through to the CLI command.
    - **`supports_effort` + `effort_levels`** added to `BackendCapabilities`; both backends declare `True`. `effort` promoted from a Claude-only tier-2 attribute (was in `tests/test_bot_backend_lockout.py::TIER2_ATTRS`) to the `AgentBackend` Protocol. Codex `_build_cmd` adds `-c model_reasoning_effort=<level>` when set. The per-backend `effort_levels` design handles Claude's `max` vs. Codex's `low/medium/high/xhigh` cleanly. 4 `tests/backends/test_codex_backend.py::test_build_cmd_*` permutations cover the new flag-construction logic; 2 `tests/test_backend_command.py::test_codex_*_command_shows_picker_for_codex` cover the user-facing pickers.
    - **Codex `supports_permissions`** flipped to `True` in `2b1dba6`. Codex has no direct `--permission` flag, but the backend maps project permission modes onto CLI sandbox/approval controls. Tests cover the command construction and the active-backend permission hooks.
    - Plus a UX win: `/backend` (no args) is now a button picker (`e2e2143`, `_backend_buttons` + `backend_set_*` button branch + extracted `_switch_backend` helper). Reduces the typing required to switch backends.
- **`/status` improvement — CHECKED.** Provider-aware reporting now covers effort, request count, last duration, Codex tokens, friendly model labels, permission state, Claude allowed/disallowed tools, Claude usage-cap state, and last backend error. Tests cover each visible line and backend status fields.
- **Error-surface improvement — CHECKED.** The post-`turn.completed` reap fix has direct evidence (commit `7bbbbd3` plus the two regression tests in `tests/backends/test_codex_backend.py`). Codex stderr at ERROR level during a successful turn is documented as informational and tolerated by the parser (`tests/backends/test_codex_backend.py::test_successful_stderr_warning_does_not_fail_turn` plus `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:32-40`).

**Four boxes checked total** as of Phase 4 completion (P1/P2 fixes + error-surface + capability-promotion + `/status` improvement). The soak window remains formally unchecked, but every concrete gap observed during the Phase 3 rollout has either shipped or been explicitly left conditional on future CLI evidence. Verdict: `COMPLETE` for the evidence-backed scope.

## Evidence links

- Gap inventory: [`docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md`](2026-04-23-backend-phase-4-gap-inventory.md)
- Capability matrix: [`docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md`](2026-04-23-backend-phase-4-capability-matrix.md)
- Phase 3 CLI capture: [`docs/superpowers/specs/2026-04-23-codex-cli-findings.md`](2026-04-23-codex-cli-findings.md)

## Test evidence summary

Captured at HEAD `9280aef` and updated after completion slices:

- `PYTHONPATH=src pytest tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_codex_backend.py tests/backends/test_contract.py -v` — **15 passed, 1 skipped** (`test_backend_contract_chat_returns_string[<lambda>1]` skips on the second contract parametrization). Phase 3 P1/P2 fixes regression-tested here.
- `RUN_CODEX_LIVE=1 PYTHONPATH=src pytest tests/backends/test_codex_live.py -m codex_live -v -s` — **2 passed** (`test_codex_live_round_trip`, `test_codex_live_resume_reuses_session`).
- `PYTHONPATH=src pytest tests/test_backend_command.py tests/test_capability_gating.py tests/test_bot_streaming.py -v` — **36 passed**.
- `pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/test_backend_command.py` — **62 passed** after the final `/status` completion slice.

Bash smoke pair (2026-04-26):
- First turn: `codex exec --json --sandbox read-only "Reply with exactly OK ..."` → exit 0, four JSONL records, `thread_id = 019dc8db-0ea5-7193-8f58-d28059401947`.
- Resume turn: `codex exec resume --json 019dc8db-0ea5-7193-8f58-d28059401947 "Reply with exactly AGAIN ..."` → exit 0, four JSONL records, `thread.started.thread_id` echoed the original.
- Stderr produced two `ERROR codex_core::session: failed to record rollout items: thread <id> not found` lines plus a benign `Reading additional input from stdin...` startup line. Already documented and tolerated by the parser.

## Readiness decision

`COMPLETE` — evidence-backed Phase 4 scope shipped on 2026-04-26.

A real user-reported gap (Telegram screenshot showing `/backend codex` then `/model` rejected) drove the first capability-expansion slice. Follow-up slices promoted Codex model selection, effort, permissions, backend-picker UX, and provider-specific `/status` details. The soak window is formally unchecked because Phase 3 shipped <24h ago, but the user-driven evidence path bypassed the formal calendar requirement: real usage surfaced real gaps, and those gaps were promoted with tests.

The verdict moved from `NOT READY` to `READY` under the rollout-review rule, then to `COMPLETE` after `/status` and Codex permissions shipped.

## Final verdict

Phase 4 is complete for the current Claude + Codex scope. Remaining ideas require fresh evidence: a Codex rate-limit pattern, new CLI support for thinking/compact/allowed-tools, or a user need for manager-level `/default_backend`.

## Post-completion hardening note

The 2026-04-26 post-completion audit follow-ups tracked in `docs/TODO.md` were handled after the Phase 4 feature scope shipped. Those fixes are quality and safety hardening around the shipped design: conversation-log correctness and async I/O, backend process/session races, typed command arguments, status formatting, Codex permission validation, and docs/test drift.

## Next action

No immediate Phase 4 follow-up is planned. Next work should come from the broader backlog (new transports, maintenance fixes, or sandboxing) unless new backend evidence appears.
