# Transport — Manager Bot Port — Design Spec

**Status:** Designed (2026-04-21). Not yet implemented.
**Date:** 2026-04-21
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a)
**Part of:** Final spec in the transport-abstraction follow-up track. Closes the manager-bot port deferred from spec #0a.

---

## 1. Overview

Specs #0, #0b, and #0a ported the project bot's full surface (DM text, voice, group/team) through the `Transport` abstraction. The manager bot — `src/link_project_to_chat/manager/bot.py` (~1,920 lines, 30+ telegram references) — is the last large telegram-coupled module in the codebase. This spec ports its command, button, and file surface to `TelegramTransport`; wires the previously-unused `TelegramTransport.enable_team_relay` (shipped in spec #0a Task 12) so project bots own their relay; moves the last Telethon helper module into `transport/`; and shims `ConversationHandler` wizards onto `IncomingMessage` while leaving the wizard machinery telegram-typed.

**The deliverable**: the manager bot uses `TelegramTransport` for all message/button/file work; `_team_relays` ownership moves from manager to project bot; a lockout test pins the residual telegram surface (`Update` + `ConversationHandler` family); and `manager/telegram_group.py` joins `_telegram_relay.py` under `transport/`.

A portable conversation/wizard primitive on the `Transport` Protocol is **out of scope** — that design problem belongs to spec #1 (Web UI), where cross-platform conversation semantics (Discord modals, Slack Block Kit dialogs, web forms) can drive the abstraction.

## 2. Goals & non-goals

**Goals**
- Manager bot uses `TelegramTransport` for command dispatch, inline buttons, and file ops.
- `manager/telegram_group.py` moves to `transport/_telegram_group.py`. Establishes the invariant: **all `import telethon` lives in `transport/`**.
- `TelegramTransport.enable_team_relay` actually wired: project bots receive a Telethon session-file path (env var) and construct their own `TelegramClient` in `build()`, calling `enable_team_relay(client, peer_handles, group_chat_id, team_name)`. Manager loses `_team_relays` dict and `_start_team_relays` method.
- Wizard step bodies port to `IncomingMessage` (replies via `transport.send_text` + `Buttons`); `ConversationHandler` machinery (states, entry points, fallbacks, `return STATE_X`) stays as-is.
- New `tests/test_manager_lockout.py` enforces a small allowlist of telegram imports in `manager/bot.py`.
- Closes the deferred-port compromise documented in spec #0a Task 12.

**Non-goals (this spec)**
- A portable `Conversation`/`Wizard` primitive on the `Transport` Protocol. Deferred to spec #1.
- Web/Discord/Slack manager bots.
- Removing the last `Update` / `ConversationHandler` references from `manager/bot.py` — would require the conversation primitive.
- Per-platform admin ops (e.g., `Transport.create_team_room(name)`) — the shape doesn't generalize across platforms cleanly enough to justify the abstraction yet.
- Refactoring `manager/process.py` or `manager/config.py` — neither has telegram coupling.
- Restructuring `/setup` Telethon authentication. Manager continues to perform the one-time login at `/setup`; project bots only consume the resulting session file.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-21:

| # | Question | Decision |
|---|---|---|
| 1 | Scope of #0c | A — Pragmatic port (commands + buttons + files via Transport; wizards stay `ConversationHandler` with shim; `telegram_group.py` moves to `transport/`); no new Conversation primitive |
| 2 | How does `enable_team_relay` get wired? | A' — Manager passes Telethon session-file path to each project bot subprocess via env var (`LP2C_TELETHON_SESSION`); project bot constructs its own `TelegramClient` from that file and calls `enable_team_relay` from `build()` |
| 3 | What happens to `manager/telegram_group.py`? | A — Move to `transport/_telegram_group.py`; cements the "all telethon under `transport/`" invariant |
| 4 | Wizard shim shape + lockout allowlist | A — Boundary-only shim (`_incoming_from_update`); step bodies are mostly Transport-native; allowlist permits `Update`, `ConversationHandler`, `ContextTypes`, `MessageHandler`, `CommandHandler`, `CallbackQueryHandler`, `filters` |
| 5 | Strangler step order | 9 steps: move telegram_group → instantiate manager Transport → wire `enable_team_relay` → port commands → port buttons → wizard shim → drop dead imports → add lockout test → sweep + version bump |

