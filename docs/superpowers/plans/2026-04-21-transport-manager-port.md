# Manager Bot Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the manager bot's command/button/file surface to `TelegramTransport`; wire `enable_team_relay` (shipped unused in spec #0a) so project bots own their relay; move `manager/telegram_group.py` into `transport/`; shim `ConversationHandler` wizard step bodies onto `IncomingMessage`; pin residual telegram imports with a lockout test.

**Architecture:** Nine-step strangler split into 17 executable tasks. Steps 1-3 build substrate (move telegram_group, add `TelegramTransport.app` accessor, manager constructs Transport). Steps 4-6 wire `enable_team_relay`. Steps 7-14 port commands, buttons, and wizards. Steps 15-17 cleanup + lockout + version bump. Each task is independently shippable; manager + project bots stay functional end-to-end at every step.

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22.0`, `telethon>=1.36`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), existing Transport abstraction (specs #0/#0a/#0b).

**Reference spec:** [docs/superpowers/specs/2026-04-21-transport-manager-port-design.md](docs/superpowers/specs/2026-04-21-transport-manager-port-design.md)

---

## File Structure

**Create:**
- `tests/test_manager_lockout.py` — enforces telegram-import allowlist for `manager/bot.py` (Task 16).

**Modify:**
- `src/link_project_to_chat/transport/telegram.py` — add `app` property accessor (Task 2).
- `src/link_project_to_chat/transport/__init__.py` — re-export new accessor if needed (Task 2).
- `src/link_project_to_chat/manager/bot.py` — large changes across Tasks 3-15.
- `src/link_project_to_chat/manager/process.py` — pass `LP2C_TELETHON_SESSION` env var to subprocesses (Task 4).
- `src/link_project_to_chat/bot.py` — `build()` constructs `TelegramClient` from env var and calls `enable_team_relay` (Task 5).
- `tests/transport/test_telegram_transport.py` — test `app` accessor (Task 2).
- `tests/test_process_manager_teams.py` — test env var passed to subprocess (Task 4).
- `tests/test_bot_team_wiring.py` — test `enable_team_relay` invoked from `build()` (Task 5).
- `tests/test_manager_create_team.py` — refactor as commands/buttons port (Tasks 8-10) and wizards port (Tasks 11-14).
- `tests/test_team_relay.py` — no change needed (lives at `transport/_telegram_relay.py` already from spec #0a Task 11).
- `where-are-we.md` — spec #0c summary entry (Task 17).
- `pyproject.toml` — version bump 0.15.0 → 0.16.0 (Task 17).
- `src/link_project_to_chat/__init__.py` — sync `__version__` to 0.16.0 (Task 17).

**Move:**
- `src/link_project_to_chat/manager/telegram_group.py` → `src/link_project_to_chat/transport/_telegram_group.py` (Task 1).

**Not touched by this plan:**
- `src/link_project_to_chat/manager/config.py` — pure data, no telegram coupling.
- `src/link_project_to_chat/manager/__init__.py` — empty.
- `src/link_project_to_chat/transport/_telegram_relay.py` — moved in spec #0a; #0c only changes its caller (Task 6 removes manager's direct usage).
- `src/link_project_to_chat/group_state.py`, `group_filters.py` — already ported in spec #0a.

---

## Task 1: Move `telegram_group.py` into `transport/`

**Files:**
- Move: `src/link_project_to_chat/manager/telegram_group.py` → `src/link_project_to_chat/transport/_telegram_group.py`
- Modify: `src/link_project_to_chat/manager/bot.py` (import paths only)
- Modify: any test file that imports from `manager.telegram_group`

- [ ] **Step 1.1: Pre-flight grep**

```bash
grep -rn "manager\.telegram_group\|manager/telegram_group\|from \.telegram_group" src tests 2>&1 | head -20
```

Identify every importer. Note line numbers.

- [ ] **Step 1.2: Perform the git move**

```bash
git mv src/link_project_to_chat/manager/telegram_group.py src/link_project_to_chat/transport/_telegram_group.py
```

- [ ] **Step 1.3: Update import paths in `manager/bot.py`**

Find every `from .telegram_group import ...` (relative-to-manager). Replace with `from ..transport._telegram_group import ...` (cross-package relative — manager is one level under root, transport is one level under root, so `..transport._telegram_group` is correct).

Re-grep to confirm zero remaining references to the old path:
```bash
grep -n "manager\.telegram_group\|telegram_group" src/link_project_to_chat/manager/bot.py | grep -v "_telegram_group"
```
Expected: zero matches.

- [ ] **Step 1.4: Update import paths in test files**

```bash
grep -rn "manager\.telegram_group\|from .*manager\.telegram_group" tests 2>&1
```

For each test file: replace `from link_project_to_chat.manager.telegram_group import ...` with `from link_project_to_chat.transport._telegram_group import ...`.

- [ ] **Step 1.5: Run tests**

```bash
pytest tests/test_manager_create_team.py tests/test_team_relay.py tests/test_process_manager_teams.py tests/transport/ tests/test_transport_lockout.py -q
```
Expected: all green.

- [ ] **Step 1.6: Verify rename history preserved**

```bash
git log --follow --oneline src/link_project_to_chat/transport/_telegram_group.py | head -3
```
Should reach the original file's commits.

- [ ] **Step 1.7: Commit**

```bash
git add -A
git commit -m "refactor(transport): move manager/telegram_group.py into transport/_telegram_group.py"
```

(Use `git add -A` to capture the rename + import updates atomically.)

---

## Task 2: Add `TelegramTransport.app` accessor

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 2.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_app_property_returns_underlying_application():
    """TelegramTransport.app exposes the underlying telegram.ext.Application
    so the manager bot can attach ConversationHandlers directly."""
    t, _bot = _make_transport_with_mock_bot()
    app = t.app
    assert app is t._app  # exposes the same instance
    assert hasattr(app, "add_handler")  # quacks like an Application
```

- [ ] **Step 2.2: Run test, confirm failure**

