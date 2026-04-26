# Backend Abstraction Phase 4 — Capability Expansion & Hardening

**Status:** Stub (2026-04-23). Intentionally under-specified pending Phase 3 outcomes.
**Date:** 2026-04-23
**Part of:** Backend-abstraction track, spec #4 of 4.
**Depends on:** Spec #3 (Codex adapter shipped opt-in).

---

## 1. Current readiness status

Phase 3 follow-up evidence lives in:
- [Gap inventory](2026-04-23-backend-phase-4-gap-inventory.md)
- [Capability matrix](2026-04-23-backend-phase-4-capability-matrix.md)
- [Rollout review](2026-04-23-backend-phase-4-rollout-review.md)

Readiness decision:
- `READY` if the rollout review checked two or more trigger boxes
- `NOT READY` otherwise

## 2. Why this spec is a stub

Phase 3 ships `CodexBackend` with **conservative capabilities**: only features validated against the installed Codex CLI are declared `True`; the rest are declared `False` and gated out by the spec #2 capability machinery.

Phase 4's purpose is to **expand that surface once the adapter has been used in practice and the gaps are known**. Writing a detailed Phase 4 spec now would force guesses about:

- Which Codex features Phase 3 left unsupported and why.
- Whether those gaps come from Codex CLI limitations (won't change) or adapter conservatism (fixable).
- Whether real Codex usage revealed error-surface issues (stderr noise, rate-limit shape, cancel edge cases) that need adapter-side hardening.
- Whether the Protocol needs a second-pass extension.
- Whether `default_backend` should be configurable via command surface.

These questions are **answerable only after Phase 3 is implemented and exercised**. Attempting to answer them now produces a spec that either repeats Phase 3 content or drifts from reality.

The full Phase 4 spec is therefore **written after Phase 3 lands**, using this stub as the scope skeleton.

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

If the rollout review is `NOT READY`, keep this file as a stub and continue gathering evidence.

If the rollout review is `READY`, do not add speculative code here. Start a new concrete implementation plan for the first validated Phase 4 slice, using the evidence docs above as the only source of truth.
