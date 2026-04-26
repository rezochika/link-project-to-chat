# Backend Phase 4 Gap Inventory

**Status:** Captured 2026-04-26 from the post-Phase-3 verification suite + one direct Codex CLI smoke run.
**Phase 3 shipped:** Earlier on 2026-04-26 (Codex adapter opt-in via `/backend codex`).
**Caveat:** The Phase 3 soak window has not elapsed; this inventory captures evidence only from automated tests and a single live smoke pair, not from real user dogfooding.

## Re-check log

- 2026-04-26 (afternoon): re-ran the verification suites and the direct Codex CLI smoke against an unchanged code state at HEAD `03ef9e0`. Backend unit suites: 15 passed + 1 skipped (`tests/backends/test_env_policy.py` + `test_capability_declaration.py` + `test_codex_backend.py` + `test_contract.py`). Bot-level suites: 36 passed (`tests/test_backend_command.py` + `test_capability_gating.py` + `test_bot_streaming.py`). Live smoke: 2 passed in 18.20s (`tests/backends/test_codex_live.py`). Direct CLI smoke: first turn rc=0 + turn.completed reached, resume rc=0 + turn.completed reached, same `thread_id` (`019dc923-7953-7be3-a7cc-3706085fbc7d`); stderr emitted only the now-known noise pattern (`ERROR codex_core::session: failed to record rollout items: thread <id> not found`, 2 lines + 1 stdin notice). No new gaps surfaced. Verdict unchanged.

---

## Validated capabilities that are already working

These bullets are tied to a passing test or a successful smoke transcript:

- **Codex resume reuses `thread_id`** — `tests/backends/test_codex_live.py::test_codex_live_resume_reuses_session` asserts `backend.session_id == first_session` after a second `chat()` call. Independently re-verified by the bash smoke run on 2026-04-26 — both `codex exec --json ...` and `codex exec resume --json <thread_id> ...` exited 0 and the resume's `thread.started` event echoed `019dc8db-0ea5-7193-8f58-d28059401947` rather than minting a new id.
- **Codex live text streaming** — `tests/backends/test_codex_live.py::test_codex_live_round_trip` asserts at least one `TextDelta` event and a closing `Result` arrive from the real CLI. The fake-proc unit test `tests/backends/test_codex_backend.py::test_chat_stream_emits_text_delta_then_result` confirms the same ordering against the captured fixture `tests/fixtures/codex_exec_ok.jsonl`.
- **Codex env scrubbing** — `tests/backends/test_env_policy.py::test_codex_keeps_openai_but_scrubs_anthropic` confirms `OPENAI_*` / `CODEX_*` survive while `ANTHROPIC_*` and other secret-shaped keys are stripped before spawn.
- **Codex capability declaration is conservative and stable** — `tests/backends/test_capability_declaration.py::test_codex_capabilities_match_validated_findings` pins all `False`/`()` values from `src/link_project_to_chat/backends/codex.py:22-30`. No drift.
- **Codex stderr noise during a successful turn does not abort** — `tests/backends/test_codex_backend.py::test_successful_stderr_warning_does_not_fail_turn` asserts an ERROR-level stderr line emitted alongside a clean stdout sequence still yields a `Result`. Confirmed live by the smoke run: stderr produced `2026-04-26T08:14:59.087519Z ERROR codex_core::session: failed to record rollout items: thread <id> not found` and the turn still completed `0`.
- **Codex subprocess cleanup on the success path** — see Error-surface gaps below.
- **Capability gating against Codex's `False` flags** — `tests/test_capability_gating.py::test_thinking_command_rejected_when_backend_does_not_support_it`, `::test_permissions_command_rejected_when_backend_does_not_support_it`, `::test_compact_command_rejected_when_backend_does_not_support_it`, `::test_model_command_rejected_when_backend_has_no_models` all pass — `/thinking`, `/permissions`, `/compact`, `/model` are silently rejected when the active backend declares `False`.
- **`/backend` switching while idle** — `tests/test_backend_command.py::test_backend_command_switches_to_other_registered_backend` and `::test_backend_command_rejects_when_tasks_running` confirm switching works when no task is in flight and is refused otherwise. `::test_backend_command_switch_persists_for_team_bot` confirms team-bot persistence under the spec #2 config shape.
- **Claude backend session round-trip** — `tests/test_stream.py::test_result_event` proves `parse_stream_line` correctly extracts `session_id` from a Claude `result` event (`s1` round-trips through `Result.session_id`), backing the Phase 1 Protocol contract that Claude's `--resume` works the same way Codex's `exec resume` does.
- **Backend Protocol contract** — `tests/backends/test_contract.py::test_backend_contract_declares_name_and_capabilities`, `::test_codex_backend_contract_chat_returns_string`, `::test_backend_contract_probe_health` all pass for both backends.

## Candidate capability promotions

**None.** This is the conservative bullet the Self-review checklist demands: every `False` field on `CODEX_CAPABILITIES` was declared `False` in Phase 3 because the Codex CLI 0.125.0 surface does not expose the underlying feature, and nothing in this round of evidence contradicts that.

