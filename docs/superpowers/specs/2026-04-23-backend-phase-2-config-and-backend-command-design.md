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
- Implement config migration: the new `backend_state` shape is authoritative in memory and on every write; legacy flat fields are *mirrored* on write for one release (downgrade safety) and read as a fallback when `backend_state` is absent. Mirror fields are computed from `backend_state["claude"]` at write time, never stored independently in memory. Details in §4.2.
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
| 3 | How does the manager bot know which backend to launch a project with? | The project-bot subprocess reads its own config on startup, so launch doesn't require manager knowledge. **But** the manager *does* have user-facing commands that edit per-project model/permissions and a global `/model` (see §4.7). Those paths get concrete migration work — not just schema preservation. Decision: manager/config.py gets a lockdown test to preserve `backend_state` verbatim; manager/bot.py gets enumerated per-command changes. |
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

`Config` top-level changes:
```python
# New:
default_backend: str = "claude"
default_model_claude: str = ""   # was: default_model (renamed; see §4.2 and §4.7)
# Legacy (kept for one release, mirrored on write):
default_model: str = ""          # mirrored from default_model_claude at write time
```

**Why `default_model` is renamed.** The existing top-level `default_model` is the per-project new-project default and is consumed by the manager's global `/model` command. Once `default_backend` exists, that field is semantically backend-specific — Claude's model names and Codex's model names do not share a namespace. Renaming per backend (Option A in §4.7) is the minimal change that keeps the semantics honest without introducing a dict-valued field that has no second entry yet. `default_model_codex` is added in spec #3, not here.

### 4.2 Migration: read-old / write-new

In [`config.py`](src/link_project_to_chat/config.py) `load_config`:
1. Parse the JSON as today.
2. For each `ProjectConfig` and `TeamBotConfig` entry:
   - If `backend` is absent: set `backend = "claude"`.
   - If `backend_state` is absent or missing `"claude"`: build `backend_state["claude"]` from any legacy flat fields (`model`, `effort`, `permissions`, `session_id`, `show_thinking`). Skip `None` values.
   - Keep the legacy fields on the dataclass instance populated (so any code path that still reads them keeps working during the transition).
3. Top-level:
   - If `default_backend` is absent: set to `"claude"`.
   - If `default_model_claude` is absent and legacy `default_model` is present: copy `default_model` → `default_model_claude`.
   - Keep `default_model` populated on the dataclass (mirror; read fallback).

In `save_config`:
1. Write the new shape at project/team-bot level (`backend` + `backend_state`) and at top level (`default_backend`, `default_model_claude`).
2. Also emit legacy flat fields — `model`/`effort`/`permissions`/`session_id`/`show_thinking` **mirrored from `backend_state["claude"]`** at project/team-bot level; `default_model` **mirrored from `default_model_claude`** at top level — for downgrade safety.
3. Do not emit `backend_state["claude"]` entries that are `None`/default.

**Migration tests** in `tests/test_config_migration.py`:
- Legacy-only config (flat project fields + legacy top-level `default_model`) → load → write → re-load round-trip preserves all fields in new shape.
- New-shape config → load → write → re-load round-trip is idempotent.
- Mixed project config (has both legacy flat and `backend_state["claude"]`) → reader prefers `backend_state["claude"]`; legacy fields are ignored.
- Mixed top-level (has both `default_model` and `default_model_claude`) → reader prefers `default_model_claude`.
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

### 4.4 Backend factory — swap input source from legacy fields to `backend_state`