```bash
pytest tests/transport/test_telegram_transport.py::test_app_property_returns_underlying_application -v
```
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute 'app'`.

- [ ] **Step 2.3: Add the property**

In `src/link_project_to_chat/transport/telegram.py`, find the `TelegramTransport` class. Add this property near the other public methods (e.g., near `enable_team_relay` or after `__init__`):

```python
    @property
    def app(self):
        """Expose the underlying telegram.ext.Application.

        TelegramTransport-specific accessor — NOT on the Transport Protocol.
        Used by callers that need to attach legacy handlers (e.g.,
        ConversationHandler) that don't yet have a Transport equivalent.
        """
        return self._app
```

No type annotation on the return value — `telegram.ext.Application` is a heavy import; if the file already imports it at top level, annotate. Otherwise leave untyped (consistent with `_app` which is also untyped).

- [ ] **Step 2.4: Run test, confirm pass**

```bash
pytest tests/transport/test_telegram_transport.py -q
```
Expected: all PASS (existing + new).

Regression:
```bash
pytest tests/transport/ tests/test_transport_lockout.py -q
```
Expected: green.

- [ ] **Step 2.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.app accessor for ConversationHandler integration"
```

---

## Task 3: Manager bot constructs `TelegramTransport` in `__init__`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 3.1: Inspect current Application construction**

```bash
grep -n "Application\.builder\|ApplicationBuilder\|self\._app\s*=" src/link_project_to_chat/manager/bot.py | head -10
```

Identify where the Application is currently created. Note the call site and any builder configuration (token, parse_mode, etc.).

- [ ] **Step 3.2: Replace Application construction with TelegramTransport**

In `ManagerBot.__init__` (or wherever `self._app` is currently assigned):

**Before** (illustrative):
```python
self._app = Application.builder().token(config.telegram_token).build()
```

**After**:
```python
from ..transport import TelegramTransport
self._transport = TelegramTransport(token=config.telegram_token)
self._app = self._transport.app  # alias preserves existing add_handler call sites
```

If `TelegramTransport`'s constructor accepts other kwargs (e.g., `parse_mode`, `concurrent_updates`), pass them through to match prior behavior. Verify by reading `TelegramTransport.__init__` in `src/link_project_to_chat/transport/telegram.py`.

If the manager passes any builder-only options (like `concurrent_updates(True)`), check whether `TelegramTransport.__init__` supports them. If not, this is the moment to add a passthrough kwarg to TelegramTransport's `__init__` — flag it to the controller via `DONE_WITH_CONCERNS` if such a kwarg is needed but not yet supported.

- [ ] **Step 3.3: Verify ConversationHandler wiring still works**

The existing `app.add_handler(ConversationHandler(...))` calls don't change — they go through `self._app` which now aliases `self._transport.app`. Visually scan to confirm no direct `Application.builder()` references remain in `manager/bot.py`:

```bash
grep -n "Application\.builder\|ApplicationBuilder" src/link_project_to_chat/manager/bot.py
```
Expected: zero matches.

- [ ] **Step 3.4: Run tests**

```bash
pytest tests/test_manager_create_team.py tests/test_process_manager_teams.py tests/test_manager_*.py -q 2>&1 | tail -10
```
Expected: green (no behavior change; only construction site moved).

Also run the full transport-layer suite:
```bash
pytest tests/transport/ tests/test_transport_lockout.py -q
```
Expected: green.

- [ ] **Step 3.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "refactor(manager): instantiate TelegramTransport for the manager bot"
```

---

## Task 4: Pass `LP2C_TELETHON_SESSION` env var to project bot subprocesses

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_teams.py`

- [ ] **Step 4.1: Inspect subprocess spawn**

```bash
grep -n "subprocess\|Popen\|spawn\|env\s*=" src/link_project_to_chat/manager/process.py | head -10
```

Identify where the project bot subprocess is launched. Note how `env` is currently constructed (likely `os.environ.copy()` plus per-team overrides).

- [ ] **Step 4.2: Inspect Telethon session path**

```bash
grep -n "telethon\.session\|session_path\|telethon_session" src/link_project_to_chat/manager/bot.py src/link_project_to_chat/manager/config.py src/link_project_to_chat/manager/process.py | head -10
```

Note where the session-file path is determined (likely `<config_dir>/telethon.session`).

- [ ] **Step 4.3: Write failing test**

In `tests/test_process_manager_teams.py`, append (or add to relevant test class):

```python
def test_team_bot_spawn_passes_telethon_session_env_var(tmp_path, monkeypatch):
    """When spawning a team-mode project bot, the manager passes the Telethon
    session file path via LP2C_TELETHON_SESSION env var."""
    from link_project_to_chat.manager.process import build_project_bot_env  # adapt import to actual helper name

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    env = build_project_bot_env(
        team_name="acme",
        config_dir=tmp_path,
        # adapt remaining args to actual signature
    )
    assert env.get("LP2C_TELETHON_SESSION") == str(session_path)
```

If `build_project_bot_env` (or equivalent) doesn't exist yet — i.e., the env construction is inline in a `Popen(...)` call — the test should construct a minimal `ProcessManager` (or whatever the spawn class is) and inspect the env it would pass. Adapt the test to the actual spawn API after Step 4.1's inspection.

- [ ] **Step 4.4: Run test, confirm failure**

```bash
pytest tests/test_process_manager_teams.py::test_team_bot_spawn_passes_telethon_session_env_var -v
```
Expected: FAIL — env var not set.

- [ ] **Step 4.5: Wire the env var**

In `src/link_project_to_chat/manager/process.py`, in the spawn function, add the env var when the bot is in team mode and the session file exists:

```python
env = os.environ.copy()
# ... existing per-team overrides ...
session_path = config_dir / "telethon.session"
if session_path.exists():
    env["LP2C_TELETHON_SESSION"] = str(session_path)
```

Place this near the other env-var assignments. The check is `session_path.exists()` rather than unconditional — solo-mode bots and bots launched before `/setup` shouldn't get the env var.

If the env construction is inlined into `Popen(env=...)`, refactor to build the env dict in a named helper (`_build_env_for_team_bot(...)` or similar) so the test in Step 4.3 has something to call.

