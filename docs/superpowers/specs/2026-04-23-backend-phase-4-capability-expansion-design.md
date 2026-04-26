# Backend Abstraction Phase 4 — Capability Expansion & Hardening

**Status:** Shipped — evidence-backed Claude + Codex capability expansion complete (2026-04-26).
**Originally drafted:** 2026-04-23 as a deliberately under-specified stub pending Phase 3 outcomes.
**Part of:** Backend-abstraction track, spec #4 of 4.
**Depends on:** Spec #3 (Codex adapter shipped opt-in, 2026-04-26).

---

## 1. Current readiness status

**Verdict: `COMPLETE` for the evidence-backed Claude + Codex scope as of the Phase 4 completion slice (2026-04-26).**

Phase 3 follow-up evidence lives in:
- [Gap inventory](2026-04-23-backend-phase-4-gap-inventory.md)
- [Capability matrix](2026-04-23-backend-phase-4-capability-matrix.md)
- [Rollout review](2026-04-23-backend-phase-4-rollout-review.md)

The readiness decision rule was: `READY` when the rollout review checks two or more trigger boxes (with the P1/P2 fixes box as gating prerequisite). Phase 4 is now complete for the concrete gaps that surfaced: P1/P2 fixes, error-surface hardening, capability promotion, `/status` reporting, and documentation alignment.

**Validated slices shipped:**
- `93f8b9c` — Codex `/model` (5 cached GPT-5 slugs) + `/effort` (low/medium/high/xhigh) with `-c model_reasoning_effort=<level>` config override on the CLI command. Triggered by a real user-reported gap (`/model` rejected as "doesn't support /model" even though Codex CLI accepts `--model <slug>`). Adds `supports_effort` and `effort_levels` to `BackendCapabilities`; lifts `effort` to the `AgentBackend` Protocol.
- `e2e2143` — `/backend` is now a button picker; the four-form switch logic is shared between the typed-arg and button paths via an extracted `_switch_backend` helper.
- `7245199` — `/status` reports effort, request count, last turn duration, and Codex token usage.
- `d0e4b97` — `/status` resolves friendly model labels from provider wire identifiers.
- `2b1dba6` — backend-level `current_permission()` / `set_permission()` generalizes permissions; Codex `/permissions` maps to CLI sandbox controls.
- Phase 4 completion slice — `/status` adds permissions, Claude allowed/disallowed tools, Claude usage-cap state, and last backend error for both backends.

No further Phase 4 slice is planned without new evidence. Codex thinking, compact, allowed-tools, and usage-cap detection remain disabled because the observed CLI surface does not support them.

## 2. Why this spec was originally a stub (and what changed)

Phase 3 shipped `CodexBackend` with **conservative capabilities**: only features validated against the installed Codex CLI were declared `True`; the rest were declared `False` and gated out by the spec #2 capability machinery.

Phase 4's purpose was to **expand that surface once the adapter had been used in practice and the gaps were known**. Writing a detailed Phase 4 spec at the time of Phase 3 design would have forced guesses about:

- Which Codex features Phase 3 left unsupported and why.
- Whether those gaps come from Codex CLI limitations (won't change) or adapter conservatism (fixable).
- Whether real Codex usage would reveal error-surface issues (stderr noise, rate-limit shape, cancel edge cases) that need adapter-side hardening.
- Whether the Protocol would need a second-pass extension.
- Whether `default_backend` should be configurable via command surface.

These questions were answered the day Phase 3 shipped, faster than the design anticipated:

- **Codex CLI accepts `--model <slug>` and `~/.codex/models_cache.json` enumerates the visible models.** Phase 3's `models = ()` was over-conservative; the cache file makes the promotion mechanical. Slice 1 shipped this on 2026-04-26.
- **Codex CLI accepts `-c model_reasoning_effort=<level>`** for `low/medium/high/xhigh`. Live-CLI-verified before commit. `effort` was a Claude-only tier-2 attribute; Phase 4's first slice promotes it to a real `BackendCapabilities` flag with per-backend `effort_levels`. Per-backend levels handle Claude's extra `max` cleanly.
- **Adapter-side hardening surfaced one real bug** (post-`turn.completed` proc not reaped, fixed in `7bbbbd3` with regression tests) and one config-layer bug (partial team entries crashing the manager loader, fixed in `ceca7ca`). Both pre-empted in the same day Phase 3 shipped.
- **Protocol second-pass was minor:** lifted `model_display` (Phase 3 review fix `f73b43e`), `effort` (Phase 4 slice 1), and permission accessors (`2b1dba6`) to backend-level Protocol surface. No broader Protocol reshaping was needed.
- **`default_backend` command:** `/backend` button picker (Phase 4 slice 1, `e2e2143`) addresses the muscle-memory part of this question. A separate `/default_backend` for the manager-bot global default is still future work.

The final state and intentionally unsupported Codex capabilities are documented in the [gap inventory](2026-04-23-backend-phase-4-gap-inventory.md).

## 3. Provisional scope (to be validated after Phase 3)

Likely contents, each contingent on Phase 3 findings:

### 3.1 Capability expansion
For each `BackendCapabilities` field that Phase 3 declared `False` for Codex:
- Re-examine whether Codex can support it (direct CLI feature, or via adapter-side emulation).
- Expand the capability declaration.
- Add tests.

### 3.2 `/status` reporting
The original v1.0 spec (§9.2) requires `/status` to report provider-specific status. Phase 2 does the minimum; Phase 4 refines:
- What model is currently active.
- Session size / token usage (if the backend exposes it).
- Rate-limit state (if detectable).
- Any adapter-internal state worth surfacing.

### 3.3 Error surface review
After real Codex use, consolidate error handling:
- Are there Codex-specific stderr patterns that should map to cleaner Telegram messages?
- Is there a Codex analogue of Claude's usage-cap detection ([claude.py:41–66](src/link_project_to_chat/backends/claude.py))?
- Are cancel edge cases surfacing as generic errors when they should be silent?

### 3.4 Documentation
- User-facing: how to switch backends, what each supports, when to use which.
- Developer-facing: how to add a third backend. This is the test of whether the abstraction is right — if it's painful, fix the abstraction here.

### 3.5 Possibly: default-backend command
If `default_backend` needs to change without config-edit (spec #2 §10 open question), add `/default_backend <name>` at the manager-bot level.

### 3.6 Possibly: Protocol second-pass
If Phase 3's rollback §4.5 triggered Option 2 (Protocol extension), Phase 4 reviews whether the extension is still the right shape or should be reshaped now that Codex integration has stabilized.

## 4. Triggers that prompt writing the full spec

Write the full Phase 4 spec when at least two of:
- Phase 3 has been opt-in for ≥2 weeks and users have reported concrete gaps.
- A capability Phase 3 declared `False` has a clear path to `True`.
- `/status` reporting has a concrete user-asked-for improvement.
- A rate-limit or error pattern has hit real users and needs handling.

Writing the full spec earlier risks over-designing for hypothetical usage.

## 5. What stays out of Phase 4

- **No new transports.** That's the transport-abstraction track, not this one.
- **No third backend.** Phase 4 is about polishing the Claude + Codex pair, not adding more.
- **No redesign of Phase 1's Protocol** unless Phase 3 already forced a change. Abstraction stability matters.
- **No user-mode "auto-route to cheapest backend" features.** Out of scope for this track.

## 6. Sign-off

The rollout review reached `READY`, and the evidence-backed Phase 4 scope is now shipped: Codex `/model`, `/effort`, `/permissions`, `/backend` picker UX, provider-aware `/status`, model-label cleanup, and backend status/error reporting.

Do not add speculative code to this spec. Any future slice needs fresh evidence: a concrete user report, a newly observed Codex CLI capability, or a real error/rate-limit pattern.

### Shipped slices

| Slice | Commit(s) | Trigger | What landed |
|-------|-----------|---------|-------------|
| 1 — `/model` + `/effort` for Codex; `/backend` button picker | `93f8b9c`, `e2e2143` | User-reported `/model` gap | `CODEX_CAPABILITIES.models` = 5-tuple; `BackendCapabilities.supports_effort`/`effort_levels`; `effort` lifted to Protocol; per-backend `MODEL_OPTIONS`; `_switch_backend` helper; button-picker UX for `/backend`.
| 2 — Provider-aware `/status` | `7245199`, `d0e4b97`, completion slice | Phase 4 reporting gaps | `/status` shows effort, requests, last duration, Codex tokens, friendly model labels, permissions, Claude tool allow/deny lists, usage-cap state, and last backend error. |
| 3 — Codex `/permissions` | `2b1dba6` | Capability promotion after CLI mapping review | Backend-level permission accessors; Codex maps `plan`, auto/edit modes, and bypass modes to CLI sandbox/approval flags. |

### Conditional Future Work

1. Codex usage-cap detection if a real stderr/stdout pattern emerges from ChatGPT-tier traffic.
2. Manager-level `/default_backend` command if config-edit-only default backend changes prove friction-heavy.
3. Any new Codex capability if a future CLI release exposes thinking, compact/session compression, or allowed-tools controls.
