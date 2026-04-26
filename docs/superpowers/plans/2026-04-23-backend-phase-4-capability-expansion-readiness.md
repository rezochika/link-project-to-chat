# Backend Phase 4 Capability Expansion Readiness Implementation Plan

> **2026-04-26 update:** Completed. The readiness package produced concrete follow-up slices, and Phase 4 is now shipped for the evidence-backed Claude + Codex scope.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 4 stub into an evidence-backed readiness package so the real post-Phase-3 capability-expansion work is planned from observed behavior instead of guesses.

**Architecture:** Phase 4 is intentionally not a coding phase yet. First capture soak evidence from the shipped Claude + Codex pair, then summarize validated gaps in dedicated docs, and only then mark the stub as ready or not ready for concrete follow-up implementation planning.

**Tech Stack:** Markdown docs, pytest, backend live tests, local CLI smoke checks

---

## File Map

| File | Change |
|------|--------|
| `docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md` | **NEW**: concrete list of observed gaps after the Phase 3 soak |
| `docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md` | **NEW**: side-by-side Claude/Codex capability matrix backed by evidence |
| `docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md` | **NEW**: trigger checklist and readiness decision |
| `docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md` | Update the stub with links to the evidence docs and a readiness status line |

---

### Task 1: Capture Phase 3 Soak Evidence

**Files:**
- Create: `docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md`
- Create: `docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md`
- Create: `docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md`

- [ ] **Step 1: Run the post-Phase-3 verification suite**

```bash
pytest tests/backends/test_env_policy.py tests/backends/test_capability_declaration.py tests/backends/test_codex_backend.py tests/backends/test_contract.py -v
pytest tests/backends/test_codex_live.py -m codex_live -v -s
pytest tests/test_backend_command.py tests/test_capability_gating.py tests/test_bot_streaming.py -v
```

Expected:
- Unit suites PASS
- Live Codex suite either PASSes on an authenticated machine or SKIPs cleanly
- Backend command and capability-gating tests still PASS after Codex support landed
- Known Phase 3 backend lifecycle regressions are fixed or covered by focused tests, including Codex subprocess cleanup after a successful `turn.completed`

- [ ] **Step 1a: Add or run a focused Codex subprocess cleanup regression check**

Before any readiness verdict, verify the Phase 3 Codex adapter still reaps its subprocess on the successful-turn path. This was fixed after Phase 3 review and must remain covered as a regression. The specific behavior to preserve is:

- `CodexBackend.chat_stream()` receives `turn.completed`
- It yields the closing `Result`
- It still reads/drains stderr as needed and awaits `proc.wait()` before clearing `_proc`
- A process that exits non-zero after a syntactically complete JSONL turn is not silently treated as a clean success unless that behavior is explicitly documented and accepted

Suggested focused test target:

```bash
pytest tests/backends/test_codex_backend.py -v
```

Expected:
- The suite includes a regression test proving successful Codex turns do not leave an unreaped process
- The regression test passes
- If the regression returns, record it in the gap inventory and mark Phase 4 `NOT READY`

- [ ] **Step 2: Run one direct CLI smoke check so stderr/noise is captured alongside the test results**

```powershell
$stderr = Join-Path $env:TEMP "phase4-codex-smoke-stderr.txt"
if (Test-Path $stderr) { Remove-Item $stderr }

$first = codex exec --json --sandbox read-only "Reply with exactly OK and do not run any commands." 2> $stderr
$sessionId = (
    $first |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.type -eq "thread.started" } |
    Select-Object -First 1
).thread_id
$resume = codex exec resume --json $sessionId "Reply with exactly AGAIN and do not run any commands." 2>> $stderr

$first
$resume
Get-Content $stderr | Select-Object -First 20
```

Expected:
- First and resumed turns both exit `0`
- Resume reuses the same `thread_id`
- Any stderr warnings are visible for documentation in the gap inventory

- [ ] **Step 3: Write the gap inventory from the observed failures, skips, and user-visible rough edges**

```markdown
# Backend Phase 4 Gap Inventory

## Validated capabilities that are already working
- Record each Phase 3 behavior that passed in Task 1 using one bullet per behavior.

## Candidate capability promotions
- Record only capabilities that have direct evidence from tests or live smoke.
- For each bullet, include the backend name, the user-visible command or feature, and the evidence source.

## Status/reporting gaps
- Record every missing `/status` detail that surfaced during testing.
- Include whether the missing field belongs to Claude, Codex, or both.

## Error-surface gaps
- Record every error or warning pattern that leaked through in a confusing way.
- Include the exact command or test that surfaced it.
- Include known Phase 3 lifecycle findings that were fixed and regression-tested, especially the Codex successful-turn path where `turn.completed` could previously yield `Result` before `proc.wait()`.

## Protocol fit concerns
- Record any place where `AgentBackend` or `BackendCapabilities` felt awkward in real use.
- If there were no protocol issues, write a single bullet saying that explicitly.
```

- [ ] **Step 4: Write the capability matrix from the same evidence**