- [ ] **Step 4.6: Run test, confirm pass**

```bash
pytest tests/test_process_manager_teams.py -v 2>&1 | tail -15
```
Expected: all PASS.

Regression:
```bash
pytest tests/test_manager_create_team.py tests/transport/ tests/test_transport_lockout.py -q
```
Expected: green.

- [ ] **Step 4.7: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_teams.py
git commit -m "feat(manager): pass LP2C_TELETHON_SESSION to team-mode project bot subprocesses"
```

---

## Task 5: Project bot constructs `TelegramClient` and calls `enable_team_relay`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `tests/test_bot_team_wiring.py`

- [ ] **Step 5.1: Locate the project bot's build/start lifecycle**

```bash
grep -n "def build\|def start\|def __init__\|self\._transport\.start" src/link_project_to_chat/bot.py | head -20
```

Identify where the project bot constructs its `TelegramTransport` and where `self._transport.start()` is called. The `enable_team_relay` call must happen AFTER transport construction and BEFORE `start()` (per `enable_team_relay`'s docstring).

- [ ] **Step 5.2: Write failing test**

In `tests/test_bot_team_wiring.py`, append:

```python
@pytest.mark.asyncio
async def test_team_mode_bot_calls_enable_team_relay_when_session_env_set(tmp_path, monkeypatch):
    """When LP2C_TELETHON_SESSION is set and the bot is in team mode,
    build() constructs a TelegramClient and calls enable_team_relay."""
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    # Build a team-mode ProjectBot with a fake transport so we can spy on enable_team_relay.
    bot = _make_team_bot_for_test(  # use existing test factory; verify name via grep
        team_name="acme",
        bot_username="acme_dev_bot",
        peer_bot_username="acme_manager_bot",
        group_chat_id=-100123,
    )
    bot._transport = MagicMock()
    bot._transport.enable_team_relay = MagicMock()

    with patch("link_project_to_chat.bot.TelegramClient") as MockClient:
        await bot.build()  # or whichever lifecycle method wires the relay

    MockClient.assert_called_once()
    bot._transport.enable_team_relay.assert_called_once()
    call_kwargs = bot._transport.enable_team_relay.call_args.kwargs
    assert call_kwargs["group_chat_id"] == -100123
    assert call_kwargs["team_name"] == "acme"
    assert "acme_dev_bot" in call_kwargs["team_bot_usernames"] or \
           "acme_manager_bot" in call_kwargs["team_bot_usernames"]


@pytest.mark.asyncio
async def test_no_relay_when_session_env_unset(tmp_path, monkeypatch):
    """Without LP2C_TELETHON_SESSION, build() does NOT call enable_team_relay
    (solo mode or pre-/setup state)."""
    monkeypatch.delenv("LP2C_TELETHON_SESSION", raising=False)
    from unittest.mock import MagicMock

    bot = _make_team_bot_for_test(
        team_name="acme",
        bot_username="acme_dev_bot",
        peer_bot_username="acme_manager_bot",
        group_chat_id=-100123,
    )
    bot._transport = MagicMock()
    bot._transport.enable_team_relay = MagicMock()

    await bot.build()
    bot._transport.enable_team_relay.assert_not_called()


@pytest.mark.asyncio
async def test_no_relay_when_solo_mode(tmp_path, monkeypatch):
    """A solo-mode bot (no team_name) does NOT call enable_team_relay even if env set."""
    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))
    from unittest.mock import MagicMock

    bot = _make_solo_bot_for_test()  # use existing solo-bot factory
    bot._transport = MagicMock()
    bot._transport.enable_team_relay = MagicMock()

    await bot.build()
    bot._transport.enable_team_relay.assert_not_called()
```

Adapt factory function names (`_make_team_bot_for_test`, `_make_solo_bot_for_test`) to whatever the test file already uses — grep for existing factories in `tests/test_bot_team_wiring.py`.

- [ ] **Step 5.3: Run tests, confirm failure**

```bash
pytest tests/test_bot_team_wiring.py::test_team_mode_bot_calls_enable_team_relay_when_session_env_set -v
```
Expected: FAIL — `enable_team_relay.assert_called_once()` fails because `build()` doesn't call it yet.

- [ ] **Step 5.4: Wire the call in `build()`**

In `src/link_project_to_chat/bot.py`, find `build()` (or the appropriate lifecycle method). Add at the END of `build()`, after `self._transport` is constructed and before `start()` is called:

```python
        # Team-mode bot: if the manager passed a Telethon session, wire the relay.
        if self.team_name and self.group_chat_id and self._team_bot_usernames:
            session_env = os.environ.get("LP2C_TELETHON_SESSION")
            if session_env:
                from telethon import TelegramClient
                from .config import get_telegram_api_credentials  # adapt to actual helper
                api_id, api_hash = get_telegram_api_credentials()
                client = TelegramClient(session_env, api_id, api_hash)
                self._transport.enable_team_relay(
                    telethon_client=client,
                    team_bot_usernames=self._team_bot_usernames,
                    group_chat_id=self.group_chat_id,
                    team_name=self.team_name,
                )
```

**Adapt to actual attribute names:**
- `self._team_bot_usernames` may be named differently — grep `team_bot_usernames|peer_bot|bot_usernames` on bot.py to find the actual attribute.
- `get_telegram_api_credentials` is illustrative — check how the manager bot constructs its Telethon client to find the existing helper.
- `os` and `telethon.TelegramClient` may need top-level imports — `os` likely already imported; add `from telethon import TelegramClient` lazily inside the conditional (matches the codebase's pattern of lazy-importing optional deps).

If `get_telegram_api_credentials` doesn't exist as a single helper, look at how `manager/bot.py` constructs its `TelegramClient` (lines around 640, 908, 1140) and copy that pattern — likely reads `config.telegram_api_id` and `config.telegram_api_hash`. The project bot may need to load `ManagerConfig` from disk to access these; if so, the conditional becomes:

```python
        if self.team_name and self.group_chat_id and self._team_bot_usernames:
            session_env = os.environ.get("LP2C_TELETHON_SESSION")
            if session_env:
                from telethon import TelegramClient
                from .manager.config import load_manager_config  # adapt
                config = load_manager_config()
                client = TelegramClient(session_env, config.telegram_api_id, config.telegram_api_hash)
                self._transport.enable_team_relay(
                    telethon_client=client,
                    team_bot_usernames=self._team_bot_usernames,
                    group_chat_id=self.group_chat_id,
                    team_name=self.team_name,
                )
