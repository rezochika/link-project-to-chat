# Backend Abstraction Phase 1 — Claude Extraction Behind `AgentBackend`

**Status:** Designed (2026-04-23). Not yet implemented.
**Date:** 2026-04-23
**Part of:** Backend-abstraction track, spec #1 of 4. Stacks on the `feat/transport-abstraction` branch. Refines the external v1.0 draft (`Codex_CLI_Architecture_Spec_Link_Project_to_Chat.docx`, 2026-04-23) into actionable phases.
**Depends on:** `feat/transport-abstraction` landing (bot.py is already decoupled from Telegram there; this spec decouples it from Claude).
**Blocks:** Spec #2 (backend-aware config + `/backend` command).

---

## 1. Overview

The codebase currently instantiates [`ClaudeClient`](src/link_project_to_chat/claude_client.py) directly from [`TaskManager`](src/link_project_to_chat/task_manager.py) ([line 18 import](src/link_project_to_chat/task_manager.py:18)) and uses a single Claude-specific stream parser ([`parse_stream_line`](src/link_project_to_chat/stream.py:61)). Adding a second CLI (Codex) cleanly requires a backend interface.

This spec extracts the existing Claude behavior behind a new `AgentBackend` Protocol, splits the stream module into shared event types + a Claude parser, and swaps `TaskManager`'s direct construction for dependency injection. **No behavior change**: existing Claude users observe no difference.

**The deliverable is the Protocol, the `ClaudeBackend` implementation of it, and the parser split. No new commands, no config schema changes, no Codex code.**

## 2. Goals & non-goals

**Goals**
- Define `AgentBackend` Protocol covering the methods `TaskManager` and `ProjectBot` call on `ClaudeClient` today.
- Move `ClaudeClient` to `backends/claude.py` and expose it as an `AgentBackend`.
- Split [`stream.py`](src/link_project_to_chat/stream.py) into shared event dataclasses (`events.py`) + `backends/claude_parser.py`.
- Inject an `AgentBackend` into `TaskManager` instead of constructing one internally.
- Add a `FakeBackend` test double and a contract test (parametrized over backends) in `tests/backends/`.
- Audit [`skills.py`](src/link_project_to_chat/skills.py) and record the skill-scope decision (see §6).

