# Backend Abstraction Phase 2 — Backend-Aware Config + `/backend` Command

**Status:** Designed (2026-04-23). Not yet implemented.
**Date:** 2026-04-23
**Part of:** Backend-abstraction track, spec #2 of 4.
**Depends on:** Spec #1 (Claude extracted behind `AgentBackend`).
**Blocks:** Spec #3 (Codex adapter — needs the config + factory + capability gating in place).

---

## 1. Overview

After spec #1, the code has an `AgentBackend` Protocol and a `ClaudeBackend`, but the configuration still stores Claude-shaped flat fields (`model`, `effort`, `permissions`, `session_id`, `show_thinking`) directly on [`ProjectConfig`](src/link_project_to_chat/config.py) and [`TeamBotConfig`](src/link_project_to_chat/config.py). A single shared `model`/`session_id` is insufficient once a project can switch between backends.

This spec teaches the config to carry a `backend` selector plus a per-provider `backend_state` map, migrates legacy configs on load, adds a `/backend` command, gates capability-dependent commands on `AgentBackend.capabilities`, and propagates backend selection through the manager bot's subprocess-launch flow.

**Still no Codex code.** At runtime, the only registered backend remains Claude. The user-visible diff: `/backend` exists (and shows "claude" as the only option); `/thinking`, `/permissions`, `/compact` now route through a capability check that today always passes.

## 2. Goals & non-goals

