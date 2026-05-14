# Porting the GitLab plugin system onto the Transport/Backend architecture

**Date:** 2026-05-13 (rev. 2026-05-14 — auth model flipped: `AllowedUser` replaces `allowed_usernames` / `trusted_users` / `trusted_user_ids` rather than living alongside them; rev. 2026-05-14 — migration corrected for `trusted_users` dict shape, team-bot scope clarified, open questions resolved)
**Status:** Approved with revisions; implementation plan reflects this design.
**Author:** Revaz Chikashua (drafted with Claude)

## Summary

Port the **design** of the GitLab fork's plugin system into the primary fork, fitted to the Transport+Backend architecture that landed via `feat/transport-abstraction` (v0.13.0–v0.16.0).

This is **no longer a literal commit-level merge.** The GitLab plugin code was written directly against `python-telegram-bot`, but the primary fork's `bot.py` is now transport-agnostic (all I/O flows through the `Transport` Protocol). We rebuild the plugin framework natively on top of `Transport`, preserving the GitLab design's semantics (entry-point discovery, lifecycle hooks, command/callback registration, Claude-prompt prepend, viewer/executor role model).

The deliverable is **transport-portable plugins**: a single plugin works unchanged against `TelegramTransport`, `WebTransport`, and any future Discord/Slack/Google Chat transport.

This revision (2026-05-14) folds in a **breaking auth-model change**: `AllowedUser` is no longer an additive parallel field — it **replaces** `allowed_usernames`, `trusted_users`, and `trusted_user_ids`. Legacy configs migrate one-way on first load (legacy users → `executor` role, legacy IDs → `locked_user_id` on the matching `AllowedUser`), and the legacy keys are stripped from the on-disk format on next save. Operators upgrading need to run the migration during a quiet window and verify the resulting `allowed_users` list before exposing the bot to traffic.

## Background