```

**Lockout note:** This adds `from telethon import TelegramClient` inside `bot.py`. The lockout test enforces "no `from telegram` / `import telegram`" — `telethon` is a different package and the lockout doesn't restrict it. But verify this doesn't accidentally trip the lockout regex. If the lockout regex is loose enough to match `from telethon`, fix the lockout test to require the word boundary `\btelegram\b` instead of `telegram\.`. Run the lockout test to confirm.

- [ ] **Step 5.5: Run tests, confirm pass**

```bash
pytest tests/test_bot_team_wiring.py -v 2>&1 | tail -20
```
Expected: all PASS including the 3 new ones.

Regression:
```bash
pytest tests/test_bot_streaming.py tests/test_bot_voice.py tests/test_group_state.py tests/test_group_filters.py tests/test_group_halt_integration.py tests/test_cap_probe.py tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

- [ ] **Step 5.6: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_team_wiring.py
git commit -m "feat(bot): build() wires enable_team_relay from LP2C_TELETHON_SESSION env"
```

---

## Task 6: Drop manager-side `_team_relays` ownership

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_team.py` and any other test that exercises `_team_relays`

- [ ] **Step 6.1: Inventory manager's relay usage**

```bash
grep -n "_team_relays\|_start_team_relays\|TeamRelay" src/link_project_to_chat/manager/bot.py
```