Specifically:
- No thinking-delta event was observed on stdout in the smoke run (only `thread.started` / `turn.started` / `item.completed` (`agent_message`) / `turn.completed`). The `turn.completed.usage.reasoning_output_tokens` field exists but is a token count, not a stream — already noted in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:48-56`. No path to `supports_thinking = True` without CLI changes.
- No `--permission`, `--compact`, or `--allowed-tools` flag exists on `codex exec` or `codex exec resume` (confirmed in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:41`).
- `codex exec --model <name>` is accepted but the CLI does not advertise a fixed validated model list. Promoting `models` to a non-empty tuple would require a second live capture across known model names; that work has not been done and is therefore explicitly out of scope here.
- No usage-cap pattern has been observed — only token counts in the `usage` dict. Codex on a ChatGPT-tier login may surface a cap eventually, but no evidence has been captured.

## Status/reporting gaps

`bot.py:_compose_status` (`src/link_project_to_chat/bot.py:1842-1865`) currently emits:

```
Project, Path, Backend, Model, Uptime, Session, Agent, Running tasks, Waiting, Skill, Persona
```

Missing fields the Phase 4 design (§2.2) calls out, against the data the backends already track:

- **Token / usage totals** — `CodexBackend` stores `_last_usage` (populated from `turn.completed.usage`, see `backends/codex.py:50, 137-138`). Not surfaced in `/status`. Same gap on the Claude side: `total_requests` and `last_duration` are tracked (`backends/claude.py:430-441`) but `/status` never references them. **Both backends.**
- **Rate-limit / usage-cap state** — Claude's `_detect_usage_cap` (`backends/claude.py:50-55`) and `is_usage_cap_error` exist for runtime branching, but `/status` does not expose whether the last request hit a cap or how recent that signal is. **Claude only** (Codex declares `supports_usage_cap_detection = False`).
- **Active model display vs. configured model** — `model_display` is now a Protocol attribute (Phase 3 Task 3 amend, `f73b43e`) and the bot falls back to `backend.model` when `model_display` is `None`. Both backends initialize `model_display = None` in `__init__`; neither populates it from the wire. So `/status` always shows the configured model, never the model the provider actually executed against. **Both backends.**
- **Effort / permission / allowed-tools snapshot** — Claude tracks `effort`, `permission_mode`, `allowed_tools`, `disallowed_tools` on the backend but `/status` shows none of them. Hidden user-mutable state. **Claude only.** (Codex declares all three `False` and would stay blank.)
- **Last error / capped flag** — Neither backend records the last `Error` event in `status`. **Both backends.**

These are all reporting gaps, not behavior gaps — the data exists, `/status` just doesn't surface it.

## Error-surface gaps

- **Codex stderr at ERROR level during a successful turn — already handled.** The CLI emits `ERROR codex_core::session: failed to record rollout items: thread <id> not found` at startup and after each turn in this environment. Both turns still exit `0` with valid JSONL on stdout. The adapter's `chat_stream` reads stdout to JSON-parse turn events and only consults stderr after `turn.completed` or stdout EOF. Recorded in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:32-40`. No action needed.
- **Codex success-path subprocess cleanup — fixed and regression-tested.** The original Phase 3 implementation returned from `chat_stream` the moment it yielded the closing `Result`, leaving `proc` unreaped. Commit `7bbbbd3` reorders the success path to drain `stderr`, `await proc.wait()`, and log a WARNING when the post-`turn.completed` exit code is non-zero before clearing `_proc`. Regression covered by `tests/backends/test_codex_backend.py::test_chat_stream_drains_proc_after_turn_completed` (asserts `proc.wait_count == 1` and stderr fully drained) and `::test_chat_stream_logs_post_turn_nonzero_exit` (asserts the WARNING line includes both `exited 1` and the stderr text). Both pass at HEAD `9280aef`.
- **Codex CLI 0.125.0 refuses non-git directories.** Phase 3 deliberately did not pass `--skip-git-repo-check`. The live test `tests/backends/test_codex_live.py::_trusted_project` initializes `tmp_path` as a git repo to mirror production project paths. This is documented in the test's docstring; no adapter-side gap, but worth recording so Phase 4 doesn't accidentally drop the requirement.
- **No Codex analogue of Claude's usage-cap detection.** `backends/claude.py:42-67` matches stderr against `_USAGE_CAP_PATTERNS`. Codex's stderr in the wild has been only the rollout-write ERROR; no rate-limit signal has surfaced. If/when the ChatGPT tier returns a cap, Phase 4 will need a Codex-side detector. No evidence yet.

## Protocol fit concerns

- **No protocol-shape regrets surfaced from the Phase 3 commits.** `AgentBackend` and `BackendCapabilities` both held without modification across the Codex landing. The single Protocol-level change Phase 3 made — promoting `model_display` from a Claude-only attribute to a Protocol attribute (commit `f73b43e`) — fixed an `isinstance(ClaudeBackend)` leak in `_compose_status` cleanly without re-shaping the rest of the Protocol. The `BackendCapabilities` flags map 1:1 to user commands and gate cleanly via `tests/test_capability_gating.py`. No reshape proposed.
