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

    Phase 1 uses this only to describe Claude's capabilities; capability-based
    gating of commands is introduced in spec #2.
    """
    models: tuple[str, ...]
    supports_thinking: bool
    supports_permissions: bool
    supports_resume: bool
    supports_compact: bool
    supports_allowed_tools: bool


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
- `capabilities = BackendCapabilities(models=MODELS, supports_thinking=True, supports_permissions=True, supports_resume=True, supports_compact=True, supports_allowed_tools=True)` class attribute added.

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

### 4.5 `TaskManager` injection

Today ([task_manager.py:18](src/link_project_to_chat/task_manager.py:18)):
```python
from .claude_client import ClaudeClient
```
`TaskManager` currently constructs/owns `ClaudeClient` instances.

After this phase:
```python
from .backends.base import AgentBackend
```
`TaskManager.__init__` takes `backend: AgentBackend` (required, no default) and stores it. All internal references to `self.claude` or similar become `self.backend`. Method names that include "claude" in their identifier (if any — audit during implementation) are renamed to agent-neutral equivalents.

`bot.py` is the construction site: it creates a `ClaudeBackend` (using the existing config shape — flat `model`, `effort`, `permissions`, `session_id`, `show_thinking` fields — unchanged from today) and passes it to `TaskManager`. A thin helper `_make_backend_from_legacy_config(project)` in `backends/__init__.py` centralizes this so spec #2 has one place to expand when backend-aware config arrives.

## 5. Testing strategy

### 5.1 Contract test

New file: `tests/backends/test_contract.py`. Parametrized over all registered backends (today: only `ClaudeBackend`). For each:
- Accepts a `user_message: str` on `chat_stream` and yields `StreamEvent` instances.
- `chat()` returns a string when a terminal `Result` event is yielded.
- `chat()` raises when a terminal `Error` event is yielded.
- `cancel()` is idempotent.
- `close_interactive()` is safe to call when no process is live.

A `FakeBackend` test double lives in `tests/backends/fakes.py` and is included in the parametrization. Modeled on `FakeTransport` ([transport/fake.py](src/link_project_to_chat/transport/fake.py), already established by the transport abstraction).

### 5.2 Regression tests

- Every existing `claude_client` / `task_manager` / `bot` test continues to pass unchanged.
- The transport-lockout test ([tests/test_transport_lockout.py](tests/test_transport_lockout.py)) continues to pass (this spec doesn't touch telegram imports).
- New "backend-lockout" test: assert `task_manager.py` has **no** direct import of `claude_client` or `backends.claude` — only `backends.base`. (Mirrors the transport-lockout pattern.)

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
3. **Introduce `AgentBackend` Protocol.** New file `backends/base.py`. No callers yet. Green tests.
4. **Move `ClaudeClient` → `ClaudeBackend`.** New file `backends/claude.py`. Old `claude_client.py` becomes a one-line re-export shim: `from .backends.claude import ClaudeBackend as ClaudeClient`. Call sites unchanged. Green tests.
5. **Inject backend into `TaskManager`.** `TaskManager.__init__` takes `backend: AgentBackend`. `bot.py` updated to construct and pass. Claude-specific class name references in `bot.py` switch to `backends.claude`. `claude_client.py` shim removed. Green tests + manual smoke.
6. **Update tests.** Add `FakeBackend`, contract test, backend-lockout test.
7. **Document decision.** Add skills audit note per §6. Update [CLAUDE.md](CLAUDE.md) "Key modules" section to mention `backends/`.

### 7.2 Rollback

Each commit is revertable independently. If any commit reveals a blocker, revert to the previous green state; no cross-commit data migration is involved.

## 8. Exit criteria

- [ ] `task_manager.py` has zero direct imports from `claude_client` or `backends.claude`.
- [ ] `backends/base.py` defines `AgentBackend` Protocol and `BackendCapabilities`.
- [ ] `backends/claude.py` implements `AgentBackend` with `name = "claude"` and declared capabilities.
- [ ] `events.py` and `backends/claude_parser.py` exist; `stream.py` is a shim or deleted.
- [ ] `claude_client.py` is deleted (or is a one-line shim scheduled for deletion in spec #2).
- [ ] Contract test passes for `ClaudeBackend` and `FakeBackend`.
- [ ] Backend-lockout test passes.
- [ ] All existing tests pass unchanged.
- [ ] Manual smoke: real Claude session completes a round-trip.
- [ ] Skills audit decision is documented in `skills.py` docstring and this spec.

## 9. Open questions

None that block implementation. The following are confirmed out-of-scope for this phase and land in later specs:

- `bot.py` still imports Claude-specific constants (`MODELS`, `EFFORT_LEVELS`, `PERMISSION_MODES`). Spec #2 generalizes.
- `_TELEGRAM_AWARENESS` preamble in `backends/claude.py` hardcodes command list. Spec #2 parameterizes by backend capabilities.
- Env-var scrubbing in `backends/claude.py` (line 262–267) blanket-scrubs `OPENAI_*`. Spec #3 makes this per-backend.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Renaming `ClaudeClient` → `ClaudeBackend` breaks hidden imports | Keep a one-line `claude_client.py` re-export shim through this phase; delete in spec #2 after audit |
| Parser split breaks tests that import from `stream` | Keep `stream.py` as a re-export shim; delete in spec #2 |
| Circular imports between `events.py`, `backends/base.py`, and `backends/claude.py` | Protocol in `backends/base.py` imports events only; concrete backends import both; `events.py` imports nothing from `backends` |
| `TaskManager` constructor change cascades into many test-setups | The constructor is the one injection point; add a `FakeBackend` fixture so test updates are small and uniform |