You should see:
- `self._team_relays: dict[str, TeamRelay] = {}` (instance init)
- `_start_team_relays(...)` method (one or more callers)
- Direct `from ..transport._telegram_relay import TeamRelay` (one or more sites — the spec #0a Task 11 move updated these)
- `TeamRelay(client=..., team_name=..., ...)` instantiations (likely 2 sites)

- [ ] **Step 6.2: Delete the relay management surface**

In `src/link_project_to_chat/manager/bot.py`:

1. Delete `self._team_relays: dict[str, TeamRelay] = {}` from `__init__`.
2. Delete the `_start_team_relays` method entirely.
3. Delete every direct `TeamRelay(...)` instantiation (the manager no longer owns relays — project bots do, per Task 5).
4. Delete every `from ..transport._telegram_relay import TeamRelay` import.
5. Delete any `await relay.start()` / `await relay.stop()` calls on manager-owned relay instances.
6. Delete any cleanup loops that iterate over `self._team_relays.values()` for shutdown.

**CRITICAL:** Verify the team-bot launch flow still triggers the project bot subprocess to be spawned. The manager's job is to spawn the subprocess (Task 4 wired the env var); the subprocess's job is to construct the relay (Task 5 wired the call). Manager no longer touches `TeamRelay` at all.

- [ ] **Step 6.3: Verify no orphaned references**

```bash
grep -n "_team_relays\|_start_team_relays\|TeamRelay" src/link_project_to_chat/manager/bot.py
```
Expected: zero matches.

```bash
grep -n "_telegram_relay\|TeamRelay" src/link_project_to_chat/manager/bot.py
```
Expected: zero matches.

- [ ] **Step 6.4: Update or remove tests that asserted on `_team_relays`**

```bash
grep -rn "_team_relays\|_start_team_relays" tests
```

For each match in tests:
- If the test asserts on manager-side relay state, remove or refactor: the relay now lives on the project bot's transport, accessed via `bot._transport._team_relay` (set by `enable_team_relay`). Since project bots are subprocesses in production, manager-side tests can't easily assert on relay state — the assertion belongs in `test_bot_team_wiring.py` (already added in Task 5).
- If the test mocks `_start_team_relays`, remove the mock — the method is gone.

- [ ] **Step 6.5: Run tests**

```bash
pytest tests/test_manager_create_team.py tests/test_process_manager_teams.py tests/test_team_relay.py tests/test_bot_team_wiring.py -q 2>&1 | tail -15
```
Expected: green. If a test fails because it relied on `_team_relays`, refactor or remove it (the relay-launch behavior is now exercised by the Task 5 tests).

Regression:
```bash
pytest tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

- [ ] **Step 6.6: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): remove _team_relays; relay ownership moves to project bots"
```

---

## Task 7: Add `ManagerBot._incoming_from_update` helper

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 7.1: Add the helper**

In `src/link_project_to_chat/manager/bot.py`, add as a method on `ManagerBot` (place near other private helpers):

```python
    def _incoming_from_update(self, update) -> "IncomingMessage":
        """Build a transient IncomingMessage from a telegram Update.

        Used by wizard step bodies (Task 11+) to read message data through the
        Transport-shaped contract while ConversationHandler still consumes Updates
        at the boundary.
        """
        from ..transport import IncomingMessage
        from ..transport.telegram import chat_ref_from_telegram, identity_from_telegram_user
        msg = update.effective_message
        return IncomingMessage(
            chat=chat_ref_from_telegram(update.effective_chat),
            sender=identity_from_telegram_user(update.effective_user),
            text=(msg.text if msg else "") or "",
            files=[],
            reply_to=None,
            native=msg,
        )
```

Local imports (inside the method) match the existing manager-bot pattern of lazy-importing transport modules.

- [ ] **Step 7.2: No new test yet — exercised in Tasks 11-14**

The helper is a pure factory; integration tests in Tasks 11-14 will exercise every code path.

- [ ] **Step 7.3: Run tests**

```bash
pytest tests/test_manager_create_team.py tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

- [ ] **Step 7.4: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat(manager): _incoming_from_update helper for upcoming wizard shim"
```

---

## Task 8: Port simple commands batch 1 (read-only commands)

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

**Scope:** Read-only commands that don't mutate state — easy to port, reduce blast radius. Specifically: `/version`, `/help`, `/projects`, `/teams`, `/users`.

- [ ] **Step 8.1: Inspect current command registrations**

```bash
grep -n "CommandHandler\b" src/link_project_to_chat/manager/bot.py | head -30
```

Find the registration block (likely in a `_register_handlers` or `_setup` method). Note which commands are simple (single function) vs wizards (`ConversationHandler`).

For each of `/version`, `/help`, `/projects`, `/teams`, `/users`, locate:
- The registration line (`app.add_handler(CommandHandler("name", self._on_name))`)
- The handler method body (`async def _on_name(self, update, ctx) -> None:`)

- [ ] **Step 8.2: Port one command at a time**

For each of the 5 commands:

1. **Add a transport-native handler** alongside the existing `_on_name`:

```python
    async def _on_name_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /name."""
        # Body uses invocation.chat, invocation.sender, invocation.args.
        # Replies via self._transport.send_text(invocation.chat, ..., html=True if needed).
```

2. **Translate the original body**: replace `update.effective_message.reply_text(text)` with `await self._transport.send_text(invocation.chat, text)`. Replace `update.effective_user` with `invocation.sender` (Identity-typed). Replace `ctx.args` with `invocation.args` (already split by whitespace). Replace `update.effective_message.text` with `invocation.raw_text`.

3. **Auth check**: if the original body checked `self._auth(update.effective_user)`, change to `self._auth_identity(invocation.sender)` (Identity-typed auth — already exists from spec #0).

4. **Delete the old `_on_name` method** AND its `app.add_handler(CommandHandler("name", self._on_name))` registration. Replace the registration with `self._transport.on_command("name", self._on_name_from_transport)` placed wherever the manager wires its handlers (likely the same `_register_handlers` method).

For `/help`: the body likely calls `update.effective_message.reply_text(_HELP_TEXT, parse_mode="HTML")`. Translate to `await self._transport.send_text(invocation.chat, _HELP_TEXT, html=True)`.

- [ ] **Step 8.3: Run tests after each command**

After porting each command, run:
```bash
pytest tests/test_manager_*.py tests/test_transport_lockout.py -q
```
Expected: green. If a test asserts on `update.message.reply_text(...)` mocks, refactor the test to use `bot._transport.sent_messages` (FakeTransport pattern from spec #0a Task 9).

- [ ] **Step 8.4: After all 5 commands ported, full sweep**

```bash
pytest tests/ -q 2>&1 | tail -10
```
Expected: matches baseline (or better — tests that depended on the old API now use the cleaner new API).

- [ ] **Step 8.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): port /version /help /projects /teams /users to transport.on_command"
```

---

## Task 9: Port simple commands batch 2 (action commands)

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

**Scope:** Commands that change state but don't use ConversationHandler. Specifically: `/start_all`, `/stop_all`, `/model`, `/add_user`, `/remove_user`, `/setup` (if single-step).

- [ ] **Step 9.1: Verify scope**

```bash
grep -n "CommandHandler(\"start_all\\|CommandHandler(\"stop_all\\|CommandHandler(\"model\\|CommandHandler(\"add_user\\|CommandHandler(\"remove_user\\|CommandHandler(\"setup" src/link_project_to_chat/manager/bot.py
```

If `/setup` shows up as a `ConversationHandler.entry_points`, it's a wizard — defer to Task 11. If it's a standalone `CommandHandler`, port here.

`/add_user` and `/remove_user` may be standalone (`/add_user @alice` argument-only) or wizards (interactive prompt). Inspect:

```bash
grep -n "def _on_add_user\|def _on_remove_user" src/link_project_to_chat/manager/bot.py
```

If the function signature is `async def _on_add_user(self, update, ctx) -> None`, it's standalone. If `-> int`, it's a wizard step.

- [ ] **Step 9.2: Port each in-scope command**

Same pattern as Task 8: add `_on_NAME_from_transport(invocation)` handler; translate body; replace registration; delete old method.

For commands that take args (`/add_user @alice`):
- `update.effective_message.text.split()[1:]` → `invocation.args`

For commands that send confirmation messages with inline buttons:
- `InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=value)]])` → `Buttons(rows=[[Button(label=label, value=value)]])`
- Defer the `Buttons`-conversion until Task 10 if it's tangled with `CallbackQueryHandler` — for now, the text-only paths can land here.

- [ ] **Step 9.3: Run tests after each command + after batch**

```bash
pytest tests/test_manager_*.py tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

- [ ] **Step 9.4: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): port /start_all /stop_all /model /add_user /remove_user /setup to transport.on_command"
```

(Adjust the message to reflect which commands actually landed.)

---

## Task 10: Port inline-button menus to `Buttons` + `transport.on_button`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 10.1: Inventory current button surface**

```bash
grep -n "InlineKeyboardButton\|InlineKeyboardMarkup\|CallbackQueryHandler\|callback_data" src/link_project_to_chat/manager/bot.py
```

Identify:
- Every `InlineKeyboardMarkup(...)` construction site (these become `Buttons(...)`).
- Every `CallbackQueryHandler` registration (these become `transport.on_button(...)`).
- Every callback handler body (`async def _on_callback_X(self, update, ctx) -> None`).

- [ ] **Step 10.2: Convert each menu-construction site**

For every `InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=value), ...], ...])`, replace with:

```python
from ..transport import Button, Buttons
buttons = Buttons(rows=[[Button(label=label, value=value), ...], ...])
await self._transport.send_text(chat_ref, text, buttons=buttons)
```

The `chat_ref` derivation depends on context — if you're inside a transport-native command handler, it's `invocation.chat`. If inside a wizard step (still Update-typed), use `chat_ref_from_telegram(update.effective_chat)` or the helper from Task 7.

- [ ] **Step 10.3: Convert callback handlers**

For each `CallbackQueryHandler(self._on_callback_X, pattern=r"...")`:

1. Add transport-native handler:
```python
    async def _on_button_from_transport(self, click: "ButtonClick") -> None:
        """Dispatch button clicks. Pattern-match on click.value."""
        value = click.value
        if value.startswith("xxx_"):
            await self._handle_xxx(click)
        elif value.startswith("yyy_"):
            await self._handle_yyy(click)
        # ... etc.