## 4. Architecture

### 4.1 `enable_team_relay` wiring

Manager owns the Telethon session (created at `/setup`, written to `<config_dir>/telethon.session`). Today, manager constructs `TeamRelay` directly per team and starts it on team-bot launch. After spec #0c:

- Manager spawns each project bot subprocess with `LP2C_TELETHON_SESSION=<absolute path to .session file>` in the environment.
- Project bot's `build()` checks the env var. If set AND the bot is in team mode AND `team_bot_usernames` + `group_chat_id` are configured:
  - Constructs a `TelegramClient` from the session file (read-only mode if the API supports it; otherwise standard mode with no auth changes).
  - Calls `self._transport.enable_team_relay(client, team_bot_usernames, group_chat_id, team_name)`.
- The transport's `start()` then activates the relay; `stop()` deactivates. Lifecycle ownership moves from manager to project bot.

**Why session-file path, not shared client?** Telethon `TelegramClient` is not safely shared across processes. Each project bot is a subprocess (per `manager/process.py`); each needs its own client. Sharing the **session file** (SQLite) works because all relay-mode usage is read-mostly listening — manager performed authentication at `/setup` and the session file already carries the auth state. Project bots open the file with `read-only=True` semantics where possible (Telethon's `TelegramClient` accepts a `session` kwarg; using `MemorySession.load(...)` after reading the file is a safer alternative if SQLite write contention turns out to matter — to be evaluated at implementation time).

**Manager-side cleanup:**
- `_team_relays: dict[str, TeamRelay]` (instance attribute) — removed.
- `_start_team_relays()` method — removed.
- Direct `from ..transport._telegram_relay import TeamRelay` in `manager/bot.py` — removed (manager no longer touches `TeamRelay`).

### 4.2 `manager/telegram_group.py` → `transport/_telegram_group.py`

`git mv src/link_project_to_chat/manager/telegram_group.py src/link_project_to_chat/transport/_telegram_group.py`. The file is 89 lines of pure Telethon TL helpers (`create_supergroup`, `add_bot`, `promote_admin`, `invite_user`, `delete_supergroup`) used only by the manager's `/create_team` and `/delete_team` wizards. Update import paths in `manager/bot.py` and tests.

This establishes the invariant: **all `import telethon` lives in `src/link_project_to_chat/transport/`**. A future grep for `from telethon` outside `transport/` should be a smell.

The functions stay module-level (not TelegramTransport methods). Rationale: they're admin/provisioning operations called once per team creation, not lifecycle-bound. Wrapping them as Transport methods would add 5 entries to `TelegramTransport`'s surface for a single caller.

### 4.3 Manager-side `TelegramTransport` instantiation

Today, `ManagerBot` constructs `telegram.ext.Application` directly. After spec #0c:

```python
class ManagerBot(AuthMixin):
    def __init__(self, config: ManagerConfig, ...) -> None:
        self._transport = TelegramTransport(token=config.telegram_token, ...)
        self._app = self._transport.app  # exposed accessor
        # ConversationHandlers continue to attach to self._app
```

`TelegramTransport.app` is a new property exposing the underlying `telegram.ext.Application` so callers (the manager's wizards) can register `ConversationHandler` directly. This is the only place a TelegramTransport-specific accessor is needed for #0c — Web/Discord/Slack transports won't have `app` at all (it's TelegramTransport-specific, not on the Protocol).

Manager-side simple commands and button handlers move from direct `app.add_handler(CommandHandler(...))` to `self._transport.on_command("name", handler)` and `self._transport.on_button(handler)`.

### 4.4 Wizard shim

The manager's wizards (`/add_project`, `/create_project`, `/create_team`, `/delete_team`, `/edit_project`, plus any other multi-step flows discovered during implementation — confirm via `grep -n "ConversationHandler.END" manager/bot.py`) continue to use `ConversationHandler`. Each step body's outer signature stays `(update, ctx)`. Inside the step:

```python
async def _wizard_step_2(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    incoming = self._incoming_from_update(update)
    name = incoming.text.strip()
    if not name:
        await self._transport.send_text(incoming.chat, "Name cannot be empty.")
        return STATE_2  # stay in same state
    ctx.user_data["project_name"] = name
    await self._transport.send_text(incoming.chat, f"Got it, {name}.", buttons=Buttons(...))
    return STATE_3
```

The `_incoming_from_update` helper:

```python
def _incoming_from_update(self, update: Update) -> IncomingMessage:
    msg = update.effective_message
    return IncomingMessage(
        chat=chat_ref_from_telegram(update.effective_chat),
        sender=identity_from_telegram_user(update.effective_user),
        text=msg.text or "",
        files=[],  # wizards don't take file inputs today
        reply_to=None,
        native=msg,
    )
```

Step bodies access only `incoming.text`, `incoming.sender`, `incoming.chat`. State-machine bookkeeping (`return ConversationHandler.END`, state IDs, entry-point/fallback wiring) stays telegram-typed.

**`ctx.user_data` stays.** It's the natural state-storage for ConversationHandler steps; no Transport equivalent exists. Documented in the lockout test rationale.

### 4.5 Lockout test

New file: `tests/test_manager_lockout.py`. Same pattern as `test_transport_lockout.py` but with a non-empty allowlist:

```python
ALLOWED_MANAGER_TELEGRAM_IMPORTS: set[str] = {
    "from telegram import Update",
    "from telegram.ext import ConversationHandler, ContextTypes, "
    "MessageHandler, CommandHandler, CallbackQueryHandler, filters",
}
```

Multi-line imports normalized for comparison (whitespace + line-continuation handling). Anything outside the allowlist is a regression. The test pins the conversation-machinery surface as the only telegram coupling permitted in `manager/bot.py` — when spec #1 designs the Conversation primitive, this allowlist becomes empty.

## 5. Migration — strangler step sequence

Nine steps. Each independently landable; the manager + project bots stay functional end-to-end at every step.

### Step 1 — Move `telegram_group.py` into `transport/`

`git mv src/link_project_to_chat/manager/telegram_group.py src/link_project_to_chat/transport/_telegram_group.py`. Update imports in `manager/bot.py` (currently `from .telegram_group import ...` — adapt to `from ..transport._telegram_group import ...`) and in any test files that import directly. Mechanical, mirrors spec #0a Task 11.

**Exit:** `git mv` preserves history (100% similarity); imports updated; manager `/create_team` and `/delete_team` wizards still work (manual smoke acceptable; full unit-test pass required).

### Step 2 — Instantiate `TelegramTransport` for the manager bot

Add `TelegramTransport.app` accessor (returns the underlying `telegram.ext.Application`). In `ManagerBot.__init__`, construct `self._transport = TelegramTransport(token=...)` and use `self._transport.app` where the manager currently constructs/holds the Application. Existing `ConversationHandler` registrations continue to attach to `self._transport.app`. No behavior change — purely substitution of who owns the Application.

**Exit:** manager bot starts/stops cleanly through `self._transport.start()` / `stop()`; `app.add_handler(...)` calls continue to work via `self._transport.app`; existing tests pass.

### Step 3 — Wire `enable_team_relay`; project bots own the relay

In `manager/process.py` (or wherever subprocess spawn happens), pass `LP2C_TELETHON_SESSION=<path>` env var to project bot subprocesses. In `ProjectBot.build()`, if env var set and team-mode active, construct `TelegramClient(session_path, api_id, api_hash)` and call `self._transport.enable_team_relay(client, peer_handles, group_chat_id, team_name)`. Remove `_team_relays`, `_start_team_relays`, and the direct `TeamRelay` import from `manager/bot.py`.

**Exit:** team-mode bot-to-bot relay works as before. New test: a manager test verifies subprocess spawn includes the env var; a project bot test verifies `enable_team_relay` is called when env var is set + team-mode active.

This is the **load-bearing payoff** of #0c — `enable_team_relay` ships used.

### Step 4 — Port simple manager commands through `transport.on_command`

Port every non-wizard `CommandHandler` registration. Concrete enumeration is left to implementation (grep for `app.add_handler(CommandHandler(...))` in `manager/bot.py`), but at minimum: `/projects`, `/teams`, `/start_all`, `/stop_all`, `/users`, `/version`, `/help`, `/model`, `/add_user`, `/remove_user`, `/setup` (if implemented as a single-step status command — if it's actually a wizard, it ports in Step 6 instead). Replace `app.add_handler(CommandHandler("name", self._on_name))` with `self._transport.on_command("name", self._on_name_from_transport)`. Each command's handler body ports from `(update, ctx) -> None` to `(invocation: CommandInvocation) -> None`, accessing `invocation.chat`, `invocation.sender`, `invocation.args`, `invocation.raw_text`. Replies use `self._transport.send_text(invocation.chat, ...)`.

Wizards (`/add_project`, `/create_project`, `/create_team`, `/delete_team`, `/edit_project`) are NOT touched here — they have their own Step 6.

**Exit:** every simple command returns identical output to its pre-port version. Tests in `tests/test_manager_*.py` that exercise commands pass with their assertions adapted to FakeTransport-style observation.

### Step 5 — Port inline-button menus

`InlineKeyboardButton(label, callback_data=value)` → `Button(label, value)`. `InlineKeyboardMarkup(rows)` → `Buttons(rows)`. `CallbackQueryHandler` registrations → `self._transport.on_button(self._on_button_from_transport)`. Callback handler body ports from `(update, ctx)` to `(click: ButtonClick)`, accessing `click.value`, `click.message`, `click.sender`.

**Exit:** every button in the manager UI clicks correctly; menus render identically.

### Step 6 — Wizard shim

For each `ConversationHandler` step body in the manager: convert from direct `update.message.reply_text(...)` and `update.effective_user.username` access to using `_incoming_from_update(update)` to build an `IncomingMessage`, then `incoming.text`, `incoming.sender.handle`, etc. Replies via `self._transport.send_text(incoming.chat, ..., buttons=Buttons(...))`. State-machine returns (state IDs, `ConversationHandler.END`) untouched. `ctx.user_data` continues to be the state store.

**Exit:** every wizard discovered in §4.4 completes its flow identically to pre-port. Test scenarios for each wizard refactored to inject Telegram Updates via the existing pytest helpers (the wizard machinery still consumes Updates at the boundary; this is fine).

### Step 7 — Drop dead telegram imports

After steps 4-6, `InlineKeyboardButton`, `InlineKeyboardMarkup`, and any other telegram imports outside the allowlist (per §4.5) should be unused. Delete them. Verify with grep.

**Exit:** `grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/manager/bot.py` returns only allowlist entries.

### Step 8 — Add `tests/test_manager_lockout.py`

Mirror `tests/test_transport_lockout.py`. Allowlist per §4.5. Run; expect green.

**Exit:** `pytest tests/test_manager_lockout.py` passes; future PRs that add disallowed telegram imports fail this test.

### Step 9 — Final sweep + docs + version bump

- Final grep sweeps confirming Steps 1-8 invariants.
- Run the full test suite.
- Update `where-are-we.md` with spec #0c summary entry under `## Done`.
- Bump `pyproject.toml`: `0.15.0` → `0.16.0` and sync `__init__.py.__version__`.

**Exit:** spec #0c complete.

## 6. Testing approach

Same model as spec #0a:

- **`FakeTransport` powers manager-bot tests.** Manager command and button tests inject `CommandInvocation` / `ButtonClick` and assert on `bot._transport.sent_messages` / `edited_messages` / `sent_files`.
- **Wizard tests stay Update-driven at the boundary.** ConversationHandler dispatch consumes Updates; tests build telegram Updates and call into the wizard step. Inside, the step builds IncomingMessage via the shim and the rest is Transport-native. Where helpful, factor `_incoming_from_update`-equivalent test helpers.
- **Telethon client mocked.** `enable_team_relay` wiring tested with `MagicMock(TelegramClient)`; verify `add_event_handler` is called on transport `start()`, removed on `stop()`.
- **Manager subprocess spawn tested.** A unit test verifies the env var `LP2C_TELETHON_SESSION` is set when spawning a team-mode project bot.
- **Existing tests refactor**: `tests/test_manager_create_team.py`, `tests/test_process_manager_teams.py`, `tests/test_manager_*.py` — refactored where they test commands/buttons; left untouched where they test internal helpers.
- **No new integration tests against real Telegram.** Same policy as #0/#0a/#0b.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Spec #1 (Web UI) | Conversation/Wizard primitive on Transport Protocol |
| Spec #1/#2/#3 | Non-Telegram manager bots |
| Future | Per-platform admin ops (`Transport.create_team_room(name)`, etc.) |
| Future | Removing residual `Update`/`ConversationHandler` references from `manager/bot.py` (depends on Conversation primitive) |
| Future | Telethon session file storage abstraction (multi-process safety) — current shared-file approach assumed sufficient; revisit if Step 3 surfaces races |

## 8. Risks

- **Step 3 is the validation moment.** If the wired `enable_team_relay` doesn't fit cleanly — session-file race conditions across subprocesses, `TelegramClient` connect-time issues at project bot startup, auth-state corruption — the wiring may need a different ownership model. Mitigation: rollback checkpoint after Step 3; if the wiring is awkward, revisit the design before continuing.
- **Telethon session file shared between manager + N project bots.** SQLite under concurrent open is a known fragility. Mitigation: project bots open the session in a non-modifying mode; manager remains the only writer (only at `/setup`, when no project bots are running). Document the constraint; if a race surfaces, fall back to per-bot session files (each project bot performs its own auth at first launch — heavier but safer).
- **Wizard shim leakage.** If a step body needs a field `_incoming_from_update` doesn't populate, devs may fall back to direct `update.*` access, bypassing the shim. Mitigation: keep the shim minimal but complete; per-wizard code review during Step 6.
- **Manager bot is large.** ~1,920 LOC, 4 wizards, ~12 commands, several inline-button menus. Step 6 (wizard shim) has the highest churn. Risk of regressing wizard behavior. Mitigation: run wizard test suite after each wizard's port; manual smoke test of all four wizards before Step 9.
- **`TelegramTransport.app` accessor leaks abstraction.** Exposing the underlying `Application` lets callers bypass the Transport interface. Acceptable for the manager (which legitimately needs `ConversationHandler` integration), but the accessor should be marked Telegram-specific (not on the Protocol) and ideally shrink in scope as the Conversation primitive arrives in spec #1.

## 9. Next steps after this spec ships

Spec #0c closes the Transport story for the *project bot* and *manager bot* against Telegram. The remaining work is additive transports:

1. **Spec #1 — Web UI transport.** First non-Telegram transport. Forces design of the Conversation/Wizard primitive against real cross-platform requirements. Will exercise the `is_relayed_bot_to_bot=False` + native `sender.is_bot=True` bot-to-bot path validated by spec #0a.
2. **Spec #2 — Discord transport.**
3. **Spec #3 — Slack transport.**

After spec #1 ships the Conversation primitive, a follow-up spec can drop the wizard shim from #0c and empty the manager's lockout allowlist — completing the story.