**Non-goals**
- No `backend` field in config; no per-provider `backend_state`. (Deferred to spec #2.)
- No `/backend` command, no capability gating of `/thinking`, `/permissions`, `/compact`. (Deferred to spec #2.)
- No Codex code. (Deferred to spec #3.)
- No rename of user-facing strings ("Chatting with Claude…" in help text stays as-is for this phase).
- No changes to env-var scrubbing.
- Feature additions of any kind.

## 3. Decisions driving this design

Outcomes from brainstorming on 2026-04-23:

| # | Question | Decision |
|---|---|---|
| 1 | Decomposition strategy? | By phase (matches original spec's 4-phase rollout and the repo's per-concern design-doc pattern) |
| 2 | How does this sequence with the transport refactor? | Stack on `feat/transport-abstraction` — its bot.py decoupling is a prerequisite this phase builds on |
| 3 | Where do the gaps from analysis land? | Fold into the phase they fit — skills audit belongs here, manager/relay in #2, concurrency + rollback in #3 |
| 4 | What does "zero behavior change" mean here? | Functionally equivalent from the user's perspective; internal restructuring (parser split, module moves) is allowed since it's invisible |
| 5 | Are skills/personas backend-scoped or shared? | Shared — skills are prompt text, not CLI-specific (see §6 for rationale and audit) |

## 4. Architecture

### 4.1 Module layout after this phase

```
src/link_project_to_chat/
  backends/
    __init__.py
    base.py              # AgentBackend Protocol, BackendCapabilities dataclass
    claude.py            # ClaudeBackend (moved from claude_client.py; implements AgentBackend)
    claude_parser.py     # parse_stream_line (moved from stream.py)
  events.py              # StreamEvent, TextDelta, ThinkingDelta, ToolUse, AskQuestion,
                         # Result, Error, Question, QuestionOption (moved from stream.py)
  stream.py              # Shim: re-exports from events + backends/claude_parser for
                         # backward-compat with unported callers. Deleted in spec #2.
  task_manager.py        # accepts AgentBackend via constructor; no direct claude_client import
  bot.py                 # constructs ClaudeBackend via a tiny helper; no other changes
  claude_client.py       # DELETED — contents moved to backends/claude.py
```

### 4.2 `AgentBackend` Protocol

```python
# src/link_project_to_chat/backends/base.py
from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..events import StreamEvent


@dataclass(frozen=True)
class BackendCapabilities:
    """Static declaration of what the backend supports.

    Phase 1 uses this to describe Claude's capabilities and to gate the
    cap-probe loop in bot.py. Capability-based gating of user-facing
    commands (/thinking, /permissions, /compact) is introduced in spec #2.
    """
    models: tuple[str, ...]
    supports_thinking: bool
    supports_permissions: bool
    supports_resume: bool
    supports_compact: bool
    supports_allowed_tools: bool
    supports_usage_cap_detection: bool


@dataclass(frozen=True)
class HealthStatus:
    """Result of `AgentBackend.probe_health()`. See §4.7."""
    ok: bool
    usage_capped: bool
    error_message: str | None = None


class AgentBackend(Protocol):
    """A selectable AI CLI provider.

    Phase 1: only ClaudeBackend implements this. Phase 3 adds CodexBackend.
    """

    name: str                          # "claude", "codex", …
    capabilities: BackendCapabilities
    project_path: "Path"               # required so TaskManager can reuse it
    model: str | None
    session_id: str | None

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[..., None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[..., None] | None = None,
    ) -> str: ...

    async def probe_health(self) -> HealthStatus: ...

    def close_interactive(self) -> None: ...
    def cancel(self) -> bool: ...

    @property
    def status(self) -> dict: ...
```

**Shape rationale.** The method set is exactly what `TaskManager` and `ProjectBot` call on `ClaudeClient` today; the Protocol is extracted from real usage, not designed aspirationally. Attributes `model` and `session_id` remain on the backend instance (mutable) because `/model` and `/compact` currently mutate them and their Phase 2 equivalents will continue to. The `capabilities` field is declared now but unused in this phase — capability-based gating of commands lands in spec #2.

### 4.3 `ClaudeBackend` — minimal surface change

`claude_client.py` → `backends/claude.py`. The class rename `ClaudeClient` → `ClaudeBackend` happens in the same commit as the move. Existing attributes/methods that fit the Protocol keep their current signatures:

- `chat_stream`, `chat`, `close_interactive`, `cancel` — already match.
- `status` — already a property-like method; formalize as `@property`.
- `name = "claude"` class attribute added.
- `capabilities = BackendCapabilities(models=MODELS, supports_thinking=True, supports_permissions=True, supports_resume=True, supports_compact=True, supports_allowed_tools=True, supports_usage_cap_detection=True)` class attribute added.
- `probe_health()` method added — spawns a detached `ClaudeClient`, sends `"ping"`, returns `HealthStatus(ok, usage_capped, error_message)` using the existing `is_usage_cap_error` check.

Claude-specific helpers that are not part of the Protocol (e.g., `EFFORT_LEVELS`, `MODELS`, `PERMISSION_MODES`, `is_usage_cap_error`, `ClaudeStreamError`, `ClaudeUsageCapError`, `_TELEGRAM_AWARENESS`, `_ASK_DISMISSED_HINT`) stay inside `backends/claude.py` and are re-exported from a module-level `__all__` for callers that still need them. `bot.py` currently imports some of these constants directly; those imports switch from `claude_client` to `backends.claude` but remain Claude-specific. **They are renamed/generalized in spec #2, not here.**

### 4.4 Stream/event split

`stream.py` today mixes event dataclasses with a Claude-specific parser. Phase 1 splits them:

- **`events.py`** (new) — all dataclasses: `StreamEvent`, `TextDelta`, `ThinkingDelta`, `ToolUse`, `Result`, `Question`, `QuestionOption`, `AskQuestion`, `Error`. Zero Claude-specific logic.
- **`backends/claude_parser.py`** (new) — `parse_stream_line` moved here verbatim. Imports events from `..events`.
- **`stream.py`** (kept temporarily) — becomes a shim:
  ```python
  from .events import *  # noqa: F401,F403
  from .backends.claude_parser import parse_stream_line  # noqa: F401
  ```
  This keeps any test or caller that still imports from `stream` working. **The shim is deleted in spec #2 after call sites are updated.**

### 4.5 `TaskManager` — full rename to agent-neutral identifiers

Today ([task_manager.py:18](src/link_project_to_chat/task_manager.py:18)):
```python
from .claude_client import ClaudeClient
```
`TaskManager` currently constructs/owns `ClaudeClient` instances and exposes a mesh of Claude-named identifiers. A grep of [task_manager.py](src/link_project_to_chat/task_manager.py) confirms the following must rename:

| Current | After this phase |
|---|---|
| `self._claude` (attr, line 174) | `self._backend` |
| `self._claude_owner_task_id` (line 181) | `self._backend_owner_task_id` |
| `self.claude` (property, line 185) | `self.backend` |
| `_acquire_claude_slot` (line 187) | `_acquire_backend_slot` |
| `_release_claude_slot` (line 192) | `_release_backend_slot` |
| `_close_claude_interactive` (line 196) | `_close_backend_interactive` |
| `_cleanup_cancelled_claude_task` (line 203) | `_cleanup_cancelled_agent_task` |
| `_exec_claude` (line 275) | `_exec_agent_turn` |
| `_run_claude_turn` (line 317) | `_run_agent_turn` |
| `submit_claude` (line 228) | `submit_agent` |
| `TaskType.CLAUDE` | `TaskType.AGENT` |

**Rationale for renaming in Phase 1 (not later):** after the transport refactor already decoupled UI, backend identity is the only remaining layer still pinned to "Claude". Leaving Claude-named identifiers across `TaskManager` and `bot.py` while the Protocol exists creates a misleading split where the *interface* is agent-neutral but the *implementation sites* claim it's a Claude. Phase 2 would then have to touch all these sites *again* for capability gating — doubling the diff. Better to rename once, now.

`TaskManager.__init__` takes `backend: AgentBackend` (required, no default) and stores it as `self._backend`. Public construction is done via a helper in `backends/__init__.py`.

`bot.py` is the construction site: it creates a `ClaudeBackend` (using the existing config shape — flat `model`, `effort`, `permissions`, `session_id`, `show_thinking` fields — unchanged from today) and passes it to `TaskManager`. A thin helper `_make_backend_from_legacy_config(project)` in `backends/__init__.py` centralizes this so spec #2 has one place to expand when backend-aware config arrives.

### 4.6 `bot.py` — Claude-identifier scrub

Grep-audit of [bot.py](src/link_project_to_chat/bot.py) produces three categories. Phase 1 handles categories A and B; category C moves to Phase 2.

**Category A — Direct attribute access via `task_manager.claude`.** ≥20 occurrences (lines 493, 497, 844, 852, 856, 875, 949, 1020, 1051, 1112, 1120, 1394, 1395, 1405, 1426, 1427, …). All rename to `task_manager.backend` in lockstep with the `TaskManager` rename. Every call is still legal against the Protocol because `session_id`, `model`, `effort`, `skip_permissions`, `permission_mode`, `model_display`, `append_system_prompt`, `team_system_note` are all preserved as instance attributes on `ClaudeBackend`.

**Category B — Direct imports from `claude_client`.** [bot.py:32](src/link_project_to_chat/bot.py:32) imports `EFFORT_LEVELS, MODELS, PERMISSION_MODES, ClaudeStreamError, is_usage_cap_error`. Action in Phase 1:
- Move imports from `claude_client` → `backends.claude` (module path change only).
- Keep them Claude-typed for now — these are legitimately Claude-specific values until the capability-aware generalization in spec #2.
- Leaves the user-visible behavior unchanged while severing the dead-module dependency (`claude_client.py` is deleted in spec #2).

**Category C — User-facing "Claude" strings.** Lines 43 ("Set Claude model"), 575 ("chat with Claude"), and similar. **Not touched in Phase 1.** These need to say the active backend's name ("Claude" or "Codex"), which requires spec #2's capability-aware plumbing. Changing them now either hardcodes "Claude" (wrong later) or introduces a branch that has no second arm yet. Left for Phase 2 §4.8.

### 4.7 Backend health probe — move `_schedule_cap_probe` off `ClaudeClient`

Today ([bot.py:420–442](src/link_project_to_chat/bot.py:420)):
```python
def _schedule_cap_probe(self, chat: ChatRef, interval_s: int = 1800) -> None:
    async def _probe() -> None:
        from .claude_client import ClaudeClient
        while self._group_state.get(chat).halted:
            await asyncio.sleep(interval_s)
            ...
            probe = ClaudeClient(project_path=self.path)
            result = await probe.chat("ping")
            if not result.startswith("Error:") and not is_usage_cap_error(result):
                self._group_state.resume(chat)
                ...
```
This bypasses the backend abstraction entirely — `bot.py` lazy-imports `ClaudeClient` and instantiates it directly. Once Codex is selectable, the probe only knows how to probe Claude.

**Fix in Phase 1.** Add a probe method to the Protocol:

```python
# backends/base.py
class AgentBackend(Protocol):
    ...
    async def probe_health(self) -> HealthStatus: ...
```

Where `HealthStatus` is:
```python
@dataclass(frozen=True)
class HealthStatus:
    """Result of a lightweight backend probe.

    usage_capped: True if the backend reported a usage-cap / rate-limit condition.
    ok: True if the probe completed without error and without a cap.
    error_message: scrubbed one-line description if the probe errored.
    """
    ok: bool
    usage_capped: bool
    error_message: str | None = None
```

`ClaudeBackend.probe_health` does what `_schedule_cap_probe` does internally today: spawns a detached `ClaudeClient` (its own subprocess, separate from the main interactive turn, to avoid stdin contention), sends `"ping"`, runs the existing `is_usage_cap_error` check on the result, returns a `HealthStatus`. Error and usage-cap surfaces are **unchanged from today** — the probe is moved, not reshaped.

`bot.py` becomes:
```python
def _schedule_cap_probe(self, chat: ChatRef, interval_s: int = 1800) -> None:
    async def _probe() -> None:
        while self._group_state.get(chat).halted:
            await asyncio.sleep(interval_s)
            if not self._group_state.get(chat).halted:
                return
            status = await self.task_manager.backend.probe_health()
            if status.ok and not status.usage_capped:
                self._group_state.resume(chat)
                await self._send_to_chat(chat_id, "Usage cap cleared. Resumed.")
                return
    ...
```

The lazy `from .claude_client import ClaudeClient` goes away; `bot.py` stops knowing which CLI is underneath.

**Capability declaration.** `BackendCapabilities` gains one field in Phase 1:
```python
supports_usage_cap_detection: bool
```
`ClaudeBackend` declares `True` (it has `is_usage_cap_error` and the cap-probe path already works). Spec #2 uses this in `_schedule_cap_probe` to skip the probe when the active backend doesn't support cap detection. Spec #3 will set it `False` for `CodexBackend` until validation confirms an equivalent exists.

## 5. Testing strategy

### 5.1 Contract test

New file: `tests/backends/test_contract.py`. Parametrized over all registered backends (today: only `ClaudeBackend`). For each:
- Accepts a `user_message: str` on `chat_stream` and yields `StreamEvent` instances.
- `chat()` returns a string when a terminal `Result` event is yielded.
- `chat()` raises when a terminal `Error` event is yielded.
- `probe_health()` returns a `HealthStatus` and does not interfere with an in-flight `chat_stream` on the same backend instance.
- `cancel()` is idempotent.
- `close_interactive()` is safe to call when no process is live.

A `FakeBackend` test double lives in `tests/backends/fakes.py` and is included in the parametrization. Modeled on `FakeTransport` ([transport/fake.py](src/link_project_to_chat/transport/fake.py), already established by the transport abstraction).

### 5.2 Regression tests

- Every existing `claude_client` / `task_manager` / `bot` test continues to pass (updated for the rename — the test bodies must reference `backend` instead of `claude`, but assertions don't change).
- The transport-lockout test ([tests/test_transport_lockout.py](tests/test_transport_lockout.py)) continues to pass (this spec doesn't touch telegram imports).
- New **backend-lockout test**: assert `task_manager.py` has **no** direct import of `claude_client` or `backends.claude` — only `backends.base`. (Mirrors the transport-lockout pattern.)
- New **bot-backend-lockout test**: assert `bot.py` contains no `ClaudeClient(` text (construction banned) and no attribute access to `.claude.` on any `task_manager` reference. Protects category A/B rename from regressing. `ClaudeStreamError`, `MODELS`, `EFFORT_LEVELS`, `PERMISSION_MODES`, `is_usage_cap_error` imports from `backends.claude` remain allowed (category B).
- Cap-probe regression: existing group-halt/resume test (if present) continues to pass with the probe routed through `backend.probe_health()`; if no such test exists, add one using `FakeBackend` that returns a staged sequence of `HealthStatus` values.

### 5.3 Manual smoke

One real-Claude smoke run end-to-end (start a bot, send a message, confirm streaming works) after the module moves, before merging.

## 6. Folded gap: skills/personas audit

The original external spec is silent on how [`skills.py`](src/link_project_to_chat/skills.py) (skill/persona loading with the priority chain `project > global > Claude Code user > bundled`) interacts with backend choice. This phase resolves it.

**Decision: skills are shared across backends.** Rationale:

1. A "skill" in this codebase is **prompt text** injected via `append_system_prompt`, not a CLI-specific artifact. Any backend that supports a system prompt can consume them verbatim.
2. The loader's fourth priority level ("Claude Code user") is a convenience path for `~/.claude/skills/`. It stays Claude-oriented by default, but the skill *content* it produces is plain text — no Claude lock-in.
3. Making skills backend-scoped would double the user-facing surface (separate Claude skills vs. Codex skills) with no offsetting benefit.

**Action items in this phase:**
- Add one-paragraph docstring to `skills.py` documenting "skills are backend-agnostic; the loader's Claude-named path is a convenience, not a lock-in."
- No code change to `skills.py`.
- Capture the decision in this spec (done — §6).

Spec #3 revisits only if Codex-CLI-specific skill packaging turns out to exist.

## 7. Migration & rollout

### 7.1 Commit sequence (each commit is independently shippable)

1. **Introduce events module.** Copy event dataclasses from `stream.py` into new `events.py`. `stream.py` re-exports from `events`. No behavior change, no call-site updates. Green tests.
2. **Introduce backends package with parser.** Move `parse_stream_line` to `backends/claude_parser.py`. `stream.py` re-exports it. Green tests.
3. **Introduce `AgentBackend` Protocol + `HealthStatus`.** New file `backends/base.py`. No callers yet. Green tests.
4. **Move `ClaudeClient` → `ClaudeBackend`.** New file `backends/claude.py`. Implement `probe_health()` method (replaces what `_schedule_cap_probe` does inline today). Old `claude_client.py` becomes a one-line re-export shim: `from .backends.claude import ClaudeBackend as ClaudeClient`. Call sites unchanged. Green tests.
5. **Inject backend into `TaskManager` + rename internals.** `TaskManager.__init__` takes `backend: AgentBackend`. All identifiers renamed per §4.5 table (`_claude` → `_backend`, `submit_claude` → `submit_agent`, `TaskType.CLAUDE` → `TaskType.AGENT`, etc.). `bot.py` updated to construct and pass. Existing test files updated to match new identifiers. Green tests + manual smoke.
6. **`bot.py` Claude-identifier scrub (category A + B).** Rename all `self.task_manager.claude.X` → `self.task_manager.backend.X` (≥20 sites per §4.6). Redirect `claude_client` imports → `backends.claude`. `claude_client.py` shim removed. Green tests.
7. **Route `_schedule_cap_probe` through `backend.probe_health()`.** Remove the lazy `from .claude_client import ClaudeClient` from `bot.py`. Green tests + manual smoke (halt group, wait for probe, verify resume).
8. **Update tests.** Add `FakeBackend` with `probe_health` support, contract test, backend-lockout test, bot-backend-lockout test.
9. **Document decision.** Add skills audit note per §6. Update [CLAUDE.md](CLAUDE.md) "Key modules" section to mention `backends/`.

### 7.2 Rollback

Each commit is revertable independently. If any commit reveals a blocker, revert to the previous green state; no cross-commit data migration is involved.

## 8. Exit criteria

- [ ] `task_manager.py` has zero direct imports from `claude_client` or `backends.claude`.
- [ ] `task_manager.py` has no identifier containing "claude" (attrs, methods, TaskType values) — verified by `grep -iw claude src/link_project_to_chat/task_manager.py` returning no hits.
- [ ] `bot.py` has zero `ClaudeClient(` construction sites and zero `.claude.` attribute-chain references off `task_manager`.
- [ ] `backends/base.py` defines `AgentBackend` Protocol, `BackendCapabilities` (with `supports_usage_cap_detection`), and `HealthStatus`.
- [ ] `backends/claude.py` implements `AgentBackend` with `name = "claude"`, declared capabilities, and working `probe_health()`.
- [ ] `_schedule_cap_probe` in `bot.py` routes through `task_manager.backend.probe_health()` — no lazy `ClaudeClient` import.
- [ ] `events.py` and `backends/claude_parser.py` exist; `stream.py` is a shim or deleted.
- [ ] `claude_client.py` is deleted (or is a one-line shim scheduled for deletion in spec #2).
- [ ] Contract test passes for `ClaudeBackend` and `FakeBackend` (including `probe_health` coverage).
- [ ] Backend-lockout test and bot-backend-lockout test pass.
- [ ] All existing tests pass (updated for the rename, assertions unchanged).
- [ ] Manual smoke: real Claude session completes a round-trip; group halt + cap probe resume works.
- [ ] Skills audit decision is documented in `skills.py` docstring and this spec.

## 9. Open questions

None that block implementation. The following are confirmed out-of-scope for this phase and land in later specs:

- `bot.py` still imports Claude-specific constants (`MODELS`, `EFFORT_LEVELS`, `PERMISSION_MODES`) from `backends.claude` and uses "Claude" in user-facing strings ("Set Claude model", "chat with Claude"). Spec #2 capability-gates these.
- `_TELEGRAM_AWARENESS` preamble in `backends/claude.py` hardcodes command list. Spec #2 parameterizes by backend capabilities.
- Env-var scrubbing in `backends/claude.py` (line 262–267) blanket-scrubs `OPENAI_*`. Spec #3 makes this per-backend.
- Direct JSON mutators ([load_sessions, load_session, save_session, clear_session, patch_project, patch_team](src/link_project_to_chat/config.py:605) — 22 call sites) still target flat `session_id` keys. Spec #2 migrates them.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Renaming `ClaudeClient` → `ClaudeBackend` breaks hidden imports | Keep a one-line `claude_client.py` re-export shim through step 4; delete in step 6 |
| Parser split breaks tests that import from `stream` | Keep `stream.py` as a re-export shim; delete in spec #2 |
| Circular imports between `events.py`, `backends/base.py`, and `backends/claude.py` | Protocol in `backends/base.py` imports events only; concrete backends import both; `events.py` imports nothing from `backends` |
| `TaskManager` constructor change cascades into many test-setups | The constructor is the one injection point; add a `FakeBackend` fixture so test updates are small and uniform |
| Large identifier rename across `task_manager.py` + `bot.py` (§4.5, §4.6) produces a sprawling diff that's hard to review | Commit rename as its own step (step 5) separate from semantic changes; diff is mechanical and can be verified by `grep -iw claude` post-commit |
| `probe_health()` invocation interferes with the main interactive Claude process | `ClaudeBackend.probe_health` spawns a **detached** subprocess (same pattern as today's `_schedule_cap_probe` inline code), not the main `self._proc`. Contract test asserts this non-interference. |
| Health probe's "ping" string produces a token-costly response on some models | Unchanged from today — `_schedule_cap_probe` already sends `"ping"`. The move preserves cost behavior exactly. |