```

2. Replace the original pattern-routed `CallbackQueryHandler` registrations with a single `self._transport.on_button(self._on_button_from_transport)` and a dispatch ladder inside the handler.

3. The dispatch ladder uses `click.value.startswith(...)` (matching the spec #0a project-bot pattern). If the codebase has many distinct button kinds, consider grouping into a small dispatch dict:

```python
    _BUTTON_DISPATCH = {
        "xxx_": "_handle_xxx",
        "yyy_": "_handle_yyy",
        # ...
    }

    async def _on_button_from_transport(self, click):
        for prefix, method_name in self._BUTTON_DISPATCH.items():
            if click.value.startswith(prefix):
                return await getattr(self, method_name)(click)
```

- [ ] **Step 10.4: Run tests after each conversion**

```bash
pytest tests/test_manager_*.py tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

If a test asserts on the old `update.callback_query.answer()` or `update.callback_query.edit_message_text(...)` pattern, refactor to use FakeTransport's `edited_messages` / `sent_messages`.

- [ ] **Step 10.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): port inline-button menus to Buttons + transport.on_button"
```

---

## Task 11: Wizard shim — `/add_project`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 11.1: Inspect the wizard's structure**

```bash
grep -n "_on_add_project\|ADD_PROJECT_" src/link_project_to_chat/manager/bot.py | head -20
```

Identify:
- The `ConversationHandler` definition (states, entry points, fallbacks)
- Each step body (functions returning state IDs or `ConversationHandler.END`)
- Any `update.effective_message.reply_text(...)` calls inside step bodies
- Any `InlineKeyboardMarkup(...)` constructions inside step bodies (some wizards use buttons mid-flow)

- [ ] **Step 11.2: For each step body, convert to use `_incoming_from_update`**

**Pattern (apply to every step body in the wizard):**

```python
    async def _on_add_project_step_N(self, update, ctx) -> int:
        incoming = self._incoming_from_update(update)
        # Use incoming.text, incoming.sender.handle, incoming.chat instead of
        # update.message.text, update.effective_user.username, etc.

        text = incoming.text.strip()
        if not text:
            await self._transport.send_text(incoming.chat, "Cannot be empty.")
            return STATE_N  # stay in same state
        ctx.user_data["field"] = text  # ctx is unchanged
        await self._transport.send_text(
            incoming.chat,
            "Got it.",
            buttons=Buttons(rows=[[Button(label="Yes", value="add_project_yes"), Button(label="No", value="add_project_no")]]) if buttons_needed else None,
        )
        return NEXT_STATE
```

**Auth checks:** if a step body checks `self._auth(update.effective_user)`, replace with `self._auth_identity(incoming.sender)`.

**State-machine bookkeeping** (`return STATE_N`, `return ConversationHandler.END`, fallback wiring) is **unchanged**. Only the *message read/write* code changes.

**`ctx.user_data`** is unchanged — it stays the wizard's state store.

- [ ] **Step 11.3: If the wizard uses inline buttons mid-flow**

Convert `InlineKeyboardMarkup` to `Buttons` (per Task 10's pattern) inside the step body. The `CallbackQueryHandler` for the wizard's mid-flow buttons is part of the `ConversationHandler.states` config — leaving it as-is is fine (the wizard machinery still handles routing). What changes is the construction and the data: button `value` (was `callback_data`) gets passed via the same string-based mechanism.

If button clicks need to advance state, they continue to do so via the `ConversationHandler.states` dict. The transport-native `on_button` from Task 10 is for non-wizard button clicks; wizard buttons are managed by ConversationHandler.

**This is an asymmetry** — wizard buttons stay routed via ConversationHandler's `CallbackQueryHandler` even though all other buttons go through `transport.on_button`. Document with a comment near the wizard:

```python
# Wizard's mid-flow buttons stay on CallbackQueryHandler for ConversationHandler
# state-routing; non-wizard buttons go through transport.on_button (Task 10).
```

- [ ] **Step 11.4: Run wizard tests**

```bash
pytest tests/test_manager_create_team.py tests/test_manager_*.py -v 2>&1 | tail -20
```

Find tests for `/add_project` specifically (likely `test_add_project_*` patterns). Expect them to pass.

If a test asserts on `update.message.reply_text("expected text")` mocks, refactor to inspect `bot._transport.sent_messages[-1].text` (FakeTransport pattern). The wizard tests still construct telegram Updates at the boundary; what changes is how the test verifies replies.

- [ ] **Step 11.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): wizard shim for /add_project — step bodies use IncomingMessage"
```

---

## Task 12: Wizard shim — `/create_project`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 12.1: Apply the same shim pattern as Task 11 to `/create_project`'s step bodies**

```bash
grep -n "_on_create_project\|CREATE_PROJECT_" src/link_project_to_chat/manager/bot.py | head -20
```

For each step body, convert per Task 11's pattern: build `incoming` via `_incoming_from_update(update)`; use `incoming.*`; replies via `self._transport.send_text(incoming.chat, ...)`; `ctx.user_data` unchanged; state-machine returns unchanged.

`/create_project` is more complex than `/add_project` — it likely calls into `BotFatherClient` (for creating a real Telegram bot via @BotFather). That logic is unchanged; only the message-handling around it shifts to Transport.

- [ ] **Step 12.2: Run tests**

```bash
pytest tests/test_manager_*.py -v 2>&1 | tail -20
```
Expected: green for all wizards-touched tests.

- [ ] **Step 12.3: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): wizard shim for /create_project"
```

---

## Task 13: Wizard shim — `/create_team`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 13.1: Apply shim pattern to `/create_team`'s step bodies**

```bash
grep -n "_on_create_team\|CREATE_TEAM_" src/link_project_to_chat/manager/bot.py | head -20
```

`/create_team` calls into `transport/_telegram_group.py` (the relocated module from Task 1) for `create_supergroup`, `add_bot`, `promote_admin`, `invite_user`. The Telethon TL operations are unchanged — only the wizard's message-handling shell shifts to Transport.

Apply the standard pattern: `incoming = self._incoming_from_update(update)`; use `incoming.*`; replies via `self._transport.send_text(incoming.chat, ...)`.

- [ ] **Step 13.2: Run tests**

```bash
pytest tests/test_manager_create_team.py tests/test_manager_*.py -v 2>&1 | tail -20
```
Expected: green.

- [ ] **Step 13.3: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): wizard shim for /create_team"
```