The primary fork shipped 236 commits of work between v0.11 and v0.16, including:
- **Transport abstraction** (specs #0, #0a, #0b, #0c, #1): `bot.py` has zero direct telegram imports. Transport Protocol with `IncomingMessage`, `CommandInvocation`, `ButtonClick`, `Buttons`, `PromptSpec`. `TelegramTransport`, `FakeTransport`, `WebTransport` all implement it.
- **Backend abstraction** (phases 1–4): `backends/` package with `AgentBackend` Protocol. Claude and Codex shipped. `BackendCapabilities` gates command behavior (e.g., `/effort` only when supported).
- **Per-chat conversation log** (`conversation_log.py`) for cross-backend continuity.
- **Identity-keyed auth** (`_auth_identity(identity)`) with `transport_id:native_id` rate-limit keys.

The GitLab fork (still at v0.16-with-plugins, never merged here) defines the plugin design we want:
- `plugin.py` (~143 LOC) — `Plugin` base, `PluginContext`, `BotCommand`, `load_plugin` via `importlib.metadata.entry_points(group="lptc.plugins")`.
- Plugin manager UI (toggle per project).
- `plugin-call` CLI subcommand.
- Operational scripts (`restart.sh`, `stop.sh`).
- `AllowedUser{username, role}` (`viewer`/`executor`).

Plugin implementations (`in-app-web-server`, `diff-reviewer`) live in an external `link-project-to-chat-plugins` package — not part of this port.

## Goals

1. Plugin framework matching GitLab's semantics, but **transport-portable** (handlers receive `CommandInvocation`/`IncomingMessage`/`ButtonClick`, not `Update`/`Context`).
2. Plugin toggle UI in the manager bot.
3. `plugin-call` CLI subcommand.
4. `restart.sh`, `stop.sh` operational scripts.
5. `AllowedUser` role model **replaces** existing `allowed_usernames` / `trusted_users` / `trusted_user_ids` as the single source of auth + authority for both **`ProjectConfig`** (per-project allow-list) and **`Config`** (global allow-list — used by the manager bot and as the default for team bots). `TeamBotConfig` continues to have no per-bot allow-list and reads from `Config.allowed_users` (matches existing behavior). Legacy configs migrate on first load; the loader marks the config "migration pending" and the next save writes the new shape with legacy keys stripped. The CLI forces a save on first start after a migration so on-disk migration is deterministic.

## Non-goals

- Wire-compatibility with GitLab plugin packages that expect telegram-PTB handler signatures. Plugin authors will rewrite handlers to the transport-agnostic signature; this is a one-time porting cost that buys multi-transport portability.
- Migrating the primary fork's existing features (team_relay, livestream, personas, skills, voice) to the role model. The role model gates entry, plain-text messages, and state-changing commands only; feature internals are untouched.
- Backend-aware plugins (e.g., a plugin that reacts differently to Claude vs Codex). Plugins see backend output via `on_tool_use`/`on_task_complete` events but don't gate by backend.
- Building any specific plugin (those live in the external package).

## Architecture

Plugins are external Python packages discovered via `importlib.metadata.entry_points(group="lptc.plugins")`. The framework sits **alongside** primary's existing features inside `ProjectBot`:

```
ProjectBot (transport-agnostic)
├─ existing: team_relay, group_filters, personas, skills, conversation_log,
│            voice (transcriber/synthesizer), backend abstraction
├─ Transport (Telegram|Web|Fake|future Discord/Slack)
│   └─ on_message, on_command, on_button, on_prompt_submit, on_ready, set_authorizer
└─ NEW: plugin lifecycle
        ├─ load via entry points (per-project config)
        ├─ topo-sort by depends_on
        ├─ hooks: on_message(IncomingMessage), on_task_complete(Task), on_tool_use(tool, path)
        ├─ Claude prompt prepend via get_context() — Claude backend only
        ├─ command/callback registration on the active Transport
        └─ start()/stop() on bot lifecycle (after Transport ready)
```

Roles (viewer/executor) **replace** the flat allow-list. `AllowedUser{username, role, locked_user_id}` is the sole source of auth + authority for project and team bots. Legacy `allowed_usernames` / `trusted_users` / `trusted_user_ids` are migrated on load (one-way) and dropped from the on-disk format on next save. After migration, role is the only access decision; there is no second layer.

## Components

### New files

- **`src/link_project_to_chat/plugin.py`** (~150 LOC) — the framework:
  - `BotCommand{command, description, handler, viewer_ok: bool = False}`
    - `handler: Callable[[CommandInvocation], Awaitable[None]]` (transport-agnostic)
  - `PluginContext` dataclass:
    - `bot_name: str`
    - `project_path: Path`
    - `bot_username: str`
    - `data_dir: Path | None`
    - `transport: Transport | None` (reference, not the telegram-specific bot token)
    - `backend_name: str` (so plugins can detect Claude vs Codex)
    - `allowed_user_ids: list[int]`, `executor_user_ids: list[int]` (for plugins that gate themselves)
    - `web_port: int | None`, `public_url: str | None` (web-server plugin compatibility)
    - `register_in_app_web_handler: Callable | None`
    - `_send: Callable[..., Awaitable[Any]] | None` (back-compat shim; delegates to `transport.send_text`)
    - Method: `async send_message(chat_id: int | ChatRef, text: str, **kwargs) -> Any` — convenience proxy that builds a `ChatRef` if given an int and calls `transport.send_text(...)`.
  - `Plugin` base class with same hook surface as GitLab:
    - Lifecycle: `start()`, `stop()`
    - Hooks: `on_message(msg: IncomingMessage) -> bool`, `on_task_complete(task)`, `on_tool_use(tool: str, path: str | None)`
    - Claude integration: `get_context() -> str | None`, `tools() -> list[dict]`, `call_tool(name, args) -> str`
    - Registration: `commands() -> list[BotCommand]`, `buttons() -> Callable | None` (button-click handler; consumes by returning True)
  - `load_plugin(name, context, config) -> Plugin | None` via entry points.
- **`scripts/restart.sh`**, **`scripts/stop.sh`** — copied verbatim from GitLab.

### `bot.py` changes (additive, ~120 LOC)

- Import: `from .plugin import Plugin, PluginContext, load_plugin, BotCommand`
- `__init__` gains kwargs:
  - `plugins: list[dict] | None = None`
  - `allowed_users: list | None = None` (`list[AllowedUser]`)
- New instance state: `_plugin_configs`, `_plugins: list[Plugin]`, `_plugin_button_handlers: list[Callable]`, `_plugin_command_handlers: dict[str, Callable]`, `_shared_ctx: PluginContext | None`.
- Module-level `_topo_sort(plugins)` helper (same as GitLab).
- `_init_plugins(transport)` called from `_after_ready` (after `bot_username` is populated):
  - Build `PluginContext(transport=self._transport, backend_name=self._backend_name, ...)`.
  - Instantiate plugins via `load_plugin`, skip missing.
  - For each plugin's `commands()`: wrap handler with auth + role gate (see `_wrap_plugin_command`), then `self._transport.on_command(bc.command, wrapped)`.
  - For each plugin's `buttons()`: register the handler in `self._plugin_button_handlers`.
  - Call `start()` in topo-sorted order; on failure, unregister that plugin's commands (improvement over GitLab default).
- `_dispatch_plugin_on_message(msg)`, `_dispatch_plugin_tool_use(event)`, `_dispatch_plugin_task_complete(task)`, `_dispatch_plugin_button(click)` — all try/except per plugin, all preserve "one plugin doesn't kill the others" semantics.
- `_plugin_context_prepend(prompt)` — joins `get_context()` outputs with `\n\n`, separator `\n\n---\n\n`, prepended to Claude prompt. **Gated to Claude backend**: when `backend_name != "claude"`, returns `prompt` unchanged (Codex/Gemini don't accept arbitrary system text prepends in the same way; capability-checked).
- `_on_text_from_transport(msg)` — after auth (already handled by transport's authorizer) and before submitting to the backend:
  - `consumed = await self._dispatch_plugin_on_message(msg)`; if `consumed`, return.
  - Role check: if `self._require_executor(identity)` is False, reply "Read-only access" and return.
- `_on_button(click)` — before primary's own button dispatch:
  - `if await self._dispatch_plugin_button(click): return`
- `_on_stream_event(task, event)` on `ToolUse` — after primary's existing handling, `await self._dispatch_plugin_tool_use(event)`.
- `_on_task_complete(task)` — at the end, `await self._dispatch_plugin_task_complete(task)` (CANCELLED tasks excluded).
- `_post_stop()` hook (already exists on the new architecture via `Transport.stop`) — calls `_shutdown_plugins()` to invoke `plugin.stop()` in reverse order.
- `_wrap_plugin_command(bc)` — wraps the plugin's handler with `_auth_identity` (defense-in-depth; the transport's authorizer already gated, but cheap) + `_require_executor` gate (skipped when `bc.viewer_ok=True`).

### `config.py` changes (~150 LOC: additive + legacy removal)

- New dataclass:
  ```python
  @dataclass
  class AllowedUser:
      username: str                  # normalized: lowercase, no leading "@"
      role: str = "viewer"           # "viewer" | "executor"
      locked_user_id: int | None = None   # populated on first contact; replaces trusted_user_ids
  ```
- `ProjectConfig` and `Config` (global):
  - Add `plugins: list[dict] = field(default_factory=list)` (ProjectConfig only — plugins are per-project).
  - Add `allowed_users: list[AllowedUser] = field(default_factory=list)`.
  - **Remove** `allowed_usernames`, `trusted_users`, `trusted_user_ids` from the dataclass and from the save format.
- `TeamBotConfig`: **untouched.** Team bots inherit from `Config.allowed_users` (the global allow-list) — same pattern as today. No per-team-bot allow-list is added in this revision. (A future spec can layer per-team-bot allow-lists on top if needed.)
- `_parse_allowed_users` / `_serialize_allowed_users` / `_parse_plugins` helpers.
- **One-shot migration on load**:
  - Legacy `allowed_usernames: list[str]` entries → `AllowedUser{username, role="executor", locked_user_id=None}`. Default role is `executor` because legacy users had full access; preserving that prevents silent privilege loss.
  - Legacy `trusted_users` — **this field is a `dict[str, int | str]` on disk** (username → user_id mapping). For every key in this dict, populate `locked_user_id` on the matching `AllowedUser` (created above if not already present). Confirm shape with `isinstance(raw_trusted, dict)`; older list-shape configs (pre-A1) align with `trusted_user_ids` by index against `allowed_usernames` and are still supported for one release.
  - Legacy `trusted_user_ids: list[int]` is treated as a fallback only when `trusted_users` is missing or empty (matches the current loader semantics in `_effective_trusted_users`).
  - The loader sets a `migration_pending: bool` flag on the returned `Config` object when any legacy field was read. Callers (CLI `start`, manager bot startup) check this flag and call `save_config` once to materialize the new on-disk shape.
- Unknown role on load → log warning, treat as `viewer` (least-privilege).
- Malformed `plugins` entry → log, skip.
- Malformed `allowed_users` entry → log, skip (auth fails closed for that entry; user is denied until corrected).
- Empty `allowed_users` after migration → log WARNING per project at load time; CLI startup phase additionally logs a single CRITICAL line listing all such projects so operators see the issue without per-load log spam.

### `_auth.py` changes (~80 LOC: rewrite, not addition)

`AuthMixin` is rewritten around `allowed_users` as the sole source of truth. Legacy code paths that referenced `allowed_usernames` / `trusted_users` / `trusted_user_ids` are deleted.

- `_get_user_role(identity) -> str | None`:
  - Reads `self._allowed_users` (populated by `ProjectBot.__init__`).
  - **First** checks `locked_user_id` (numeric platform-native ID lock from first contact) — this is the security-critical fast path and prevents username-change attacks.
  - Falls back to a case- and `@`-insensitive username match when no ID is locked yet for that user.
  - On first match by username, atomically writes back `locked_user_id` on the `AllowedUser` and persists the config; subsequent requests are validated by ID, not username. This preserves the brute-force / username-spoof protection from the old `trusted_user_ids` design but applies it uniformly to every allowed user (not just the trusted subset).
  - Returns `"executor"`, `"viewer"`, or `None` (not listed → denied).
- `_auth_identity(identity) -> bool`:
  - True iff `_get_user_role(identity)` returns a role (any non-None).
  - Empty `allowed_users` → deny everyone. **Fail-closed** is the new default; the old laxity around missing-allowlists is gone.
- `_require_executor(identity) -> bool`:
  - True iff `_get_user_role(identity) == "executor"`.
- Brute-force lockout and rate-limit dictionaries are re-keyed on `_identity_key(identity)` (the `f"{transport_id}:{native_id}"` string). The current `_init_auth` already uses this key for `_rate_limits`; `_failed_auth_counts` is migrated to the same keying so Discord/Slack identities can't collide with Telegram ones.
- First-contact write: when `_get_user_role` matches by username and populates `locked_user_id`, it sets `self._auth_dirty = True` on the bot. `ProjectBot._on_text_from_transport` (and other message-handling tails) call `self._persist_auth_if_dirty()` which invokes `save_config` once and clears the flag. Concurrent first-contacts on different users are idempotent (each write sees the same in-memory state); save serialization is handled by the existing `_config_lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows).
- Read-only command set (always allowed for viewers): `/tasks`, `/log`, `/status`, `/help`, `/version`, `/skills` (listing only), `/context` (display side).
- State-changing command set (executor required): plain text messages routed to Claude/Codex, `/run`, `/use`, `/persona`, `/model`, `/effort`, `/thinking`, `/permissions`, `/compact`, `/reset`, `/backend`, `/stop_skill`, `/stop_persona`, `/create_skill`, `/delete_skill`, `/create_persona`, `/delete_persona`, `/voice`, `/lang`, `/halt`, `/resume`, file uploads, voice uploads.
- Startup-ping recipients: `AllowedUser` with `role == "executor"` **and** a non-`None` `locked_user_id`. Viewers do not receive the startup ping. Executors without a locked ID yet are pinged on first contact instead.
- Brute-force lockout and rate-limit keying (`transport_id:native_id`) unchanged.

### `manager/bot.py` changes (~80 LOC)

The manager bot is also transport-ported (via `TelegramTransport`). It uses `CommandInvocation` and `ButtonClick` for handlers.

- Per-project keyboard gains a `Plugins` button.
- `_available_plugins()` — list `lptc.plugins` entry points via `importlib.metadata.entry_points`.
- `_plugins_markup(name)` — `Buttons` with `✓ active` / `+ available` per installed plugin, plus a `« Back` row.
- Button-click branches (via the existing prefix routing):
  - `proj_plugins_{name}` — show the toggle keyboard or "no plugins installed".
  - `proj_ptog_{plugin_name}|{name}` — flip a plugin in/out of the project's `plugins` list and persist via `manager/config.py`.
- "Restart required after changes" hint shown in the toggle message body.

**User-management commands** on the manager bot are updated to operate on `AllowedUser`. These changes are scoped to the manager-bot scope of the global `Config.allowed_users` list (the manager bot is the operator's surface for editing the global allow-list). Project-scoped allow-lists are edited via the per-project keyboard (planned for a follow-up; out of scope for this rev).

- `/users` — list rows as `username (role) [ID locked: <id> | not yet]`.
- `/add_user <username> [viewer|executor]` — default role `executor` (matches legacy `/add_user` semantics: previously all added users had full access).
- `/remove_user <username>` — unchanged signature.
- New: `/promote_user <username>` and `/demote_user <username>` toggle role.
- New: `/reset_user_id <username>` clears `locked_user_id` (recovery path for users whose Telegram ID changed — rare but happens with account migration / re-registration).
- All commands persist by calling `save_config` immediately and reply with the updated `/users` listing.

### `cli.py` changes (~120 LOC)

**New subcommand:** `link-project-to-chat plugin-call <project> <plugin_name> <tool_name> <args_json>`
- Loads project's config to get path/data_dir.
- Builds a minimal `PluginContext` (no transport — standalone mode).
- Calls `plugin.call_tool(tool_name, args)`, prints result.
- Used by Claude via Bash inside a task.

**New subcommand:** `link-project-to-chat migrate-config [--dry-run] [--project NAME]`
- Loads the config (triggering any migration), shows a human-readable diff between on-disk legacy fields and the resulting `allowed_users` list.
- Without `--dry-run`: saves the migrated shape to disk.
- With `--dry-run`: prints what would change; no write.
- With `--project NAME`: limit output to a single project (still includes the global allow-list).
- Exit code: `0` on success; non-zero if any project ends up with an empty `allowed_users` (operators must see this before they expose the bot).

**`configure` subcommand — user-management flags** (operate on the **global** `Config.allowed_users`; project-scoped editing is via the manager bot):
- `--add-user USERNAME[:ROLE]` — adds an `AllowedUser`. Default role `executor`. Examples: `--add-user alice`, `--add-user bob:viewer`.
- `--remove-user USERNAME` — removes the entry.
- `--reset-user-id USERNAME` — clears `locked_user_id` (recovery path).
- Legacy flags `--username` and `--remove-username` are kept as aliases for one release with a deprecation warning, then removed.

**`start` subcommand:**
- `ProjectConfig.plugins` and `ProjectConfig.allowed_users` flow through `run_bot` / `run_bots` once their signatures gain the new kwargs.
- The legacy `--username`/`--token` quick-start path implicitly creates one `AllowedUser{username, role="executor"}` entry (transient, in-memory; not persisted unless `projects add` is used).
- On first start after a migration (loader-set `migration_pending`), the CLI invokes `save_config` to materialize the new shape, logs a one-line "Migrated config.json from legacy auth fields to allowed_users" message, then proceeds to start the bot.
- Startup phase enumerates projects with empty `allowed_users` and logs a single CRITICAL line listing them; this replaces per-load CRITICAL spam.

### `pyproject.toml`

No structural change (plugins declare their own entry points in their own packages). Optional version bump.

## Data flow

### Bot startup
```
ProjectBot.__init__(plugins=[...], allowed_users=[...])
   └─ store _plugin_configs, _allowed_users

build() → Transport instance + set_authorizer + on_ready + on_message + on_button + on_command's

Transport.start() — completes platform-specific init (get_me, delete_webhook, set_my_commands)
   └─ fires on_ready callback

_after_ready(self_identity)
   ├─ self.bot_username = self_identity.handle
   ├─ self._refresh_team_system_note()  (existing)
   ├─ self._init_plugins(self._transport)  ← NEW
   │      ├─ build _shared_ctx = PluginContext(transport=self._transport, ...)
   │      ├─ for cfg in _plugin_configs:
   │      │      plugin = load_plugin(cfg["name"], _shared_ctx, cfg)
   │      │      _plugins.append(plugin)
   │      ├─ for plugin in _plugins:
   │      │      for bc in plugin.commands():
   │      │          wrapped = _wrap_plugin_command(bc)
   │      │          _transport.on_command(bc.command, wrapped)
   │      │      if (button_handler := plugin.buttons()):
   │      │          _plugin_button_handlers.append(button_handler)
   │      └─ for plugin in _topo_sort(_plugins):
   │             try: await plugin.start()
   │             except: unregister this plugin's commands, log
   └─ existing startup pings to trusted users
```

### Incoming text message (plain, not a `/command`)
```
Transport receives platform-native event → builds IncomingMessage
   ├─ Transport.set_authorizer pre-check (auth_identity) — drops unauthorized
   └─ MessageHandler → _on_text_from_transport(msg)
          ├─ existing group-mode filters
          ├─ for plugin in _plugins:
          │      consumed = await plugin.on_message(msg)
          │      if any consumes: return
          ├─ role check (always runs — fail-closed):
          │      if not _require_executor(msg.sender):
          │          reply "Read-only access" and return
          ├─ existing pending_skill / pending_persona handling
          ├─ existing waiting-input routing
          ├─ existing supersede check
          └─ prompt = _plugin_context_prepend(user_text, persona_text, …)
                       (only when backend is Claude)
              → task_manager.submit_claude(...)  / task_manager.submit_codex(...)
```

### Incoming `/command`
```
Transport receives command → CommandInvocation
   ├─ Transport.set_authorizer pre-check
   ├─ command_dispatch → handler (one of primary's _on_X_t or a plugin's wrapped handler)
   └─ For plugin commands: _wrap_plugin_command runs:
          ├─ defense-in-depth auth (already gated by transport, cheap)
          ├─ if not bc.viewer_ok and not _require_executor(invocation.sender):
          │      reply "Read-only access" and return
          └─ await bc.handler(invocation)
```

### Button click
```
Transport receives button click → ButtonClick
   └─ _on_button(click)
          ├─ NEW: for handler in _plugin_button_handlers:
          │       consumed = await handler(click)
          │       if consumed: return
          └─ existing primary button dispatch (ask_, proj_, model_, …)
```

### Tool use & task complete
- `ToolUse` event in `_on_stream_event` → existing handling → `plugin.on_tool_use(event.tool, event.path)` per plugin (try/except).
- Task transitions to DONE/FAILED in `_on_task_complete` → existing handling → `plugin.on_task_complete(task)` per plugin (try/except). CANCELLED tasks not delivered.

### Shutdown
```
Transport.stop() — platform-specific shutdown
   ↑
_post_stop hook (already exists via TelegramTransport's lifecycle)
   └─ _shutdown_plugins(): for plugin in reversed(_plugins): try await plugin.stop()
```

## Error handling

- Every plugin hook wrapped in `try/except Exception`, logging `"plugin %s <hook> failed"` with `exc_info=True`. One bad plugin never blocks others or the bot.
- `start()` failure → log, **unregister** that plugin's commands/buttons, continue.
- `stop()` failure → log, continue.
- `get_context()` raising → log, skip that plugin's contribution for the turn.
- `commands()` / `buttons()` raising during registration → log, skip that plugin entirely.
- `load_plugin` returns `None` (entry point absent) → log clear error, continue.
- `plugin-call` CLI with missing plugin → non-zero exit with a clear message.
- Unknown role string → treat as `viewer` (least-privilege).
- Malformed `plugins` entry → skip, continue.

## Testing

### New tests
- `tests/test_plugin_framework.py` — entry-point discovery, `_topo_sort`, `PluginContext.send_message` proxy (against `FakeTransport`), `Plugin.data_dir` directory creation.
- `tests/test_bot_plugin_hooks.py` — using a `FakePlugin` and `FakeTransport`:
  - `on_message(IncomingMessage)` consumes → backend not called.
  - `on_message` raises → other plugins still run.
  - `get_context()` outputs concatenated and prepended, but **only when backend is Claude**.
  - `on_tool_use` fired per `ToolUse` event.
  - `on_task_complete` fired on DONE and FAILED, NOT CANCELLED.
  - `start()` failure unregisters that plugin's commands.
  - `stop()` called in reverse order on shutdown.
  - Plugin button handler consumes correctly.
- `tests/test_config_allowed_users.py` — `AllowedUser` parse/serialize roundtrip, unknown role → `viewer`, malformed entries skipped, empty-after-migration logs CRITICAL.
- `tests/test_config_migration.py` — golden-file suite covering six legacy shapes:
  (a) `allowed_usernames` only, no trust info.
  (b) `allowed_usernames` + `trusted_users` (dict shape, current on-disk format) covering a subset.
  (c) `allowed_usernames` + `trusted_users` (dict) covering everyone.
  (d) `allowed_usernames` + legacy `trusted_user_ids` list (no `trusted_users` dict) — pre-A1 shape; align by index against `allowed_usernames`.
  (e) Global `Config.allowed_usernames` migrating into `Config.allowed_users` while a project's per-project allow-list is empty (verifies the global path).
  (f) `trusted_users` dict containing a username not in `allowed_usernames` (orphan trust) — must still be migrated into an executor entry; no `allowed_usernames` data loss.
  Each test asserts: in-memory `AllowedUser` shape after load, `migration_pending` flag set on the returned `Config`, saved JSON contains *only* `allowed_users` (no legacy keys), round-trip load-save-load is stable, second load has `migration_pending=False`.
- `tests/test_auth_roles.py` — `Identity`-keyed: viewer denied state-changing commands, executor allowed, no-entry denied (fail-closed), locked `locked_user_id` validates by ID, first-contact races lock the ID atomically.
- `tests/manager/test_bot_plugins.py` — plugin toggle button callback_data, available plugins listed from entry points, toggle updates config.

### Cross-transport coverage
- `tests/test_bot_plugin_hooks.py` uses `FakeTransport` for speed.
- Add at least one integration test using `TelegramTransport` (via the contract-test pattern in `tests/transport/test_contract.py`) to confirm a plugin command round-trips.
- A web-transport plugin test (using `WebTransport`) verifies the transport-portability claim.

### End-to-end integration (new — replaces "manual smoke is enough")
- `tests/test_plugin_e2e_fake.py` — drives a full `ProjectBot` through `FakeTransport`:
  1. Build `ProjectBot` with one allowed executor user and a stub plugin.
  2. Call `bot.build()` and trigger `_after_ready` directly (or via fake `transport.start()`).
  3. Assert `plugin.start()` was called.
  4. Deliver an `IncomingMessage` from the executor via the fake transport's queue.
  5. Assert `plugin.on_message` was called; assert backend got the prepended prompt.
  6. Deliver a `ButtonClick`; assert plugin button handler ran when consuming.
  7. Trigger `transport.stop()`; assert `plugin.stop()` was called in reverse order, `on_stop` callbacks fired.
- `tests/test_auth_migration_e2e.py` — drives the full migration → first-contact → persistence chain:
  1. Write a legacy `config.json` with `allowed_usernames` + `trusted_users` (dict).
  2. `load_config(...)` produces `Config` with `migration_pending=True` and synthesized `AllowedUser` entries.
  3. `save_config(...)` rewrites the file; reload confirms on-disk JSON has *only* `allowed_users`, legacy keys gone.
  4. Build `ProjectBot` with the loaded config and `FakeTransport`; deliver a message from a user who was in `allowed_usernames` but not in `trusted_users` (no locked ID yet).
  5. Assert the auth-dirty flag fires; `_persist_auth_if_dirty()` runs once; on-disk file now shows the populated `locked_user_id`.
  6. Deliver a second message: no new save (idempotent).

### Regression coverage
Existing tests that referenced `allowed_usernames` / `trusted_users` / `trusted_user_ids` need updating (estimate: ~30 tests across `tests/test_auth*.py`, `tests/test_config*.py`, `tests/manager/test_bot*.py`, `tests/test_bot_team_wiring.py`). After those updates, the rest of the suite (1003 → ~970) must continue to pass without modification. Net test count rises with the new migration + role coverage.

### Manual smoke
- Pre-upgrade config (`allowed_usernames: [alice]`, `trusted_users: [alice]`, `trusted_user_ids: [12345]`) → load, then save → on disk: `allowed_users: [{username: alice, role: executor, locked_user_id: 12345}]`. Legacy keys absent.
- Project with `plugins: []` and one `executor` user → identical behavior to today.
- Project with one stub plugin → `start()` logged, command registered, hooks fire.
- Project with `allowed_users: [{username, role: "viewer"}]` → plain message replied "Read-only access", `/tasks` allowed.
- Project with empty `allowed_users` → bot starts, CRITICAL log line, all incoming messages denied.
- Same plugin + same config, start with `--transport web --port 8080` → plugin command works via the browser UI.

## Execution plan (high level)

Branch: `feat/plugin-system` off `main`. Each step a single commit. Tasks 3, 5, and 6 carry the load-bearing auth changes.

1. **Plugin file + scripts + `Transport.on_stop`** — `plugin.py` with transport-aware `PluginContext`, operational scripts, `on_stop` Protocol method implemented across all three transports.
2. **bot.py plugin lifecycle** — `_init_plugins`, dispatch helpers, hook wiring in `_after_ready` / `_on_text_from_transport` / `_on_button` / `_on_stream_event` / `_on_task_complete`; shutdown via `Transport.on_stop`.
3. **Config schema + dict-shape-aware migration + eager save** — `AllowedUser` dataclass; `plugins` + `allowed_users` fields on `ProjectConfig` and `Config` (global); `_migrate_legacy_auth` branches on `isinstance(trusted_users_raw, dict)` to handle the current on-disk shape correctly; loader sets `migration_pending`; legacy-key removal on save; `tests/test_config_migration.py` 6-shape golden-file suite. **`TeamBotConfig` is not modified.**
4. **CLI** — `plugin-call` subcommand; new `migrate-config [--dry-run] [--project NAME]` subcommand; new `--add-user`/`--remove-user`/`--reset-user-id` flags on `configure`; legacy `--username`/`--remove-username` aliased with deprecation warning; `start` invokes `save_config` when `migration_pending` and logs single-line CRITICAL for empty allow-lists at startup phase.
5. **Role enforcement (rewrite, not addition)** — `_get_user_role` / `_auth_identity` / `_require_executor` on `AuthMixin` rewritten around `allowed_users`; `_failed_auth_counts` re-keyed on `_identity_key`; ID-locking moved from `trusted_user_ids` to `AllowedUser.locked_user_id`; `_persist_auth_if_dirty` introduced with its own TDD step; `_wrap_plugin_command`; gates on state-changing handlers; fail-closed on empty allowlist; `tests/test_auth_migration_e2e.py` integration test added.
6. **Manager UI + user-management commands** — plugin toggle (Transport-ported); `/users` / `/add_user` / `/remove_user` / `/promote_user` / `/demote_user` / `/reset_user_id` against `Config.allowed_users` (global allow-list).
7. **Docs + version bump** — README plugin section, README auth-migration section, CHANGELOG entry with **BREAKING CHANGES** call-out, **v1.0.0** bump.

Verification gate after each step: `pytest -q` (must stay at 1003 passing + new tests for that step).

## Risks

- **Plugin commands gated by transport's authorizer plus by `_wrap_plugin_command`'s role check.** Two layers means slightly more logging on denials. Acceptable.
- **`get_context()` is Claude-only.** Plugins that depend on this won't extend Codex/Gemini turns. The contract is documented; plugins should branch on `ctx.backend_name` if they care.
- **Plugin button-handler ordering.** Multiple plugins all see each click; first one to return `True` consumes. Order is plugin-registration order (which matches plugin-config order). Documented in `plugin.py`.
- **`PluginContext.send_message(chat_id)` with int — needs a `ChatRef` to call `transport.send_text`.** The proxy synthesizes `ChatRef(transport_id=transport.TRANSPORT_ID, native_id=str(chat_id), kind=ChatKind.DM)` as a best-effort default; plugins that need a specific kind should pass a `ChatRef` directly.
- **Plugin authors writing telegram-PTB-style handlers will need to migrate.** This is the one-time cost of the transport port. The new signature is simpler (`async def(invocation: CommandInvocation)`) and works on every transport.
- **Auth model is a breaking on-disk change.** Eager migration on first start rewrites `config.json` without the legacy keys. Operators on an older binary reading that file afterward will see no users authorized. Mitigation: bump to v1.0.0, document the migration in the changelog, and have the loader log a one-line "migrating auth model" line. The new `migrate-config --dry-run` subcommand lets operators preview the migration before exposing the bot to traffic.
- **`trusted_users` ⊂ `allowed_usernames` distinction is lost.** Legacy deployments where the DM-ping recipient set was strictly smaller than the allow-list collapse into a single `executor` role; *every* executor now gets the startup ping. If anyone relied on the asymmetry, add a `notify: bool` flag on `AllowedUser` in a follow-up. Realistic risk: low — most deployments had `trusted_users == allowed_usernames` in practice.
- **Locked-ID re-lock race window.** Pre-A1 deployments with the list-shape `trusted_users` and a length-mismatched `trusted_user_ids` lose alignment; affected entries start with `locked_user_id=None` and re-lock on next contact. A username-spoof attempt landing in that window could plant the wrong ID. Mitigation: migrate during quiet windows; the dict-shape `trusted_users` (current format since A1 closed) has explicit name→id mapping and is not affected.
- **Empty-allowlist deployments hard-fail.** Previously, missing-or-empty allowlists may have failed open in obscure paths. New behavior is fail-closed everywhere. The CLI startup logs a single CRITICAL line listing affected projects so the issue is visible (replaces per-load log spam). Pre-upgrade audit step: confirm every active project bot has at least one allowed user, or run `migrate-config --dry-run` and inspect the output.
- **First-contact persistence races.** When `_get_user_role` populates `locked_user_id`, the bot sets `_auth_dirty=True` and the next message-handling tail calls `save_config`. Multiple bots writing to the same `config.json` concurrently are serialized by the existing `_config_lock` (`fcntl.flock` / `msvcrt.locking`). Concurrent first-contacts on different users converge correctly (each save reads the latest in-memory state). The one edge case — concurrent first-contacts on the *same* user with different `native_id` values — is impossible in practice (one user can't impersonate themselves) but the second writer would no-op (id already locked).

## Resolved questions (decisions baked into this rev)

1. **`notify: bool` on `AllowedUser`?** No — ship without it. The pre-v1.0 "trusted = also gets DM ping" semantic collapses into `executor`. If a deployment really cared about the asymmetry, a follow-up spec can add `notify`. Migration default is "every executor gets the startup ping" (matches the most common deployment shape).
2. **CLI flag shape:** Single flag, `--add-user USER[:ROLE]`. Legacy `--username` / `--remove-username` aliased with deprecation warning for one release.
3. **Migration durability:** **Eager save.** The loader sets `migration_pending=True` on the returned `Config`; the CLI's `start` and `start-manager` entry points check this flag and call `save_config` once before serving traffic. A read-only `--dry-run` path (the new `migrate-config --dry-run` subcommand) is the explicit way to preview without writing.
4. **Test coverage for migration:** Six golden-file shapes plus the end-to-end `tests/test_auth_migration_e2e.py` (see Testing).
5. **Team-bot migration semantics:** `TeamBotConfig` is **not** changed. Team bots continue to read from `Config.allowed_users` (the global allow-list), matching today's behavior. The "per-team-bot allow-list" feature is explicitly deferred to a future spec — it's a real feature, just not part of this rev.
6. **`role: "owner"` reserved?** No — only `viewer` and `executor` for v1.0.0. A future spec can add a third role; the loader's "unknown role → viewer" fallback keeps forward-compat on the read side.

## Open questions

None for this rev. All design decisions are baked in. If any of the resolutions above turn out to be wrong in practice, follow-up specs can revisit individually.
