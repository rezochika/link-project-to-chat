# Porting the GitLab plugin system onto the Transport/Backend architecture

**Date:** 2026-05-13 (rev. 2026-05-13 after landing `feat/transport-abstraction`)
**Status:** Approved, awaiting implementation plan
**Author:** Revaz Chikashua (drafted with Claude)

## Summary

Port the **design** of the GitLab fork's plugin system into the primary fork, fitted to the Transport+Backend architecture that landed via `feat/transport-abstraction` (v0.13.0–v0.16.0).

This is **no longer a literal commit-level merge.** The GitLab plugin code was written directly against `python-telegram-bot`, but the primary fork's `bot.py` is now transport-agnostic (all I/O flows through the `Transport` Protocol). We rebuild the plugin framework natively on top of `Transport`, preserving the GitLab design's semantics (entry-point discovery, lifecycle hooks, command/callback registration, Claude-prompt prepend, viewer/executor role model).

The deliverable is **transport-portable plugins**: a single plugin works unchanged against `TelegramTransport`, `WebTransport`, and any future Discord/Slack/Google Chat transport.

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
5. `AllowedUser` role model added **alongside** existing `allowed_usernames`/`trusted_users`/`trusted_user_ids` — legacy projects unaffected.

## Non-goals

- Wire-compatibility with GitLab plugin packages that expect telegram-PTB handler signatures. Plugin authors will rewrite handlers to the transport-agnostic signature; this is a one-time porting cost that buys multi-transport portability.
- Migrating the primary fork's existing features (team_relay, livestream, personas, skills, voice) to the role model.
- Replacing `allowed_usernames`/`trusted_users` on team bots.
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

Roles (viewer/executor) layer as a **second, optional access check** on top of the existing flat allow-list. If a project's config has the new `allowed_users` field populated, role enforcement gates state-changing handlers and plugin commands. If absent, behavior is identical to today.

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

### `config.py` changes (additive, ~60 LOC)

- New dataclass `AllowedUser{username: str, role: str = "viewer"}` (roles: `"viewer"` | `"executor"`).
- `ProjectConfig` gains:
  - `plugins: list[dict] = field(default_factory=list)`
  - `allowed_users: list[AllowedUser] = field(default_factory=list)`
- `_parse_allowed_users` / `_serialize_allowed_users` / `_parse_plugins` helpers.
- **Legacy migration on load (in-memory only)**: if `allowed_users` is empty but `allowed_usernames` has entries, synthesize equivalent `AllowedUser{username, role="executor"}` entries. Don't write back — preserve the on-disk form unless the user explicitly opts in.
- Unknown role → log warning, treat as `viewer` (least-privilege).
- Malformed `plugins` entry → log, skip.

### `_auth.py` changes (~25 LOC)

- Add `_get_user_role(identity) -> str | None`:
  - Reads `self._allowed_users` (populated by `ProjectBot.__init__`).
  - Matches `_normalize_username(identity.handle)` against `AllowedUser.username` (case- and `@`-insensitive).
  - Returns `"executor"`, `"viewer"`, or `None` (not listed).
- `_require_executor(identity) -> bool`:
  - Empty `allowed_users` → legacy path, allow (no behavior change).
  - Role is `executor` → allow.
  - Role is `viewer` or `None` → deny.
- Authority order when `allowed_users` is set: `allowed_users` is authoritative; `allowed_usernames` / `trusted_users` are ignored for role-gated decisions (auth via `_auth_identity` still requires the user to be in `allowed_usernames` — this is layered, not replaced).
- Read-only command set (always allowed for viewers): `/tasks`, `/log`, `/status`, `/help`, `/version`, `/skills` (listing only), `/context` (display side).
- State-changing command set (executor required when roles active): plain text messages routed to Claude/Codex, `/run`, `/use`, `/persona`, `/model`, `/effort`, `/thinking`, `/permissions`, `/compact`, `/reset`, `/backend`, `/stop_skill`, `/stop_persona`, `/create_skill`, `/delete_skill`, `/create_persona`, `/delete_persona`, `/voice`, `/lang`, `/halt`, `/resume`, file uploads, voice uploads.

### `manager/bot.py` changes (~80 LOC)

The manager bot is also transport-ported (via `TelegramTransport`). It uses `CommandInvocation` and `ButtonClick` for handlers.

