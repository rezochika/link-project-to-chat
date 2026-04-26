# Backend Abstraction Phase 3 — Codex Adapter (Conservative)

**Status:** Shipped (2026-04-26). Phase 4 later promoted Codex `/model`, `/effort`, and `/permissions` after additional CLI validation.
**Date:** 2026-04-23
**Part of:** Backend-abstraction track, spec #3 of 4.
**Depends on:** Spec #2 (backend-aware config + `/backend` + factory + capability gating).
**Blocks:** Spec #4 (capability expansion & hardening).

---

## 1. Overview

With spec #1 and #2 landed, the system has an `AgentBackend` Protocol, a registered `ClaudeBackend`, per-backend config, a `/backend` command, and capability-gated commands. This phase adds a second registered backend — `CodexBackend` — wrapping the `codex` CLI.

**This is the first phase that touches uncharted territory.** The original external v1.0 spec explicitly does not pin Codex CLI protocol details. Phase 3 begins with a **validation stage** to map the installed `codex` CLI's actual behavior (command flags, streaming format, session semantics, env-var requirements), then implements the adapter conservatively — declaring only capabilities that validation confirms.

Features Codex doesn't cleanly support are left off the `BackendCapabilities` declaration and handled by the spec #2 gating that already exists. Spec #4 expands coverage once the adapter is validated in real use.

## 2. Goals & non-goals

**Goals**
- Validate the installed Codex CLI: flags, streaming format, resume/session model, env-var requirements, exit/error codes. Produce a short findings doc.
- Implement `CodexBackend` in `backends/codex.py` + a parser in `backends/codex_parser.py`.
- Register `"codex"` with the backend factory.
- Declare `BackendCapabilities` for Codex conservatively — only what's validated.
- Implement per-backend env-var policy (the current blanket `OPENAI_*` scrub blocks Codex auth).
- Decide concurrent-task semantics when a project has multiple backends: per-backend cap, per-project cap, or global?
- Document a rollback plan: what to do if the Protocol proves wrong-shaped for Codex.