---

## Task 14: Wizard shim — remaining wizards (`/delete_team`, `/edit_project`, plus any others)

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 14.1: Identify remaining wizards**

```bash
grep -n "ConversationHandler.END\|def _on_.*-> int" src/link_project_to_chat/manager/bot.py | head -30
```

Cross-reference with Tasks 11-13 — anything not yet ported is a candidate for this task. Plan-spec listed `/delete_team` and `/edit_project`; verify against actual code.

- [ ] **Step 14.2: Apply shim pattern to each remaining wizard's step bodies**

Same pattern as Tasks 11-13. Each wizard ports independently; if convenient, do them in sub-commits within this task — but a single commit covering the remaining wizards is acceptable since the pattern is identical.

- [ ] **Step 14.3: After all wizards ported, run full manager test suite**

```bash
pytest tests/test_manager_*.py tests/test_process_manager_teams.py -v 2>&1 | tail -30
```
Expected: green.

- [ ] **Step 14.4: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/
git commit -m "refactor(manager): wizard shim for remaining wizards (/delete_team, /edit_project, ...)"
```

(Adjust message to reflect actual wizards ported.)

---

## Task 15: Drop dead telegram imports

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 15.1: Inspect remaining telegram imports**

```bash
grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/manager/bot.py
```

The expected residual set after Tasks 1-14:
- `from telegram import Update` (still used by ConversationHandler step signatures and `_incoming_from_update`)
- `from telegram.ext import (ConversationHandler, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters,)` (ConversationHandler machinery)

Anything else — `InlineKeyboardButton`, `InlineKeyboardMarkup`, `Bot`, `Application`, etc. — is now dead.

- [ ] **Step 15.2: Verify each candidate is unused**

For each suspect import, grep:
```bash
grep -n "InlineKeyboardButton\|InlineKeyboardMarkup\|ApplicationBuilder\|telegram\.Bot\b" src/link_project_to_chat/manager/bot.py
```
Expected: zero matches in code (only in import statements).

- [ ] **Step 15.3: Delete dead imports**

Remove each unused import line. Update the multi-line `from telegram.ext import (...)` block to keep only the allowlisted names.

- [ ] **Step 15.4: Re-grep to confirm allowlist match**

```bash
grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/manager/bot.py
```
Expected output:
```
N: from telegram import Update
M: from telegram.ext import (
M+1:     ConversationHandler,
M+2:     ContextTypes,
M+3:     MessageHandler,
M+4:     CommandHandler,
M+5:     CallbackQueryHandler,
M+6:     filters,
M+7: )
```
(Or single-line equivalent. Order of imported names may vary.)

- [ ] **Step 15.5: Run tests**

```bash
pytest tests/ -q 2>&1 | tail -5
```
Expected: green (no behavior change; just removing unused imports).

- [ ] **Step 15.6: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "chore(manager): drop dead telegram imports after Transport port"
```

---

## Task 16: Add `tests/test_manager_lockout.py`

**Files:**
- Create: `tests/test_manager_lockout.py`

- [ ] **Step 16.1: Write the lockout test**

Create `tests/test_manager_lockout.py`:

```python
"""Enforce the manager lockout: manager/bot.py imports a small allowlisted
telegram surface (ConversationHandler family + Update) and nothing else.

This locks the residual telegram coupling at the conversation-machinery layer.
When a future spec adds a portable Conversation primitive on the Transport
Protocol, this allowlist becomes empty.
"""
from __future__ import annotations

import re
from pathlib import Path


# Multi-line `from telegram.ext import (...)` is normalized: whitespace
# collapsed, line-continuations removed, then compared against the canonical
# allowlist below.

ALLOWED_MANAGER_TELEGRAM_IMPORTS: set[str] = {
    "from telegram import Update",
    "from telegram.ext import ConversationHandler, ContextTypes, "
    "MessageHandler, CommandHandler, CallbackQueryHandler, filters",
}


def _normalize_imports(src: str) -> set[str]:
    """Extract telegram-related import statements, normalized for comparison.

    Handles multi-line parenthesized imports, trailing commas, whitespace.
    """
    # Match `from telegram(.\w+)* import (...)` possibly spanning multiple lines.
    pattern = re.compile(
        r"^\s*(from\s+telegram(?:\.\w+)*\s+import\s+(?:\(.*?\)|.+?))$",
        re.MULTILINE | re.DOTALL,
    )
    found: set[str] = set()
    for match in pattern.finditer(src):
        stmt = match.group(1)
        # Collapse multi-line parenthesized form into single-line `from X import a, b, c`.
        # Strip parens, collapse whitespace, normalize commas.
        stmt = stmt.replace("(", "").replace(")", "")
        stmt = re.sub(r"\s+", " ", stmt).strip()
        # Drop trailing comma if any
        if stmt.endswith(","):
            stmt = stmt[:-1].rstrip()
        # Normalize "import a , b" → "import a, b"
        stmt = re.sub(r"\s*,\s*", ", ", stmt)
        found.add(stmt)

    # Also catch bare `import telegram` / `import telegram.ext`
    bare_pattern = re.compile(r"^\s*(import\s+telegram(?:\.\w+)*)\s*$", re.MULTILINE)
    for match in bare_pattern.finditer(src):
        found.add(re.sub(r"\s+", " ", match.group(1)).strip())

    return found


def test_manager_bot_telegram_imports_within_allowlist():
    src = Path("src/link_project_to_chat/manager/bot.py").read_text(encoding="utf-8")
    actual = _normalize_imports(src)
    unexpected = actual - ALLOWED_MANAGER_TELEGRAM_IMPORTS
    assert not unexpected, (
        f"Unexpected telegram imports in manager/bot.py: {unexpected}. "
        "All new telegram coupling must go through the Transport abstraction."
    )
```