- Per-project keyboard gains a `Plugins` button.
- `_available_plugins()` — list `lptc.plugins` entry points via `importlib.metadata.entry_points`.
- `_plugins_markup(name)` — `Buttons` with `✓ active` / `+ available` per installed plugin, plus a `« Back` row.
- Button-click branches (via the existing prefix routing):
  - `proj_plugins_{name}` — show the toggle keyboard or "no plugins installed".
  - `proj_ptog_{plugin_name}|{name}` — flip a plugin in/out of the project's `plugins` list and persist via `manager/config.py`.
- "Restart required after changes" hint shown in the toggle message body.

### `cli.py` changes (~30 LOC)

- New subcommand: `link-project-to-chat plugin-call <project> <plugin_name> <tool_name> <args_json>`
  - Loads project's config to get path/data_dir.
  - Builds a minimal `PluginContext` (no transport — standalone mode).
  - Calls `plugin.call_tool(tool_name, args)`, prints result.
  - Used by Claude via Bash inside a task.
- `start` subcommand — `ProjectConfig.plugins` and `ProjectConfig.allowed_users` already flow through `run_bot`/`run_bots` once their signatures gain the new kwargs.

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
          ├─ NEW role check (only if allowed_users set):
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
- `tests/test_config_allowed_users.py` — `AllowedUser` parse/serialize roundtrip, legacy `allowed_usernames` → `executor` migration, unknown role → `viewer`, malformed entries skipped.
- `tests/test_auth_roles.py` — `Identity`-keyed: viewer denied state-changing commands, executor allowed, legacy projects unaffected, mixed list.
- `tests/manager/test_bot_plugins.py` — plugin toggle button callback_data, available plugins listed from entry points, toggle updates config.

### Cross-transport coverage
- `tests/test_bot_plugin_hooks.py` uses `FakeTransport` for speed.
- Add at least one integration test using `TelegramTransport` (via the contract-test pattern in `tests/transport/test_contract.py`) to confirm a plugin command round-trips.
- A web-transport plugin test (using `WebTransport`) verifies the transport-portability claim.

### Regression coverage
All 1003 existing tests must continue to pass without modification.

### Manual smoke
- Project with `plugins: []` and no `allowed_users` → identical behavior to today.
- Project with one stub plugin → `start()` logged, command registered, hooks fire.
- Project with `allowed_users: [{username, role: "viewer"}]` → `/run` denied, `/tasks` allowed.
- Same plugin + same config, start with `--transport web --port 8080` → plugin command works via the browser UI.

## Execution plan (high level)

Branch: `feat/plugin-system` off `main`. Each step a single commit.

1. **Plugin file + scripts** — `plugin.py` with transport-aware `PluginContext`, scripts.
2. **bot.py plugin lifecycle** — `_init_plugins`, dispatch helpers, hook wiring in `_after_ready` / `_on_text_from_transport` / `_on_button` / `_on_stream_event` / `_on_task_complete` / `_post_stop`.
3. **Config schema** — `plugins` field + `AllowedUser`.
4. **CLI** — `plugin-call` subcommand; `start` passes `plugins` and `allowed_users` through `run_bot`/`run_bots`.
5. **Role enforcement** — `_get_user_role` / `_require_executor` on `AuthMixin` (Identity-keyed); `_wrap_plugin_command`; gates on state-changing handlers.
6. **Manager UI** — plugin toggle (Transport-ported, uses `CommandInvocation` / `ButtonClick`).
7. **Docs + version bump** — README plugin section, CHANGELOG entry, optional `v0.17.0`.

Verification gate after each step: `pytest -q` (must stay at 1003 passing + new tests for that step).

## Risks

- **Plugin commands gated by transport's authorizer plus by `_wrap_plugin_command`'s role check.** Two layers means slightly more logging on denials. Acceptable.
- **`get_context()` is Claude-only.** Plugins that depend on this won't extend Codex/Gemini turns. The contract is documented; plugins should branch on `ctx.backend_name` if they care.
- **Plugin button-handler ordering.** Multiple plugins all see each click; first one to return `True` consumes. Order is plugin-registration order (which matches plugin-config order). Documented in `plugin.py`.
- **`PluginContext.send_message(chat_id)` with int — needs a `ChatRef` to call `transport.send_text`.** The proxy synthesizes `ChatRef(transport_id=transport.TRANSPORT_ID, native_id=str(chat_id), kind=ChatKind.DM)` as a best-effort default; plugins that need a specific kind should pass a `ChatRef` directly.
- **Plugin authors writing telegram-PTB-style handlers will need to migrate.** This is the one-time cost of the transport port. The new signature is simpler (`async def(invocation: CommandInvocation)`) and works on every transport.

## Open questions

None at design time. Implementation details defer to the implementation plan.