```markdown
# Backend Phase 4 Capability Matrix

| Capability | Claude | Codex | Evidence |
|------------|--------|-------|----------|
| Resume/session reuse | yes or no | yes or no | Reference the exact test or smoke command |
| Live text streaming | yes or no | yes or no | Reference the exact test or smoke command |
| Thinking stream | yes or no | yes or no | Reference the exact test or smoke command |
| Model selection via command | yes or no | yes or no | Reference the exact test or smoke command |
| Permission switching via command | yes or no | yes or no | Reference the exact test or smoke command |
| Compact/session compression | yes or no | yes or no | Reference the exact test or smoke command |
| Usage-cap detection | yes or no | yes or no | Reference the exact test or smoke command |
| Provider-specific `/status` details | yes or no | yes or no | Reference the exact test or smoke command |
```

- [ ] **Step 5: Write the rollout review with a concrete readiness verdict**

```markdown
# Backend Phase 4 Rollout Review

## Trigger checklist
- [ ] Phase 3 has been opt-in for at least the agreed soak window
- [ ] Known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or explicitly accepted as non-blocking
- [ ] At least one capability promotion has direct evidence and a clear implementation path
- [ ] At least one `/status` improvement has direct user or test evidence
- [ ] At least one error-surface improvement has direct evidence from live or unit runs

## Evidence links
- Gap inventory: `docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md`
- Capability matrix: `docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md`

## Readiness decision
- Write `READY` only if two or more trigger checkboxes are checked.
- Write `NOT READY` if fewer than two trigger checkboxes are checked.
- Always write `NOT READY` if the known Phase 3 P1/P2 backend correctness checkbox is unchecked, even if two other trigger boxes are checked.
- Add one short paragraph explaining the decision in plain language.
```

- [ ] **Step 6: Commit the readiness docs**

```bash
git add docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md
git commit -m "docs: capture phase 4 backend readiness evidence"
```

---

### Task 2: Update The Stub Spec With Evidence Links And Readiness Status

**Files:**
- Modify: `docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md`

- [ ] **Step 1: Add an evidence section near the top of the Phase 4 stub**

```markdown
## 1. Current readiness status

Phase 3 follow-up evidence lives in:
- [Gap inventory](docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md)
- [Capability matrix](docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md)
- [Rollout review](docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md)

Readiness decision:
- `READY` if the rollout review checked two or more trigger boxes
- `NOT READY` otherwise
```

- [ ] **Step 2: Keep the rest of the stub intact, but replace the current sign-off with an explicit next action**

```markdown
## 5. Sign-off

If the rollout review is `NOT READY`, keep this file as a stub and continue gathering evidence.

If the rollout review is `READY`, do not add speculative code here. Start a new concrete implementation plan for the first validated Phase 4 slice, using the evidence docs above as the only source of truth.
```

- [ ] **Step 3: Run a quick docs sanity pass**

```bash
Select-String -Path docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md,docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md,docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md,docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md -Pattern ('T' + 'BD|TO' + 'DO|implement' + ' later')
```

Expected: no matches.

- [ ] **Step 4: Commit the stub update**

```bash
git add docs/superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md
git commit -m "docs: connect phase 4 stub to readiness evidence"
```

---

### Task 3: Make The Go Or No-Go Decision Explicit

**Files:**
- Modify: `docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md`

- [ ] **Step 1: Re-open the rollout review and write the final verdict in one sentence**

```markdown
## Final verdict

Phase 4 is `READY` for concrete implementation planning because two or more trigger conditions are satisfied.
```

Only use the `READY` verdict if known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or explicitly accepted as non-blocking.

Or, if the trigger threshold is not met:

```markdown
## Final verdict

Phase 4 is `NOT READY` for concrete implementation planning because fewer than two trigger conditions are satisfied.
```

Also use the `NOT READY` verdict if any known Phase 3 P1/P2 backend correctness finding remains unresolved, untested, and unaccepted, even when the trigger threshold is otherwise met.

- [ ] **Step 2: Add the exact next command the team should run**

If the verdict is `READY`:

```text
Start a fresh planning pass for the first validated slice, using:
docs/superpowers/specs/2026-04-23-backend-phase-4-gap-inventory.md
docs/superpowers/specs/2026-04-23-backend-phase-4-capability-matrix.md
docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md
```

If the verdict is `NOT READY`:

```text
Re-run the readiness plan after the next Codex soak window and append new evidence to the same three docs.
```

- [ ] **Step 3: Commit the verdict**

```bash
git add docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md
git commit -m "docs: record phase 4 backend readiness verdict"
```

---

## Phase 4 Readiness Self-Review Checklist

- [ ] The readiness package does not invent unsupported Codex capabilities.
- [ ] Every capability promotion candidate is backed by a concrete test or live smoke reference.
- [ ] Known Phase 3 P1/P2 backend correctness findings are fixed with regression coverage or recorded as explicit readiness blockers.
- [ ] The stub spec now points at the evidence docs instead of standing alone.
- [ ] The rollout review contains a plain `READY` or `NOT READY` decision.
- [ ] The next action is explicit for both outcomes.