- [ ] **Step 16.2: Run the test**

```bash
pytest tests/test_manager_lockout.py -v
```
Expected: PASS. If FAIL, the actual import in `manager/bot.py` doesn't match the allowlist exactly — check the multi-line normalization or update the allowlist constant to match the actual single-line form used.

If the manager-bot's actual import line is on a single line like:
```python
from telegram.ext import ConversationHandler, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
```
the normalized form will match the allowlist directly.

If the actual import uses parenthesized multi-line form:
```python
from telegram.ext import (
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)
```
the normalizer collapses it to the same single-line string. The allowlist string has the names in this exact order — if the manager imports them in a different order, either reorder the imports OR update the allowlist string to match. Prefer reordering imports for readability.

- [ ] **Step 16.3: Run regression**

```bash
pytest tests/test_transport_lockout.py tests/transport/ -q
```
Expected: green.

- [ ] **Step 16.4: Commit**

```bash
git add tests/test_manager_lockout.py
git commit -m "test: lockout test pins manager/bot.py telegram-import allowlist"
```

---

## Task 17: Final sweep + docs + version bump

**Files:**
- Modify: `where-are-we.md`
- Modify: `pyproject.toml`
- Modify: `src/link_project_to_chat/__init__.py`

- [ ] **Step 17.1: Final grep sweeps**

Run these verification greps:

```bash
# Lockout (manager): only allowlist
grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/manager/bot.py

# Lockout (project): zero
grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/bot.py

# All telethon under transport/:
grep -rnE "^\s*(from telethon|import telethon)" src tests | grep -v "/transport/"
# Expected: only test files (tests/ may import telethon for mocking)

# Manager no longer touches TeamRelay or _team_relays
grep -n "TeamRelay\|_team_relays" src/link_project_to_chat/manager/bot.py
# Expected: zero matches

# Manager constructed TelegramTransport (no direct Application.builder)
grep -n "Application\.builder\|ApplicationBuilder" src/link_project_to_chat/manager/bot.py
# Expected: zero matches
```

If any grep returns unexpected matches, STOP and report.

- [ ] **Step 17.2: Run the full test suite**

```bash
pytest -q 2>&1 | tail -10
```
Note pass count. Expected: at minimum, the count from end of Task 16 (no regressions). Q4 onwards likely added 5-10 new manager tests; expect ~535+ pass (was 530 at end of spec #0a).

- [ ] **Step 17.3: Update where-are-we.md**

Read `where-are-we.md`, find the `## Done` section (with prior entries for spec #0a, #0b, etc.). Append after the most recent entry:

```markdown
- **Manager bot port — Transport-native** (spec #0c, v0.16.0):
  - `manager/bot.py` uses `TelegramTransport` for command dispatch, inline buttons, and file ops; legacy `Application.builder()` gone
  - `manager/telegram_group.py` moved to `transport/_telegram_group.py` — invariant: all `import telethon` lives in `transport/`
  - `TelegramTransport.enable_team_relay` (shipped unused in #0a) now wired: project bots receive a Telethon session-file path via `LP2C_TELETHON_SESSION` env var, construct their own `TelegramClient` in `build()`, and call `enable_team_relay`. Manager loses `_team_relays` dict + `_start_team_relays`. Project bots own their relay.
  - Wizard step bodies (`/add_project`, `/create_project`, `/create_team`, `/delete_team`, `/edit_project`) use `_incoming_from_update` shim — Transport-native reads/writes; `ConversationHandler` machinery (states, returns) preserved
  - `tests/test_manager_lockout.py` enforces the residual telegram allowlist (`Update` + `ConversationHandler` family)
  - `TelegramTransport.app` accessor exposes the underlying `telegram.ext.Application` for ConversationHandler integration (TelegramTransport-specific, not on Protocol)
```

If the `## Pending` section has entries about manager-bot porting being deferred (e.g., from spec #0a's note), remove them.

- [ ] **Step 17.4: Bump version**

In `pyproject.toml`, change:
```toml
version = "0.15.0"
```
to:
```toml
version = "0.16.0"
```

In `src/link_project_to_chat/__init__.py`, change:
```python
__version__ = "0.15.0"
```
to:
```python
__version__ = "0.16.0"
```

- [ ] **Step 17.5: Final commit**

```bash
git add where-are-we.md pyproject.toml src/link_project_to_chat/__init__.py
git commit -m "docs: note manager bot port complete; bump to 0.16.0"
```

---

## Completion checklist

- [ ] All 17 tasks committed in order.
- [ ] `grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/manager/bot.py` returns only the allowlist (Update + ConversationHandler family).
- [ ] `grep -nE "^\s*(from telegram|import telegram)" src/link_project_to_chat/bot.py` returns zero matches.
- [ ] `grep -rnE "^\s*(from telethon|import telethon)" src` returns matches only under `src/link_project_to_chat/transport/`.
- [ ] `grep -n "TeamRelay\|_team_relays" src/link_project_to_chat/manager/bot.py` returns zero matches.
- [ ] `grep -n "Application\.builder" src/link_project_to_chat/manager/bot.py` returns zero matches.
- [ ] `pytest tests/test_manager_lockout.py -v` passes.
- [ ] `pytest tests/test_bot_team_wiring.py -v` passes (includes the 3 new `enable_team_relay` wiring tests from Task 5).
- [ ] `pytest tests/test_process_manager_teams.py -v` passes (includes the env-var test from Task 4).
- [ ] `pytest tests/test_manager_create_team.py -v` passes (wizards still functional).
- [ ] Full `pytest -q` suite passes.
- [ ] `where-are-we.md` mentions spec #0c under `## Done`.
- [ ] `pyproject.toml` version == `0.16.0`.
- [ ] `src/link_project_to_chat/__init__.py` `__version__` == `"0.16.0"`.
- [ ] Spec #0c is closed; specs #1 (Web UI), #2 (Discord), #3 (Slack) are unblocked.
