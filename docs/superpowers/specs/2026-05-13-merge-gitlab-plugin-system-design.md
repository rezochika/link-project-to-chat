# Merging the GitLab plugin system into the primary fork

**Date:** 2026-05-13
**Status:** Approved, awaiting implementation plan
**Author:** Revaz Chikashua (drafted with Claude)

## Summary

Bring the plugin system, operational scripts, and role-based access (`AllowedUser`) from the GitLab fork (`~/PycharmProjects/gl/link-project-to-chat`) into the primary fork (`~/PycharmProjects/link-project-to-chat`) **without disturbing** the primary fork's team-relay, livestream, personas, skills, voice-transcription, and group-chat features.

The primary fork is the base. The GitLab fork is the source of additions. The merge is one-way and additive.

## Background

Both forks share common ancestor `3589cbd` (v0.11.0, 2026-04-12).

- **Primary fork** has diverged with 187 commits, 79 files changed, ~28K LOC added. It contains the major feature work the user wants to preserve: multi-agent teams (`team_relay`, `telegram_group`), personas, skills, voice transcriber, livestream, GitHub client, group filters, plus a heavy security/quality remediation pass.
- **GitLab fork** has diverged with 9 commits, 15 files changed, ~1K LOC added. It contains:
  - A plugin framework (`plugin.py`, ~143 LOC) with entry-point-based discovery, lifecycle hooks, command/callback registration, Claude-prompt prepend.
  - Plugin toggle UI in `manager/bot.py`.
  - `plugin-call` CLI subcommand for Claude-via-Bash tool invocation.
  - Operational scripts (`restart.sh`, `stop.sh`).
  - `AllowedUser{username, role}` model with `viewer`/`executor` roles.

Plugin implementations (`in-app-web-server`, `diff-reviewer`) live in an external `link-project-to-chat-plugins` package, not in either repo. Only the framework needs porting.

A direct `git merge` is rejected: `bot.py` (1910 vs 967 LOC), `manager/bot.py` (1944 vs 540), `config.py` (540 vs 206) are heavily divergent — three-way conflicts would be tedious and regression-prone.

## Goals

1. Plugin framework available in the primary fork, identical in semantics to GitLab.
2. Plugin toggle UI in the manager bot.
3. `plugin-call` CLI subcommand.
4. `restart.sh`, `stop.sh` operational scripts.
5. `AllowedUser` role model added **alongside** existing `allowed_usernames` and `trusted_user_ids` — legacy projects unaffected.

## Non-goals

- Migrating any of the primary fork's existing features (team_relay, livestream, etc.) to the role model.
- Replacing `allowed_usernames` / `trusted_user_ids` on team bots.
- Bringing in the rest of GitLab's drift (different `_auth.py` shape, different `task_manager.py`, etc.).
- Building any specific plugin (those live in the external package).

## Architecture

Plugins remain external Python packages discovered via the `lptc.plugins` entry-point group. The framework sits **alongside** primary's existing features inside `ProjectBot`:

```
ProjectBot
├─ existing: team_relay, livestream, personas, skills, group_filters
└─ NEW: plugin lifecycle
        ├─ load via entry points (per-project config)
        ├─ topo-sort by depends_on
        ├─ hooks: on_message, on_task_complete, on_tool_use
        ├─ Claude prompt prepend via get_context()
        ├─ command/callback registration
        └─ start()/stop() on bot lifecycle
```

Roles (viewer/executor) layer as a **second, optional access check** on top of the existing flat allow-list. If a project's config has the new `allowed_users` field populated, role enforcement gates state-changing commands. If absent, behavior is identical to today.

## Components

### New files

- **`src/link_project_to_chat/plugin.py`** — copied from GitLab (~143 LOC) with one additive field on `BotCommand`:
  - `BotCommand{command, description, handler, viewer_ok: bool = False}` (the `viewer_ok` field is new — defaults to False so existing GitLab plugins remain executor-only by default)
  - `PluginContext` dataclass (bot_name, project_path, bot_token, trusted_user_id, allowed_user_ids, executor_user_ids, bot_username, data_dir, web_port, public_url, register_in_app_web_handler, _send, send_message)
  - `Plugin` base class (lifecycle, hooks, registration, claude-integration)
  - `load_plugin(name, context, config)` via `importlib.metadata.entry_points(group="lptc.plugins")`