The factory (`backends/factory.py`) and `ClaudeBackend` self-registration were **introduced in Phase 1** (see spec #1 §4.4.1). Phase 2 does not re-introduce them; it only changes the input source.

**Phase 1 call in bot.py (from Phase 1 §4.4.1):**
```python
state = {
    "model": project_config.model,
    "effort": project_config.effort,
    "permissions": project_config.permissions,
    "session_id": project_config.session_id,
    "show_thinking": project_config.show_thinking,
}
backend = create("claude", Path(project_config.path), state)
```

**Phase 2 call in bot.py:**
```python
backend = create(
    project_config.backend,
    Path(project_config.path),
    project_config.backend_state.get(project_config.backend, {}),
)
```

Factory signature and registration are unchanged. Only the input dict moves from flat legacy fields to `backend_state[<active>]`. The `ClaudeBackend` `_make_claude` factory function already reads from a state dict, so no factory-side change is needed.

### 4.5 `/backend` command

Four forms:

| Form | Behavior |
|---|---|
| `/backend` | Reply with active backend name + available backends + one-line capability summary of the active one |
| `/backend <active>` | **No-op** when `<active>` equals the currently-active backend name. Reply: "`<Name>` is already active." No `close_interactive`, no rehydration, no session reset, no disk write. |
| `/backend <other>` | Switch the active backend. See ordering below. |
| `/backend <unknown>` | Reply with error + available list. |

Handler lives in [`bot.py`](src/link_project_to_chat/bot.py) alongside other command handlers. Wires through the existing auth mixin (trusted-user check).

**Switch ordering — activate first, persist on success.**

1. Validate the new backend name is registered. On failure, reply "unknown backend" and return. No state changed.
2. Build the new backend via `factory.create(new_name, self.path, backend_state.get(new_name, {}))`. If construction raises, reply with the error and return. Old backend remains active; disk unchanged.
3. Call `close_interactive()` on the current backend. Swap `task_manager._backend` to the new instance.
4. **On success**, persist the new selection: update `project_config.backend = new_name` and call `patch_project(name, {"backend": new_name})` (or the analogous team-bot helper).
5. Reply "Switched to `<name>`."

Rationale for this order (contrary to the earlier draft): persist-first means a crash between persist and activation leaves disk ahead of runtime — on next start, the bot loads a backend that was never successfully activated. Activate-first keeps disk as the follower. If a crash happens mid-swap (between step 3 and step 4), the on-disk selection still points at the previous backend; the process will be restarted by the manager and come up on the old backend, which the user can retry from.

**No tasks-in-flight.** If `task_manager` reports any live agent task, `/backend <other>` rejects with "Cancel running tasks before switching backend." (Matches spec #3 §4.4; introduced here so it's in place by the time Codex lands.)

**Team-bot scope.** `/backend` is available on **both** project bots and team bots. A team bot is just a project bot launched with a team-role config. The handler lives in the shared command surface ([`bot.py`](src/link_project_to_chat/bot.py)) so both inherit it.

Persistence differs by context:
- **Project bot:** on successful switch, `patch_project(project_name, {"backend": new_name})` updates the project's top-level `backend` field.
- **Team bot:** on successful switch, `patch_team_bot_backend(team_name, role, new_name)` — a new helper with the same shape as `patch_team_bot_backend_state` but targeting the team-bot's top-level `backend` field at `raw["teams"][team_name]["bots"][role]["backend"]`.

The team-bot bot knows its own team+role from the config it was launched with. The handler distinguishes via `self.team_name` / `self.team_role` (existing fields on `ProjectBot` for team mode). If both are set, the team-bot persistence path is used; otherwise the project path.

Add helper in `config.py`:
```python
def patch_team_bot_backend(
    team_name: str,
    role: str,
    backend_name: str,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Set the active backend on a team bot. Analogous to patch_project
    for project bots. Writes only the 'backend' key at the team-bot level;
    does not touch 'backend_state'."""
```

Test coverage: `tests/test_backend_command.py` parametrizes `/backend` across project-bot mode and team-bot mode, confirming each persists to the correct config path.

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

**Current state of `manager/config.py`.** Verified at [manager/config.py](src/link_project_to_chat/manager/config.py): it is a **raw-dict passthrough**, not a schema-aware serializer. `load_project_configs` returns `dict[str, dict]` filtered only by "has a `path` key". `save_project_configs` does `raw.update({"projects": projects})` — it replaces the entire projects dict wholesale. There is no migration, no field validation, no typed dataclass round-trip.

**Changes needed in `manager/config.py`:**

- Preserve `backend` and `backend_state` keys verbatim on any round-trip through `load_project_configs`/`save_project_configs`. Because the module already treats projects as opaque dicts, this "just works" for any new keys — **but** add an explicit round-trip test to lock the behavior in, because nothing in the current code distinguishes "known" from "unknown" project fields. Without a test, a future refactor that starts validating fields could silently drop `backend_state`.
- `_filter_valid_projects` currently filters on "is a dict with a `path` key". Leave this unchanged — it correctly handles entries that have `backend_state` too.
- Add helper `set_project_backend(project_name, backend_name, path)` that writes `project["backend"] = backend_name` via `_patch_json`, analogous to the existing `set_project_autostart`.

**Current state of `manager/bot.py` — real changes, not just an audit.** The reviewer's original concern was correct: manager/bot.py does more than just launch subprocesses. Verified sites:

- [manager/bot.py:34–35](src/link_project_to_chat/manager/bot.py:34) registers `/add_project` and `/edit_project` commands.
- [manager/bot.py:49](src/link_project_to_chat/manager/bot.py:49) defines `_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions")` — `/edit_project <proj> model <value>` and `/edit_project <proj> permissions <value>` write flat keys directly, bypassing `backend_state`.
- [manager/bot.py:629](src/link_project_to_chat/manager/bot.py:629) `_add_model` is a step in the `/add_project` wizard that stores the user's model choice.
- [manager/bot.py:1934](src/link_project_to_chat/manager/bot.py:1934) writes `projects[name]["model"] = model_id` on the global model-selection button callback.
- [manager/bot.py:507–527, 1952–1965](src/link_project_to_chat/manager/bot.py:507) implement a global `/model` command on the manager that reads/writes `Config.default_model`.

**Concrete changes in Phase 2:**

1. **`_EDITABLE_FIELDS` update.** `model` and `permissions` entries in the list stay, but the handler at [line 746+](src/link_project_to_chat/manager/bot.py:746) routes writes through the new `patch_backend_state(project_name, project_config.backend, {"model": value})` helper from §4.3, not through `patch_project` with flat keys. The user-facing command surface is unchanged; the underlying write target changes.
2. **`/add_project` wizard `_add_model` step.** Change the final config write to populate `backend_state[default_backend]["model"] = model_id` and `backend = default_backend`. Flat `model` is still mirrored (downgrade safety per §4.2) but the authoritative write is to `backend_state`.
3. **Line 1934 project-model callback.** Same change: write to `backend_state[project["backend"]]["model"]`, mirror flat.
4. **Global `/model` command.** The schema rename `default_model` → `default_model_claude` is defined and migrated in §4.1 + §4.2 (Option A). In `manager/bot.py`, the manager's global `/model` button callback ([line 1959](src/link_project_to_chat/manager/bot.py:1959)) writes to `cfg.default_model_claude` instead of `cfg.default_model`; the selector at [lines 507–527](src/link_project_to_chat/manager/bot.py:507) shows the model list for `default_backend` (today always `"claude"`, so the list is unchanged user-visibly). `default_model_codex` is added in spec #3. Option B (`default_models: dict[str, str]`) was considered and rejected as premature — no second entry exists until spec #3, and a single-entry dict is just a rename with extra noise.

No "audit" framing — these are concrete, enumerated changes with code-line citations.

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

The `_TELEGRAM_AWARENESS` preamble (now at `backends/claude.py` after Phase 1 step 4; was formerly `claude_client.py:94–115`) hardcodes a command list. This phase moves it to a template that the backend fills in using its declared capabilities, so Codex (in spec #3) can omit commands it doesn't support without forking the preamble.

## 5. Testing strategy

### 5.1 New tests

- `tests/test_config_migration.py` — four cases from §4.2 plus the JSON-helper cases from §4.3 (six helpers, legacy-only / new-shape / mixed entries).
- Extend `tests/backends/test_factory.py` (created in Phase 1) — round-trip through `create()` using `backend_state` input.
- `tests/test_backend_command.py` — `/backend`, `/backend claude` (no-op case), `/backend <switch>` (activate-first ordering; crash-during-activate leaves disk unchanged), `/backend bogus`, switch-with-live-task rejection.
- `tests/test_capability_gating.py` — `/thinking`, `/permissions`, `/compact` gated correctly. Uses a `FakeBackend` with capabilities tuned per test.
- Extend `tests/test_manager_config.py` (create if absent) — `manager/config.py` round-trips `backend` and `backend_state` verbatim (locks in the raw-dict passthrough behavior per §4.7).
- New `tests/test_manager_bot_backend.py` — `/add_project` wizard writes to `backend_state[default_backend]`; `/edit_project <proj> model <value>` writes to `backend_state[<proj's backend>].model`; global `/model` shows models for `default_backend`.

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

Covered in §4.7 above.

## 7. Folded gap: group/team relay Claude-name audit

Covered in §4.8 above.

## 8. Migration & rollout

### 8.1 Commit sequence

1. **Extend dataclasses.** Add `backend` + `backend_state` fields; add `default_backend` on `Config`. No reader/writer changes yet. Green tests.
2. **Reader migration.** `load_config` populates `backend_state["claude"]` from legacy fields. Writer still emits legacy shape only. Green tests.
   - **2a. JSON-helper readers.** Migrate `load_sessions`, `load_session` to the new shape with flat-key fallback. Green tests (including new `tests/test_config_migration.py` cases for the helpers).
3. **Writer dual-emit.** `save_config` writes new shape + mirrored legacy fields. Migration round-trip tests added. Green tests.
   - **3a. JSON-helper writers.** Migrate `save_session`, `clear_session` to write `backend_state[<active>]["session_id"]` with mirrored flat key. Add `patch_backend_state` / `patch_team_bot_backend_state`. Green tests.
   - **3b. Call-site migration.** Update the 22 call sites across `bot.py`, `manager/bot.py`, `config.py` to use the migrated helpers (or the new `patch_backend_state` helper for model/effort/permissions/show_thinking). Green tests + manual smoke: run a Claude turn end-to-end, inspect the config on disk, confirm `backend_state["claude"]["session_id"]` is populated and the flat `session_id` mirror stays in sync.
4. **Factory input source swap.** Change `bot.py`'s `create(...)` call from flat-fields input to `backend_state[<active>]` input. Factory and `ClaudeBackend` registration are unchanged (inherited from Phase 1 step 3). Green tests.
5. **`/backend` command.** Handler with four forms (including same-backend no-op and activate-first ordering per §4.5). Reject switch with live tasks. Tests added. Green tests.
6. **Capability gating.** `/thinking`, `/permissions`, `/compact`, `/model` routed through capabilities. Cap-probe gate added (calls `probe_health()` only when `supports_usage_cap_detection`). Tests added. Green tests.
7. **Manager `config.py` round-trip.** Add test that locks in preservation of `backend` and `backend_state`. Add `set_project_backend` helper. Green tests.
8. **Manager `bot.py` per-project command migration.** `/edit_project model`, `/edit_project permissions`, `/add_project` wizard, line-1934 project-model callback all write via `patch_backend_state`. Green tests.
9. **Manager global `/model` + `default_model` split.** Rename `default_model` → `default_model_claude`; global `/model` selector consults `default_backend`'s model list. Migration reader mirrors old key. Green tests.
10. **Relay audit + fixes.** Grep pass on team_relay/group_*/tests; apply fixes. Green tests + manual smoke with a team bot.
11. **Help-text + preamble generalization.** Parameterize `_TELEGRAM_AWARENESS` (now at `backends/claude.py`) by capabilities. Smoke-test Claude response quality. Green tests.
12. **Drop `stream.py` shim.** Update any stragglers that still import from `stream`. Green tests. (Note: `claude_client.py` is already deleted by Phase 1 step 6, not here.)

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
- [ ] `stream.py` shim deleted (Phase 2 step 12). `claude_client.py` was already deleted by Phase 1 step 6 — Phase 2 confirms no imports of it remain.
- [ ] `manager/config.py` preserves `backend`/`backend_state` on round-trip (locked in by test).
- [ ] `manager/bot.py` per-project commands (`/edit_project`, `/add_project`, project-model callback at line 1934) write via `patch_backend_state`, not flat keys.
- [ ] Global `/model` in manager consults `default_backend`'s model list. `default_model` renamed to `default_model_claude` with legacy-key mirror.
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
