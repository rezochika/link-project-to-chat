# PM Analysis — Backend Abstraction Phases 1–4

_Analyzed: 2026-04-23 | Branch: `feat/transport-abstraction`_

## 1. Goal

Extract Claude-specific runtime details behind an `AgentBackend` Protocol, then add Codex as an opt-in second backend, enabling per-project backend selection and capability-aware command gating without breaking existing Claude behavior.

## 2. Phase Summary

| Phase | Name | Scope | Risk | Effort |
|-------|------|-------|------|--------|
| **1** | Claude Extraction | `ClaudeClient` → `ClaudeBackend` Protocol impl; split `stream.py` → `events.py` + parser; inject backend into `TaskManager`; rename `claude_*` to agent-neutral names | **Low** (refactor, zero behavior change) | **M** |
| **2** | Config & `/backend` | `ProjectConfig.backend` + `backend_state`; migrate 22 call sites; `/backend` command; capability-gate `/thinking` `/permissions` `/compact` `/model` | **Med** (config migration, many sites) | **L** |
| **3** | Codex Adapter | Validate real Codex CLI; implement `CodexBackend` + JSONL parser; opt-in registration; per-backend env scrub policy | **High** (unknown CLI behavior) | **M** |
| **4** | Capability Readiness | Post-soak review; expand capability declarations; `/status` improvements; third-backend docs. Approval-gated. | **Low** | **S** |

## 3. Architecture

**Protocol:** `AgentBackend` (src/link_project_to_chat/backends/base.py) — 8 methods, 4 attributes.

**New modules:**
- `backends/base.py` — `AgentBackend`, `BackendCapabilities`, `HealthStatus`, `BaseBackend`
- `backends/factory.py` — registry
- `backends/claude.py` — moved from `claude_client.py`
- `backends/claude_parser.py` — moved from `stream.py`
- `backends/codex.py` + parser — Phase 3
- `events.py` — shared `StreamEvent` dataclasses

**Coexistence model:** one active backend per bot; each backend carries its own `model`, `session_id`, `effort`, `permissions` inside `backend_state[<name>]`. Capability flags drive command gating. Env scrub policy is per-backend (Claude scrubs `OPENAI_*`; Codex scrubs `ANTHROPIC_*`).

**Tier-2 escape hatch:** `ProjectBot._claude` property asserts the active backend is `ClaudeBackend` for Claude-specific attrs (`effort`, `append_system_prompt`). Phase 2 capability gates make these unreachable when Codex is active.

## 4. Ordering & Dependencies

- **1 → 2 → 3 is a hard sequence.** 2 needs the Protocol; 3 needs Phase 2 gating.
- **4 is evidence-gated.** Scope determined by Phase 3 soak findings (2+ weeks).
- **No circular deps.** Factory → backends is unidirectional; task_manager accepts injected backend.
- **Each phase is independently shippable** (Phase 3 is opt-in by design).

## 5. Gaps & Concerns

### Specification
1. **Phase 3 rollback plan is narrow.** No explicit "what if Codex streaming is fundamentally incompatible with the Protocol" branch. Mitigated by mandatory validation stage before adapter code.
2. **Env scrub cross-contamination test is Phase 3-only.** Phase 2 could ship without ensuring a new backend can't inherit the wrong pattern. Suggest adding a Phase 2 unit test.
3. **Manager-bot migration call sites (22) aren't enumerated.** Phase 2 §4.7 implies them — explicit list reduces regression risk.
4. **Group/relay audit is "grep-pass" only.** Findings not committed; could be re-discovered later.

### Implementation
5. **Phase 1 identifier rename touches 14+ `bot.py` sites.** Mechanical but numerous; commit as its own step with grep verification.
6. **Config dual-write must stay in sync.** Phase 2 writes both `backend_state["claude"]` and legacy flat keys. Single-source-of-truth rule is correct; missed call site = stale state.
7. **Session helper re-routing in Phase 2.** `save_session`/`clear_session`/`patch_project`/`patch_team` routed to new `patch_backend_state`. Regression risk if any caller keeps old path. Needs grep lockout test.

### Missing acceptance criteria
8. **Phase 1 exit criteria lack "one real Claude smoke test round-trip"** after the module moves. Protocol correctness ≠ working behavior.
9. **Phase 2 exit criteria don't include "team-bot `/backend` switch works and persists"** — the test is mentioned in body but not checklist.

## 6. Readiness — Can dev bot start Phase 1 today?

**Yes. Go-ahead blockers: none.**

Phase 1 is pure refactoring. All needed info is in the codebase. Plan is broken into 5 tasks with code snippets, test names, and commit messages. TDD discipline enforced at each step. Checkpoints allow stop-and-review between tasks.

**Open questions that do NOT block Phase 1:**
- Is the Protocol shape right? — Phase 2 confirms when first capability gate is implemented.
- Will Codex fit the Protocol? — Phase 3 validation stage answers this.

**One caveat:** The spec was written 2026-04-23. If `task_manager.py` or `bot.py` have diverged since then (e.g., the 2026-04-22 remediation on `main` touched both), the code diffs in the plan may need adjustment. Worth a sanity-check pass before the dev bot starts.

## Recommended Next Steps

1. **Rebase `feat/transport-abstraction` onto latest `main`** so the 2026-04-22 security remediation is included before Phase 1 implementation starts.
2. **Start Phase 1 Task 1** (split `stream.py` into `events.py` + `claude_parser.py`) — smallest, lowest risk, proves the structure works before committing to the full rename.
3. **Before Phase 2 starts**, enumerate the 22 call sites explicitly in the plan.
4. **Before Phase 3 starts**, complete Codex CLI validation and post findings to `docs/`.

---

_PM: @lptc_mgr_claude_bot_