- **`scripts/restart.sh`**, **`scripts/stop.sh`** — copied verbatim from GitLab.

### `bot.py` changes (additive, ~80 LOC)

- Import `Plugin`, `PluginContext`, `load_plugin`.
- `__init__` accepts `plugins: list[dict] | None = None`; stores `_plugin_configs`, initializes `_plugins`, `_plugin_callbacks`, `_shared_ctx`.
- New module-level `_topo_sort(plugins)` helper.
- During startup (after `bot.get_me()`):
  - Build `_shared_ctx = PluginContext(...)` with bot identity and `_send=self._send_message`.
  - Instantiate plugins via `load_plugin`, skip-and-log on `None`.
  - Register `plugin.commands()` as Telegram `CommandHandler`s.
  - Register `plugin.callbacks()` into `self._plugin_callbacks` (prefix-keyed).
  - Call `plugin.start()` in topo-sorted order; on failure, **unregister** that plugin's commands/callbacks (improvement over GitLab default).
- During incoming-message dispatch (after auth, after the new role check, before Claude):
  - Loop plugins, call `on_message(...)`. If any returns `True`, consume and return. Errors logged per plugin and treated as `consumed=False`.
- Before building Claude prompt:
  - Concatenate non-None `get_context()` outputs with `\n\n` separators; prepend with `\n\n---\n\n` divider in front of the existing persona/skill/user content.
- On `ToolUse` stream event (after existing livestream handling):
  - Call `plugin.on_tool_use(event.tool, event.path)` for each plugin, try/except per plugin.
- On task transition to DONE/FAILED (after existing `_on_task_complete` logic):
  - Call `plugin.on_task_complete(task)` for each plugin, try/except per plugin. CANCELLED tasks are NOT delivered.
- Callback dispatcher consults `self._plugin_callbacks` first (prefix match) before falling through to existing handlers.
- On bot shutdown (before existing teardown):
  - `for plugin in reversed(self._plugins): await plugin.stop()` with try/except.

### `config.py` changes (additive, ~60 LOC)

- New dataclass:
  ```python
  @dataclass
  class AllowedUser:
      username: str
      role: str = "viewer"  # "viewer" | "executor"
  ```
- `ProjectConfig` gains:
  - `plugins: list[dict] = field(default_factory=list)`
  - `allowed_users: list[AllowedUser] = field(default_factory=list)`
- `_parse_allowed_users(raw_list)` and `_serialize_allowed_users(users)` helpers.
- **Legacy migration on load (in-memory only)**: if `allowed_users` is empty but `allowed_usernames` is non-empty, synthesize equivalent `AllowedUser` entries with `role="executor"`. Do **not** write back to disk — preserve the on-disk form unless the user explicitly opts in.
- Unknown role string → log warning, treat as `viewer` (least-privilege).
- Malformed `plugins` entries (missing `name`) → log, skip the entry, keep others.

### `_auth.py` changes (~25 LOC)

- `_get_user_role(user_id, username) -> str | None` — consults the current project's `allowed_users`. Returns `"executor"`, `"viewer"`, or `None`.
- `_require_executor()` helper for state-changing handlers. Behavior:
  - If `allowed_users` is empty → legacy path, allow (no behavior change).
  - If user resolves to `executor` → allow.
  - If user resolves to `viewer` → deny with "Read-only access".
  - If user resolves to `None` → deny with the existing "Not authorized" message.
- Read-only command set (always allowed for viewers): `/tasks`, `/log`, `/status`, `/help`, `/version`, `/skills` (list).
- State-changing command set (require executor when roles active): plain Claude messages, `/run`, `/use`, `/persona`, `/model`, `/effort`, `/permissions`, `/reset`, `/compact`, `/voice`, `/stop_skill`, `/stop_persona`, `/create_skill`, `/delete_skill`, `/create_persona`, `/delete_persona`, `/thinking`.
- Plugin-registered commands: treated as **executor-only by default**. A plugin can mark a command as viewer-safe by setting an attribute on its `BotCommand` (e.g., `viewer_ok: bool = False` — added as a small forward-compatible extension to the `BotCommand` dataclass). This keeps viewers from triggering unknown side effects via plugin commands.
- Authority order when `allowed_users` is set: `allowed_users` is authoritative; `allowed_usernames` and `trusted_user_ids` are ignored for that project. A user in `trusted_user_ids` who is NOT in `allowed_users` is denied.