**Goals**
- Extend `ProjectConfig` and `TeamBotConfig` with `backend: str = "claude"` and `backend_state: dict[str, dict]`.
- Implement read-old/write-new migration: legacy flat fields read from disk populate `backend_state["claude"]`; writes go to the new shape only.
- **Migrate the direct JSON helpers** (`load_sessions`, `load_session`, `save_session`, `clear_session`, `patch_project`, `patch_team` — 22 call sites) so they read/write the new `backend_state[<provider>]` shape instead of flat keys. Without this, Claude turns would revert migration on every save. See §4.3.
- Add a backend factory keyed by name; register `ClaudeBackend`.
- Add `/backend` command (show + switch).
- Gate `/thinking`, `/permissions`, `/compact`, `/model` responses on `AgentBackend.capabilities`.
- Propagate the active backend through the manager bot's project-launch flow ([manager/bot.py](src/link_project_to_chat/manager/bot.py) + [manager/config.py](src/link_project_to_chat/manager/config.py)).
- Audit group/team relay ([manager/team_relay.py](src/link_project_to_chat/manager/team_relay.py), [group_filters.py](src/link_project_to_chat/group_filters.py), [group_state.py](src/link_project_to_chat/group_state.py)) for Claude-named assumptions and fix any found.
- Generalize user-facing help strings (category C from spec #1 §4.6) that currently say "Claude" where they should say "the agent" or the active backend's name.

**Non-goals**
- No Codex implementation.
- No change to the Claude CLI subprocess invocation.
- No change to env-var scrubbing (deferred to spec #3).
- No UI for adding new backends beyond Claude (the factory supports one registrant by design in this phase).
- No cross-backend session migration (switching between backends keeps each side's session independent, per the original spec).

## 3. Decisions driving this design

| # | Question | Decision |
|---|---|---|
| 1 | Should legacy flat fields be deleted on first write, or kept for one release? | **Keep for one release.** Config writer emits both new shape and legacy fields until a subsequent release. Reader prefers new shape. Prevents downgrade breakage. |
| 2 | Where does the backend factory live? | `backends/factory.py`. Module-level registry dict. `ClaudeBackend` self-registers at import. |
| 3 | How does the manager bot know which backend to launch a project with? | It doesn't need to know directly. The manager stores `backend` + `backend_state` in the per-project config; the project-bot subprocess reads its own config on startup. Manager-side changes are limited to preserving the new schema on save. |
| 4 | What happens when `/backend <name>` is called for an unregistered backend? | Return a clear error listing available backends. Do not create a `backend_state[<name>]` entry. |
| 5 | What happens to the current interactive process when `/backend` switches? | `close_interactive()` is called on the old backend before the new one activates. (Matches original spec §8.2.) |
| 6 | Should the default backend be configurable globally? | Yes — add `default_backend: str = "claude"` on top-level `Config`. New projects use this when `backend` is not set. |

## 4. Architecture

### 4.1 Config schema changes

**Before (from spec #1 end-state):**
```python
@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = ...
    trusted_users: dict[str, int] = ...
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None
    session_id: str | None = None
    show_thinking: bool = False
    autostart: bool = False
    active_persona: str | None = None
```

**After:**
```python
@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = ...
    trusted_users: dict[str, int] = ...
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    autostart: bool = False
    active_persona: str | None = None
    # Legacy fields retained through this release for downgrade safety;
    # populated by the migration reader, not by new writes.
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None
    session_id: str | None = None
    show_thinking: bool = False
```

`TeamBotConfig` receives the same treatment.

`backend_state["claude"]` shape:
```python
{
    "model": str | None,
    "effort": str | None,
    "permissions": str | None,      # "default" | "acceptEdits" | "bypassPermissions" | "dontAsk" | "plan" | "auto" | "dangerously-skip-permissions"
    "session_id": str | None,
    "show_thinking": bool,
}
```

`Config` top-level gains:
```python
default_backend: str = "claude"
```

### 4.2 Migration: read-old / write-new

In [`config.py`](src/link_project_to_chat/config.py) `load_config`:
1. Parse the JSON as today.
2. For each `ProjectConfig` and `TeamBotConfig` entry:
   - If `backend` is absent: set `backend = "claude"`.
   - If `backend_state` is absent or missing `"claude"`: build `backend_state["claude"]` from any legacy flat fields (`model`, `effort`, `permissions`, `session_id`, `show_thinking`). Skip `None` values.
   - Keep the legacy fields on the dataclass instance populated (so any code path that still reads them keeps working during the transition).
3. If `default_backend` is absent: set to `"claude"`.

In `save_config`:
1. Write the new shape (`backend` + `backend_state`).
2. Also emit legacy flat fields **mirrored from `backend_state["claude"]`** for downgrade safety.
3. Do not emit `backend_state["claude"]` entries that are `None`/default.

**Migration tests** in `tests/test_config_migration.py`:
- Legacy-only config → load → write → re-load round-trip preserves all fields in new shape.
- New-shape config → load → write → re-load round-trip is idempotent.
- Mixed config (has both legacy flat and `backend_state["claude"]`) → reader prefers `backend_state["claude"]`; legacy fields are ignored.
- Team bot entries go through the same path.

### 4.3 JSON helpers — direct mutators/readers must migrate too

`load_config`/`save_config` are not the only config I/O paths. [`config.py`](src/link_project_to_chat/config.py) exposes six direct JSON helpers that bypass the dataclass layer and read/write flat Claude-shaped keys under `raw["projects"][<name>]` and `raw["teams"][<name>]["bots"][<role>]`:

| Helper | Line | What it does today | What it must do after Phase 2 |
|---|---|---|---|
| `load_sessions` | [config.py:605](src/link_project_to_chat/config.py:605) | Reads `proj["session_id"]` flat | Read from `proj["backend_state"][proj.get("backend","claude")]["session_id"]`, with flat-key fallback for unmigrated entries |
| `load_session` | [config.py:625](src/link_project_to_chat/config.py:625) | Reads one project's flat `session_id` | Same new path + fallback |
| `save_session` | [config.py:710](src/link_project_to_chat/config.py:710) | Writes `raw["projects"][name]["session_id"] = sid` | Write into `backend_state[<active>]["session_id"]`; also mirror to flat `session_id` for one release (downgrade safety, per §4.2) |
| `clear_session` | [config.py:726](src/link_project_to_chat/config.py:726) | Deletes flat `session_id` | Delete from `backend_state[<active>]["session_id"]` **and** flat `session_id` (both the new and mirrored copies) |
| `patch_project` | [config.py:650](src/link_project_to_chat/config.py:650) | Arbitrary field patch at project top-level | Unchanged at top-level for non-backend-state fields (`path`, `telegram_bot_token`, `autostart`, …); callers that previously patched `model`/`effort`/`permissions`/`show_thinking`/`session_id` migrate to a new `patch_backend_state(project_name, backend_name, fields)` helper |
| `patch_team` | [config.py:662](src/link_project_to_chat/config.py:662) | Arbitrary field patch at team top-level | Same rule as `patch_project`; team-bot field patches go through a new `patch_team_bot_backend_state(team_name, role, backend_name, fields)` helper |

**Call-site impact.** A grep in the current branch shows **22 call sites** across `config.py` (6), `bot.py` (14), and `manager/bot.py` (2). Each call site must be reviewed:
- Reads of `session_id`: change to the migrated helper; no API change for the caller.
- Writes of `session_id`: same.
- Patches of `model`/`effort`/`permissions`/`show_thinking` via `patch_project`/`patch_team`: change the call to `patch_backend_state(name, <active>, fields)` or `patch_team_bot_backend_state(team, role, <active>, fields)`.

**Why this is a load-bearing section:** without migrating these helpers, the Phase 2 release has two disagreeing code paths for the same data — the dataclass layer writes the new shape, but `save_session` called at end-of-turn [bot.py:497](src/link_project_to_chat/bot.py:497) overwrites it with the flat key, reverting the migration on every Claude turn.

**New helpers.** Add to `config.py`:

```python
def patch_backend_state(
    project_name: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Update fields inside backend_state[<backend_name>] on a project.

    Creates backend_state and the per-backend sub-dict if absent.
    None values remove the key. Mirrors writes to legacy flat fields
    (model/effort/permissions/session_id/show_thinking) for one release.
    """

def patch_team_bot_backend_state(
    team_name: str,
    role: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Analogous for team bots."""
```

**Testing.** `tests/test_config_migration.py` adds cases for each helper:
- `save_session` on a legacy-only entry → populates `backend_state["claude"]["session_id"]` + mirrored flat `session_id`.
- `save_session` on a new-shape entry → writes only into `backend_state[<active>]`.
- `clear_session` → both paths cleared.
- `patch_backend_state` on a team bot with an existing unrelated `backend_state["codex"]` entry → leaves the `codex` entry untouched.
- `load_session` with both flat and new-shape fields present → reads from new shape.

**Call-site migration is part of this phase's commit sequence**, not deferred. See step 2b in §8.1.

### 4.4 Backend factory

New file `backends/factory.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .base import AgentBackend

# Factory takes the project path + the provider-specific state dict
# and returns a ready-to-use backend instance.
BackendFactory = Callable[[Path, dict], AgentBackend]

_registry: dict[str, BackendFactory] = {}


def register(name: str, factory: BackendFactory) -> None:
    if name in _registry:
        raise ValueError(f"Backend {name!r} already registered")
    _registry[name] = factory


def create(name: str, project_path: Path, state: dict) -> AgentBackend:
    if name not in _registry:
        raise KeyError(f"Unknown backend {name!r}; available: {sorted(_registry)}")
    return _registry[name](project_path, state)


def available() -> list[str]:
    return sorted(_registry)
```

`ClaudeBackend` self-registers at `backends/claude.py` import:
```python
from .factory import register


def _make_claude(project_path, state):
    backend = ClaudeBackend(
        project_path=project_path,
        model=state.get("model") or DEFAULT_MODEL,
        skip_permissions=(state.get("permissions") == "dangerously-skip-permissions"),
        permission_mode=state.get("permissions") if state.get("permissions") != "dangerously-skip-permissions" else None,
    )
    backend.session_id = state.get("session_id")
    backend.show_thinking = bool(state.get("show_thinking"))
    backend.effort = state.get("effort") or "medium"
    return backend


register("claude", _make_claude)
```

`bot.py`'s backend-construction helper (added in spec #1's step 5) now reads:
```python
from .backends.factory import create
backend = create(
    project_config.backend,
    Path(project_config.path),
    project_config.backend_state.get(project_config.backend, {}),
)
```

### 4.5 `/backend` command

Three forms:

| Form | Behavior |
|---|---|
| `/backend` | Reply with active backend name + available backends + one-line capability summary of the active one |
| `/backend <name>` | Switch the active backend; persist to config; close_interactive() on old backend; hydrate new one from `backend_state[<name>]` (empty dict if absent) |
| `/backend <unknown>` | Reply with error + available list |

Handler lives in [`bot.py`](src/link_project_to_chat/bot.py) alongside other command handlers. Wires through the existing auth mixin (trusted-user check).

`/backend <name>` persists the new selection to disk **before** activating the new backend, so a crash during activation doesn't leave the on-disk state ahead of the runtime state.

### 4.6 Capability gating

`/thinking`, `/permissions`, `/compact` currently proceed unconditionally. After this phase, each handler starts with:
```python
if not task_manager.backend.capabilities.supports_thinking:
    await transport.send_text(chat, "This backend doesn't support /thinking.")
    return
```
Analogous for `/permissions` (→ `supports_permissions`) and `/compact` (→ `supports_compact`).

`/model` already lists Claude models from `MODELS`. After this phase it consults `task_manager.backend.capabilities.models` so future backends can declare their own. Selection writes to `backend_state[<active>]["model"]`.

`/status` is extended to report the active backend name alongside existing status fields.

### 4.7 Manager-bot propagation

[`manager/config.py`](src/link_project_to_chat/manager/config.py) is the schema-aware writer used when the manager creates/edits project entries. Changes:

- Preserve `backend` and `backend_state` on round-trip. Do not flatten.
- New project creation defaults `backend = config.default_backend` and `backend_state = {}`.
- When the manager edits per-project fields (e.g., model, permissions) via its command surface, write to `backend_state[<active>]` instead of flat fields.

[`manager/bot.py`](src/link_project_to_chat/manager/bot.py) launches each project bot as a subprocess. No changes needed there — the subprocess reads its own config on startup via the updated loader. **Audit item:** grep `manager/bot.py` for any direct reads of `model`/`effort`/`permissions` on a `ProjectConfig` before subprocess launch; replace with `backend_state[cfg.backend].get(...)` if found.

### 4.8 Group/team relay audit

The original v1.0 spec flagged this as a vague risk ("Any direct assumptions in tests or manager utilities that reference Claude by name may cause hidden regressions"). This phase resolves it with an explicit audit:

**Files to grep:** [`manager/team_relay.py`](src/link_project_to_chat/manager/team_relay.py), [`group_filters.py`](src/link_project_to_chat/group_filters.py), [`group_state.py`](src/link_project_to_chat/group_state.py), `tests/test_group_*.py`.

**Search patterns:**
- `ClaudeClient`, `claude_client`, `Claude ` (word), `claude_` (snake_case names), hardcoded `"claude"` string literals outside backend-factory code.

**Action for each hit:**
- If the reference is to the backend class, port to the `AgentBackend` interface.
- If it's a user-facing string, generalize to "the agent" or parameterize.
- If it's a test fixture, update to use `FakeBackend`.

**Recorded in this phase:** the audit findings go into the spec commit message so spec #3 doesn't re-discover them. A placeholder section in `CLAUDE.md`'s "Manager & team support" subsection is updated.

### 4.9 Help-text generalization

User-facing strings in `bot.py` that currently reference "Claude" by name are reviewed and changed to one of:
- **Kept Claude-specific** if truly Claude-specific (e.g., usage-cap message).
- **Generalized** to "the agent" if the concept applies to any backend.
- **Parameterized** via `task_manager.backend.name` if the user benefits from seeing which backend is active.

The `_TELEGRAM_AWARENESS` preamble ([claude_client.py:94–115](src/link_project_to_chat/claude_client.py:94)) hardcodes a command list. This phase moves it to a template that the backend fills in using its declared capabilities, so Codex (in spec #3) can omit commands it doesn't support without forking the preamble.

## 5. Testing strategy

### 5.1 New tests

- `tests/test_config_migration.py` — four cases from §4.2.
- `tests/backends/test_factory.py` — register/create/available; unknown backend raises.
- `tests/test_backend_command.py` — `/backend`, `/backend claude`, `/backend bogus`; switching closes old interactive process; persistence happens before activation.
- `tests/test_capability_gating.py` — `/thinking`, `/permissions`, `/compact` gated correctly. Uses a `FakeBackend` with capabilities tuned per test.
- Extend `tests/test_manager_config.py` (create if absent) — manager writes preserve `backend_state`.

### 5.2 Regression

- All spec #1 tests continue to pass.
- Group/team tests continue to pass after the audit-driven changes.
- Transport-lockout and backend-lockout tests continue to pass.

### 5.3 Manual smoke

- Load a legacy config; verify it round-trips through save without losing data.
- Run `/backend` with only Claude registered; confirm response.
- Run `/backend claude` from a bot with a live Claude session; confirm no error and session continues.
- Flip `show_thinking` via `/thinking`; confirm it's written to `backend_state["claude"]["show_thinking"]`.

## 6. Folded gap: manager-bot backend propagation

Covered in §4.6 above.

## 7. Folded gap: group/team relay Claude-name audit

Covered in §4.7 above.

## 8. Migration & rollout

### 8.1 Commit sequence

1. **Extend dataclasses.** Add `backend` + `backend_state` fields; add `default_backend` on `Config`. No reader/writer changes yet. Green tests.
2. **Reader migration.** `load_config` populates `backend_state["claude"]` from legacy fields. Writer still emits legacy shape only. Green tests.
   - **2a. JSON-helper readers.** Migrate `load_sessions`, `load_session` to the new shape with flat-key fallback. Green tests (including new `tests/test_config_migration.py` cases for the helpers).
3. **Writer dual-emit.** `save_config` writes new shape + mirrored legacy fields. Migration round-trip tests added. Green tests.
   - **3a. JSON-helper writers.** Migrate `save_session`, `clear_session` to write `backend_state[<active>]["session_id"]` with mirrored flat key. Add `patch_backend_state` / `patch_team_bot_backend_state`. Green tests.
   - **3b. Call-site migration.** Update the 22 call sites across `bot.py`, `manager/bot.py`, `config.py` to use the migrated helpers (or the new `patch_backend_state` helper for model/effort/permissions/show_thinking). Green tests + manual smoke: run a Claude turn end-to-end, inspect the config on disk, confirm `backend_state["claude"]["session_id"]` is populated and the flat `session_id` mirror stays in sync.
4. **Factory + registration.** `backends/factory.py` and `ClaudeBackend` self-registration. `bot.py` switches to factory-based construction. Green tests.
5. **`/backend` command.** Handler + persistence + close_interactive-on-switch. Tests added. Green tests.
6. **Capability gating.** `/thinking`, `/permissions`, `/compact`, `/model` routed through capabilities. Tests added. Green tests.
7. **Manager propagation.** `manager/config.py` preserves new schema; audit `manager/bot.py`. Green tests.
8. **Relay audit + fixes.** Grep pass on team_relay/group_*/tests; apply fixes. Green tests + manual smoke with a team bot.
9. **Help-text + preamble generalization.** Parameterize `_TELEGRAM_AWARENESS` by capabilities. Smoke-test Claude response quality. Green tests.
10. **Drop `stream.py` shim and `claude_client.py` shim.** (Deferred from spec #1.) Update any stragglers. Green tests.

### 8.2 Downgrade safety

- Legacy flat fields are still written for one release. A user rolling back to pre-phase-2 code can still read their config.
- The release notes for this phase document that the *next* release (not this one) drops the legacy fields.

## 9. Exit criteria

- [ ] `ProjectConfig` / `TeamBotConfig` / `Config` have the new fields.
- [ ] Loader populates `backend_state["claude"]` from legacy flat fields without data loss.
- [ ] Writer emits both new and legacy shapes; round-trip is stable.
- [ ] `load_session`, `load_sessions`, `save_session`, `clear_session` read/write `backend_state[<active>]["session_id"]` with flat-key mirror.
- [ ] `patch_backend_state` and `patch_team_bot_backend_state` helpers exist and are tested.
- [ ] All 22 call sites of the legacy JSON helpers are migrated — grep confirms no remaining writes to flat `model`/`effort`/`permissions`/`session_id`/`show_thinking` keys via `patch_project`/`patch_team` (mirror writes happen inside the helpers, not at call sites).
- [ ] Factory registers `ClaudeBackend` at import; `create()` returns a usable instance.
- [ ] `/backend`, `/backend claude`, `/backend <unknown>` all behave per §4.5.
- [ ] `/thinking`, `/permissions`, `/compact` gated on capabilities.
- [ ] `/status` includes active backend name.
- [ ] `manager/config.py` round-trips `backend_state`.
- [ ] Grep audit of team_relay + group_* files produces either zero Claude-named hits or a list of applied fixes.
- [ ] `stream.py` shim and `claude_client.py` shim deleted.
- [ ] All existing tests pass; new tests from §5.1 pass.
- [ ] Manual smoke (three items in §5.3) passes, plus: run a Claude turn, stop the bot, inspect on-disk config — `session_id` is persisted at `projects.<name>.backend_state.claude.session_id` and the flat `session_id` mirror matches.

## 10. Open questions

- Should `default_backend` be changeable via a manager command, or only by editing config? **Default answer:** config-edit only for now. A command can come in spec #4 if it proves necessary.
- When switching backends, should the old backend's `session_id` be preserved in `backend_state[<old>]["session_id"]`? **Default answer:** yes, always. Spec #3 will confirm this works with real Codex sessions.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Dual-emit writer accidentally desyncs new and legacy shapes | Single source of truth: new shape is authoritative; legacy fields are computed from it at write time, never stored separately in memory |
| Manager-side config path has a different writer with its own schema | `manager/config.py` audit is an explicit exit criterion; tests cover round-trip |
| Capability gating silently blocks a command a user expected to work | Error message is specific: "Backend 'claude' does not support /thinking" — phrased so the mis-configuration is obvious |
| Team-relay audit misses a Claude-named reference | The audit is mechanical grep; all hits are reviewed; add a grep-based regression test (similar to transport-lockout) that fails if Claude-named identifiers leak into non-backend modules |
| Preamble generalization degrades Claude response quality | A/B the old and new preamble against a handful of real messages before merging; keep the old one as a fallback if regression observed |
| JSON-helper migration breaks end-of-turn session persistence mid-flight (e.g., step 3a merged without step 3b call-site updates) | Steps 2a + 3a + 3b are contiguous and must land together. Commit review checklist requires all three before the branch advances. The helpers preserve flat-key reads throughout so partial rollout still loads old data correctly. |
| 22 call sites across three files are tedious to migrate and easy to partially miss | Add a grep-based lockout test in step 3b that fails the build if `patch_project(..., {"session_id": …})` or `patch_team(..., {"session_id": …})` appears outside `config.py` itself. Catches regressions. |
