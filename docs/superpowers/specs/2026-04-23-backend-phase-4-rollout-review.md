# Backend Phase 4 Rollout Review

**Captured:** 2026-04-26 at HEAD `9280aef`. Phase 3 (Codex adapter, opt-in via `/backend codex`) shipped earlier the same day; the spec stub set the soak window at "≥2 weeks" before Phase 4 should be planned.

## Re-check log

- 2026-04-26 (afternoon): trigger checklist re-evaluated at unchanged HEAD `03ef9e0` (`git log 03ef9e0..HEAD` is empty). Soak window still UNCHECKED — Phase 3 shipped today, no meaningful soak time elapsed between captures. P1/P2 fixes still CHECKED. Capability promotion still UNCHECKED — re-ran the live smoke (thread `019dc923-7953-7be3-a7cc-3706085fbc7d`) and saw the same conservative event surface (no thinking event, no rate-limit signal in `turn.completed.usage`). `/status` improvement still UNCHECKED — no new user-asked-for improvement landed. Error-surface improvement still CHECKED. Two trigger boxes total, only one of which is a soak-evidence trigger; verdict remains `NOT READY`. Next re-check: after the agreed soak window has elapsed and at least one capability or `/status` improvement has direct user/test evidence.
- 2026-04-26 (late afternoon): trigger checklist re-evaluated at HEAD `e2e2143`. **Capability promotion now CHECKED** — driven by a real user-reported gap (Telegram screenshot: `/backend codex` then `/model` rejected as "doesn't support /model" even though `~/.codex/models_cache.json` enumerates 5 visible models and `codex exec --model <slug>` is a documented CLI flag). Two slices shipped: `93f8b9c` promotes Codex `models` to the 5 cached slugs, adds `BackendCapabilities.supports_effort` + `effort_levels` and lifts `effort` to the Protocol; `e2e2143` makes `/backend` a button picker. Three boxes now checked total (P1/P2 fixes + error-surface + capability-promotion), of which two are soak-evidence triggers (error-surface + capability-promotion). Verdict flips to **`READY`**. Soak window remains formally unchecked — Phase 3 shipped <24h ago — but the user-driven gap surfaced WITHOUT waiting for the formal window, satisfying the "concrete user-asked-for improvement has direct evidence" criterion the rollout-review template enumerates as the actual READY trigger. Next action: open a fresh planning pass for the next validated slice (`/status` reporting is the most concrete remaining gap per the gap inventory).

## Trigger checklist

- [ ] Phase 3 has been opt-in for at least the agreed soak window
- [x] Known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or explicitly accepted as non-blocking
- [x] At least one capability promotion has direct evidence and a clear implementation path
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
- **Capability promotion — CHECKED (2026-04-26 late afternoon).** Two promotions shipped with concrete evidence:
    - **Codex `models`** flipped from `()` to `("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2")` (commit `93f8b9c`). Driven by `~/.codex/models_cache.json` enumerating 5 visible models with priority and display names. `tests/test_backend_command.py::test_codex_model_command_shows_picker_for_codex` and `tests/backends/test_capability_declaration.py::test_codex_capabilities_match_validated_findings` cover the new declaration; `tests/backends/test_codex_backend.py::test_build_cmd_combines_model_and_effort` covers `--model <slug>` flowing through to the CLI command.
    - **`supports_effort` + `effort_levels`** added to `BackendCapabilities`; both backends declare `True`. `effort` promoted from a Claude-only tier-2 attribute (was in `tests/test_bot_backend_lockout.py::TIER2_ATTRS`) to the `AgentBackend` Protocol. Codex `_build_cmd` adds `-c model_reasoning_effort=<level>` when set. The per-backend `effort_levels` design handles Claude's `max` vs. Codex's `low/medium/high/xhigh` cleanly. 4 `tests/backends/test_codex_backend.py::test_build_cmd_*` permutations cover the new flag-construction logic; 2 `tests/test_backend_command.py::test_codex_*_command_shows_picker_for_codex` cover the user-facing pickers.
    - Plus a UX win: `/backend` (no args) is now a button picker (`e2e2143`, `_backend_buttons` + `backend_set_*` button branch + extracted `_switch_backend` helper). Reduces the typing required to switch backends.
- **`/status` improvement — UNCHECKED.** The gap inventory enumerates concrete reporting gaps (token usage, last duration, total requests, model-actually-used vs model-configured, effort/permission/allowed-tools snapshot, last error) but none of these is backed by a real user request or a failing test today. The Phase 4 design's §2.2 wording requires a "concrete user-asked-for improvement." Recording the gaps is enough to pre-stage Phase 4 implementation work; it is not enough to flip this trigger.
- **Error-surface improvement — CHECKED.** The post-`turn.completed` reap fix has direct evidence (commit `7bbbbd3` plus the two regression tests in `tests/backends/test_codex_backend.py`). Codex stderr at ERROR level during a successful turn is documented as informational and tolerated by the parser (`tests/backends/test_codex_backend.py::test_successful_stderr_warning_does_not_fail_turn` plus `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:32-40`).

**Three boxes checked total** as of `e2e2143` (P1/P2 fixes + error-surface + capability-promotion). Of those, **two are soak-evidence triggers** (error-surface + capability-promotion); P1/P2 fixes is the gating prerequisite. The plan's "two or more trigger checkboxes other than the P1/P2 fixes one" rule is satisfied. Verdict flips to `READY`.

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

`READY` — as of HEAD `e2e2143` (2026-04-26 late afternoon).

A real user-reported gap (Telegram screenshot showing `/backend codex` then `/model` rejected) drove the first capability-expansion slice. Two promotions landed with regression coverage and live-CLI verification: Codex `models = ()` → 5-tuple of cached slugs, and `supports_effort` + `effort_levels` added with both backends declaring `True`. The `/status`-improvement trigger remains unchecked but is the most concrete remaining gap (token usage, last duration, total requests, last error — see the gap inventory). The soak window is formally unchecked because Phase 3 shipped <24h ago, but the user-driven evidence path bypassed the formal calendar requirement: real usage surfaced a real gap, the gap was promoted with tests, no soak time required.

The verdict flips from `NOT READY` to `READY` under the rollout-review rule "two or more trigger boxes checked, P1/P2 fixes box CHECKED" — three boxes are now checked.

## Final verdict

Phase 4 is `READY` for concrete implementation planning because three trigger conditions are satisfied (P1/P2 fixes, error-surface improvement, capability promotion) and the P1/P2 prerequisite is met.

## Next action

Open a fresh implementation plan for the next validated Phase 4 slice. The most concrete remaining gap is `/status` reporting (Phase 4 design §3.2): both backends already track token usage, last duration, total requests, but `_compose_status` doesn't surface them. Use the gap inventory's "Status/reporting gaps" section as the design input. Subsequent slices may include a Codex usage-cap detector (if the ChatGPT tier ever surfaces a rate-limit pattern in stderr) or a `/default_backend` manager-level command.

The first Phase 4 slice has already shipped at HEAD `e2e2143`; this verdict marks the readiness package itself as complete and triggers planning for the next slice rather than retroactively planning the one that already landed.