### `manager/bot.py` changes (~50 LOC)

- Per-project keyboard gains a `Plugins` button.
- `_available_plugins()` — list `lptc.plugins` entry points.
- `_plugins_markup(name)` — render toggle buttons (`✓ active`, `+ available`) using `callback_data=f"proj_ptog_{plugin_name}|{name}"`.
- `_cb_proj_plugins(query, name)` — show the toggle keyboard, or a "no plugins installed" message.
- `_cb_proj_ptog(query, suffix)` — flip a plugin in/out of the project's `plugins` list and persist via `manager/config.py`.
- "Restart required after changes" hint shown in the message body.

### `cli.py` changes (~25 LOC)

- New subcommand: `link-project-to-chat plugin-call <project> <plugin_name> <tool_name> <args_json>`
  - Loads the named plugin standalone (no bot), invokes `await plugin.call_tool(tool_name, args)`, prints the result.
  - Intended for Claude to call via Bash inside a task.
- `start` subcommand passes `plugins=proj.plugins or None` into `ProjectBot(...)`.

### `pyproject.toml`

- No structural change (plugins declare their own entry points in their own packages).
- Optional version bump to mark the addition.

## Data flow

### Bot startup
```
ProjectBot.__init__(plugins=[{"name": "..."}, ...])
   └─ store _plugin_configs

start() / post_init
   ├─ existing: persona load, skill load, team_relay setup, livestream init
   ├─ build _shared_ctx = PluginContext(...)
   ├─ for cfg in _plugin_configs:
   │      plugin = load_plugin(cfg["name"], _shared_ctx, cfg)
   │      _plugins.append(plugin) if plugin else log
   ├─ register each plugin's commands() and callbacks()
   └─ for plugin in topo_sort(_plugins):
          try: await plugin.start()
          except: unregister this plugin's commands/callbacks, log
```

### Incoming message (plain text, not a `/command`)
```
update → existing auth check (user in allow list at all?)
       → for plugin in _plugins:
              consumed = await plugin.on_message(...)
              if consumed: return        ← plugins see viewer + executor messages
       → role check (only if allowed_users set and the message would go to Claude):
              if viewer → reply "Read-only access" and stop
       → existing path: build Claude prompt
              prompt = plugin_get_context_concat() + "\n\n---\n\n"
                       + persona_text + skill_text + user_text
       → Claude streams response (existing)
```

### Incoming `/command`
```
update → existing auth check
       → role check (only if allowed_users set):
              if command is read-only → allow (viewer + executor)
              elif user is executor   → allow
              elif user is viewer     → reply "Read-only access" and stop
       → existing handler dispatch (including plugin-registered commands, which
         are executor-only by default unless the plugin marks them viewer_ok)
```

### Tool use / task complete
- `ToolUse` event → existing handling → `plugin.on_tool_use(event.tool, event.path)` per plugin (try/except).
- Task transitions to DONE/FAILED → existing `_on_task_complete` → `plugin.on_task_complete(task)` per plugin (try/except). CANCELLED tasks are not delivered.

### Shutdown
```
for plugin in reversed(_plugins):
   try: await plugin.stop()
   except: log warning
existing teardown
```

### Role decision
```
if not project.allowed_users:                  # legacy
    proceed
elif role_for(user) == "executor":
    proceed
elif role_for(user) == "viewer":
    if handler is read-only: proceed
    else: reply "Read-only access" and stop
else:
    reply "Not authorized" and stop
```

## Error handling

- Every plugin hook wrapped in `try/except Exception`, logging
  `"plugin %s <hook> failed"` with `exc_info=True`. One bad plugin never blocks others or the bot.
- `start()` failure → log, **unregister** that plugin's commands/callbacks, continue starting others.
- `stop()` failure → log, continue (best-effort cleanup).
- `get_context()` raising → log, skip that plugin's contribution for that turn.
- `commands()` / `callbacks()` raising during registration → log, skip that plugin entirely.
- `load_plugin` returns `None` (entry point absent) → log clear error, continue.
- `plugin-call` CLI with missing plugin → non-zero exit with a clear message.
- Unknown role string → treat as `viewer` (least-privilege).
- Malformed `plugins` entry → skip, continue with others.

