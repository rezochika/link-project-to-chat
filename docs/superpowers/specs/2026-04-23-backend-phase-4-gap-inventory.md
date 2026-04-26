# Backend Phase 4 Gap Inventory

**Status:** Updated 2026-04-26 after Phase 4 completion slices.
**Phase 3 shipped:** Earlier on 2026-04-26 (Codex adapter opt-in via `/backend codex`).
**Caveat:** The Phase 3 soak window has not elapsed; this inventory captures evidence only from automated tests and a single live smoke pair, not from real user dogfooding.

## Re-check log

- 2026-04-26 (afternoon): re-ran the verification suites and the direct Codex CLI smoke against an unchanged code state at HEAD `03ef9e0`. Backend unit suites: 15 passed + 1 skipped (`tests/backends/test_env_policy.py` + `test_capability_declaration.py` + `test_codex_backend.py` + `test_contract.py`). Bot-level suites: 36 passed (`tests/test_backend_command.py` + `test_capability_gating.py` + `test_bot_streaming.py`). Live smoke: 2 passed in 18.20s (`tests/backends/test_codex_live.py`). Direct CLI smoke: first turn rc=0 + turn.completed reached, resume rc=0 + turn.completed reached, same `thread_id` (`019dc923-7953-7be3-a7cc-3706085fbc7d`); stderr emitted only the now-known noise pattern (`ERROR codex_core::session: failed to record rollout items: thread <id> not found`, 2 lines + 1 stdin notice). No new gaps surfaced. Verdict unchanged.
- 2026-04-26 (late afternoon): **first capability-expansion slice shipped** (`93f8b9c` Codex `/model` + `/effort`; `e2e2143` `/backend` button picker). Triggered by a real user-reported gap — `/backend codex` then `/model` rejected as "doesn't support /model" even though the Codex CLI accepts `--model <slug>` and the local cache enumerates 5 visible GPT-5 variants. Codex `models = ()` was over-conservative; promoted to the 5 cached slugs. `supports_effort` and `effort_levels` added to `BackendCapabilities`; both backends declare `True`. `effort` promoted from a Claude-only tier-2 attribute to the `AgentBackend` Protocol. `_telegram_command_summary` now emits `/effort` from `capabilities.effort_levels` rather than a hardcoded Claude string. `/backend` invocation with no args renders a button picker (`_backend_buttons` + `backend_set_*` button branch + `_switch_backend` extracted helper). 843 passed in the full suite at `e2e2143`. Live tests still pass. Two Codex capability flags moved from "candidate" to "validated working"; one capability flag now `True` on both backends (effort).
- 2026-04-26 (evening): **completion slices shipped**. `7245199` surfaced `/status` effort, total requests, last duration, and Codex token usage; `d0e4b97` resolved friendly model labels from wire identifiers; `2b1dba6` promoted Codex `/permissions` through backend-level `current_permission()` / `set_permission()` and CLI sandbox mappings; the final status slice adds permission, Claude allowed/disallowed tools, usage-cap state, and last backend error reporting. Targeted suite: `pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/test_backend_command.py` — 62 passed.

---

## Validated capabilities that are already working

These bullets are tied to a passing test or a successful smoke transcript:

- **Codex resume reuses `thread_id`** — `tests/backends/test_codex_live.py::test_codex_live_resume_reuses_session` asserts `backend.session_id == first_session` after a second `chat()` call. Independently re-verified by the bash smoke run on 2026-04-26 — both `codex exec --json ...` and `codex exec resume --json <thread_id> ...` exited 0 and the resume's `thread.started` event echoed `019dc8db-0ea5-7193-8f58-d28059401947` rather than minting a new id.
- **Codex live text streaming** — `tests/backends/test_codex_live.py::test_codex_live_round_trip` asserts at least one `TextDelta` event and a closing `Result` arrive from the real CLI. The fake-proc unit test `tests/backends/test_codex_backend.py::test_chat_stream_emits_text_delta_then_result` confirms the same ordering against the captured fixture `tests/fixtures/codex_exec_ok.jsonl`.
- **Codex env scrubbing** — `tests/backends/test_env_policy.py::test_codex_keeps_openai_but_scrubs_anthropic` confirms `OPENAI_*` / `CODEX_*` survive while `ANTHROPIC_*` and other secret-shaped keys are stripped before spawn.
- **Codex capability declaration is validated and stable** — `tests/backends/test_capability_declaration.py::test_codex_capabilities_match_validated_findings` pins the promoted `models`, `supports_effort`, `effort_levels`, and `supports_permissions` fields plus the remaining unsupported flags. No drift.
- **Codex stderr noise during a successful turn does not abort** — `tests/backends/test_codex_backend.py::test_successful_stderr_warning_does_not_fail_turn` asserts an ERROR-level stderr line emitted alongside a clean stdout sequence still yields a `Result`. Confirmed live by the smoke run: stderr produced `2026-04-26T08:14:59.087519Z ERROR codex_core::session: failed to record rollout items: thread <id> not found` and the turn still completed `0`.
- **Codex subprocess cleanup on the success path** — see Error-surface gaps below.
- **Capability gating against Codex's remaining `False` flags** — `tests/test_capability_gating.py::test_thinking_command_rejected_when_backend_does_not_support_it` and `::test_compact_command_rejected_when_backend_does_not_support_it` still assert unsupported commands reject cleanly. `/permissions` is no longer gated off for Codex after `2b1dba6`.
- **`/backend` switching while idle** — `tests/test_backend_command.py::test_backend_command_switches_to_other_registered_backend` and `::test_backend_command_rejects_when_tasks_running` confirm switching works when no task is in flight and is refused otherwise. `::test_backend_command_switch_persists_for_team_bot` confirms team-bot persistence under the spec #2 config shape.
- **Claude backend session round-trip** — `tests/test_stream.py::test_result_event` proves `parse_stream_line` correctly extracts `session_id` from a Claude `result` event (`s1` round-trips through `Result.session_id`), backing the Phase 1 Protocol contract that Claude's `--resume` works the same way Codex's `exec resume` does.
- **Backend Protocol contract** — `tests/backends/test_contract.py::test_backend_contract_declares_name_and_capabilities`, `::test_codex_backend_contract_chat_returns_string`, `::test_backend_contract_probe_health` all pass for both backends.
- **Codex `/model` button picker** (shipped `93f8b9c`) — `tests/test_backend_command.py::test_codex_model_command_shows_picker_for_codex` asserts `/model` invocation while Codex is active surfaces 5 buttons (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`). `tests/backends/test_capability_declaration.py` pins `CODEX_CAPABILITIES.models` to the 5-tuple. Selection flows through `_on_button("model_set_*")` → `backend.model = name` + `_patch_backend_config({"model": name})` → next turn includes `--model <slug>` on the CLI command (`tests/backends/test_codex_backend.py::test_build_cmd_combines_model_and_effort`).
- **Codex `/effort` reasoning-level picker** (shipped `93f8b9c`) — `tests/test_backend_command.py::test_codex_effort_command_shows_picker_for_codex` asserts the 4 buttons (`low`, `medium`, `high`, `xhigh`). `BackendCapabilities` gains `supports_effort: bool` and `effort_levels: Sequence[str]`; `effort` is promoted to the `AgentBackend` Protocol. `_build_cmd` adds `-c model_reasoning_effort=<level>` when set, verified by 4 `tests/backends/test_codex_backend.py::test_build_cmd_*` permutations covering effort-only, no-effort, model+effort, and resume+effort. Live CLI accepts the `-c` override (independently confirmed before commit).
- **`/backend` button picker** (shipped `e2e2143`) — `tests/test_backend_command.py::test_backend_command_renders_picker_with_active_marker` asserts `/backend` (no args) renders one row per registered backend with the active one ●-prefixed. Click-to-switch covered by `::test_backend_button_click_switches_backend`, no-op-on-active by `::test_backend_button_click_on_active_is_noop`, unauth-silent by `::test_backend_button_click_unauthorized_silent`. The four-form switch logic (no-op / unknown / live-task / activate-first persist) is extracted to `_switch_backend` and shared between the typed-arg path and the button branch.
- **Codex `/permissions`** (shipped `2b1dba6`) — `CODEX_CAPABILITIES.supports_permissions = True`; `CodexBackend._permission_args()` maps `plan` to read-only sandbox + never approval, `acceptEdits`/`dontAsk`/`auto` to `--full-auto`, and bypass modes to `--dangerously-bypass-approvals-and-sandbox`. Covered by `tests/backends/test_codex_backend.py::test_build_cmd_includes_read_only_sandbox_for_plan_permissions`, `::test_build_cmd_includes_full_auto_for_auto_permissions`, `::test_build_cmd_includes_dangerous_bypass_for_skip_permissions`, and capability-gating command/button tests.
- **Provider-aware `/status`** (completed after `7245199`) — `_compose_status` reports backend, friendly model, effort, permissions, Claude allowed/disallowed tools, uptime/session/task counts, request count, last duration, Codex token usage, Claude usage-cap state, last backend error, skill, and persona. Backend status dicts now expose the fields needed for Claude and Codex. Covered by status tests in `tests/test_backend_command.py` and backend status tests in `tests/backends/test_claude_backend.py` / `tests/backends/test_codex_backend.py`.

## Candidate capability promotions

**Three shipped on 2026-04-26**:
- ~~`models` from `()` to the 5 cached slugs~~ — **shipped.** Discovered `~/.codex/models_cache.json` enumerates 5 visible models with `slug` / `display_name` / `priority` / `default_reasoning_level`; promoted directly. Listed under "Validated capabilities that are already working".
- ~~`supports_effort` from implicit-False to explicit-True~~ — **shipped.** Verified `codex exec -c model_reasoning_effort=high "..."` exits `0` against the live CLI; promoted both Claude (which already had effort, just not as a capability flag) and Codex. Per-backend `effort_levels` cleanly handles Claude's "max" extra option vs. Codex's `low/medium/high/xhigh`.
- ~~`supports_permissions` from `False` to `True` for Codex~~ — **shipped.** Codex has no direct `--permission` flag, but equivalent CLI controls exist for the project's permission modes: read-only plan mode via `-c sandbox_mode='read-only' -c approval_policy='never'`, automatic edit modes via `--full-auto`, and bypass modes via Codex's explicit dangerous bypass flag.

**Remaining `False` Codex flags — still no clear promotion path:**
- No thinking-delta event was observed on stdout in the smoke run (only `thread.started` / `turn.started` / `item.completed` (`agent_message`) / `turn.completed`). The `turn.completed.usage.reasoning_output_tokens` field exists but is a token count, not a stream — already noted in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:48-56`. No path to `supports_thinking = True` without CLI changes.
- No `--compact` or `--allowed-tools` flag exists on `codex exec` or `codex exec resume` (confirmed in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:41`). Permission support is handled through equivalent sandbox/approval flags, not a direct `--permission` flag.
- No usage-cap pattern has been observed — only token counts in the `usage` dict. Codex on a ChatGPT-tier login may surface a cap eventually, but no evidence has been captured.

The remaining Codex `False` flags (`supports_thinking`, `supports_compact`, `supports_allowed_tools`, `supports_usage_cap_detection`) all reflect genuine CLI-surface absence or lack of observed rate-limit evidence, not adapter conservatism.

## Status/reporting final state

Status reporting gaps identified earlier in Phase 4 are now closed for fields with available backend data.

```
Project, Path, Backend, Model, Effort, Permissions, Allowed tools, Disallowed tools,
Uptime, Session, Agent, Running tasks, Waiting, Requests, Last duration,
Last tokens, Usage capped, Last error, Skill, Persona
```

Closed items:

- **Token / usage totals** — `CodexBackend.status["last_usage"]` is rendered as `Last tokens`; `total_requests` and Claude `last_duration` are rendered when non-empty.
- **Rate-limit / usage-cap state** — Claude status tracks and renders `usage_capped`.
- **Active model display vs. configured model** — `_current_model()` resolves friendly labels from backend `MODEL_OPTIONS`, including wire-identifier aliases.
- **Effort / permission / allowed-tools snapshot** — `/status` renders effort where supported, permissions where supported, and Claude allowed/disallowed tools.
- **Last error / capped flag** — Claude and Codex status dicts expose `last_error`, and `/status` renders a compact `Last error:` line.

Remaining intentionally absent item: Codex usage-cap state. No Codex cap/rate-limit signal has been observed, and `supports_usage_cap_detection` remains `False`.

## Error-surface gaps

- **Codex stderr at ERROR level during a successful turn — already handled.** The CLI emits `ERROR codex_core::session: failed to record rollout items: thread <id> not found` at startup and after each turn in this environment. Both turns still exit `0` with valid JSONL on stdout. The adapter's `chat_stream` reads stdout to JSON-parse turn events and only consults stderr after `turn.completed` or stdout EOF. Recorded in `docs/superpowers/specs/2026-04-23-codex-cli-findings.md:32-40`. No action needed.
- **Codex success-path subprocess cleanup — fixed and regression-tested.** The original Phase 3 implementation returned from `chat_stream` the moment it yielded the closing `Result`, leaving `proc` unreaped. Commit `7bbbbd3` reorders the success path to drain `stderr`, `await proc.wait()`, and log a WARNING when the post-`turn.completed` exit code is non-zero before clearing `_proc`. Regression covered by `tests/backends/test_codex_backend.py::test_chat_stream_drains_proc_after_turn_completed` (asserts `proc.wait_count == 1` and stderr fully drained) and `::test_chat_stream_logs_post_turn_nonzero_exit` (asserts the WARNING line includes both `exited 1` and the stderr text). Both pass at HEAD `9280aef`.
- **Codex CLI 0.125.0 refuses non-git directories.** Phase 3 deliberately did not pass `--skip-git-repo-check`. The live test `tests/backends/test_codex_live.py::_trusted_project` initializes `tmp_path` as a git repo to mirror production project paths. This is documented in the test's docstring; no adapter-side gap, but worth recording so Phase 4 doesn't accidentally drop the requirement.
- **No Codex analogue of Claude's usage-cap detection.** `backends/claude.py:42-67` matches stderr against `_USAGE_CAP_PATTERNS`. Codex's stderr in the wild has been only the rollout-write ERROR; no rate-limit signal has surfaced. If/when the ChatGPT tier returns a cap, Phase 4 will need a Codex-side detector. No evidence yet.

## Protocol fit concerns

- **No protocol-shape regrets surfaced from the Phase 3 commits.** `AgentBackend` and `BackendCapabilities` both held without modification across the Codex landing. The single Protocol-level change Phase 3 made — promoting `model_display` from a Claude-only attribute to a Protocol attribute (commit `f73b43e`) — fixed an `isinstance(ClaudeBackend)` leak in `_compose_status` cleanly without re-shaping the rest of the Protocol. The `BackendCapabilities` flags map 1:1 to user commands and gate cleanly via `tests/test_capability_gating.py`. No reshape proposed.