**Non-goals**
- No expansion of capabilities beyond what validation supports. (Spec #4.)
- No multi-backend-in-one-turn orchestration (e.g., routing a message to whichever backend is cheaper). Backend selection remains per-project.
- No UI for per-message backend override.
- No changes to `AgentBackend` Protocol unless validation forces one (if it does, the change is scoped here and documented).

## 3. Decisions driving this design

| # | Question | Decision |
|---|---|---|
| 1 | Should we validate Codex CLI before writing the adapter, or discover as we go? | **Validate first.** A one-day investigation stage produces the findings doc that the adapter is written against. Avoids re-architecting mid-implementation. |
| 2 | Which Codex CLI features are in scope for Phase 3? | The minimum viable set: send message, stream back text, get a final result, cancel. Everything else (thinking, tools, resume, compact) is declared `False` in capabilities unless validation shows it works identically to Claude. |
| 3 | How to handle per-backend env vars? | Move env scrubbing from `backends/claude.py` to a method on `AgentBackend` (default scrub patterns as a class attr per backend). Claude scrubs `OPENAI_*`; Codex does not. |
| 4 | `_MAX_CONCURRENT_RUNS = 3` — per-backend, per-project, or global? | **Per-project.** Concurrency is a project-level resource (project path, user expectation). Backend switches don't add capacity. |
| 5 | What happens when the Protocol turns out to be wrong-shaped for Codex (e.g., streaming format doesn't fit `AsyncGenerator[StreamEvent, None]`)? | **Scope the change here.** If the Protocol needs a new method or altered signature, the change lands in this spec, not the Claude phases. Spec #4 is the fallback scope-expansion venue. |
| 6 | Should we write a test harness that runs the real Codex CLI, or mock everything? | **Both.** Unit tests mock the subprocess (for CI). Integration tests run the real CLI but are marked `@pytest.mark.codex_live` and skipped by default — run manually before merging. |

## 4. Architecture

### 4.1 Stage A — Codex CLI validation

Before writing the adapter, produce `docs/superpowers/specs/2026-04-23-codex-cli-findings.md` that answers:

- **Installation**: which binary, which version, how installed, where configured.
- **Invocation**: command name and shape (e.g., `codex chat`, `codex exec`, flags, stdin behavior).
- **Streaming format**: stdout shape (JSON lines? SSE? plain text?). Whether partial-message deltas exist. How thinking/reasoning is surfaced (if at all).
- **Session model**: is there a resume flag? session-id concept? compact/summarize?
- **Model selection**: flag name, available model identifiers.
- **Tool/permission model**: does Codex expose file-edit permission modes? allowed-tools?
- **Env vars required**: auth token name, optional config vars.
- **Error surface**: stderr shape, rate-limit detection, exit codes.
- **Cancel behavior**: signal handling, process-group semantics.

This findings doc is the ground truth that the adapter is written against. Kept in-repo so spec #4 has the reference.

### 4.2 Stage B — Adapter implementation

Module layout:
```
src/link_project_to_chat/backends/
  codex.py           # CodexBackend + factory registration
  codex_parser.py    # CLI output → shared events
```

**`CodexBackend` skeleton:**
```python
# src/link_project_to_chat/backends/codex.py
from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import StreamEvent
from .base import AgentBackend, BackendCapabilities
from .factory import register


CODEX_CAPABILITIES = BackendCapabilities(
    models=(...,),                     # filled from validation findings
    supports_thinking=False,           # only True if findings confirm
    supports_permissions=False,        # only True if findings confirm
    supports_resume=False,             # only True if findings confirm
    supports_compact=False,            # only True if findings confirm
    supports_allowed_tools=False,      # only True if findings confirm
)


class CodexBackend:
    name = "codex"
    capabilities = CODEX_CAPABILITIES

    # Env-var policy: what to keep, what to scrub. Distinct from Claude's.
    _env_keep_patterns: tuple[str, ...] = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns: tuple[str, ...] = (
        "ANTHROPIC_*", "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict): ...

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[..., None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    async def chat(self, user_message: str, on_proc=None) -> str: ...
    def close_interactive(self) -> None: ...
    def cancel(self) -> bool: ...

    @property
    def status(self) -> dict: ...
```

The implementation mirrors `ClaudeBackend`'s structure (subprocess management, stderr collection, cancel-by-process-group) but uses the command shape and output parser derived from validation.

**`codex_parser.py`** converts Codex CLI output lines to shared events. At minimum it emits:
- `TextDelta` for each chunk of response text
- `Result` for terminal output
- `Error` for errors surfaced from stderr or exit code

`ThinkingDelta`, `ToolUse`, `AskQuestion` are emitted only if validation confirms equivalent Codex features exist.

**Factory registration** at module import:
```python
def _make_codex(project_path: Path, state: dict) -> CodexBackend:
    return CodexBackend(project_path, state)


register("codex", _make_codex)
```

Registration happens in `backends/__init__.py` so both backends are imported on package load.

### 4.3 Per-backend env-var policy

Problem ([backends/claude.py:262–267](src/link_project_to_chat/backends/claude.py) after spec #1 move):
```python
_SCRUB_PATTERNS = (
    "*_TOKEN", "*_KEY", "*_SECRET",
    "AWS_*", "OPENAI_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
)
```
Blanket scrubs `OPENAI_*`, which blocks Codex auth if Codex uses `OPENAI_API_KEY` (validation confirms).

**Solution.** Lift env-scrub policy onto the backend:

- Add class attributes `_env_keep_patterns` and `_env_scrub_patterns` to `AgentBackend` (via a `BaseBackend` abstract in `backends/base.py`, to share the env-prep helper).
- `ClaudeBackend._env_keep_patterns = ()` (keep nothing) and `_env_scrub_patterns` = current pattern list.
- `CodexBackend._env_keep_patterns = ("OPENAI_*", "CODEX_*")` and `_env_scrub_patterns = ("ANTHROPIC_*", "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*")` plus generic `*_TOKEN`/`*_KEY`/`*_SECRET`.
- The shared `_prepare_env()` helper in `BaseBackend` iterates the inherited env and **removes a variable only if it matches `_env_scrub_patterns` AND does not match `_env_keep_patterns`**. Keep takes precedence over scrub. Pseudocode:
  ```python
  def _prepare_env(self) -> dict:
      env = os.environ.copy()
      for key in list(env):
          if self._matches(key, self._env_keep_patterns):
              continue
          if self._matches(key, self._env_scrub_patterns):
              del env[key]
      return env
  ```
  Rationale: Codex needs `OPENAI_API_KEY`, which matches the generic `*_KEY` scrub. Declaring `OPENAI_*` as a keep pattern surgically preserves it without loosening the generic defense.

**Security implication.** Test coverage: verify that Claude subprocess envs still exclude `OPENAI_*` and Codex envs still exclude `ANTHROPIC_*`. Cross-contamination is an explicit regression to guard against.

### 4.4 Concurrency

`_MAX_CONCURRENT_RUNS = 3` lives in `task_manager.py` today. Decision: **per-project.** One `TaskManager` instance is already scoped to one project bot, and that bot has one active backend at a time (backend switching serializes). So the current cap already operates at project scope — no change is needed beyond confirming this in a comment.

If a user switches backend mid-conversation while tasks are running, the switch is rejected with "Cancel running tasks before switching backend." (Add to `/backend <name>` handler.)

### 4.5 Rollback plan

If, during implementation, validation findings reveal the Protocol doesn't fit Codex cleanly, two fallback strategies in priority order:

1. **Narrow the capability surface.** Declare every uncertain feature as `False` in `BackendCapabilities` and implement only what the Protocol handles naturally. Spec #4 revisits.
2. **Extend the Protocol.** Only if the streaming shape itself doesn't fit `AsyncGenerator[StreamEvent, None]`. Add a new method (e.g., `chat_stream_raw`) with a default that delegates to `chat_stream`. Claude keeps the default; Codex overrides. Document the extension in this spec's §9 before merging.

Option 1 is the default. Option 2 is only triggered by a concrete streaming-shape incompatibility, not by convenience.

**If rollback fails entirely** — Codex CLI is too structurally different to wrap — the fallback is to leave spec #3 unmerged and revisit after the Codex CLI stabilizes or after the Protocol is redesigned. Phase 1 and Phase 2 remain valuable on their own (they establish clean boundaries even if no second backend ships).

## 5. Testing strategy

### 5.1 Unit tests (CI)

- `tests/backends/test_codex.py` — mocked subprocess. Covers:
  - Command construction with/without session state.
  - Stream parsing: text deltas → `TextDelta`, terminal output → `Result`, error → `Error`.
  - Cancel: kills process group.
  - Env prep: includes `OPENAI_API_KEY`, excludes `ANTHROPIC_API_KEY`.
  - Contract test (from spec #1) parametrizes over `CodexBackend` with a fake subprocess.
- `tests/backends/test_env_policy.py` — cross-contamination regression: Claude env excludes `OPENAI_*`, Codex env excludes `ANTHROPIC_*`.
- `tests/backends/test_capability_declaration.py` — Codex capabilities match the findings doc's declared set. Prevents accidental capability creep.

### 5.2 Integration tests (manual)

Marked `@pytest.mark.codex_live`, skipped by default. Require `codex` CLI installed and authenticated:
- Send a single message, receive streamed response, verify terminal `Result`.
- Cancel a running turn; verify process group dies.
- Switch backend mid-conversation between Claude and Codex; verify each side's session survives.
- Env leak check: run both backends in the same Python process; inspect their environments (via `os.environ` capture hook) to confirm scrub policy is per-backend.

Add to CI via a separate job that can be enabled once a service account is set up. Skip in the default PR pipeline.

### 5.3 Regression

All spec #1 and #2 tests continue to pass. Contract test now parametrizes over three backends: `ClaudeBackend`, `CodexBackend`, `FakeBackend`.

## 6. Folded gap: concurrency

Resolved in §4.4. The existing per-project cap is correct; documented explicitly. Backend switch during live tasks is rejected with a clear message.

## 7. Folded gap: rollback plan

Resolved in §4.5. Priority-ordered fallback. Kept narrow to avoid widening scope.

## 8. Folded gap: env-var scrubbing

Resolved in §4.3. Per-backend policy via `BaseBackend` helper.

## 9. Migration & rollout

### 9.1 Commit sequence

1. **Validation stage.** Produce `docs/superpowers/specs/2026-04-23-codex-cli-findings.md`. No code change. Commit the findings doc.
2. **BaseBackend with env prep.** Extract shared env-prep helper from `ClaudeBackend`; move to `backends/base.py` as `BaseBackend` (ABC, not Protocol — the Protocol remains for callers; `BaseBackend` is for implementers who want the env helper). Claude adopts it. Green tests.
3. **Skeleton `CodexBackend`.** Module, class, registration, capability declaration (all `False`). No real subprocess yet. Green tests with mocked subprocess returning `[No response]`.
4. **Codex command construction.** Build the CLI command from validation findings. Test via mock subprocess.
5. **Codex stream parsing.** `codex_parser.py` translates output → events. Unit tests against captured fixture output from validation.
6. **Live integration.** Run one real `codex` round-trip; verify events flow. Commit a `@codex_live` test.
7. **Cancel + cleanup.** Process-group kill, close_interactive, cancel. Tests.
8. **Backend switch + concurrency rules.** `/backend codex` works end-to-end. Backend switch during live tasks rejected. Tests.
9. **Env policy cross-check.** `tests/backends/test_env_policy.py` passes. Manual smoke with both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` set in parent env.
10. **Update `CLAUDE.md`** — add Codex to "Current Development" and "Key modules" sections.

### 9.2 Release gating

Codex support is **opt-in** for this phase's release:
- The factory registers "codex" unconditionally.
- No config default changes: `default_backend` stays `"claude"`.
- Release notes describe Codex as "experimental, opt-in via `/backend codex`."
- Spec #4 is the venue for promoting it to default if real use validates stability.

## 10. Exit criteria

- [ ] Codex CLI findings doc exists and is referenced from this spec.
- [ ] `BaseBackend` with env-prep helper exists; `ClaudeBackend` uses it.
- [ ] `CodexBackend` and `codex_parser` implemented; registered in factory.
- [ ] `BackendCapabilities` for Codex matches findings (conservative — only confirmed features).
- [ ] Per-backend env policy tested (Claude excludes `OPENAI_*`; Codex excludes `ANTHROPIC_*`).
- [ ] Backend switch mid-live-task returns a clear rejection.
- [ ] Unit tests pass in CI.
- [ ] `@codex_live` integration tests pass on a machine with `codex` installed.
- [ ] Real round-trip through Codex from an actual project bot succeeds.
- [ ] Switching between Claude and Codex mid-conversation preserves each side's session.
- [ ] Release notes describe Codex as opt-in experimental.

## 11. Open questions

- **Does Codex support anything analogous to `--resume`?** Answer blocks `supports_resume` declaration. Resolved in validation stage.
- **Does Codex surface thinking/reasoning?** Answer blocks `supports_thinking`. Resolved in validation stage.
- **Is there a Codex-equivalent of `--compact`?** Answer blocks `supports_compact`. Resolved in validation stage.
- **Should the `/model` list for Codex include aliases for OpenAI model IDs (e.g., `gpt-5-codex`) or short names?** Deferred to the `CodexBackend` implementer once findings are known; record the choice in the findings doc.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Codex CLI protocol changes between the time findings are captured and the adapter ships | Findings doc is dated; adapter is pinned to a Codex version in the release notes; smoke test is run immediately before merge |
| Env policy cross-contamination leaks credentials | Dedicated `test_env_policy.py` with explicit cross-checks; runs in CI |
| Real-Codex integration test requires credentials CI doesn't have | Mark tests `@codex_live`, skipped by default; document how to run manually |
| Protocol doesn't fit Codex and rollback §4.5 is insufficient | Spec #3 can be left unmerged; phases 1 + 2 ship independently and are valuable even without Codex |
| User switches to Codex, sessions don't survive, loses conversation state | Backend-state is persisted per-provider before activation; switch-with-live-tasks is rejected to avoid mid-turn loss |
| Codex adapter CPU/memory cost different enough to destabilize TaskManager limits | Per-project cap (§4.4) is unchanged; monitor in the opt-in release and raise if real use shows trouble |