## Testing

### New tests
- `tests/test_plugin_framework.py` — entry-point discovery (with fake EP fixture), `_topo_sort` chains, missing deps, `PluginContext.send_message` proxy, `Plugin.data_dir` directory creation.
- `tests/test_bot_plugin_hooks.py` — using a `FakePlugin`:
  - `on_message` consumes → Claude not called.
  - `on_message` returns False → Claude called.
  - `on_message` raises → other plugins still run, Claude still called.
  - `get_context` outputs concatenated and prepended.
  - `on_tool_use` fired per tool event.
  - `on_task_complete` fired on DONE and FAILED, NOT on CANCELLED.
  - `start()` failure unregisters that plugin's commands; bot still starts.
  - `stop()` called in reverse order on shutdown.
  - Plugin command + callback registration end-to-end.
- `tests/test_config_allowed_users.py` — `AllowedUser` parse/serialize roundtrip, legacy `allowed_usernames` → `executor` migration, unknown role → `viewer`, malformed entries skipped.
- `tests/test_auth_roles.py` — viewer denied state-changing commands, executor allowed, legacy projects (empty `allowed_users`) unaffected, mixed list.
- `tests/manager/test_bot_plugins.py` — plugin toggle button callback_data, listing available plugins, toggle updates config.

### Regression coverage
All existing tests in `tests/` must continue to pass without modification.

### Manual smoke
- Project with `plugins: []` and no `allowed_users` → identical behavior to today.
- Project with one locally-installed stub plugin → `start()` logged, command registered, hooks fire.
- Project with `allowed_users: [{username, role: "viewer"}]` for the current user → `/run` denied, `/tasks` allowed.

## Execution plan (high level)

Work on branch `feat/plugin-system` off `main`. Each step is its own commit so it can be reviewed and reverted independently.

1. **Plugin file + scripts** — copy `plugin.py`, `scripts/restart.sh`, `scripts/stop.sh`. No wiring yet. Tests: import-only sanity test.
2. **bot.py plugin lifecycle wiring** — instantiate, register, start/stop, all hooks, prompt prepend. Tests: `test_bot_plugin_hooks.py`, `test_plugin_framework.py`. Bot still runs without `plugins` config.
3. **Config schema** — `plugins` field on `ProjectConfig`, `AllowedUser` dataclass + parser/serializer, legacy migration. Tests: `test_config_allowed_users.py`. No behavior change yet for legacy configs.
4. **CLI `plugin-call` + `start` wiring** — pipe `plugins` from config to bot. Tests: existing `test_cli.py` extended.
5. **Role enforcement in `_auth.py`** — `_get_user_role`, `_require_executor`, read-only allow-list. Tests: `test_auth_roles.py`. Legacy projects unaffected.
6. **Manager UI** — plugin toggle. Tests: `tests/manager/test_bot_plugins.py`.
7. **Docs + smoke test** — README section on plugins, manual smoke test, optional version bump.

Verification gate at the end of each commit: run `pytest tests/` and confirm no regression. Final gate before merge: full test suite + manual smoke with stub plugin.

## Risks

- **Plugin start ordering interacts with primary's team_relay / livestream / persona init.** Mitigation: build `_shared_ctx` after `bot.get_me()` and run plugin `start()` after primary's own setup completes. If a plugin needs earlier hooks, that's a future change.
- **Role enforcement in command handlers must cover every state-changing path.** Mitigation: explicit allow-list of read-only commands; everything else falls through to the executor check. Tests assert each command name in both directions.
- **`get_context()` prepend interacts with persona and skill prepends.** Mitigation: place plugin context **before** persona/skill text but inside the system-prompt slot, separated by `\n\n---\n\n`. Tests assert ordering.
- **Manager bot's plugin UI assumes single-machine entry-point discovery.** Mitigation: only show installed plugins; "no plugins installed" branch handles the empty case.

## Open questions

None at design time. Implementation details (exact handler decorators, exact hook placement in the streaming loop) are deferred to the implementation plan.
