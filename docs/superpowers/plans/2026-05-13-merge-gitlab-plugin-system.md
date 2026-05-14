# Plugin system port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the GitLab fork's plugin design onto the Transport+Backend architecture. Plugins become transport-portable (one plugin works on Telegram, Web, future Discord/Slack). Add an `AllowedUser{username, role, locked_identities}` model that **replaces** the existing `allowed_usernames` / `trusted_users` / `trusted_user_ids` flat fields as the single source of auth + authority. `locked_identities` is a **list** of `"transport_id:native_id"` strings — each transport a user contacts from gets appended, so the same username can authenticate from Telegram AND Web AND any future transport. Legacy configs migrate one-way on load; legacy keys are stripped on next save.

**Architecture:** Plugins are external Python packages discovered via `lptc.plugins` entry points. `Plugin` base class with transport-agnostic handler signatures (`CommandInvocation`, `IncomingMessage`, `ButtonClick`). Lifecycle wired through `ProjectBot._after_ready` and an `on_stop` Transport callback. Auth + role enforcement is identity-keyed: `_auth_identity`, `_require_executor`, and `_get_user_role` read `_allowed_users` exclusively and check whether `_identity_key(identity)` is in any entry's `locked_identities`. Legacy fields stay on the dataclasses through Tasks 3–4 as read-only inputs; Task 5 rewrites every call site to use `resolve_project_allowed_users(project, config)` and then removes the legacy fields from the dataclasses. **This is a breaking on-disk config change.**

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22` (Telegram transport), `click>=8`, `pytest` with `asyncio_mode=auto`. Plugin discovery via `importlib.metadata.entry_points`.

**Reference design:** [`docs/superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md`](../specs/2026-05-13-merge-gitlab-plugin-system-design.md)

**Branch:** Create and work on `feat/plugin-system` off `main` (`691d26d` or later).

---

## Task 0: Setup branch

**Files:**
- N/A (git only)

- [ ] **Step 1: Create the feature branch off `main`**

```bash
git checkout main
git pull --ff-only
git checkout -b feat/plugin-system
git status
```
Expected: `On branch feat/plugin-system` with a clean working tree.

- [ ] **Step 2: Verify baseline test suite passes**

```bash
.venv/bin/pip install -e ".[all]"   # ensure editable install matches current worktree
.venv/bin/pytest -q
```

**Record the actual passing count in this task's commit message** (e.g., `chore: pin test baseline at 1003 passed, 5 skipped`). Numbers in this plan are illustrative — they drift across reviewer environments and commits. The number recorded here in Task 0 is the regression gate for the rest of the plan.

The author observed `1003 passed, 5 skipped` on `7fd934e`; a reviewer saw `976 collected` on an older state (likely a different SHA or a partial install). Re-run `pip install -e ".[all]" && pytest -q` to get YOUR baseline before writing it down.

If the count differs, check the venv first: a stale editable install (e.g., from a deleted worktree) shows as 64 collection errors and `0 passed`, which is misleading. Re-running `pip install -e .` fixes it.

If anything fails after a clean install, **STOP** and ask before proceeding.

---

## Task 1: Add `plugin.py` framework + operational scripts + `on_stop` Transport hook + Telegram dynamic command dispatch fix

**Files:**
- Create: `src/link_project_to_chat/plugin.py`
- Create: `scripts/restart.sh`
- Create: `scripts/stop.sh`
- Create: `tests/test_plugin_framework.py`
- Create: `tests/transport/test_on_stop.py`
- Create: `tests/transport/test_dynamic_command_dispatch.py`
- Modify: `src/link_project_to_chat/transport/base.py` (add `on_stop` to Protocol)
- Modify: `src/link_project_to_chat/transport/telegram.py` (fire `on_stop` in `post_stop` + dynamic `on_command` registration)
- Modify: `src/link_project_to_chat/transport/fake.py` (fire `on_stop` in `stop`)
- Modify: `src/link_project_to_chat/web/transport.py` (fire `on_stop` during shutdown)

Two Transport changes bundled here:
1. **`on_stop` callback** — parallels `on_ready`; lets plugins shutdown cleanly before the platform tears down. Implemented across all three transports.
2. **Dynamic `on_command` registration for Telegram** — `TelegramTransport.on_command` currently just sets `self._command_handlers[name] = handler`. PTB's `CommandHandler` registration only happens in `attach_telegram_routing` for the static list passed at setup, so plugin commands registered later silently fail (PTB filters drop the update before it reaches `_dispatch_command`). The fix: when `on_command` is called and `self._app` is wired, also register a PTB `CommandHandler` so updates for the new command name actually reach `_dispatch_command`. Web and Fake transports iterate `_command_handlers` directly per message and aren't affected.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugin_framework.py`:
```python
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.plugin import (
    BotCommand,
    Plugin,
    PluginContext,
    load_plugin,
)


def test_botcommand_default_viewer_ok_is_false():
    async def handler(ci):
        return None

    cmd = BotCommand(command="x", description="d", handler=handler)
    assert cmd.viewer_ok is False


def test_botcommand_viewer_ok_can_be_set():
    async def handler(ci):
        return None

    cmd = BotCommand(command="x", description="d", handler=handler, viewer_ok=True)
    assert cmd.viewer_ok is True


def test_plugin_context_send_message_calls_send_when_set():
    send = AsyncMock()
    ctx = PluginContext(bot_name="b", project_path=Path("/tmp"), _send=send)

    asyncio.run(ctx.send_message(42, "hi", reply_to=7))

    send.assert_awaited_once()
    args, kwargs = send.call_args
    # Either the chat_id is passed through or a ChatRef-style first arg — we accept either.
    assert args[0] in (42, "42") or hasattr(args[0], "native_id")
    assert args[1] == "hi"
    assert kwargs.get("reply_to") == 7


def test_plugin_context_send_message_noop_without_send():
    ctx = PluginContext(bot_name="b", project_path=Path("/tmp"))
    asyncio.run(ctx.send_message(1, "hi"))


def test_plugin_data_dir_creates_directory(tmp_path: Path):
    ctx = PluginContext(bot_name="b", project_path=tmp_path, data_dir=tmp_path / "meta" / "b")

    class P(Plugin):
        name = "myplugin"

    p = P(ctx, config={})
    d = p.data_dir
    assert d.exists()
    assert d == tmp_path / "meta" / "b" / "plugins" / "myplugin"


def test_load_plugin_returns_none_when_missing():
    ctx = PluginContext(bot_name="b", project_path=Path("/tmp"))
    assert load_plugin("definitely-not-installed", ctx, {}) is None
```

Also write a transport contract addition. Create `tests/transport/test_on_stop.py`:
```python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_fake_transport_on_stop_callback_fires():
    transport = FakeTransport()
    cb = AsyncMock()
    transport.on_stop(cb)
    await transport.start()
    await transport.stop()
    cb.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_plugin_framework.py tests/transport/test_on_stop.py -v
```
Expected: FAIL — `link_project_to_chat.plugin` not importable; `FakeTransport.on_stop` not defined.

- [ ] **Step 3: Create `src/link_project_to_chat/plugin.py`**

```python
"""
Plugin base classes and PluginContext (transport-portable).

Plugins are installed as Python packages exposing the entry point:
    [project.entry-points."lptc.plugins"]
    my-plugin = "my_package:MyPlugin"

They are declared per-project in config.json:
    "plugins": [
        {"name": "in-app-web-server"},
        {"name": "diff-reviewer", "option": "value"}
    ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .transport.base import (
        ButtonClick,
        ChatRef,
        CommandInvocation,
        IncomingMessage,
        Transport,
    )

logger = logging.getLogger(__name__)


@dataclass
class BotCommand:
    """A Telegram-style command a plugin can register on the active transport.

    `handler` must accept a `CommandInvocation` (transport-agnostic).
    `viewer_ok=True` opts the command into the viewer-role allowlist; otherwise
    it requires the user to have the `executor` role when role enforcement is
    active on a project. Defaults to executor-only (least-privilege).
    """
    command: str
    description: str
    handler: Callable[..., Awaitable[Any]]
    viewer_ok: bool = False


@dataclass
class PluginContext:
    """Shared context for all plugins in a project. One instance per bot.

    `transport` is the active Transport — plugins should call `transport.send_text(chat_ref, ...)`
    for outbound messages when they have a `ChatRef`. The legacy `send_message(chat_id, text)`
    convenience proxy synthesizes a `ChatRef` for plain-int chat IDs.

    `web_port` / `public_url` / `register_in_app_web_handler` are API surface
    reserved for the future external in-app-web-server plugin. In v1.0.0
    they're populated only when a follow-up spec wires them through from
    the Web transport — until then plugins MUST check for None and degrade
    gracefully. `data_dir` is the per-bot meta directory
    (`~/.link-project-to-chat/meta/<bot_name>/`) wired by `ProjectBot._init_plugins`;
    each `Plugin` builds its per-plugin subdirectory via the `data_dir` property.
    """
    bot_name: str
    project_path: Path
    bot_username: str = ""
    data_dir: Path | None = None

    backend_name: str = "claude"
    transport: "Transport | None" = field(default=None, repr=False)

    # LIVE helpers that consult the bot's current _allowed_users on each call.
    # Plugins call ctx.is_allowed(identity) / ctx.is_executor(identity) to gate
    # themselves; the helpers see freshly-appended locked_identities (e.g., a
    # user who first-contacted from a new transport AFTER bot startup).
    # The earlier draft snapshotted these as `allowed_identities: list[str]` /
    # `executor_identities: list[str]` at plugin init — that went stale after
    # the first first-contact lock and gave plugins an incorrect view.
    _identity_resolver: "Callable[[Any], str | None] | None" = field(default=None, repr=False)
    # ProjectBot wires _identity_resolver to a bound method that looks up
    # the role for an Identity from self._allowed_users at call time.

    web_port: int | None = None
    public_url: str | None = None
    register_in_app_web_handler: Callable[[str, str, Callable[..., Awaitable[Any]]], None] | None = field(default=None, repr=False)

    # Legacy compat: plugins ported from the GitLab fork may call ctx.send_message(int, str).
    # The proxy below builds a ChatRef and delegates to transport.send_text when available.
    _send: Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)

    async def send_message(self, chat_id, text: str, **kwargs) -> Any:
        """Send a message without importing transport types directly.

        Accepts an int chat_id (legacy GitLab API) or a ChatRef (new style).
        Returns whatever the underlying send_text returned, or None when no
        send mechanism is wired.
        """
        if self._send is not None:
            return await self._send(chat_id, text, **kwargs)
        if self.transport is None:
            return None
        from .transport.base import ChatKind, ChatRef
        if isinstance(chat_id, ChatRef):
            chat = chat_id
        else:
            chat = ChatRef(
                transport_id=getattr(self.transport, "TRANSPORT_ID", "telegram"),
                native_id=str(chat_id),
                kind=ChatKind.DM,
            )
        return await self.transport.send_text(chat, text, **kwargs)

    def is_allowed(self, identity) -> bool:
        """Live check: is this identity currently in the bot's allow-list?

        Reads the bot's _allowed_users at call time (not a snapshot), so
        plugins see users who first-contacted from a new transport after
        startup.
        """
        if self._identity_resolver is None:
            return False
        return self._identity_resolver(identity) is not None

    def is_executor(self, identity) -> bool:
        """Live check: does this identity currently have the executor role?"""
        if self._identity_resolver is None:
            return False
        return self._identity_resolver(identity) == "executor"


class Plugin:
    """Base class for all plugins. Subclass and override what you need."""

    name: str = ""
    depends_on: list[str] = []

    def __init__(self, context: PluginContext, config: dict) -> None:
        self._ctx = context
        self._config = config

    @property
    def data_dir(self) -> Path:
        """Per-plugin persistent storage.

        Returns ``<ctx.data_dir>/plugins/<plugin_name>/`` when the context
        supplied an explicit data_dir; otherwise defaults to
        ``~/.link-project-to-chat/meta/<bot_name>/plugins/<plugin_name>/``.
        Creates the directory tree if missing.
        """
        base = self._ctx.data_dir or (Path.home() / ".link-project-to-chat" / "meta" / self._ctx.bot_name)
        path = base / "plugins" / self.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def start(self) -> None:
        """Called after the bot's Transport is ready. Perform setup here."""

    async def stop(self) -> None:
        """Called before the bot stops. Clean up resources here."""

    async def on_message(self, msg: "IncomingMessage") -> bool:
        """Called for every authorized incoming text message.

        Viewer policy: fires for executor AND viewer users. Plugins gate themselves
        if they care about role — call `self._ctx.is_executor(msg.sender)` (live
        helper that consults the bot's current allow-list). Return True to consume
        (skip backend); False to let the primary path proceed.
        """
        return False

    async def on_button(self, click: "ButtonClick") -> bool:
        """Called for every authorized button click BEFORE primary's branch chain.

        Same viewer policy as `on_message`: fires for both roles. Plugins gate
        themselves if needed. Return True to consume (skip primary's button
        dispatch); False to let the primary chain process the click.
        """
        return False

    async def on_task_complete(self, task) -> None:
        """Called after a task finishes (DONE or FAILED). Not called for CANCELLED."""

    async def on_tool_use(self, tool: str, path: str | None) -> None:
        """Called when the agent uses a tool during a task (e.g. Write, Edit)."""

    def get_context(self) -> str | None:
        """Text prepended to Claude's prompt before each turn. Return None to skip.

        Only applied when the active backend is Claude — Codex/Gemini don't accept
        arbitrary system-prompt prepends in the same way. Plugins that care about
        non-Claude backends should branch on `ctx.backend_name`.
        """
        return None

    def tools(self) -> list[dict]:
        """Tool definitions (schema only, for documentation)."""
        return []

    async def call_tool(self, name: str, args: dict) -> str:
        """Execute a plugin tool. Called via CLI (claude uses Bash to invoke it)."""
        return f"Unknown tool: {name}"

    def commands(self) -> list[BotCommand]:
        """Additional bot commands this plugin registers via the active transport."""
        return []


def load_plugin(name: str, context: PluginContext, config: dict) -> Plugin | None:
    """Instantiate a plugin by name using the 'lptc.plugins' entry point group.

    Returns None if the plugin is not installed (caller logs and continues).
    """
    from importlib.metadata import entry_points

    eps = entry_points(group="lptc.plugins")
    for ep in eps:
        if ep.name == name:
            cls = ep.load()
            return cls(context, config)

    logger.error("plugin %r not found — is it installed?", name)
    return None
```

- [ ] **Step 4: Add `on_stop` to Transport Protocol**

In `src/link_project_to_chat/transport/base.py`, find the existing `on_ready` definition (search for `def on_ready(self`). Right after it, add:

```python
    def on_stop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback fired during the Transport's shutdown sequence,
        BEFORE the platform actually tears down. Plugins use this to release
        resources, send a final message, etc. Multiple callbacks fire in
        registration order; exceptions are logged but do not block other
        callbacks or the transport shutdown.
        """
        ...
```

(`Callable` and `Awaitable` are already imported at the top — verify.)

- [ ] **Step 5: Implement `on_stop` in `TelegramTransport`**

In `src/link_project_to_chat/transport/telegram.py`, find the `__init__` (search for `self._on_ready_callbacks`). Right after that line, add:

```python
        self._on_stop_callbacks: list = []
```

Then find `def on_ready(self, callback)`. Right after it, add:

```python
    def on_stop(self, callback) -> None:
        self._on_stop_callbacks.append(callback)
```

In `async def post_stop(self, _app: Any = None)`, at the very beginning of the body (before `if self._team_relay is not None:`), add:

```python
        for cb in self._on_stop_callbacks:
            try:
                await cb()
            except Exception:
                logger.exception("on_stop callback failed")
```

- [ ] **Step 6: Implement `on_stop` in `FakeTransport`**

In `src/link_project_to_chat/transport/fake.py`, find `__init__` (look for `_on_ready_callbacks`). Right after, add:

```python
        self._on_stop_callbacks: list = []
```

Find `def on_ready`. Right after it, add:

```python
    def on_stop(self, callback) -> None:
        self._on_stop_callbacks.append(callback)
```

In `async def stop(self)`, at the very beginning, add:

```python
        for cb in self._on_stop_callbacks:
            try:
                await cb()
            except Exception:
                logger.exception("on_stop callback failed")
```

- [ ] **Step 7: Implement `on_stop` in `WebTransport`**

In `src/link_project_to_chat/web/transport.py`, find `_on_ready_callbacks` initialization. Mirror the same pattern: add `self._on_stop_callbacks: list = []`, add `on_stop(callback)` method, fire callbacks in the existing stop/shutdown code path (look for `async def stop` or `_shutdown`). Add the same try/except loop.

- [ ] **Step 7a: Write the failing dynamic-dispatch regression test**

Create `tests/transport/test_dynamic_command_dispatch.py`:

```python
"""Regression test: late on_command() calls must produce dispatchable handlers.

Plugins register their commands inside `_after_ready`, which fires AFTER
`attach_telegram_routing`. Before the fix, TelegramTransport.on_command only
updated `_command_handlers[name]` — PTB never got a CommandHandler for that
name and updates were dropped at the filter level.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_fake_transport_late_on_command_is_dispatchable():
    """FakeTransport already iterates _command_handlers per message — this is
    a baseline assertion that the late registration is honored."""
    transport = FakeTransport()
    handler = AsyncMock()
    # Late registration (after notional "routing" — Fake has no routing step,
    # but the contract is: on_command(name, h) makes /name dispatchable from
    # whatever point it's called).
    transport.on_command("late_cmd", handler)
    assert "late_cmd" in transport._command_handlers


def test_telegram_transport_late_on_command_registers_ptb_handler():
    """TelegramTransport.on_command called AFTER attach_telegram_routing must
    register a PTB CommandHandler so updates actually reach _dispatch_command.

    This is the fix for Issue #1 — without it, plugin commands silently fail.
    """
    pytest.importorskip("telegram")

    from telegram.ext import CommandHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(group_mode=False, command_names=["help", "tasks"])

    async def late_handler(ci):
        return None

    # The number of CommandHandlers before late registration:
    before = sum(
        1
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
    )
    transport.on_command("late_cmd", late_handler)
    after = sum(
        1
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
    )
    # The new command produced a NEW PTB CommandHandler.
    assert after == before + 1, (
        f"Expected PTB CommandHandler count to grow by 1 after late on_command, "
        f"got {before} → {after}"
    )
    # And the dispatch dict reflects it.
    assert "late_cmd" in transport._command_handlers
```

- [ ] **Step 7b: Implement dynamic PTB registration in `TelegramTransport.on_command`**

In `src/link_project_to_chat/transport/telegram.py`, find the existing `on_command` method (around line 784):

```python
    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler
```

Replace with:

```python
    def on_command(self, name: str, handler) -> None:
        self._command_handlers[name] = handler
        # If routing is already attached, also register a PTB CommandHandler
        # so this command name reaches our dispatcher. Without this, late
        # registrations (e.g., plugin commands wired in _after_ready, which
        # fires AFTER attach_telegram_routing) silently fail — PTB drops the
        # update at the filter level.
        if self._app is not None and self._routing_attached:
            from telegram.ext import CommandHandler as _PTBCommandHandler
            from telegram.ext import filters as _filters

            chat_filter = (
                _filters.ChatType.GROUPS if self._group_mode_attached
                else _filters.ChatType.PRIVATE
            )
            self._app.add_handler(_PTBCommandHandler(
                name,
                lambda u, c, _n=name: self._dispatch_command(_n, u, c),
                filters=chat_filter,
            ))
```

Two pieces of state need adding to `TelegramTransport.__init__` (locate the `self._command_handlers = {}` initializer):

```python
        self._routing_attached: bool = False
        self._group_mode_attached: bool = False
```

In `attach_telegram_routing`, at the END of the method (after `self._app.add_error_handler(...)`), set the flags:

```python
        self._routing_attached = True
        self._group_mode_attached = group_mode
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
pytest tests/test_plugin_framework.py tests/transport/test_on_stop.py tests/transport/test_dynamic_command_dispatch.py -v
```
Expected: All tests PASS — including the new dynamic-dispatch regression test.

- [ ] **Step 9: Add operational scripts**

Create `scripts/restart.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LPTC="$SCRIPT_DIR/../.venv/bin/link-project-to-chat"
RUN_DIR="/tmp/link-project-to-chat-manager"
PID_FILE="$RUN_DIR/pid"
LOG_FILE="$RUN_DIR/log"

mkdir -p "$RUN_DIR"

nohup bash -c "
    sleep 2
    '$SCRIPT_DIR/stop.sh'
    nohup '$LPTC' start-manager >> '$LOG_FILE' 2>&1 &
    echo \$! > '$PID_FILE'
" > /dev/null 2>&1 &
disown

echo "Restart scheduled."
```

Create `scripts/stop.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/link-project-to-chat-manager/pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found, nothing to stop."
    exit 0
fi

PID=$(cat "$PID_FILE")

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID not running, cleaning up PID file."
    rm -f "$PID_FILE"
    exit 0
fi

kill "$PID"
echo "Sent SIGTERM to process $PID."

for i in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
    echo "Process $PID force-killed."
fi

rm -f "$PID_FILE"
echo "Stopped."
```

Make executable:
```bash
chmod +x scripts/restart.sh scripts/stop.sh
```

- [ ] **Step 10: Run the full suite**

```bash
pytest -q
```
Expected: All tests PASS. Count must be ≥ the baseline recorded in Task 0 plus the new tests added in this task; if anything dropped, investigate before continuing.

- [ ] **Step 11: Commit**

```bash
git add src/link_project_to_chat/plugin.py \
        src/link_project_to_chat/transport/base.py \
        src/link_project_to_chat/transport/telegram.py \
        src/link_project_to_chat/transport/fake.py \
        src/link_project_to_chat/web/transport.py \
        tests/test_plugin_framework.py \
        tests/transport/test_on_stop.py \
        tests/transport/test_dynamic_command_dispatch.py \
        scripts/restart.sh scripts/stop.sh
git commit -m "$(cat <<'EOF'
feat(plugin): framework + Transport.on_stop + TelegramTransport dynamic command dispatch

Adds plugin.py (transport-agnostic Plugin base, PluginContext with
transport-portable identity strings, BotCommand, load_plugin via entry
points) and operational scripts. Extends the Transport Protocol with
on_stop(callback) so plugins can shutdown cleanly before the platform
tears down. All three transports (Telegram, Fake, Web) implement it.

Also fixes a latent dispatch bug: TelegramTransport.on_command previously
only updated _command_handlers without registering a PTB CommandHandler,
so any command registered AFTER attach_telegram_routing was silently
dropped by PTB at the filter level. The fix registers a PTB
CommandHandler immediately when on_command runs post-routing. Regression
test in tests/transport/test_dynamic_command_dispatch.py.

No bot wiring yet — Task 2 wires plugin lifecycle into ProjectBot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire plugin lifecycle into `ProjectBot`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_bot_plugin_hooks.py`

Hook points (verified line refs against current `bot.py`):
- `__init__` (line 94)
- `_on_stream_event` ToolUse branch (line 454)
- `_on_task_complete` (line 737)
- `_on_text` (line 1003) — where plain text becomes a Claude/Codex turn
- `_submit_group_message_to_claude` (line 938) — bot-to-bot path; reuses `_build_user_prompt`
- `_build_user_prompt` (line 976) — where `get_context()` prepend lives
- `_on_button` (line 1960)
- `_after_ready` (line 2485) — where `_init_plugins` is called
- `build()` (line 2518) — register `_on_stop` callback with transport
- `run_bot` (line 2649) and `run_bots` (line 2740) — accept `plugins` kwarg

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot_plugin_hooks.py`:
```python
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest

from link_project_to_chat.bot import ProjectBot, _topo_sort
from link_project_to_chat.plugin import BotCommand, Plugin, PluginContext
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.stream import TextDelta, ToolUse
from link_project_to_chat.transport.base import (
    ButtonClick,
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)


def _ctx() -> PluginContext:
    return PluginContext(bot_name="b", project_path=Path("/tmp"))


def _msg(text: str = "hi") -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg_ref = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg_ref,
    )


def _click(value: str = "rec_open") -> ButtonClick:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg_ref = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return ButtonClick(chat=chat, message=msg_ref, sender=sender, value=value)


class _RecordingPlugin(Plugin):
    name = "rec"

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.events: list[tuple[str, Any]] = []
        self.consume_message = False
        self.consume_button = False
        self.context_text: str | None = None
        self.start_raises = False

    async def start(self) -> None:
        if self.start_raises:
            raise RuntimeError("boom")
        self.events.append(("start", None))

    async def stop(self) -> None:
        self.events.append(("stop", None))

    async def on_message(self, msg):
        self.events.append(("on_message", msg.text))
        return self.consume_message

    async def on_button(self, click):
        self.events.append(("on_button", click.value))
        return self.consume_button

    async def on_task_complete(self, task) -> None:
        self.events.append(("on_task_complete", task.id))

    async def on_tool_use(self, tool, path) -> None:
        self.events.append(("on_tool_use", (tool, path)))

    def get_context(self):
        return self.context_text


def _make_bot(plugins=None, backend_name="claude"):
    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.path = Path("/tmp/p")
    bot._allowed_users = []  # role enforcement off
    bot._plugins = plugins or []
    bot._plugin_configs = []
    bot._plugin_command_handlers = {}
    bot._shared_ctx = None
    bot._backend_name = backend_name
    return bot


def test_topo_sort_orders_by_depends_on():
    a = _RecordingPlugin(_ctx(), {})
    a.name = "a"
    a.depends_on = ["b"]
    b = _RecordingPlugin(_ctx(), {})
    b.name = "b"
    assert [p.name for p in _topo_sort([a, b])] == ["b", "a"]


def test_topo_sort_missing_dep_still_returns_plugin():
    p = _RecordingPlugin(_ctx(), {})
    p.name = "p"
    p.depends_on = ["unknown"]
    assert [x.name for x in _topo_sort([p])] == ["p"]


@pytest.mark.asyncio
async def test_on_tool_use_fires_for_each_plugin():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))
    assert ("on_tool_use", ("Write", "/x")) in p1.events
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_tool_use_continues_after_plugin_raises():
    p1 = _RecordingPlugin(_ctx(), {})
    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p1.on_tool_use = boom  # type: ignore[assignment]
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_task_complete_fires_for_done_and_failed_not_cancelled():
    p = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p])
    for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task = Task.__new__(Task)
        task.id = 1
        task.status = status
        await bot._dispatch_plugin_task_complete(task)
    fired = [e for e in p.events if e[0] == "on_task_complete"]
    assert len(fired) == 2


@pytest.mark.asyncio
async def test_on_message_consumes_when_any_plugin_returns_true():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    p2.consume_message = True
    bot = _make_bot([p1, p2])
    consumed = await bot._dispatch_plugin_on_message(_msg("hello"))
    assert consumed is True
    assert ("on_message", "hello") in p1.events
    assert ("on_message", "hello") in p2.events


@pytest.mark.asyncio
async def test_on_message_no_consume_when_plugin_raises():
    p = _RecordingPlugin(_ctx(), {})
    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p.on_message = boom  # type: ignore[assignment]
    bot = _make_bot([p])
    consumed = await bot._dispatch_plugin_on_message(_msg("hi"))
    assert consumed is False


@pytest.mark.asyncio
async def test_on_button_consumes_when_any_plugin_returns_true():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    p2.consume_button = True
    bot = _make_bot([p1, p2])
    consumed = await bot._dispatch_plugin_button(_click("rec_open"))
    assert consumed is True
    assert ("on_button", "rec_open") in p1.events


def test_plugin_context_prepend_with_claude_backend():
    p1 = _RecordingPlugin(_ctx(), {})
    p1.context_text = "FIRST"
    p2 = _RecordingPlugin(_ctx(), {})
    p2.context_text = "SECOND"
    p3 = _RecordingPlugin(_ctx(), {})
    p3.context_text = None
    bot = _make_bot([p1, p2, p3], backend_name="claude")
    out = bot._plugin_context_prepend("USER")
    assert out.startswith("FIRST\n\nSECOND")
    assert "\n\n---\n\nUSER" in out


def test_plugin_context_prepend_skips_on_codex_backend():
    p = _RecordingPlugin(_ctx(), {})
    p.context_text = "WOULD-BE-PREPEND"
    bot = _make_bot([p], backend_name="codex")
    assert bot._plugin_context_prepend("USER") == "USER"


def test_plugin_context_prepend_empty_when_no_contexts():
    p = _RecordingPlugin(_ctx(), {})
    p.context_text = None
    bot = _make_bot([p])
    assert bot._plugin_context_prepend("USER") == "USER"


@pytest.mark.asyncio
async def test_shutdown_calls_stop_in_reverse_order():
    order: list[str] = []

    class P(_RecordingPlugin):
        async def stop(self):
            order.append(self.name)

    p1 = P(_ctx(), {})
    p1.name = "first"
    p2 = P(_ctx(), {})
    p2.name = "second"
    bot = _make_bot([p1, p2])
    await bot._shutdown_plugins()
    assert order == ["second", "first"]


@pytest.mark.asyncio
async def test_shutdown_continues_when_stop_raises():
    p1 = _RecordingPlugin(_ctx(), {})
    async def boom():
        raise RuntimeError("boom")
    p1.stop = boom  # type: ignore[assignment]
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._shutdown_plugins()
    assert ("stop", None) in p2.events


# --- Plugin command collision policy ---
# Step 6's _init_plugins blocks two collision categories:
#   1. Plugin command shadowing a CORE_COMMAND_NAMES entry (e.g., /help).
#   2. Plugin command already claimed by an earlier plugin (last-load
#      silently winning is wrong — both should keep their other commands
#      but the duplicate gets dropped with a WARNING).
# Without these tests, the collision logic regresses silently — it's
# only observable via a WARNING log line today.


class _RecordingCommandPlugin(_RecordingPlugin):
    """Plugin variant that exposes a configurable commands() list.

    `_commands_to_register` is a list[BotCommand]; commands() returns it.
    """

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self._commands_to_register: list[BotCommand] = []

    def commands(self):
        return list(self._commands_to_register)


def _bc(name: str) -> BotCommand:
    async def _h(_ci):  # pragma: no cover — never invoked in these tests
        return None
    return BotCommand(command=name, description="", handler=_h)


@pytest.mark.asyncio
async def test_plugin_cannot_shadow_core_command(monkeypatch, caplog):
    """A plugin registering /help (a CORE_COMMAND_NAMES entry) is dropped
    for that command. Other commands from the same plugin still register."""
    from link_project_to_chat.bot import ProjectBot

    p = _RecordingCommandPlugin(_ctx(), {})
    p.name = "rec"
    p._commands_to_register = [_bc("help"), _bc("rec_open")]

    registered: list[tuple[str, Any]] = []

    class _FakeTransport:
        TRANSPORT_ID = "fake"
        def on_command(self, name, handler):
            registered.append((name, handler))

    bot = _make_bot([])
    bot._transport = _FakeTransport()
    bot._plugin_configs = [{"name": "rec"}]
    bot._plugins = [p]  # bypass load_plugin; pre-seed the list.
    # Stub load_plugin so _init_plugins doesn't try entry-point discovery.
    import link_project_to_chat.bot as bot_mod
    monkeypatch.setattr(bot_mod, "load_plugin", lambda *a, **kw: p)
    # Stub _wrap_plugin_command (defined in Task 5) so this test passes
    # before Task 5 lands — it returns the raw handler in Task 2's window.
    bot._wrap_plugin_command = lambda plugin, bc: bc.handler

    with caplog.at_level("WARNING"):
        await bot._init_plugins()

    names = [n for n, _ in registered]
    assert "help" not in names, "core command must not be shadowed"
    assert "rec_open" in names, "non-core command from same plugin should still register"
    assert any("reserved core command" in r.message.lower() or "core command" in r.message.lower()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_plugin_command_collision_between_plugins(monkeypatch, caplog):
    """Two plugins both claim /share_cmd. First-load wins; the second
    plugin's /share_cmd is dropped, but its other commands still register."""
    from link_project_to_chat.bot import ProjectBot

    p1 = _RecordingCommandPlugin(_ctx(), {})
    p1.name = "first"
    p1._commands_to_register = [_bc("share_cmd")]

    p2 = _RecordingCommandPlugin(_ctx(), {})
    p2.name = "second"
    p2._commands_to_register = [_bc("share_cmd"), _bc("only_second")]

    registered: list[tuple[str, Any]] = []

    class _FakeTransport:
        TRANSPORT_ID = "fake"
        def on_command(self, name, handler):
            registered.append((name, handler))

    bot = _make_bot([])
    bot._transport = _FakeTransport()
    bot._plugin_configs = [{"name": "first"}, {"name": "second"}]
    bot._plugins = [p1, p2]
    import link_project_to_chat.bot as bot_mod
    plugin_by_name = {"first": p1, "second": p2}
    monkeypatch.setattr(bot_mod, "load_plugin",
                        lambda name, *a, **kw: plugin_by_name[name])
    bot._wrap_plugin_command = lambda plugin, bc: bc.handler

    with caplog.at_level("WARNING"):
        await bot._init_plugins()

    names = [n for n, _ in registered]
    # share_cmd registered exactly once (by p1 — first wins).
    assert names.count("share_cmd") == 1
    # p2's other command still registers.
    assert "only_second" in names
    assert any("already claimed" in r.message.lower() or "duplicate" in r.message.lower()
               for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: FAIL — `_topo_sort` and dispatch helpers not defined.

- [ ] **Step 3: Edit `bot.py` — add imports**

In `src/link_project_to_chat/bot.py`, find the imports near line 50 (the `from .stream import ...` line, currently line 53). After it, add:

```python
from .plugin import BotCommand, Plugin, PluginContext, load_plugin
```

- [ ] **Step 4: Edit `bot.py` — add module-level `_topo_sort` helper**

Find the `_parse_task_id` helper (line 89). Right after it (before `class ProjectBot(AuthMixin)` at line 93), insert:

```python
def _topo_sort(plugins: "list[Plugin]") -> "list[Plugin]":
    """Order plugins so each comes after the plugins it depends_on.

    Missing dependencies are logged but do not drop the plugin (best-effort).
    """
    by_name = {p.name: p for p in plugins}
    visited: set[str] = set()
    temp: set[str] = set()
    out: list[Plugin] = []

    def visit(p: Plugin) -> None:
        if p.name in visited:
            return
        if p.name in temp:
            logger.warning("plugin dependency cycle involving %s", p.name)
            return
        temp.add(p.name)
        for dep in p.depends_on:
            target = by_name.get(dep)
            if target is None:
                logger.warning(
                    "plugin %s depends_on %s which is not installed; ignoring",
                    p.name, dep,
                )
                continue
            visit(target)
        temp.discard(p.name)
        visited.add(p.name)
        out.append(p)

    for p in plugins:
        visit(p)
    return out
```

- [ ] **Step 5: Edit `bot.py` — add constructor params and state**

In `ProjectBot.__init__` signature (line 94), add new kwargs **immediately after** `conversation_log: ConversationLog | None = None,`:

```python
        plugins: list[dict] | None = None,
        allowed_users: list | None = None,
```

(`allowed_users` is `list[AllowedUser]` — `AllowedUser` is added in Task 3; for now `list | None` is sufficient.)

In the body, find the final state initialization (the `self._probe_tasks: set[asyncio.Task] = set()` line, currently line 235). Right after it, add:

```python
        # Plugin framework state. Populated in _after_ready after transport is ready.
        self._plugin_configs: list[dict] = list(plugins or [])
        self._plugins: list[Plugin] = []
        self._plugin_command_handlers: dict[str, list] = {}
        self._shared_ctx: PluginContext | None = None
        # Sole auth + authority source. Empty list → fail-closed (no users authorized).
        self._allowed_users: list = list(allowed_users or [])
        # Track which scope this allow-list came from so _persist_auth_if_dirty
        # writes back to the right place. "project" for per-project lists,
        # "global" when run_bots used the Config.allowed_users fallback.
        self._auth_source: str = auth_source if auth_source in ("project", "global") else "project"
```

And add `auth_source: str = "project"` to the `ProjectBot.__init__` signature alongside `allowed_users`.

- [ ] **Step 6: Edit `bot.py` — add plugin dispatch helpers and `_init_plugins` / `_shutdown_plugins`**

Insert these methods on `ProjectBot`. Locate `async def _after_ready(self, self_identity)` at line 2485 — put the new methods immediately above it:

```python
    async def _dispatch_plugin_on_message(self, msg) -> bool:
        """Fire on_message for each plugin. Return True if ANY plugin consumed."""
        consumed = False
        for plugin in self._plugins:
            try:
                if await plugin.on_message(msg):
                    consumed = True
            except Exception:
                logger.warning("plugin %s on_message failed", plugin.name, exc_info=True)
        return consumed

    async def _dispatch_plugin_button(self, click) -> bool:
        """Fire on_button for each plugin. Return True if ANY plugin consumed."""
        for plugin in self._plugins:
            try:
                if await plugin.on_button(click):
                    return True
            except Exception:
                logger.warning("plugin %s on_button failed", plugin.name, exc_info=True)
        return False

    async def _dispatch_plugin_tool_use(self, event) -> None:
        for plugin in self._plugins:
            try:
                await plugin.on_tool_use(event.tool, event.path)
            except Exception:
                logger.warning("plugin %s on_tool_use failed", plugin.name, exc_info=True)

    async def _dispatch_plugin_task_complete(self, task) -> None:
        if task.status == TaskStatus.CANCELLED:
            return
        for plugin in self._plugins:
            try:
                await plugin.on_task_complete(task)
            except Exception:
                logger.warning("plugin %s on_task_complete failed", plugin.name, exc_info=True)

    def _plugin_context_prepend(self, prompt: str) -> str:
        """Prepend get_context() outputs to a Claude prompt.

        Only active when the current backend is Claude. Non-Claude backends
        (Codex, future Gemini) don't accept arbitrary system-prompt prepends
        the same way, so plugins that care should branch on ctx.backend_name.
        """
        if self._backend_name != "claude":
            return prompt
        parts: list[str] = []
        for plugin in self._plugins:
            try:
                ctx = plugin.get_context()
            except Exception:
                logger.warning("plugin %s get_context failed", plugin.name, exc_info=True)
                continue
            if ctx:
                parts.append(ctx)
        if not parts:
            return prompt
        return "\n\n".join(parts) + "\n\n---\n\n" + prompt

    def _wrap_plugin_command(self, plugin, bc):
        """Wrap a plugin command handler with auth + role gating + persist.

        Three guards:
        1. **Active-plugin check.** If `plugin.start()` failed (we removed
           the plugin from `self._plugins`), the registered command handler
           silently no-ops. Without this, a failed plugin would still serve
           half-initialized state via its commands.
        2. **Auth + role gate.** `_auth_identity` then `_require_executor`
           (unless `bc.viewer_ok=True`). Same pattern as `_guard_executor`,
           inlined here because `_guard_executor` expects an `IncomingMessage`
           or `CommandInvocation` shape that matches the core handler chain.
        3. **try/finally persist.** Plugin commands can append a first-
           contact identity (via `_auth_identity` → `_get_user_role`). The
           `finally` block calls `_persist_auth_if_dirty` so that lock isn't
           lost when the plugin handler exits — including the viewer-denied
           and exception paths.
        """
        from functools import wraps
        handler = bc.handler
        viewer_ok = bc.viewer_ok
        plugin_ref = plugin  # captured for the active-plugin check

        @wraps(handler)
        async def _wrapped(invocation):
            try:
                # 1. Active-plugin check — plugin may have been removed from
                #    self._plugins after its start() failed.
                if plugin_ref not in self._plugins:
                    logger.debug(
                        "Plugin %s command %r invoked after start failure; "
                        "ignoring",
                        plugin_ref.name, bc.command,
                    )
                    return
                # 2a. Auth (defense-in-depth; transport's authorizer already gated).
                if not self._auth_identity(invocation.sender):
                    return
                # 2b. Role gate (unless viewer_ok).
                if not viewer_ok and not self._require_executor(invocation.sender):
                    assert self._transport is not None
                    await self._transport.send_text(
                        invocation.chat,
                        "Read-only access — your role is viewer.",
                        reply_to=invocation.message,
                    )
                    return
                await handler(invocation)
            finally:
                # 3. Always persist any first-contact lock the auth checks
                #    above may have appended.
                await self._persist_auth_if_dirty()

        return _wrapped

    async def _init_plugins(self) -> None:
        """Instantiate, register, and start plugins. Called from _after_ready."""
        if not self._plugin_configs or self._transport is None:
            return
        # PluginContext field provenance:
        #   bot_name / project_path / bot_username / backend_name / transport
        #     — sourced from ProjectBot state (set in __init__ / _after_ready).
        #   data_dir — fixed convention `~/.link-project-to-chat/meta/<bot_name>`;
        #     created with mkdir below.
        #   _identity_resolver — live bound method (see comment inline).
        #   web_port / public_url / register_in_app_web_handler — INTENTIONALLY
        #     left at their None defaults in v1.0.0. They're API surface
        #     reserved for the future external in-app-web-server plugin and
        #     would be populated by a follow-up spec that wires bot.py to the
        #     Web transport's port / public URL and registers an HTTP-route
        #     callback. Plugins that need them must check for None and
        #     degrade gracefully (documented in PluginContext's docstring).
        self._shared_ctx = PluginContext(
            bot_name=self.name,
            project_path=self.path,
            bot_username=self.bot_username,
            data_dir=Path.home() / ".link-project-to-chat" / "meta" / self.name,
            backend_name=self._backend_name,
            transport=self._transport,
            # LIVE identity resolver. Plugins call ctx.is_allowed(identity) /
            # ctx.is_executor(identity); the helpers consult the bot's
            # _allowed_users at call time, so locks added AFTER plugin init
            # (e.g., a user first-contacting from a new transport later)
            # are visible. The earlier draft snapshotted allowed_identities /
            # executor_identities as flat lists here — that went stale.
            _identity_resolver=self._get_user_role,
        )
        self._shared_ctx.data_dir.mkdir(parents=True, exist_ok=True)

        for cfg in self._plugin_configs:
            pname = cfg.get("name")
            if not pname:
                logger.warning("skipping plugin entry without 'name': %r", cfg)
                continue
            plugin = load_plugin(pname, self._shared_ctx, cfg)
            if plugin:
                self._plugins.append(plugin)

        # Core command names plugins are NOT allowed to shadow. Sourced from
        # `ported_commands` in bot.py:build()/setup; keep in sync if a new
        # core command is added.
        CORE_COMMAND_NAMES: set[str] = {
            "help", "version", "status", "tasks", "backend", "model", "effort",
            "thinking", "context", "permissions", "compact", "reset", "skills",
            "stop_skill", "create_skill", "delete_skill", "persona",
            "stop_persona", "create_persona", "delete_persona", "lang", "start",
            "run", "voice", "halt", "resume",
        }

        # Track which plugin owns which command so we can detect duplicates
        # across plugins (last-load-wins is silently wrong — plugins should
        # not clobber each other).
        registered_command_owner: dict[str, str] = {}

        # Register each plugin's commands on the transport.
        for plugin in self._plugins:
            try:
                cmds = plugin.commands()
            except Exception:
                logger.warning("plugin %s commands() failed; skipping plugin", plugin.name, exc_info=True)
                continue
            for bc in cmds:
                name = bc.command
                if name in CORE_COMMAND_NAMES:
                    logger.warning(
                        "Plugin %s tried to register reserved core command /%s; "
                        "ignoring this command (other commands from this plugin "
                        "remain registered)",
                        plugin.name, name,
                    )
                    continue
                prior_owner = registered_command_owner.get(name)
                if prior_owner is not None:
                    logger.warning(
                        "Plugin %s tried to register /%s already claimed by "
                        "plugin %s; ignoring",
                        plugin.name, name, prior_owner,
                    )
                    continue
                wrapped = self._wrap_plugin_command(plugin, bc)
                self._transport.on_command(name, wrapped)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(name)
                registered_command_owner[name] = plugin.name

        # Start plugins in dependency order; on failure, "unregister" by
        # removing from _plugins so further dispatch skips them. Transport
        # doesn't expose remove-handler, so the registered command stays
        # wired but its plugin is dead — log clearly so this is visible.
        for plugin in _topo_sort(list(self._plugins)):
            try:
                await plugin.start()
            except Exception:
                logger.warning(
                    "plugin %s start failed; removing from dispatch (commands %s remain inert)",
                    plugin.name, self._plugin_command_handlers.get(plugin.name, []),
                    exc_info=True,
                )
                if plugin in self._plugins:
                    self._plugins.remove(plugin)

    async def _shutdown_plugins(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.warning("plugin %s stop failed", plugin.name, exc_info=True)
```

- [ ] **Step 7: Edit `bot.py` — call `_init_plugins` from `_after_ready`**

In `_after_ready` (line 2485), find the existing `self._refresh_team_system_note()` call (line 2497). Right after it, add:

```python
        await self._init_plugins()
```

Make sure `_after_ready` does NOT early-return before this for team bots — re-read the method body. If the team-bot branch returns at line 2504 (`if self.team_name and self.role: return`), move the `await self._init_plugins()` call to **before** that return:

```python
        self.bot_username = self_identity.handle or ""
        if self.team_name and self.role and self.bot_username:
            self._backfill_own_bot_username()
        self._refresh_team_system_note()
        await self._init_plugins()
        # ... then the existing trusted-user ping logic
```

- [ ] **Step 8: Edit `bot.py` — register `on_stop` callback in `build()`**

In `build()` (line 2518), find `self._transport.on_ready(self._after_ready)` (line 2546). Right after it, add:

```python
        self._transport.on_stop(self._shutdown_plugins)
```

- [ ] **Step 9: Edit `bot.py` — fire `on_tool_use` from `_on_stream_event`**

In `_on_stream_event` (line 388), find the `elif isinstance(event, ToolUse):` branch (line 454). After the existing `if event.path and self._is_image(event.path): await self._send_image(...)` block (line 455–456), add:

```python
            await self._dispatch_plugin_tool_use(event)
```

- [ ] **Step 10: Edit `bot.py` — fire `on_task_complete` from `_on_task_complete`**

In `_on_task_complete` (line 737), at the very end (after the `else: await self._finalize_command_task(task)` branch at line 772), add:

```python
        await self._dispatch_plugin_task_complete(task)
```

- [ ] **Step 11: Edit `bot.py` — fire `on_message` in `_on_text`**

In `_on_text` (line 1003), find the auth + rate-limit block. After the rate-limit check (line 1024, after the `Rate limited. Try again shortly.` reply) and **before** the `if incoming.has_unsupported_media:` branch (line 1026), insert:

```python
        consumed = await self._dispatch_plugin_on_message(incoming)
        if consumed:
            return
```

(Plugins see authorized, rate-limit-passing messages. They get the chance to consume before primary's flow processes pending-skill/pending-persona/waiting/supersede/Claude submission.)

- [ ] **Step 12: Edit `bot.py` — prepend plugin context in `_build_user_prompt`**

In `_build_user_prompt` (line 976), find the end of the prompt-assembly logic (after persona is applied around line 999). Find the `return prompt` at the end of the method. Replace it with:

```python
        prompt = self._plugin_context_prepend(prompt)
        return prompt
```

(This is the central point where every Claude turn's prompt is built — both `_on_text` and `_submit_group_message_to_claude` go through this. One edit covers both.)

- [ ] **Step 13: Edit `bot.py` — fire plugin button dispatch in `_on_button`**

In `_on_button` (line 1960), find the body right after the `if not self._auth_identity(click.sender): return` check (line 1962–1963). After `chat = click.chat` (line 1967), add:

```python
        if await self._dispatch_plugin_button(click):
            return
```

- [ ] **Step 14: Edit `bot.py` — accept `plugins` and `allowed_users` in `run_bot`**

In `run_bot` (line 2649), add to the signature (right after `context_history_limit: int = 10,` at line 2681):

```python
    plugins: list[dict] | None = None,
    allowed_users: list | None = None,
```

In the body, find the `bot = ProjectBot(...)` call (line 2696). Add to its kwargs (right before the closing `)`):

```python
        plugins=plugins,
        allowed_users=allowed_users,
```

Mirror in `run_bots` (line 2740). Add to its `run_bot(...)` call (line 2766):

```python
            plugins=getattr(proj, "plugins", None) or None,
            allowed_users=getattr(proj, "allowed_users", None) or None,
```

`getattr` with a default keeps Task 2 green at commit time — `ProjectConfig.plugins` and `ProjectConfig.allowed_users` don't exist until Task 3 lands. Once Task 3 adds the fields, these reads return the real lists. Task 4 Step 6 replaces the line with the `resolve_project_allowed_users` call that returns `(users, source)`, so the `getattr` shape is transient.

- [ ] **Step 15: Run tests to verify they pass**

```bash
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: All tests PASS.

- [ ] **Step 16: Run the full suite for regressions**

```bash
pytest -q
```
Expected: Pre-existing baseline (recorded in Task 0) plus this task's new tests, all PASS. If anything else breaks, the most likely cause is a hook placement issue. Re-read the surrounding code.

- [ ] **Step 17: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_plugin_hooks.py
git commit -m "$(cat <<'EOF'
feat(plugin): wire plugin lifecycle into ProjectBot

Adds plugin constructor params, _topo_sort, dispatch helpers
(_dispatch_plugin_on_message, _dispatch_plugin_button,
_dispatch_plugin_tool_use, _dispatch_plugin_task_complete,
_plugin_context_prepend, _wrap_plugin_command), _init_plugins called
from _after_ready, _shutdown_plugins registered via transport.on_stop.
Plugin commands flow through transport.on_command with auth + role
gating. get_context() prepend only active for Claude backend.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Config schema — `plugins`, `AllowedUser` (with `locked_identities`), `resolve_project_allowed_users` helper, transitional legacy fields

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/test_config_allowed_users.py`
- Create: `tests/test_config_migration.py`

`config.py` is now 1379 LOC with `BotPeerRef`, `RoomBinding`, `backend_state`, etc. The shape we're changing:

- **Current on-disk shape** (verified by reading config.py at line 46 and 98):
  - `allowed_usernames: list[str]`
  - `trusted_users: dict[str, int | str]`   ← **dict**, not list (post-A1; current default)
  - `trusted_user_ids: list[int]`            ← legacy fallback only when `trusted_users` is missing/empty

- **Our migration must handle three shapes** of `trusted_users` on input:
  1. `dict[str, username → int_user_id]` (current; populated by A1 fix in 2a7b8e7)
  2. Legacy `list[str]` (pre-A1; aligned with `trusted_user_ids` by index)
  3. Missing entirely (rely on `trusted_user_ids` alone)

We add `plugins` and `allowed_users` to `ProjectConfig` and to `Config` (global). **`TeamBotConfig` is not touched** — team bots inherit auth from `Config.allowed_users`, matching existing behavior. The legacy `allowed_usernames` / `trusted_users` / `trusted_user_ids` fields **stay on the dataclasses** through this task as read-only inputs — the loader populates them so existing callers in `bot.py` / `cli.py` / `manager/bot.py` keep working until Task 5 migrates every call site. The **save format already writes only `allowed_users`** — legacy keys are stripped from disk on first save after upgrade. Legacy fields get removed from the dataclasses in Task 5's final step once nothing reads them.

We add a one-way migration on load that sets `Config.migration_pending` for the CLI to act on, plus a new helper `resolve_project_allowed_users(project, config) -> tuple[list[AllowedUser], str]` (returns `(users, "project"|"global")` so the caller knows which scope to persist back to). Callers use this helper instead of touching the legacy fields directly.

**Migration contract:** legacy keys are read on load (synthesized into `allowed_users` with `role="executor"`), then **stripped** from the save format. The first save after upgrade rewrites `config.json` without them. There is no path back to the legacy shape.

- [ ] **Step 1: Write the failing tests — `tests/test_config_allowed_users.py`**

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    AllowedUser,
    Config,
    ProjectConfig,
    load_config,
    save_config,
)


def test_allowed_user_defaults_to_viewer():
    u = AllowedUser(username="alice")
    assert u.role == "viewer"
    assert u.locked_identities == []


def test_project_config_has_plugins_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.plugins == []


def test_project_config_has_allowed_users_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.allowed_users == []


# NOTE: A `test_legacy_fields_are_not_dataclass_attributes` would belong
# here in principle, but legacy fields STAY on ProjectConfig and Config
# through Tasks 3–4 as transitional read-only inputs (existing callers in
# bot.py / cli.py / manager/bot.py still read them until Task 5's audit
# rewrites them). That test is added in **Task 5 Step 12** after the
# dataclass field removal lands. Don't add it here — it would fail by
# design at the end of Task 3.


def test_save_load_roundtrip(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["myp"] = ProjectConfig(
        path="/tmp/p",
        telegram_bot_token="t",
        allowed_users=[
            AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
            AllowedUser(username="bob", role="viewer"),
        ],
        plugins=[{"name": "in-app-web-server"}, {"name": "diff", "option": 1}],
    )
    save_config(cfg, cfg_file)
    loaded = load_config(cfg_file)
    p = loaded.projects["myp"]
    assert {(u.username, u.role, tuple(u.locked_identities)) for u in p.allowed_users} == {
        ("alice", "executor", 12345),
        ("bob", "viewer", None),
    }
    assert p.plugins == [{"name": "in-app-web-server"}, {"name": "diff", "option": 1}]


def test_unknown_role_falls_back_to_viewer(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [{"username": "x", "role": "admin"}],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    assert p.allowed_users == [AllowedUser(username="x", role="viewer", locked_identities=[])]


def test_malformed_plugin_entry_skipped(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "plugins": [{"name": "good"}, {"not_name": "bad"}, "string-not-dict"],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].plugins == [{"name": "good"}]


def test_malformed_allowed_user_entry_skipped_with_warning(tmp_path, caplog):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "good", "role": "viewer"},
                    {"not_username": "missing"},
                    "string-not-dict",
                ],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.projects["p"].allowed_users == [
        AllowedUser(username="good", role="viewer", locked_identities=[]),
    ]


def test_empty_allowed_users_after_load_logs_warning(tmp_path, caplog):
    """Per-load empty allowlist emits WARNING; CRITICAL aggregation is done by CLI start."""
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        load_config(cfg_file)
    assert any(
        "no users authorized" in r.message.lower() and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_migration_pending_flag_unset_on_clean_config(tmp_path):
    """A config without any legacy fields loads with migration_pending=False."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor"},
                ],
            }
        }
    }))
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is False
```

- [ ] **Step 2: Write the failing migration tests — `tests/test_config_migration.py`**

Six golden-file fixtures matching the spec's testing section:
(a) `allowed_usernames` only;
(b) dict-shape `trusted_users` (current on-disk format) covering a subset of `allowed_usernames`;
(c) dict-shape `trusted_users` covering all of `allowed_usernames`;
(d) legacy list-shape `trusted_users` aligned with `trusted_user_ids` by index;
(e) global `Config.allowed_usernames` migrating while a project's per-project list is empty;
(f) orphan trust — `trusted_users` contains a username not in `allowed_usernames`.

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    AllowedUser,
    load_config,
    save_config,
)


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2))


def test_migration_a_allowed_usernames_only(tmp_path: Path):
    """Shape (a): only allowed_usernames; no trust info at all."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    p = loaded.projects["p"]
    assert p.allowed_users == [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor"),
    ]
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in written["projects"]["p"]
    assert "allowed_users" in written["projects"]["p"]


def test_migration_b_trusted_users_dict_subset(tmp_path: Path):
    """Shape (b): current on-disk format — trusted_users is dict, subset of allowed_usernames."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob", "carol"],
                "trusted_users": {"alice": 12345},  # dict shape (post-A1)
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].role == "executor" and by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].role == "executor" and by_user["bob"].locked_identities == []
    assert by_user["carol"].role == "executor" and by_user["carol"].locked_identities == []


def test_migration_c_trusted_users_dict_full(tmp_path: Path):
    """Shape (c): every allowed user is in the trusted dict."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": {"alice": 12345, "bob": 67890},
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_d_legacy_list_with_ids_aligned(tmp_path: Path):
    """Shape (d): pre-A1 — trusted_users is a list aligned with trusted_user_ids by index."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": ["alice", "bob"],
                "trusted_user_ids": [12345, 67890],
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_e_global_config_migration(tmp_path: Path):
    """Shape (e): global Config.allowed_usernames migrates while per-project is empty."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "allowed_usernames": ["admin"],
        "trusted_users": {"admin": 99999},
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                # No legacy fields at project scope.
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    # Global allow-list got migrated.
    assert loaded.allowed_users == [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:99999"]),
    ]
    # Project has empty allowed_users (and that's fine — it'll fall back to
    # Config.allowed_users in some paths, or warn at startup).
    assert loaded.projects["p"].allowed_users == []
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in written
    assert "trusted_users" not in written
    assert written["allowed_users"] == [
        {"username": "admin", "role": "executor", "locked_identities": ["telegram:99999"]},
    ]


def test_migration_f_orphan_trust(tmp_path: Path):
    """Shape (f): trusted_users contains a username not in allowed_usernames.

    Should still produce an AllowedUser entry — preserves access. No data loss.
    """
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"bob": 67890},  # bob NOT in allowed_usernames
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert "alice" in by_user
    assert "bob" in by_user
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_g_web_session_id_normalized(tmp_path: Path):
    """Pre-v1.0 Web stored trusted_users["alice"] = "web-session:abc-def".
    The legacy value contains ":" but lacks the "web:" transport prefix
    that the new identity-keyed auth comparison requires. Migration must
    normalize "web-session:abc" → "web:web-session:abc" so the locked
    identity matches _identity_key(web_identity) at runtime."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "web-session:abc-def"},
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["web:web-session:abc-def"]


def test_migration_h_unknown_prefix_falls_back_to_telegram(tmp_path: Path):
    """Bare strings that don't match a known transport prefix migrate as
    telegram (the legacy default — pre-multi-transport configs)."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "12345"},  # bare numeric string
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]


def test_legacy_list_length_mismatch_drops_ids(tmp_path, caplog):
    """Mismatched legacy list shapes drop the IDs and log WARNING."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": ["alice"],  # list shape
                "trusted_user_ids": [],      # length mismatch
            }
        }
    })
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert "length mismatch" in caplog.text.lower()
    p = loaded.projects["p"]
    assert p.allowed_users == [AllowedUser(username="alice", role="executor", locked_identities=[])]


def test_save_strips_legacy_keys(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    })
    loaded = load_config(cfg_file)
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    p = written["projects"]["p"]
    assert "allowed_usernames" not in p
    assert "trusted_users" not in p
    assert "trusted_user_ids" not in p
    assert p["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]


def test_load_save_load_is_stable(tmp_path: Path):
    """Second load after save has migration_pending=False (idempotent)."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    })
    once = load_config(cfg_file)
    assert once.migration_pending is True
    save_config(once, cfg_file)
    twice = load_config(cfg_file)
    assert twice.migration_pending is False
    save_config(twice, cfg_file)
    final = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in final["projects"]["p"]
    assert final["projects"]["p"]["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]
```

- [ ] **Step 3: Run the tests — expect failure**

```bash
pytest tests/test_config_allowed_users.py tests/test_config_migration.py -v
```
Expected: FAIL — `AllowedUser` not importable; `ProjectConfig.plugins` / `.allowed_users` not defined; legacy fields still on dataclass.

- [ ] **Step 4: Edit `config.py` — add `AllowedUser` dataclass and helpers**

In `src/link_project_to_chat/config.py`, find the `BotPeerRef` dataclass (line 28). Right before it, add:

```python
_VALID_ROLES = ("viewer", "executor")


@dataclass
class AllowedUser:
    username: str
    role: str = "viewer"
    locked_identities: list[str] = field(default_factory=list)
    # Platform-portable identity locks: list of "transport_id:native_id" strings.
    # Each transport a user contacts from gets a new entry appended on first
    # contact (no replacement). Auth succeeds if any entry matches the
    # current identity. Examples after first contact:
    #   ["telegram:12345"]
    #   ["web:web-session:abc-def"]
    #   ["telegram:12345", "web:web-session:abc-def"]  (same user, two transports)
    # Replaces the int-only ID locking from the legacy design.


def _parse_allowed_users(raw_list) -> list[AllowedUser]:
    out: list[AllowedUser] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            logger.warning("malformed allowed_users entry (not a dict): %r", entry)
            continue
        username = entry.get("username")
        if not username:
            logger.warning("malformed allowed_users entry (missing username): %r", entry)
            continue
        role = entry.get("role", "viewer")
        if role not in _VALID_ROLES:
            logger.warning("unknown role %r for %s; defaulting to viewer", role, username)
            role = "viewer"
        # Accept the new list shape; tolerate the single-string shape used
        # during the migration window (auto-wrap).
        raw_locked = entry.get("locked_identities", [])
        if isinstance(raw_locked, str):
            raw_locked = [raw_locked]
        if not isinstance(raw_locked, list):
            logger.warning(
                "malformed locked_identities for %s (expected list of strings): %r; dropping",
                username, raw_locked,
            )
            raw_locked = []
        locked_identities = [s for s in raw_locked if isinstance(s, str)]
        if len(locked_identities) != len(raw_locked):
            logger.warning(
                "dropped non-string entries from locked_identities for %s", username,
            )
        out.append(AllowedUser(
            username=str(username).lstrip("@").lower(),
            role=role,
            locked_identities=locked_identities,
        ))
    return out


def _serialize_allowed_users(users: list[AllowedUser]) -> list[dict]:
    out = []
    for u in users:
        entry: dict = {"username": u.username, "role": u.role}
        if u.locked_identities:
            entry["locked_identities"] = list(u.locked_identities)
        out.append(entry)
    return out


def _parse_plugins(raw_list) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        if not entry.get("name"):
            continue
        out.append(entry)
    return out


def _migrate_legacy_auth(raw: dict) -> tuple[list[AllowedUser], bool]:
    """One-way migration from legacy fields → AllowedUser list.

    Reads `allowed_usernames` (list[str]), `trusted_users` (dict[str, int|str]
    OR legacy list[str]), and `trusted_user_ids` (list[int], legacy-only) from
    `raw` and synthesizes `AllowedUser{role="executor"}` entries.

    Returns (allowed_users, migrated) where `migrated` is True iff any legacy
    field was present in `raw`. The caller uses `migrated` to set
    `migration_pending` on the Config so the CLI saves on first start.

    Username normalization: lowercase, strip leading `@`.
    Locked ID source order:
      1. `trusted_users` dict — explicit username → user_id mapping (current shape).
      2. `trusted_users` list (pre-A1) aligned with `trusted_user_ids` by index.
      3. `trusted_user_ids` aligned with `allowed_usernames` by index (oldest shape).
    On mismatched lengths in the list paths, IDs are dropped and the entries
    re-lock on next contact (logged at WARNING).
    """
    legacy_unames = raw.get("allowed_usernames") or []
    raw_trusted = raw.get("trusted_users")
    legacy_ids = raw.get("trusted_user_ids") or []
    if not (legacy_unames or raw_trusted or legacy_ids):
        return [], False

    def _norm(name) -> str:
        return str(name).lstrip("@").lower()

    # Build a username → locked_identities map ("telegram:<id>" strings).
    # Legacy fields predate multi-transport support, so every legacy ID belongs
    # to Telegram; we prefix with "telegram:" so each entry in locked_identities is
    # immediately usable by the new identity-keyed auth comparison.
    # Returns lists (not single strings) so the migration is shape-compatible
    # with the new `locked_identities` field.
    identities_for: dict[str, list[str]] = {}
    legacy_trusted_names: list[str] = []
    # Only values that already start with a KNOWN transport prefix are passed
    # through; everything else is assumed Telegram (legacy default). This is
    # the fix for the Web case: pre-v1.0 Web bound trusted_users["alice"] =
    # "web-session:abc" — contains ":" but does NOT have the "web:" transport
    # prefix that auth comparisons need. We detect this by matching a known
    # transport whitelist; anything else falls back to telegram or to bare
    # passthrough only if a known prefix is present.
    _KNOWN_TRANSPORT_PREFIXES = ("telegram:", "web:", "discord:", "slack:")

    def _normalize_legacy_trust_id(uid_str: str) -> str:
        """Turn a legacy trusted_users value into a 'transport_id:native_id' string."""
        # Already correctly prefixed?
        for prefix in _KNOWN_TRANSPORT_PREFIXES:
            if uid_str.startswith(prefix):
                return uid_str
        # The Web case: bare "web-session:abc" → "web:web-session:abc".
        if uid_str.startswith("web-session:"):
            return f"web:{uid_str}"
        # Plain numeric or arbitrary string → telegram (legacy default).
        try:
            return f"telegram:{int(uid_str)}"
        except (TypeError, ValueError):
            return f"telegram:{uid_str}"

    # SHAPE DISCRIMINATOR — isinstance() dispatches the three on-disk
    # trusted_users shapes (see spec section "Migration semantics" and
    # Step 2's golden-file tests (b)/(c) [dict shape] and (d) [list shape]).
    # Without this branch, dict-shape configs would silently fall into the
    # list branch and crash on `for name, uid in zip(...)` with a TypeError.
    if isinstance(raw_trusted, dict):
        # Current on-disk shape: username → user_id (int or str).
        # Covered by tests (b) test_migration_b_trusted_users_dict_subset
        # and (c) test_migration_c_trusted_users_dict_full.
        for uname, uid in raw_trusted.items():
            norm = _norm(uname)
            legacy_trusted_names.append(norm)
            if uid is None:
                continue
            identities_for.setdefault(norm, []).append(_normalize_legacy_trust_id(str(uid)))
    elif isinstance(raw_trusted, list):
        # Pre-A1 shape: list of usernames aligned with trusted_user_ids by index.
        # Covered by test (d) test_migration_d_legacy_list_with_ids_aligned.
        legacy_trusted_names = [_norm(n) for n in raw_trusted]
        if len(legacy_trusted_names) == len(legacy_ids):
            for name, uid in zip(legacy_trusted_names, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        elif legacy_ids:
            logger.warning(
                "legacy trusted_users(list) / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs — affected users will re-lock on next contact",
                len(legacy_trusted_names), len(legacy_ids),
            )
        # else: list trusted_users without ids; nothing to map.
    elif legacy_ids and legacy_unames:
        # Oldest shape: trusted_user_ids aligned with allowed_usernames by index.
        norm_allowed = [_norm(n) for n in legacy_unames]
        if len(norm_allowed) == len(legacy_ids):
            for name, uid in zip(norm_allowed, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        else:
            logger.warning(
                "legacy allowed_usernames / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs", len(norm_allowed), len(legacy_ids),
            )

    # Union of allowed_usernames + any trusted-only usernames (orphan trust);
    # all become executor.
    seen: set[str] = set()
    out: list[AllowedUser] = []
    for raw_name in list(legacy_unames) + list(legacy_trusted_names):
        norm = _norm(raw_name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(AllowedUser(
            username=norm,
            role="executor",
            locked_identities=list(identities_for.get(norm, [])),
        ))
    locked_count = sum(1 for u in out if u.locked_identities)
    logger.info(
        "migrated legacy auth fields → %d AllowedUser entries (%d with locked identities)",
        len(out), locked_count,
    )
    return out, True


def resolve_project_allowed_users(project, config) -> tuple[list[AllowedUser], str]:
    """Project allow-list with global fallback. Returns (users, source).

    Source is "project" when the project's own allow-list is non-empty,
    "global" when falling back to Config.allowed_users. The bot uses the
    source to write back to the matching scope when persisting first-contact
    locks via `_persist_auth_if_dirty`.

    Matches the precedence of the existing `resolve_project_auth_scope`
    (project overrides global, falls back to global) so deployments where the
    project list is empty don't suddenly fail-closed when only the global
    list is populated.

    Callers in bot.py / cli.py / manager/bot.py use this helper instead of
    reading project.allowed_usernames / project.trusted_users directly.
    """
    if project.allowed_users:
        return project.allowed_users, "project"
    return config.allowed_users, "global"
```

- [ ] **Step 5: Edit `config.py` — `ProjectConfig` and `Config` (NOT `TeamBotConfig`)**

Find `class ProjectConfig:` (line ~42) and `class Config:` (line ~96). **Add** these fields to both (keeping the legacy three for now as read-only inputs — Task 5 removes them once all call sites have migrated):

```python
    allowed_users: list[AllowedUser] = field(default_factory=list)
```

**Add to `ProjectConfig` only** (plugins are per-project, not global):
```python
    plugins: list[dict] = field(default_factory=list)
```

The legacy fields `allowed_usernames` / `trusted_users` / `trusted_user_ids` **stay on the dataclass** during this task. Reason: existing callers (`run_bots` → `resolve_project_auth_scope`, manager bot, CLI configure) still read them. Removing them in this commit would break the suite. They get removed in Task 5's final step once all call sites use `resolve_project_allowed_users`.

**`TeamBotConfig` (line ~65) is not modified** — team bots inherit auth from `Config.allowed_users`, same as today. Add no fields there.

Also add a `migration_pending: bool` attribute on `Config`. This is a runtime flag, not persisted:

```python
# Inside Config dataclass, near the other fields:
    migration_pending: bool = field(default=False, repr=False, compare=False)
    # Runtime flag set by load_config when legacy auth fields were read; CLI
    # start uses it to force a save before serving traffic. Intentionally
    # serialization-suppressed via `field(default=False)`; save_config skips
    # writing it.
```

- [ ] **Step 6: Edit `config.py` — `load_config` migration wiring (populate BOTH new and legacy fields during the transition)**

In `load_config`, locate the per-project parsing block (`ProjectConfig(...)` construction; search for the first instance of `allowed_usernames=` near line 95). Augment (don't replace) the existing legacy-field reads with the new fields. Both populate.

```python
            explicit = _parse_allowed_users(proj.get("allowed_users", []))
            migrated, did_migrate = _migrate_legacy_auth(proj)
            # Explicit allowed_users wins; migration only fills if the
            # explicit list is empty.
            effective = explicit or migrated
            if did_migrate:
                config.migration_pending = True
            # Keep building the legacy fields too — Task 5 removes them once
            # every call site has switched to resolve_project_allowed_users.
            # ... existing allowed_usernames=, trusted_users=, trusted_user_ids= reads ...
            # PLUS:
            project = ProjectConfig(
                ...,  # existing kwargs
                plugins=_parse_plugins(proj.get("plugins", [])),
                allowed_users=effective,
            )
```

Apply the same pattern to the **global** `Config(...)` parsing block (search for `allowed_usernames=_migrate_usernames(raw, ...)` near line 160). The global path also calls `_migrate_legacy_auth(raw)` and sets `migration_pending` on the same `config` object.

Empty `effective` at the project scope logs WARNING (the CLI aggregates these into a single CRITICAL line at startup phase — see Task 4):

```python
            if not effective and not config.allowed_users:
                # Only warn when BOTH scopes are empty — global fallback would
                # otherwise cover an empty project list.
                logger.warning(
                    "project %r has no users authorized at either project or "
                    "global scope; bot will reject all messages until populated",
                    name_iter,
                )
```

The `Config.migration_pending` flag is initialized `False` at the top of `load_config` (`config = Config()` already does that since the field default is False).

- [ ] **Step 7: Edit `config.py` — `save_config` (write new shape, strip legacy keys)**

Find `save_config` (or `_merge_project_entry`; search for the per-project serialization that writes `allowed_usernames`). Remove any code that emits `allowed_usernames`, `trusted_users`, `trusted_user_ids` to the project dict — even though the dataclass still carries those fields during this task, **disk format never sees them again**. Add:

```python
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)

        if p.allowed_users:
            proj["allowed_users"] = _serialize_allowed_users(p.allowed_users)
        else:
            proj.pop("allowed_users", None)

        # Strip legacy keys (idempotent — present only on first save after upgrade).
        proj.pop("allowed_usernames", None)
        proj.pop("trusted_users", None)
        proj.pop("trusted_user_ids", None)
```

Mirror in the **global** serialization block (where `raw["allowed_usernames"] = config.allowed_usernames` was written near line 277). Replace with the `allowed_users` write and the same legacy-key pops.

**Do NOT touch the team-bot serialization block.** It never carried these fields.

After saving, clear the in-memory flag: `config.migration_pending = False`. (Optional, but lets callers re-read state without re-saving.)

- [ ] **Step 7b: Add atomic-RMW helpers (`locked_config_rmw` + `save_config_within_lock`)**

Task 5's `_persist_auth_if_dirty` needs an atomic load-modify-save cycle: two concurrent first-contact saves would each read the pre-write state and clobber each other under the current "lock-only-on-save" model. Add a context manager that holds `_config_lock` across the WHOLE cycle.

Refactor the existing `load_config` and `save_config` into wrapper + inner pairs:

```python
# Existing _config_lock context manager is already in config.py
# (around line 143). Keep it. Refactor load/save to expose unlocked
# inner helpers so the RMW manager can hold the lock for both.

def _load_config_unlocked(path: Path) -> Config:
    """Load Config without acquiring _config_lock. Caller must hold the lock."""
    # Move the body of the current load_config here, dropping the with statement.
    ...

def _save_config_unlocked(config: Config, path: Path) -> None:
    """Save Config without acquiring _config_lock. Caller must hold the lock."""
    # Move the body of the current save_config here, dropping the with statement.
    ...

def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    """Public API: acquires _config_lock for the read."""
    with _config_lock(path):
        return _load_config_unlocked(path)

def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Public API: acquires _config_lock for the write."""
    with _config_lock(path):
        _save_config_unlocked(config, path)

@contextmanager
def locked_config_rmw(path: Path = DEFAULT_CONFIG):
    """Hold _config_lock across a load-modify-save cycle.

    Yields a freshly-loaded Config; caller mutates it; the context manager's
    block writes it back. Use `save_config_within_lock` inside the block
    (NOT save_config — that would deadlock by re-acquiring the same lock).

    Used by ProjectBot._persist_auth_if_dirty so concurrent first-contact
    locks serialize correctly. Without this, two processes could each
    `load_config()` the same pre-write state, append different identities,
    and `save_config()` — last writer wins.
    """
    with _config_lock(path):
        config = _load_config_unlocked(path)
        yield config

def save_config_within_lock(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Save Config without re-acquiring _config_lock. For callers inside
    locked_config_rmw who already hold the lock."""
    _save_config_unlocked(config, path)
```

Add a small TDD test in `tests/test_config_allowed_users.py`:

```python
def test_locked_config_rmw_round_trip_smoke(tmp_path: Path):
    """Smoke test: API exists and a basic RMW cycle works.

    This does NOT prove the lock is held across the load/save — it only
    proves the context manager round-trips cleanly. The real concurrency
    test (`test_locked_config_rmw_actually_serializes_writers` below) uses
    multiprocessing to force contention.
    """
    from link_project_to_chat.config import locked_config_rmw, save_config_within_lock

    cfg_file = tmp_path / "config.json"
    save_config(Config(), cfg_file)

    with locked_config_rmw(cfg_file) as cfg:
        cfg.allowed_users = [AllowedUser(username="alice", role="executor")]
        save_config_within_lock(cfg, cfg_file)

    reloaded = load_config(cfg_file)
    assert reloaded.allowed_users == [AllowedUser(username="alice", role="executor", locked_identities=[])]


# Module-scope worker — multiprocessing's default start method on macOS and
# Windows is "spawn", which requires the target callable to be importable
# (= pickled by qualified name). Nested functions inside a test body can't
# be pickled under spawn. Keep this at module scope.
def _rmw_contention_worker(cfg_file_path: str, identity: str) -> None:
    from pathlib import Path as _Path
    import time
    from link_project_to_chat.config import (
        locked_config_rmw, save_config_within_lock,
    )
    with locked_config_rmw(_Path(cfg_file_path)) as disk:
        # Tiny sleep widens the contention window — without the cross-phase
        # lock, this all but guarantees one writer clobbers the other.
        time.sleep(0.05)
        for au in disk.allowed_users:
            if au.username == "alice" and identity not in au.locked_identities:
                au.locked_identities = au.locked_identities + [identity]
        save_config_within_lock(disk, _Path(cfg_file_path))


def test_locked_config_rmw_actually_serializes_writers(tmp_path: Path):
    """Real contention test: two writers, each appending a different
    identity to the same user, must converge to BOTH identities on disk.

    If `locked_config_rmw` only locked the write phase (like the rejected
    earlier design), one writer would load the pre-write state, the other
    would also load it, both would compute different merged states, and the
    last-to-save would clobber the first. With the lock held across the
    whole load→modify→save cycle, the second writer sees the first writer's
    result and unions on top.

    Uses multiprocessing to force separate file-lock holders (a single
    process can't really test fcntl.flock contention against itself).
    Forces the 'spawn' context explicitly so the test behaves the same on
    Linux (default 'fork') and macOS/Windows (default 'spawn'); requires
    the worker to be at module scope so it can be pickled.
    """
    import multiprocessing as mp
    from link_project_to_chat.config import (
        AllowedUser, Config, load_config, save_config,
    )

    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [AllowedUser(username="alice", role="executor")]
    save_config(cfg, cfg_file)

    ctx = mp.get_context("spawn")  # explicit; identical behavior across OSes
    p1 = ctx.Process(target=_rmw_contention_worker, args=(str(cfg_file), "telegram:1"))
    p2 = ctx.Process(target=_rmw_contention_worker, args=(str(cfg_file), "web:web-session:abc"))
    p1.start(); p2.start()
    p1.join(); p2.join()
    assert p1.exitcode == 0 and p2.exitcode == 0

    final = load_config(cfg_file)
    alice = next(u for u in final.allowed_users if u.username == "alice")
    # Both writers' identities must be present — neither clobbered the other.
    assert "telegram:1" in alice.locked_identities
    assert "web:web-session:abc" in alice.locked_identities
```

**On test stability**: the contention test uses a 50ms `time.sleep` to widen the race window. The flakiness shape would be a false PASS (lock not held but they happened to interleave benignly) — not a false FAIL. If `fcntl.flock` is not respected on the runner's filesystem (rare; some network-mounted /tmp setups), this test will fail consistently, which is the desired signal. Skip on Windows if the file-locking semantics are too platform-specific to mirror in CI; the integration test in Task 5 (`test_persist_merges_per_user_not_replace`) covers the higher-level behavior.

- [ ] **Step 8: Audit `config.py` only — confirm new code paths are wired in**

```bash
grep -n "allowed_users\|migration_pending\|resolve_project_allowed_users" src/link_project_to_chat/config.py
```

Verify that the loader populates **both** the new `allowed_users` field and the legacy `allowed_usernames` / `trusted_users` / `trusted_user_ids` fields on `ProjectConfig` and `Config`, that `migration_pending` is set when legacy keys were read, and that `resolve_project_allowed_users` is defined and exported.

**The legacy fields stay on the dataclasses through this task.** Existing callers in `bot.py` / `cli.py` / `manager/bot.py` still read them — that's expected. The repo-wide audit and call-site rewrites happen in **Task 5 Step 11**; the dataclass-level field removal happens in **Task 5 Step 12**.

Spot-check that `save_config` already strips legacy keys from disk (Step 7) — `grep -n "pop.*allowed_usernames\|pop.*trusted_users" src/link_project_to_chat/config.py` should show the pops are in place.

- [ ] **Step 9: Run target tests**

```bash
pytest tests/test_config_allowed_users.py tests/test_config_migration.py -v
```
Expected: All PASS.

- [ ] **Step 10: Verify existing config tests still pass**

Existing tests in `tests/test_config.py`, `tests/test_auth*.py`, `tests/manager/test_bot*.py`, `tests/test_bot_team_wiring.py` read `proj.allowed_usernames` / `config.trusted_users` / etc. **These tests must KEEP passing as-is at the end of this task** — because we deliberately kept the legacy fields on the dataclasses. If any test breaks here, the loader is failing to populate the legacy fields and that's a bug to fix in Step 6.

Repository-wide rewrites of the legacy-field test references happen in **Task 5 Step 11**, AFTER all source-code call sites have moved to `resolve_project_allowed_users`. Don't pre-emptively rewrite tests in this task.

- [ ] **Step 11: Run the full suite**

```bash
pytest -q
```
Expected: green. If anything in `tests/manager/` or `tests/test_bot_*` references legacy fields, fix it (these are existing tests that must adapt to the new schema).

- [ ] **Step 12: Commit**

```bash
git add src/link_project_to_chat/config.py \
        tests/test_config_allowed_users.py \
        tests/test_config_migration.py
# NOTE: do NOT include broad tests/ updates here — legacy field test
# references stay valid (loader still populates legacy fields). Repo-wide
# test rewrites land in Task 5 Step 11 after source code migrates.
git commit -m "$(cat <<'EOF'
feat(config): add AllowedUser + allowed_users field (legacy fields read-only)

ProjectConfig and Config (global) gain:
- `plugins: list[dict]` (ProjectConfig only)
- `allowed_users: list[AllowedUser{username, role, locked_identities}]`
  where locked_identities is a list of "transport_id:native_id" strings
  so the same username can authenticate from multiple transports
  (Telegram + Web + future Discord/Slack all accumulate entries).

Legacy fields `allowed_usernames` / `trusted_users` / `trusted_user_ids`
STAY on the dataclasses during this task as transitional read-only
inputs. Save format writes ONLY `allowed_users` (legacy keys stripped
from disk). Repo-wide rewrites of code that reads the legacy fields
happen in Task 5 Step 11; field-level removal in Task 5 Step 12 — once
no caller is left.

Legacy `trusted_users` dict (current format) maps username → id; legacy
list form aligned with trusted_user_ids by index is also supported.
Loader sets Config.migration_pending = True when legacy fields were
read; the CLI's start command saves once to materialize the new shape
on disk.

TeamBotConfig is unchanged — team bots continue to inherit from
Config.allowed_users (matches existing behavior). Unknown roles fall
back to viewer; malformed entries log warnings and are skipped; empty
allowed_users after migration logs WARNING (CRITICAL aggregation
happens in CLI start).

BREAKING CHANGE: config.json on-disk schema. The dataclass-level
legacy field removal lands in Task 5; this task only changes the
written shape. Operators upgrading need to verify the resulting
allowed_users list and run the bot under supervision on first start.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI — `plugin-call` + `migrate-config` + `--add-user`/`--remove-user`/`--reset-user-identity` on `configure`

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/test_cli.py` (append)

`run_bot` / `run_bots` already accept `plugins` and `allowed_users` after Task 2 (via `getattr(proj, ...)` during the transitional window). Task 3 added the dataclass fields. **This task replaces Task 2's `getattr` plumbing with calls to `resolve_project_allowed_users(proj, config)`** so the bot inherits the global allow-list when the per-project list is empty AND knows which scope to persist back to via the returned `(users, source)` tuple (passed to `run_bot` as `auth_source`). Concrete edits are in Step 6.

This task adds three CLI surfaces:

1. **`plugin-call`** — standalone plugin invocation for Claude-via-Bash.
2. **`migrate-config [--dry-run] [--project NAME]`** — preview / apply the legacy → AllowedUser migration. The `start` command also calls this implicitly when `Config.migration_pending` is set.
3. **`configure --add-user / --remove-user / --reset-user-identity`** flags on the existing `configure` subcommand. Legacy `--username` / `--remove-username` aliased with deprecation warning.

- [ ] **Step 1: Append failing tests to `tests/test_cli.py`**

Add at the bottom of `tests/test_cli.py`:

```python
def test_plugin_call_unknown_plugin_exits_nonzero(tmp_path):
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"projects": {"p": {"path": "/tmp", "telegram_bot_token": "t"}}}'
    )

    result = runner.invoke(
        main,
        ["--config", str(cfg), "plugin-call", "p", "does-not-exist", "tool", "{}"],
    )
    assert result.exit_code != 0


def test_migrate_config_dry_run_does_not_write(tmp_path):
    """`migrate-config --dry-run` shows the migration without modifying the file."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    }))
    before = cfg.read_text()
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config", "--dry-run"])
    assert result.exit_code == 0
    assert "alice" in result.output
    assert "executor" in result.output
    # File is unchanged.
    assert cfg.read_text() == before


def test_migrate_config_applies_migration(tmp_path):
    """`migrate-config` (no --dry-run) writes the new shape to disk."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    }))
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config"])
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    assert "allowed_usernames" not in written["projects"]["p"]
    assert written["projects"]["p"]["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]


def test_migrate_config_nonzero_exit_on_empty_allowlist(tmp_path):
    """Migration that leaves any project with empty allowed_users exits non-zero."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {"path": "/tmp", "telegram_bot_token": "t"}
        }
    }))
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config"])
    assert result.exit_code != 0


def test_configure_add_user_persists(tmp_path):
    """`configure --add-user alice:executor` writes an AllowedUser to the global allow-list."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    result = runner.invoke(main, ["--config", str(cfg), "configure", "--add-user", "alice:executor"])
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    assert written["allowed_users"] == [
        {"username": "alice", "role": "executor"},
    ]


def test_configure_reset_user_identity_per_transport(tmp_path):
    """`configure --reset-user-identity alice:web` clears web entries only,
    leaving other transports' locks intact. Regression test for the bug
    where the whole string was normalized before the colon-split."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "allowed_users": [{
            "username": "alice",
            "role": "executor",
            "locked_identities": ["telegram:12345", "web:web-session:abc"],
        }],
        "projects": {},
    }))
    result = runner.invoke(
        main, ["--config", str(cfg), "configure", "--reset-user-identity", "alice:web"],
    )
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    alice = written["allowed_users"][0]
    assert alice["locked_identities"] == ["telegram:12345"]


def test_configure_reset_user_identity_clears_all(tmp_path):
    """`configure --reset-user-identity alice` (no :transport) clears all."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "allowed_users": [{
            "username": "alice",
            "role": "executor",
            "locked_identities": ["telegram:12345", "web:web-session:abc"],
        }],
        "projects": {},
    }))
    result = runner.invoke(
        main, ["--config", str(cfg), "configure", "--reset-user-identity", "alice"],
    )
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    alice = written["allowed_users"][0]
    # Empty list serializes as the absent key.
    assert alice.get("locked_identities", []) == []


def test_configure_legacy_username_flag_warns(tmp_path):
    """Legacy `--username` flag works but emits a deprecation warning."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    result = runner.invoke(main, ["--config", str(cfg), "configure", "--username", "bob"])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower() or "deprecated" in (result.stderr or "").lower()
    written = json.loads(cfg.read_text())
    assert any(u["username"] == "bob" for u in written.get("allowed_users", []))
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
pytest tests/test_cli.py::test_plugin_call_unknown_plugin_exits_nonzero -v
```
Expected: FAIL — `No such command 'plugin-call'`.

- [ ] **Step 3: Add the `plugin-call` subcommand**

At the bottom of `src/link_project_to_chat/cli.py`, before any `if __name__ == "__main__":` (or just at the end), add:

```python
@main.command("plugin-call")
@click.argument("project")
@click.argument("plugin_name")
@click.argument("tool_name")
@click.argument("args_json")
@click.pass_context
def plugin_call(ctx, project: str, plugin_name: str, tool_name: str, args_json: str):
    """Call a plugin tool from the command line (used by Claude via Bash)."""
    import asyncio
    import json as _json
    from pathlib import Path

    from .plugin import PluginContext, load_plugin

    try:
        args = _json.loads(args_json)
    except _json.JSONDecodeError as e:
        raise SystemExit(f"Invalid args_json: {e}")

    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if project not in config.projects:
        raise SystemExit(f"Project {project!r} not found in config.")
    proj_path = Path(config.projects[project].path)
    data_dir = Path.home() / ".link-project-to-chat" / "meta" / project

    plugin_ctx = PluginContext(
        bot_name=project,
        project_path=proj_path,
        data_dir=data_dir,
    )
    plugin = load_plugin(plugin_name, plugin_ctx, {})
    if not plugin:
        raise SystemExit(f"Plugin {plugin_name!r} not found.")

    result = asyncio.run(plugin.call_tool(tool_name, args))
    click.echo(result)
```

- [ ] **Step 4: Add the `migrate-config` subcommand**

Below `plugin-call`, add:

```python
@main.command("migrate-config")
@click.option("--dry-run", is_flag=True, help="Print the migration without modifying config.json.")
@click.option("--project", "project_filter", default=None, help="Limit project output to this name.")
@click.pass_context
def migrate_config(ctx, dry_run: bool, project_filter: str | None):
    """Apply the legacy → AllowedUser migration on config.json.

    Exit code 0 on success; non-zero when any project ends up with empty
    `allowed_users` (operators must populate them before exposing the bot).
    """
    from .config import load_config, save_config

    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)

    # Print the resulting state for the user to inspect.
    click.echo(f"Global allow-list: {len(config.allowed_users)} users")
    for u in config.allowed_users:
        locked = f" [identities: {', '.join(u.locked_identities)}]" if u.locked_identities else ""
        click.echo(f"  - {u.username} ({u.role}){locked}")

    empty_projects: list[str] = []
    for name, proj in config.projects.items():
        if project_filter and name != project_filter:
            continue
        click.echo(f"\nProject {name!r}: {len(proj.allowed_users)} users")
        for u in proj.allowed_users:
            locked = f" [identities: {', '.join(u.locked_identities)}]" if u.locked_identities else ""
            click.echo(f"  - {u.username} ({u.role}){locked}")
        if not proj.allowed_users and not config.allowed_users:
            empty_projects.append(name)

    if not config.migration_pending:
        click.echo("\nNo migration needed (config.json already in the new shape).")
        # Still exit non-zero if there are empty allowlists — operator should know.
        if empty_projects:
            click.echo(
                f"\nERROR: projects with no users authorized: {', '.join(empty_projects)}",
                err=True,
            )
            raise SystemExit(2)
        return

    if dry_run:
        click.echo("\n(dry-run) Migration NOT applied. Re-run without --dry-run to write.")
        if empty_projects:
            click.echo(
                f"\nWARNING: after migration, projects with empty allow-lists: "
                f"{', '.join(empty_projects)}", err=True,
            )
            raise SystemExit(2)
        return

    save_config(config, cfg_path)
    click.echo("\nMigration applied. Legacy keys stripped; allowed_users written.")
    if empty_projects:
        click.echo(
            f"\nERROR: projects with no users authorized: {', '.join(empty_projects)}.\n"
            "Run `configure --add-user <username>` or edit the manager bot to fix.",
            err=True,
        )
        raise SystemExit(2)
```

- [ ] **Step 5: Add `--add-user` / `--remove-user` / `--reset-user-identity` flags on `configure`**

Find the existing `@main.command("configure")` and add three new options. Keep the existing `--username` / `--remove-username` flags but emit a deprecation warning when they're used.

```python
@main.command()
@click.option("--username", default=None, help="(DEPRECATED — use --add-user) Allowed Telegram username.")
@click.option("--remove-username", default=None, help="(DEPRECATED — use --remove-user) Remove an allowed username.")
@click.option(
    "--add-user", "add_user", default=None,
    help="Add an AllowedUser. Format: 'username' or 'username:role' (role = viewer|executor; default executor).",
)
@click.option("--remove-user", "remove_user", default=None, help="Remove an AllowedUser by username.")
@click.option("--reset-user-identity", "reset_user_identity", default=None, help="Clear the locked_identities for a user (re-locks on next contact). Use 'username:transport' to clear only one transport.")
# ... other existing options ...
@click.pass_context
def configure(ctx, username, remove_username, add_user, remove_user, reset_user_identity, ...):
    from .config import AllowedUser, load_config, save_config

    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)

    if username is not None:
        click.echo("--username is deprecated; use --add-user instead.", err=True)
        add_user = username

    if remove_username is not None:
        click.echo("--remove-username is deprecated; use --remove-user instead.", err=True)
        remove_user = remove_username

    def _find(uname: str):
        norm = uname.lstrip("@").lower()
        for u in config.allowed_users:
            if u.username == norm:
                return u
        return None

    if add_user:
        if ":" in add_user:
            uname, role = add_user.split(":", 1)
        else:
            uname, role = add_user, "executor"
        uname = uname.lstrip("@").lower()
        if role not in ("viewer", "executor"):
            raise SystemExit(f"Invalid role {role!r}; must be viewer or executor.")
        existing = _find(uname)
        if existing:
            existing.role = role
        else:
            config.allowed_users.append(AllowedUser(username=uname, role=role))
        save_config(config, cfg_path)
        click.echo(f"Added {uname} ({role}).")

    if remove_user:
        norm = remove_user.lstrip("@").lower()
        config.allowed_users = [u for u in config.allowed_users if u.username != norm]
        save_config(config, cfg_path)
        click.echo(f"Removed {norm}.")

    if reset_user_identity:
        # Parse `USERNAME[:TRANSPORT]` FIRST. The previous draft normalized
        # the entire string (including `:web`) and then tried to look up
        # `alice:web` — which never matches a username. Split first, then
        # normalize each piece.
        if ":" in reset_user_identity:
            uname_part, transport = reset_user_identity.split(":", 1)
        else:
            uname_part, transport = reset_user_identity, None
        norm = uname_part.lstrip("@").lower()
        u = _find(norm)
        if not u:
            raise SystemExit(f"User {norm!r} not in allow-list.")
        if transport is None:
            u.locked_identities = []
        else:
            u.locked_identities = [
                ident for ident in u.locked_identities
                if not ident.startswith(f"{transport}:")
            ]
        save_config(config, cfg_path)
        if transport is None:
            click.echo(f"Cleared all locked identities for {norm}.")
        else:
            click.echo(f"Cleared {transport!r} identities for {norm}.")
```

(Read the existing `configure` body and merge the new option handling in. The rest of `configure`'s body — `--manager-token` etc. — is unchanged.)

- [ ] **Step 6: Edit `start` to honor `migration_pending` and aggregate empty allow-lists**

In the `start` command (find `@main.command()` with `def start(...)`), right after `config = load_config(cfg_path)`, add:

```python
    from .config import resolve_project_allowed_users, save_config

    if config.migration_pending:
        click.echo("Migrating config.json from legacy auth fields to allowed_users...", err=True)
        save_config(config, cfg_path)
        click.echo("Migration complete.", err=True)

    # Aggregate projects where BOTH project AND global allow-lists are empty.
    # resolve_project_allowed_users returns (users, source); when source is
    # "global" and the global list is also empty, the project will fail-closed.
    empty: list[str] = []
    for name, proj in config.projects.items():
        users, _source = resolve_project_allowed_users(proj, config)
        if not users:
            empty.append(name)
    if empty:
        import logging as _logging
        _logging.getLogger(__name__).critical(
            "Projects with no users authorized at either project or global scope "
            "(will reject all messages): %s. "
            "Add users via `configure --add-user` or the manager bot.",
            ", ".join(empty),
        )
```

The `run_bot` / `run_bots` plumbing (added in Task 2) currently passes `proj.plugins or None` and `proj.allowed_users or None` to `run_bot`. **Update both call sites to use the fallback helper** so the project picks up the global allow-list when its own is empty AND so the bot knows which scope to persist back to:

```python
# In run_bots, before the existing run_bot call:
        effective_allowed, auth_source = resolve_project_allowed_users(proj, config)
        run_bot(
            ...,
            allowed_users=effective_allowed or None,
            auth_source=auth_source,           # "project" or "global"
            plugins=proj.plugins or None,
        )
```

For ad-hoc `--path`/`--token` runs (no config loaded), pass `auth_source="project"` — the bot has no shared global allow-list to fall back on. `run_bot` accepts a new `auth_source: str = "project"` kwarg.

**Update ALL `run_bot` call sites in `cli.py`** (the single-project and team-bot branches each have their own direct call — not just `run_bots`):

1. **Ad-hoc `--path`/`--token` branch** at [cli.py:359](src/link_project_to_chat/cli.py:359) — pass `auth_source="project"`. No global fallback applies because no config was loaded. Also accept the implicit one-user allow-list as `allowed_users=[AllowedUser(username=username, role="executor")]` synthesized from `--username` (the existing `allowed_usernames=` plumbing in this branch becomes the legacy-only path; the new code should build an `AllowedUser` list directly so the bot has a non-empty `_allowed_users` and fail-closed semantics work).

2. **Team-bot branch** at [cli.py:427](src/link_project_to_chat/cli.py:427) — team bots inherit auth from `Config.allowed_users` (the spec says "no per-team-bot allow-list in this revision"). Pass `allowed_users=config.allowed_users, auth_source="global"`. The existing `effective_usernames` / `effective_trusted_users` plumbing becomes the legacy-only path and the new code goes through the global allow-list.

3. **Single-project branch** at [cli.py:478](src/link_project_to_chat/cli.py:478) — use `resolve_project_allowed_users(proj, config)` and pass `(users, source)` as `allowed_users=` and `auth_source=`. This is the equivalent of the `run_bots` fix above but in the explicit `--project NAME` path.

After this edit, **no `run_bot` call site in `cli.py` should be missing `auth_source=`**. Verify:

```bash
grep -n "run_bot(" src/link_project_to_chat/cli.py
grep -n "auth_source=" src/link_project_to_chat/cli.py
```

Both greps should return the same number of `run_bot(...)` invocations, and every one should have a matching `auth_source=` line in its kwargs.

- [ ] **Step 6b: Migrate `start-manager` + update `ManagerBot.__init__` for `allowed_users`**

The spec's eager-migration decision (Resolved question #3) promises BOTH `start` and `start-manager` honor `migration_pending` and write the new shape before serving traffic. Step 6 covers `start`; this step does the parallel work for `start-manager` plus the prerequisite constructor change.

After Task 5 deletes `Config.allowed_usernames` / `Config.trusted_users`, today's `start-manager` would `AttributeError` on `main_config.allowed_usernames` at [cli.py:753](src/link_project_to_chat/cli.py:753) and would pass nonexistent fields into `ManagerBot` at [cli.py:761](src/link_project_to_chat/cli.py:761). And `ManagerBot.__init__` at [manager/bot.py:248](src/link_project_to_chat/manager/bot.py:248) doesn't yet accept `allowed_users=`, so even an updated CLI would TypeError. Both surfaces need to land in the same commit so the suite stays green.

**1. Update `ManagerBot.__init__` (additive — legacy kwargs stay until Task 5 Step 11 strips them).** In [manager/bot.py](src/link_project_to_chat/manager/bot.py):

```python
    def __init__(
        self,
        token: str,
        process_manager: ProcessManager,
        allowed_username: str = "",
        allowed_usernames: list[str] | None = None,
        trusted_users: dict[str, int] | None = None,
        trusted_user_id: int | None = None,
        trusted_user_ids: list[int] | None = None,
        allowed_users: list["AllowedUser"] | None = None,  # NEW
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        # ORDER MATTERS: all _allowed_* / _trusted_* / _allowed_users
        # assignments below MUST happen before the final _init_auth() call
        # at the end of __init__. _init_auth doesn't read instance auth
        # state today, but keeping the order explicit anchors the contract
        # in case _init_auth ever grows per-user setup (rate-limit
        # prepopulation, brute-force lockout warmup, etc.).
        #
        # Legacy kwargs preserved through Task 5; stripped in Task 5 Step 11
        # once every caller passes allowed_users= instead.
        #
        # TRANSITION SHIM (Tasks 4–5 window): synthesize the legacy
        # _allowed_usernames from the AllowedUser entries. AuthMixin is
        # still legacy at the end of Task 4 — _auth(user) reads via
        # _get_allowed_usernames → self._allowed_usernames at _auth.py:34.
        # Without this synthesis, the new start-manager (which passes only
        # allowed_users=) would leave _allowed_usernames at the class-level
        # default [] and the manager would deny everyone until Task 5 Step 3
        # rewrites AuthMixin. The whole if/elif/else block (including this
        # synthesis branch) is removed in Task 5 Step 11 once the rewrite
        # makes _allowed_users the sole source of truth.
        if allowed_usernames is not None:
            self._allowed_usernames = allowed_usernames
        elif allowed_users is not None:
            self._allowed_usernames = [u.username for u in allowed_users]
        else:
            self._allowed_username = allowed_username
        if trusted_users is not None:
            self._trusted_users = dict(trusted_users)
        if trusted_user_ids is not None:
            self._trusted_user_ids = trusted_user_ids
        else:
            self._trusted_user_id = trusted_user_id
        # NEW: AllowedUser allow-list. _init_auth + AuthMixin._allowed_users
        # is the post-Task-5 source of truth; setting it here makes the
        # transition transparent to manager auth code that lands in Task 5.
        self._allowed_users = list(allowed_users or [])
        self._started_at = time.monotonic()
        self._app = None
        self._project_config_path = project_config_path
        self._telethon_client = None
        self._init_auth()
```

The synthesis shim deliberately covers ONLY `_allowed_usernames`, not `_trusted_users` / `_trusted_user_ids`. Reason: the legacy `_auth(user)` already falls through to the username-match branch and calls `_trust_user(user.id, username)` to populate `_trusted_users` in memory on first contact, so the trusted-id fast path becomes correct from message 2 onwards without any precomputation. The first-message overhead (one extra dict-lookup-miss) is acceptable for a transient one-task window.

A small caveat during the window: `_trust_user` → `_on_trust` → `bind_trusted_user` will re-introduce a `trusted_users` dict into config.json. That's ugly but harmless — the post-Task-3 loader tolerates legacy keys (that's the whole point of `_migrate_legacy_auth`), and Task 5 Step 3 rewrites `_auth` so `_trust_user` is never reached anymore.

Add the import at the top of [manager/bot.py](src/link_project_to_chat/manager/bot.py) (in the existing `from ..config import ...` block):

```python
from ..config import AllowedUser
```

(or inline-import inside `__init__` if circular-import noise — the spec's preferred shape is module-level so type hints work without `from __future__`).

**2. Update `start_manager` in [cli.py](src/link_project_to_chat/cli.py).** Replace the body of the `start_manager` command:

```python
@main.command("start-manager")
@click.pass_context
def start_manager(ctx):
    """Start the manager bot."""
    from .manager.bot import ManagerBot
    from .manager.process import ProcessManager
    from .config import save_config

    cfg_path = ctx.obj["config_path"]
    main_config = load_config(cfg_path)

    if main_config.migration_pending:
        click.echo("Migrating config.json from legacy auth fields to allowed_users...", err=True)
        save_config(main_config, cfg_path)
        click.echo("Migration complete.", err=True)

    token = main_config.manager_telegram_bot_token
    if not token:
        raise SystemExit("No manager token configured. Run 'configure --manager-token TOKEN' first.")
    # Post-migration the only auth source is allowed_users (global allow-list).
    # Empty → fail-closed (every message rejected). The manager bot has no
    # project-scoped fallback (unlike project bots via resolve_project_allowed_users).
    if not main_config.allowed_users:
        raise SystemExit(
            "No users authorized for the manager bot. "
            "Run `configure --add-user USER[:ROLE]` or edit `allowed_users` in config.json."
        )

    pm = ProcessManager(project_config_path=cfg_path)
    restored = pm.start_autostart()
    if restored:
        click.echo(f"Autostarted {restored} project(s).")

    bot = ManagerBot(
        token, pm,
        allowed_users=main_config.allowed_users,
        project_config_path=cfg_path,
    )
    click.echo("Manager bot started.")
    bot.build().run_polling()
```

Note: legacy `allowed_usernames=` / `trusted_users=` kwargs are NOT passed anymore. The constructor's legacy kwargs default to `None`, so any in-process state they used to populate is empty — but AuthMixin now reads exclusively from `_allowed_users` (after Task 5 Step 3), and `_allowed_users` IS populated. Old kwargs become dead defaults through Task 4 and Task 5 Step 1–10, then get stripped in Task 5 Step 11.

**3. TDD verification.** Add to [tests/test_cli.py](tests/test_cli.py) (or a new `tests/test_start_manager.py` if the file is already large):

```python
def test_start_manager_requires_allowed_users(tmp_path, monkeypatch):
    """start-manager must hard-fail when allowed_users is empty (fail-closed)."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    # Manager token set, but NO allowed_users. (Config has no top-level
    # telegram_bot_token field — that's per-ProjectConfig. start-manager
    # only needs manager_telegram_bot_token.)
    save_config(
        Config(manager_telegram_bot_token="m", allowed_users=[]),
        cfg_path,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code != 0
    assert "No users authorized" in (result.output + str(result.exception))


def test_start_manager_passes_allowed_users_into_manager_bot(tmp_path, monkeypatch):
    """start-manager must construct ManagerBot with allowed_users=, not the
    legacy allowed_usernames=. Regression: pre-Task-4 start_manager passed
    allowed_usernames= and trusted_users=, both of which Task 5 deletes."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            manager_telegram_bot_token="m",
            allowed_users=[AllowedUser(username="alice", role="executor")],
        ),
        cfg_path,
    )

    captured_kwargs: dict = {}

    class _FakeBot:
        def __init__(self, *args, **kwargs):
            captured_kwargs["args"] = args
            captured_kwargs["kwargs"] = kwargs

        def build(self):
            class _App:
                def run_polling(self_inner): return None
            return _App()

    monkeypatch.setattr("link_project_to_chat.manager.bot.ManagerBot", _FakeBot)

    class _FakePM:
        def __init__(self, **kwargs): pass
        def start_autostart(self): return 0

    monkeypatch.setattr("link_project_to_chat.manager.process.ProcessManager", _FakePM)

    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code == 0, result.output
    # allowed_users= must be present; legacy kwargs must NOT be passed.
    assert "allowed_users" in captured_kwargs["kwargs"]
    assert captured_kwargs["kwargs"]["allowed_users"][0].username == "alice"
    assert "allowed_usernames" not in captured_kwargs["kwargs"]
    assert "trusted_users" not in captured_kwargs["kwargs"]


def test_start_manager_runs_migration_on_pending(tmp_path, monkeypatch):
    """start-manager must call save_config when load_config sets migration_pending,
    mirroring start's behavior."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    # Write a legacy-shaped config that load_config will migrate.
    import json
    cfg_path.write_text(json.dumps({
        "telegram_bot_token": "x",
        "manager_telegram_bot_token": "m",
        "allowed_usernames": ["alice"],  # legacy → will trigger migration
    }))

    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.ManagerBot",
        type("_FB", (), {"__init__": lambda *a, **k: None,
                        "build": lambda self: type("_A", (), {"run_polling": lambda s: None})()}),
    )
    monkeypatch.setattr(
        "link_project_to_chat.manager.process.ProcessManager",
        type("_FPM", (), {"__init__": lambda *a, **k: None,
                          "start_autostart": lambda self: 0}),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code == 0, result.output

    # After migration, the on-disk file no longer has the legacy key and DOES
    # have allowed_users with role=executor for alice.
    reloaded = json.loads(cfg_path.read_text())
    assert "allowed_usernames" not in reloaded
    assert any(u["username"] == "alice" and u["role"] == "executor"
               for u in reloaded.get("allowed_users", []))


def test_manager_bot_accepts_allowed_users_kwarg():
    """Constructor regression: ManagerBot must accept allowed_users= and set
    self._allowed_users. Catches a future caller that drops this kwarg."""
    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager

    pm = ProcessManager.__new__(ProcessManager)
    bot = ManagerBot(
        token="t",
        process_manager=pm,
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._allowed_users == [AllowedUser(username="alice", role="executor")]


def test_manager_bot_legacy_auth_works_with_allowed_users_only(tmp_path):
    """TRANSITION SHIM regression — between Task 4 and Task 5, AuthMixin is
    still legacy: _auth(user) reads _allowed_usernames via
    _get_allowed_usernames. If start-manager passes only allowed_users= and
    the constructor leaves _allowed_usernames at its class-level [] default,
    every message gets denied until Task 5 Step 3 rewrites AuthMixin.

    This test is intentionally transitional. Task 5 Step 3 deletes the
    legacy _auth(user) method, after which calling bot._auth(...) here would
    AttributeError — so Task 5 Step 11 strips both the shim and this test.
    The post-rewrite equivalent (an authorized user authenticates via
    _auth_identity) is already covered by tests/test_auth_roles.py.

    Persistence side-effect note: legacy _auth on the allow path calls
    _trust_user → _on_trust → bind_trusted_user, which writes to
    self._project_config_path or DEFAULT_CONFIG. Without an explicit
    project_config_path, that would mutate the real ~/.link-project-to-chat/
    config.json on the test runner. The tmp_path fixture sandboxes it.
    """
    from types import SimpleNamespace

    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager

    pm = ProcessManager.__new__(ProcessManager)
    bot = ManagerBot(
        token="t",
        process_manager=pm,
        allowed_users=[AllowedUser(username="alice", role="executor")],
        project_config_path=tmp_path / "config.json",
    )
    # Legacy _auth(user) takes a duck-typed user with .id / .username.
    user = SimpleNamespace(id=98765, username="alice")
    assert bot._auth(user) is True
    # Sanity: an unknown username still denies.
    intruder = SimpleNamespace(id=11111, username="mallory")
    assert bot._auth(intruder) is False
```

Run:

```bash
pytest tests/test_cli.py::test_start_manager_requires_allowed_users \
       tests/test_cli.py::test_start_manager_passes_allowed_users_into_manager_bot \
       tests/test_cli.py::test_start_manager_runs_migration_on_pending \
       tests/test_cli.py::test_manager_bot_accepts_allowed_users_kwarg \
       tests/test_cli.py::test_manager_bot_legacy_auth_works_with_allowed_users_only -v
```

Expected (BEFORE this step's edits): FAIL — `start_manager` references `allowed_usernames` (raises `SystemExit("No username configured.")` rather than the new message) and passes `allowed_usernames=` to `ManagerBot`. The constructor regression fails because `ManagerBot.__init__` doesn't accept `allowed_users=`. The legacy-auth test fails because the un-shimmed constructor leaves `_allowed_usernames` at the class-level `[]`, so `_auth` fail-closes.

Expected (AFTER): all PASS.

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/test_cli.py -v
```
Expected: All tests PASS.

- [ ] **Step 8: Run the full suite**

```bash
pytest -q
```

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/cli.py \
        src/link_project_to_chat/manager/bot.py \
        tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): plugin-call, migrate-config, AllowedUser configure flags

Adds three CLI surfaces:
- `plugin-call <project> <plugin_name> <tool_name> <args_json>` invokes
  a plugin standalone (no bot); used by Claude via Bash.
- `migrate-config [--dry-run] [--project NAME]` previews / applies the
  legacy auth → AllowedUser migration. Non-zero exit when any project
  ends up with empty allowed_users so operators see the issue.
- `configure --add-user USER[:ROLE] / --remove-user / --reset-user-identity`
  edits the global Config.allowed_users. Legacy `--username` /
  `--remove-username` aliased with deprecation warning for this release.

Both `start` and `start-manager` honor Config.migration_pending: force
a save before serving traffic so the on-disk migration is deterministic.
`start-manager` additionally hard-fails on empty Config.allowed_users
(fail-closed; manager has no per-project fallback). Empty-allow-list
projects are aggregated into a single CRITICAL log line at startup
(replaces per-load log spam).

ManagerBot.__init__ gains an `allowed_users: list[AllowedUser] | None`
kwarg additively. When only `allowed_users=` is passed (the new path),
a transition shim synthesizes the legacy `_allowed_usernames` from the
AllowedUser entries so the still-legacy `AuthMixin._auth(user)` keeps
working through the Task 4 → Task 5 window. Without this shim the
manager would start but fail-closed on every message. Legacy kwargs
plus the shim are stripped in Task 5 Step 11 once `_auth.py` rewrites
`AuthMixin` to read `_allowed_users` exclusively.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Role enforcement on `AuthMixin` — **rewrite, not addition**

**Files:**
- Modify: `src/link_project_to_chat/_auth.py`
- Modify: `src/link_project_to_chat/bot.py` (gates on state-changing handlers; pass `allowed_users` through)
- Create: `tests/test_auth_roles.py`
- Modify: `tests/test_auth.py` (and any other auth tests that reference legacy fields)

`AuthMixin` is rewritten around `_allowed_users` as the sole source of truth. The legacy `_allowed_usernames` / `_trusted_user_ids` instance state is deleted. ID-locking moves from a separate `trusted_user_ids` list to `AllowedUser.locked_identities` (a list of `"transport_id:native_id"` strings), appended on first contact via `_identity_key(identity)`. The list shape supports the same username across multiple transports.

- [ ] **Step 1: Write the failing tests — `tests/test_auth_roles.py`**

```python
from __future__ import annotations

import pytest

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


class _BotWithRoles(AuthMixin):
    """Minimal AuthMixin host for tests — only the new auth surface."""

    def __init__(self, allowed_users=None):
        self._allowed_users: list[AllowedUser] = list(allowed_users or [])
        self._init_auth()


def _identity(username: str, native_id: str = "1") -> Identity:
    return Identity(
        transport_id="telegram",
        native_id=native_id,
        display_name=username,
        handle=username,
        is_bot=False,
    )


def test_get_user_role_returns_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(_identity("alice")) == "executor"


def test_get_user_role_returns_viewer():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="bob", role="viewer")])
    assert bot._get_user_role(_identity("bob")) == "viewer"


def test_get_user_role_none_when_not_listed():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(_identity("bob")) is None


def test_empty_allowed_users_fails_closed():
    """Empty allowed_users denies everyone — no legacy 'allow-all' path."""
    bot = _BotWithRoles(allowed_users=[])
    assert bot._auth_identity(_identity("alice")) is False
    assert bot._require_executor(_identity("alice")) is False


def test_require_executor_blocks_viewer():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="bob", role="viewer")])
    assert bot._require_executor(_identity("bob")) is False


def test_require_executor_allows_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(_identity("alice")) is True


def test_require_executor_blocks_unknown_user():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(_identity("charlie")) is False


def test_require_executor_case_and_at_insensitive():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="alice", role="executor")])
    assert bot._require_executor(_identity("@ALICE")) is True


def test_first_contact_locks_identity():
    """First contact by username appends an identity to locked_identities."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    ident = _identity("alice", native_id="98765")
    bot._auth_identity(ident)  # First contact
    assert au.locked_identities == ["telegram:98765"]


def test_same_transport_spoof_blocked():
    """Same-transport spoof guard (security-critical).

    If a user already has any locked identity from a transport, an attacker
    who happens to know the username and lands on the same transport with
    a different native_id must NOT be able to bind their own identity via
    the username fallback. The fallback only runs when the user has zero
    locked identities from the incoming transport — once an identity is
    locked on telegram, every subsequent telegram contact has to match by
    identity_key (transport_id:native_id), never by handle.

    Without this guard, an attacker who renames themselves to "alice" on
    Telegram could authenticate themselves and overwrite the real alice's
    locked_identities. The earlier draft of _get_user_role had this hole;
    this test pins the fix.
    """
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Spoof attempt: same transport, different native_id, same username.
    attacker = _identity("alice", native_id="11111")  # transport_id="telegram"
    assert bot._auth_identity(attacker) is False
    # Alice's identity list was NOT mutated.
    assert au.locked_identities == ["telegram:12345"]
    assert bot._auth_dirty is False


def test_username_fallback_succeeds_for_genuinely_new_transport():
    """When the user has NO identity from the incoming transport, fallback
    succeeds and appends. That's the multi-transport onboarding case."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Different transport: web. transport_prefix "web:" not present in
    # locked_identities, so the fallback applies and appends.
    from link_project_to_chat.transport.base import Identity
    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    assert "telegram:12345" in au.locked_identities
    assert "web:web-session:abc-def" in au.locked_identities


def test_locked_identity_takes_precedence_over_username():
    """After identity is locked, validation goes through identity, not username."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:98765"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Attacker renames themselves to "alice" but their native_id is different
    # — same transport (telegram), different id, identity_key is "telegram:11111".
    attacker = _identity("alice", native_id="11111")
    assert bot._auth_identity(attacker) is False
    # The real alice still works — her identity matches even with a renamed
    # handle. (Her identity_key is "telegram:98765" regardless of handle.)
    ident_real = _identity("anything-else", native_id="98765")
    assert bot._auth_identity(ident_real) is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: FAIL — `_get_user_role` / `_require_executor` / new `_auth_identity` semantics not defined.

- [ ] **Step 2b: Rewrite manager PTB-native guards + add manager-side persist helper**

This step **must land before Step 3** (the `AuthMixin` rewrite). Step 3 deletes the legacy `_auth(user)` method; two PTB-native call sites in the manager would `AttributeError` the moment Step 3 lands. Rewrite them now, using the `_auth_identity` + `_identity_key` shape that already exists on `AuthMixin` today (already used by [`_guard_invocation` at manager/bot.py:369](src/link_project_to_chat/manager/bot.py:369)).

Affected call sites (verified by `grep -n "self\._auth(" src/link_project_to_chat/manager/bot.py`):
- `_guard(update)` at [manager/bot.py:343](src/link_project_to_chat/manager/bot.py:343) — wizard ConversationHandler shim. Calls `self._auth(user)` and `self._rate_limited(user.id)` (raw int — does NOT match the string-keyed bucket `_guard_invocation` populates).
- `_edit_field_save(update, ctx)` at [manager/bot.py:836](src/link_project_to_chat/manager/bot.py:836) — handles `pending_edit` and `setup_awaiting` text input from PTB's `MessageHandler`. Calls `self._auth(update.effective_user)` directly with no rate-limit and no persist tail.

Both paths are still reachable through `ConversationHandler` entry points and the setup-text `MessageHandler` (which never went through the transport-port).

**Why introduce `_persist_auth_if_dirty` on `ManagerBot` here, before Step 3 lands the append behavior?** Step 3 makes `_auth_identity` append first-contact identities to `locked_identities` and set `self._auth_dirty=True`. Without a persist tail on the manager's PTB shims, those first-contact locks would be lost across restarts the moment Step 3 lands. Bundling the swap with the persist helper closes the gap atomically — no commit in the Task 5 sequence leaves the manager in a half-rewritten state.

**1. Add helpers on `ManagerBot`** in [manager/bot.py](src/link_project_to_chat/manager/bot.py) (e.g., near the existing `_guard_invocation`):

```python
    def _users_config_path(self):
        """Resolve the config path for manager auth ops.

        `self._project_config_path` may be None (manager bot constructed
        without an explicit path → use the default). Passing None to
        load_config / save_config would TypeError.
        """
        from ..config import DEFAULT_CONFIG
        return self._project_config_path or DEFAULT_CONFIG

    async def _persist_auth_if_dirty(self) -> None:
        """Persist _allowed_users to disk if a first-contact lock was added.

        Manager bot's equivalent of ProjectBot._persist_auth_if_dirty (added
        in Step 4 below). Always writes to the GLOBAL Config.allowed_users
        (the manager bot has no project-scoped state). Uses the atomic
        locked_config_rmw context manager from config.py (Task 3) so
        concurrent first-contacts converge.

        Pre-Step-3 (legacy AuthMixin), `_auth_dirty` may not be set by any
        code path — the helper is a no-op and safe to call. Post-Step-3,
        it persists the append performed by `_get_user_role`.
        """
        if not getattr(self, "_auth_dirty", False):
            return
        from ..config import locked_config_rmw, save_config_within_lock
        cfg_path = self._users_config_path()
        try:
            with locked_config_rmw(cfg_path) as disk:
                in_memory_by_user = {u.username: u for u in self._allowed_users}
                for au in disk.allowed_users:
                    mem = in_memory_by_user.get(au.username)
                    if mem is None:
                        continue
                    merged = list(au.locked_identities)
                    for ident in mem.locked_identities:
                        if ident not in merged:
                            merged.append(ident)
                    au.locked_identities = merged
                save_config_within_lock(disk, cfg_path)
            self._auth_dirty = False
        except Exception:
            logger.exception("Failed to persist manager auth state; will retry on next message")
```

**2. Rewrite `_guard(update)`.** Replace the existing body:

```python
    async def _guard(self, update: Update) -> bool:
        """Returns True if the user is authorized and not rate-limited.

        ... (existing docstring preserved) ...
        """
        from ..transport.telegram import (
            chat_ref_from_telegram,
            identity_from_telegram_user,
        )
        user = update.effective_user
        chat = chat_ref_from_telegram(update.effective_chat) if update.effective_chat else None
        try:
            if not user:
                if chat is not None:
                    await self._transport.send_text(chat, "Unauthorized.")
                return False
            identity = identity_from_telegram_user(user)
            if not self._auth_identity(identity):
                if chat is not None:
                    await self._transport.send_text(chat, "Unauthorized.")
                return False
            if self._rate_limited(self._identity_key(identity)):
                if chat is not None:
                    await self._transport.send_text(chat, "Rate limited. Try again shortly.")
                return False
            return True
        finally:
            # Step 3 makes _auth_identity → _get_user_role append a first-contact
            # identity; persist before returning regardless of allow/deny branch.
            # Pre-Step-3 this is a no-op (see _persist_auth_if_dirty docstring).
            await self._persist_auth_if_dirty()
```

**3. Rewrite `_edit_field_save(update, ctx)`.** Replace the existing body:

```python
    async def _edit_field_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from ..transport.telegram import identity_from_telegram_user
        try:
            # Handle setup text input first (intentionally NOT rate-limited —
            # users paste long tokens during onboarding and shouldn't get
            # throttled mid-wizard). If the manager later wants to throttle
            # setup text, gate it inside _handle_setup_input.
            setup_awaiting = ctx.user_data.get("setup_awaiting")
            if setup_awaiting:
                await self._handle_setup_input(update, ctx, setup_awaiting)
                return
            # Existing edit logic
            pending = ctx.user_data.get("pending_edit")
            if not pending:
                return
            user = update.effective_user
            if not user:
                return
            identity = identity_from_telegram_user(user)
            if not self._auth_identity(identity):
                return
            if self._rate_limited(self._identity_key(identity)):
                return
            ctx.user_data.pop("pending_edit")
            incoming = self._incoming_from_update(update)
            await self._apply_edit(incoming.chat, pending["name"], pending["field"], incoming.text.strip())
        finally:
            # First-contact identity locks from _auth_identity must survive the
            # wizard path the same way transport-native commands persist them.
            await self._persist_auth_if_dirty()
```

**4. TDD verification — write the failing test first.** Add `tests/manager/test_guard_persistence.py`:

```python
"""Manager PTB-shim auth alignment (Task 5 Step 2b).

Regression covers two pre-rewrite bugs:
  1. _guard called self._auth(user); after Task 5 Step 3 deletes _auth, this
     path would AttributeError.
  2. _guard didn't fire a persist tail; once Step 3 makes _auth_identity
     append to locked_identities on first contact, those appends would be
     lost on restart.

Test mocks _auth_identity / _persist_auth_if_dirty to verify wiring, so it
passes immediately after Step 2b lands (does not need to wait for Step 3).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from link_project_to_chat.config import AllowedUser
from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot() -> ManagerBot:
    bot = ManagerBot.__new__(ManagerBot)
    bot._transport = FakeTransport()
    bot._project_config_path = None
    bot._allowed_users = [AllowedUser(username="alice", role="executor")]
    bot._init_auth()
    bot._auth_dirty = False
    return bot


def _make_update(username: str = "alice", user_id: int = 98765, chat_id: int = 98765):
    user = SimpleNamespace(
        id=user_id, username=username, full_name=username.title(), is_bot=False,
    )
    chat = SimpleNamespace(id=chat_id, type="private")
    return SimpleNamespace(effective_user=user, effective_chat=chat)


async def test_guard_persists_on_allow(monkeypatch):
    """Allowed path fires _persist_auth_if_dirty after returning True."""
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)
    monkeypatch.setattr(bot, "_rate_limited", lambda key: False)

    assert await bot._guard(_make_update()) is True
    assert persisted == [True]


async def test_guard_persists_on_deny(monkeypatch):
    """Denied path STILL fires _persist_auth_if_dirty (covers first-contact
    append-then-deny rate-limit edge case)."""
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: False)

    assert await bot._guard(_make_update()) is False
    assert persisted == [True]


async def test_guard_uses_identity_key_for_rate_limit(monkeypatch):
    """Rate-limit bucket is keyed on _identity_key(identity), not raw user.id.
    Regression: legacy _guard passed raw int user.id; new manager _rate_limits
    is string-keyed on 'transport_id:native_id'."""
    bot = _make_bot()

    async def _noop_persist() -> None:
        return None

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _noop_persist)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)

    seen_keys: list = []

    def _capture(key):
        seen_keys.append(key)
        return False

    monkeypatch.setattr(bot, "_rate_limited", _capture)

    await bot._guard(_make_update(user_id=98765))
    assert seen_keys == ["telegram:98765"]


async def test_edit_field_save_persists_and_uses_identity_key(monkeypatch):
    """The other moved PTB-native call site. Same wiring as _guard, but on
    PTB's MessageHandler path (pending-edit branch).

    Rate-limit short-circuits before _apply_edit runs, so _persist_auth_if_dirty
    must still fire via try/finally — and the rate-limit bucket must be
    string-keyed on _identity_key(identity), matching _guard.
    """
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    seen_keys: list = []

    def _capture(key):
        seen_keys.append(key)
        return True  # rate-limited → handler returns before pop / _apply_edit

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)
    monkeypatch.setattr(bot, "_rate_limited", _capture)

    update = _make_update(user_id=98765)
    ctx = SimpleNamespace(user_data={"pending_edit": {"name": "x", "field": "path"}})

    await bot._edit_field_save(update, ctx)
    assert persisted == [True]
    assert seen_keys == ["telegram:98765"]
    # Rate-limited path exits before pop — pending_edit stays.
    assert "pending_edit" in ctx.user_data


async def test_edit_field_save_persists_on_auth_deny(monkeypatch):
    """Auth-denied pending-edit path also fires _persist_auth_if_dirty. The
    first-contact append happens INSIDE _auth_identity in Step 3 (lands later
    in Task 5), so missing the persist tail here would lose the lock the
    moment Step 3 starts appending.
    """
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: False)

    update = _make_update()
    ctx = SimpleNamespace(user_data={"pending_edit": {"name": "x", "field": "path"}})

    await bot._edit_field_save(update, ctx)
    assert persisted == [True]
    assert "pending_edit" in ctx.user_data
```

Run: `pytest tests/manager/test_guard_persistence.py -v`
Expected (BEFORE this step's edits): FAIL — the unrewritten `_guard` / `_edit_field_save` call `self._auth(user)` and pass raw `user.id` to `_rate_limited`, so neither `_persist_auth_if_dirty` nor the identity-key path get exercised on either call site.
Expected (AFTER this step's edits): PASS — all five tests verify the rewrite (3 for `_guard`, 2 for `_edit_field_save`).

**5. Verify no legacy `_auth(user)` call sites remain:**

```bash
grep -n "self\._auth(" src/link_project_to_chat/manager/bot.py
```

Expected after Step 2b: zero matches. If anything else turns up, apply the same `identity_from_telegram_user` → `_auth_identity` → `_identity_key` → `_persist_auth_if_dirty` swap before continuing to Step 3.

**6. Verify rate-limit key consistency:**

```bash
grep -n "_rate_limited(" src/link_project_to_chat/manager/bot.py
```

Expected: every call passes `self._identity_key(identity)` (a string), never a raw int from `user.id`. Step 3 re-keys `_rate_limits` on string keys; a raw int would silently never match (throttle bypassed).

- [ ] **Step 3: Edit `_auth.py` — rewrite `AuthMixin`**

In `src/link_project_to_chat/_auth.py`, **delete** every reference to `self._allowed_usernames`, `self._trusted_user_ids`, `self._trusted_users` (dict), `_get_trusted_user_bindings`, `_get_trusted_user_ids`, `_coerce_trust_value`, `_trust_user`, `_revoke_user`, and the legacy `_auth(user)` method. The new `AuthMixin` is structured around `_allowed_users` as the sole auth source.

Preserve: `_normalize_username`, the brute-force lockout machinery (`_failed_auth_counts`), the rate-limit machinery (`_rate_limits`, `_rate_limited`), `_init_auth`, and `_identity_key`. Re-key both `_failed_auth_counts` and `_rate_limits` on `_identity_key(identity)` for uniformity.

```python
from __future__ import annotations

import collections
import logging
import time

logger = logging.getLogger(__name__)


class AuthMixin:
    """Identity-based auth backed by AllowedUser (sole source of truth).

    Set `self._allowed_users: list[AllowedUser]` in __init__. Empty list →
    fail-closed (every request denied).
    """

    _allowed_users: list = []  # list[AllowedUser]; set by ProjectBot.__init__
    _auth_source: str = "project"  # "project" | "global"; set by ProjectBot.__init__
    _MAX_MESSAGES_PER_MINUTE: int = 30
    _MAX_FAILED_AUTH: int = 5

    def _init_auth(self) -> None:
        # Both dicts keyed on `_identity_key(identity)` = "transport_id:native_id"
        # so Discord/Slack/Telegram identities never collide.
        self._rate_limits: dict[str, collections.deque] = {}
        self._failed_auth_counts: dict[str, int] = {}
        # First-contact lock APPENDS to locked_identities in-memory; this flag
        # tells the bot's message-handling tail to call save_config once.
        self._auth_dirty: bool = False

    @staticmethod
    def _normalize_username(handle) -> str:
        if not handle:
            return ""
        return str(handle).strip().lstrip("@").lower()

    @staticmethod
    def _identity_key(identity) -> str:
        """Stable string key for rate-limit / failed-auth bookkeeping."""
        return f"{identity.transport_id}:{identity.native_id}"

    def _get_user_role(self, identity) -> str | None:
        """Return 'executor', 'viewer', or None.

        Order of checks:
          1. Identity-lock fast path: `_identity_key(identity)` is in any
             AllowedUser.locked_identities list. Security-critical — prevents
             username-spoof attacks. Works for every transport since the
             keys are `transport_id:native_id` strings.
          2. Username fallback: case- and @-insensitive match against an
             entry. Appends `_identity_key(identity)` to that AllowedUser's
             locked_identities (no replacement, just append if absent) and
             sets `self._auth_dirty = True` so the message-handling tail
             persists via save_config. Multi-transport users naturally
             accumulate identities here: a user authed first on Telegram
             gets `["telegram:12345"]`, then later on Web appends
             `"web:web-session:abc"` so the list becomes
             `["telegram:12345", "web:web-session:abc"]` and both transports
             authenticate them.
        """
        if not self._allowed_users:
            return None
        ident_key = self._identity_key(identity)
        transport_prefix = f"{identity.transport_id}:"
        # 1. Identity-lock fast path.
        for au in self._allowed_users:
            if ident_key in au.locked_identities:
                return au.role
        # 2. Username fallback — ONLY when no identity from THIS transport is
        # already locked for that user. If the user has a different identity
        # from the same transport locked (e.g., locked_identities=["fake:12345"]
        # and the incoming is "fake:11111"), the fast path missed AND there's
        # already a transport lock — this is a same-transport spoof attempt.
        # Deny without appending.
        uname = self._normalize_username(getattr(identity, "handle", ""))
        if not uname:
            return None
        for au in self._allowed_users:
            if self._normalize_username(au.username) != uname:
                continue
            # Same-transport spoof guard. We only username-fallback when the
            # user has NO identity from this transport yet.
            if any(x.startswith(transport_prefix) for x in au.locked_identities):
                logger.warning(
                    "Same-transport spoof rejected: %s already has a %s lock; "
                    "ignoring incoming %s",
                    au.username, identity.transport_id, ident_key,
                )
                return None
            # First contact from this transport — append.
            au.locked_identities.append(ident_key)
            self._auth_dirty = True
            logger.info(
                "Locked identity %s for %s on first contact",
                ident_key, au.username,
            )
            return au.role
        return None

    def _auth_identity(self, identity) -> bool:
        """True iff identity resolves to any role. Fail-closed on empty."""
        if not self._allowed_users:
            return False
        key = self._identity_key(identity)
        if self._failed_auth_counts.get(key, 0) >= self._MAX_FAILED_AUTH:
            return False
        role = self._get_user_role(identity)
        if role is None:
            self._failed_auth_counts[key] = self._failed_auth_counts.get(key, 0) + 1
            return False
        return True

    def _require_executor(self, identity) -> bool:
        """True iff role is 'executor'."""
        return self._get_user_role(identity) == "executor"

    def _rate_limited(self, identity_key: str) -> bool:
        """Identity-keyed rate limiter. Caller passes _identity_key(identity)."""
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(identity_key, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
```

- [ ] **Step 4: Add `_persist_auth_if_dirty` test FIRST (TDD step)**

Append to `tests/test_auth_roles.py`:

```python
def test_auth_dirty_set_on_first_contact():
    """First contact by username appends to locked_identities AND sets _auth_dirty."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    assert bot._auth_dirty is False
    bot._auth_identity(_identity("alice", native_id="98765"))
    # _identity helper uses transport_id="telegram"; the identity_key is "telegram:98765".
    assert au.locked_identities == ["telegram:98765"]
    assert bot._auth_dirty is True


def test_auth_dirty_unset_after_persist_call():
    """_persist_auth_if_dirty clears the flag and is idempotent on a clean state."""
    saves: list[int] = []

    class _Bot(AuthMixin):
        def __init__(self):
            self._allowed_users = [AllowedUser(username="alice", role="executor")]
            self._init_auth()

        def _save_config_for_auth(self):
            saves.append(1)

        async def _persist_auth_if_dirty(self):
            if self._auth_dirty:
                self._save_config_for_auth()
                self._auth_dirty = False

    import asyncio
    bot = _Bot()
    bot._auth_identity(_identity("alice", native_id="98765"))
    assert bot._auth_dirty is True
    asyncio.run(bot._persist_auth_if_dirty())
    assert bot._auth_dirty is False
    assert saves == [1]
    # Second call is a no-op.
    asyncio.run(bot._persist_auth_if_dirty())
    assert saves == [1]


def test_locked_id_already_present_does_not_dirty():
    """When the current identity is already in locked_identities, no first-contact write happens."""
    au = AllowedUser(username="alice", role="executor", locked_identities=["telegram:98765"])
    bot = _BotWithRoles(allowed_users=[au])
    bot._auth_identity(_identity("alice", native_id="98765"))
    assert bot._auth_dirty is False
```

Run them: `pytest tests/test_auth_roles.py -v`. They should fail because `_persist_auth_if_dirty` isn't defined on `ProjectBot` yet, and `_auth_dirty` may not be initialized by `_init_auth`.

The `_init_auth` change in Step 3 already sets `self._auth_dirty = False`. Now implement `_persist_auth_if_dirty` on `ProjectBot`:

In `src/link_project_to_chat/bot.py`, immediately above `_guard_executor` (Step 6 below), add:

```python
    async def _persist_auth_if_dirty(self) -> None:
        """Save config.json once if _auth_dirty was set by a first-contact lock.

        Called from message-handling tails (_on_text, _on_run, etc.) after the
        message is processed. Cheap when nothing to do (single bool check).

        Three correctness properties:
        1. **Atomic read-modify-write.** The load → merge → save sequence
           holds the existing `_config_lock` (`fcntl.flock` POSIX,
           `msvcrt.locking` Windows) for the WHOLE duration, not just the
           write phase. Without this, two concurrent first-contacts would
           each load the pre-write state, each merge in their own change,
           each save — last writer wins and silently drops one lock.
           The lock is provided by the new `locked_config_rmw(path)` context
           manager in `config.py` (added in Task 3 Step 7b alongside the
           save logic).
        2. **Scope-aware.** Writes back to whichever scope the bot's
           _allowed_users came from. When the bot inherited the global
           allow-list via `resolve_project_allowed_users` fallback,
           `self._auth_source == "global"` and we update `Config.allowed_users`
           on disk — NOT the project's empty list (which would silently
           promote the global list to a project-scoped copy).
        3. **Per-user merge.** Find the AllowedUser by username, union its
           locked_identities with our in-memory copy, write. Does NOT replace
           the full list. Concurrent edits to OTHER users on disk are
           preserved.
        """
        if not self._auth_dirty:
            return
        from .config import locked_config_rmw, save_config_within_lock
        cfg_path = self._effective_config_path()
        try:
            with locked_config_rmw(cfg_path) as disk:
                # disk is a freshly-loaded Config with the file lock held.
                if self._auth_source == "project":
                    if self.name not in disk.projects:
                        logger.warning(
                            "Auth persist skipped: project %r missing from disk", self.name,
                        )
                        return
                    target = disk.projects[self.name].allowed_users
                else:  # "global"
                    target = disk.allowed_users

                # Per-user merge: union our in-memory locks with disk's.
                in_memory_by_user = {u.username: u for u in self._allowed_users}
                for au in target:
                    mem = in_memory_by_user.get(au.username)
                    if mem is None:
                        continue
                    merged = list(au.locked_identities)
                    for ident in mem.locked_identities:
                        if ident not in merged:
                            merged.append(ident)
                    au.locked_identities = merged

                # Inside the context manager — save_config_within_lock writes
                # without re-locking (the context manager already holds the lock).
                save_config_within_lock(disk, cfg_path)

            self._auth_dirty = False
        except Exception:
            logger.exception("Failed to persist auth state; will retry on next message")
```

This relies on two new symbols in `config.py` (add them as part of Task 3 Step 7b):

```python
# In src/link_project_to_chat/config.py, near the existing _config_lock helper:

from contextlib import contextmanager

@contextmanager
def locked_config_rmw(path: Path):
    """Hold _config_lock across a load-modify-save cycle.

    Yields a freshly-loaded Config object; caller mutates it; the context
    manager writes it back atomically when the block exits cleanly. Use
    `save_config_within_lock` inside the block to write — `save_config`
    re-acquires the lock and would deadlock.
    """
    with _config_lock(path):
        config = _load_config_unlocked(path)
        yield config

def save_config_within_lock(config, path: Path) -> None:
    """Write config to disk WITHOUT acquiring _config_lock.

    Public callers should use save_config (which acquires the lock); this is
    for callers that already hold the lock via locked_config_rmw.
    """
    _save_config_unlocked(config, path)
```

The existing `load_config` / `save_config` get refactored to: outer wrapper acquires `_config_lock`, inner `_load_config_unlocked` / `_save_config_unlocked` does the actual work. The atomic RMW path uses the inner helpers under one lock acquisition. **Add this refactor as Task 3 Step 7b** (a small follow-up to Step 7's save changes) so the helpers exist when Task 5 imports them.

The `self._auth_source` attribute is set in `ProjectBot.__init__` from the caller. In `run_bot` / `run_bots`, change the call sites that compute `effective_allowed` to also capture the source:

```python
        effective_allowed, auth_source = resolve_project_allowed_users(proj, config)
        run_bot(
            ...,
            allowed_users=effective_allowed or None,
            auth_source=auth_source,
            plugins=proj.plugins or None,
        )
```

`run_bot` accepts a new `auth_source: str = "project"` kwarg and forwards it to `ProjectBot.__init__`, which stores it on `self._auth_source`. For ad-hoc `--path`/`--token` runs (no config loaded), the auth source is implicitly `"project"` (the in-memory list belongs to the running bot, no global to fall back to).

In `_on_text` (line 1003), at the very end of the body (after `task_manager.submit_agent(...)`), add:

```python
        await self._persist_auth_if_dirty()
```

The persistence wiring is centralized in `_guard_executor` (Step 6 below) and in a `try/finally` around top-level handlers (Step 6b). The Step 6 helper persists on BOTH the success AND the viewer-denied path — `_require_executor` may have appended a first-contact identity even when the role check fails. The Step 6b wrapper picks up read-only handler paths that don't run `_guard_executor` (like `/status`, `/tasks`, plugin-consumed messages).

See Step 6 below for the canonical `_guard_executor` implementation.

(Read the existing `_auth.py` first; preserve the brute-force lockout and rate-limit code. The only change is replacing the legacy username/ID lookup with the new `_get_user_role` flow.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: All tests PASS.

- [ ] **Step 6: Edit `bot.py` — add `_guard_executor` helper**

Insert this method on `ProjectBot` immediately above `_on_text` (line 1003):

```python
    async def _guard_executor(self, ci_or_msg) -> bool:
        """Return True if the user may run state-changing actions.

        Replies 'Read-only access' on the active transport when blocked.
        `ci_or_msg` accepts either a CommandInvocation or an IncomingMessage —
        both expose `.sender`, `.chat`, and `.message`.

        IMPORTANT: persists `_auth_dirty` on BOTH the success AND the
        viewer-denied path. `_require_executor` may have appended a
        first-contact identity to the user's locked_identities even when the
        role check ultimately fails (e.g., the user is a viewer logging in
        from a new transport — they get authed, locked, then denied for the
        state-changing action). Skipping the save on the deny path would
        lose that lock.
        """
        sender = getattr(ci_or_msg, "sender", None)
        if sender is None:
            return False
        allowed = self._require_executor(sender)
        # Persist first-contact locks regardless of allow/deny outcome.
        await self._persist_auth_if_dirty()
        if allowed:
            return True
        assert self._transport is not None
        await self._transport.send_text(
            ci_or_msg.chat,
            "Read-only access — your role is viewer.",
            reply_to=getattr(ci_or_msg, "message", None),
        )
        return False
```

**This is the canonical `_guard_executor` shape.** The earlier sketch in Step 4 (that only persisted on the True branch) is superseded — disregard that snippet and use this one.

- [ ] **Step 6b: Wrap top-level handlers with a try/finally that always persists**

`_guard_executor` covers the state-changing path, but several top-level handlers can complete WITHOUT calling it: plugin-consumed messages (where `plugin.on_message` returned True), plugin-consumed buttons, read-only commands (`/status`, `/tasks`), and exception paths. In every case, the transport's authorizer may have already triggered a first-contact identity append via `_get_user_role`. Without a final persist, that lock stays in-memory and is lost on bot restart.

Add a small async helper:

```python
    async def _with_auth_persist(self, awaitable):
        """Run an awaitable, guaranteeing _persist_auth_if_dirty fires after.

        Use this in top-level handler bodies whose flow may exit through any
        of: plugin consume, viewer-denied gate, exception, normal path.
        Cheap when no first-contact happened (the persist is a single bool
        check that no-ops).
        """
        try:
            await awaitable
        finally:
            await self._persist_auth_if_dirty()
```

Then wrap the three top-level entry points the transport calls — `_on_text_from_transport`, the command-dispatch callbacks (`_on_X_t` family), and `_on_button` — so the persist fires regardless of which branch the handler took. Concretely, in each top-level handler add the `try/finally` directly:

```python
    async def _on_text_from_transport(self, incoming) -> None:
        try:
            # existing body unchanged
            ...
        finally:
            await self._persist_auth_if_dirty()

    async def _on_button(self, click) -> None:
        try:
            # existing body unchanged
            ...
        finally:
            await self._persist_auth_if_dirty()
```

Same pattern for each `_on_<command>` handler. For command handlers, since they all get registered via `transport.on_command(name, handler)`, you can also wrap them centrally by writing the registration step as:

```python
        def _wrap_command(handler):
            async def _wrapped(ci):
                try:
                    await handler(ci)
                finally:
                    await self._persist_auth_if_dirty()
            return _wrapped

        for name, handler in ported_commands:
            self._transport.on_command(name, _wrap_command(handler))
```

This is cleaner than touching every individual `_on_X_t` body.

Add a test in `tests/test_auth_roles.py`:

```python
@pytest.mark.asyncio
async def test_plugin_consumed_message_still_persists_first_contact_lock():
    """A plugin that consumes the message (returns True from on_message)
    short-circuits the role gate. The first-contact identity lock applied
    by the transport authorizer must still get persisted via the try/finally
    wrapping the top-level handler."""
    persists: list[int] = []

    class _BotWithPersistCount(AuthMixin):
        def __init__(self, allowed_users):
            self._allowed_users = list(allowed_users)
            self._init_auth()

        async def _persist_auth_if_dirty(self):
            if self._auth_dirty:
                persists.append(1)
                self._auth_dirty = False

        async def _with_auth_persist(self, awaitable):
            try:
                await awaitable
            finally:
                await self._persist_auth_if_dirty()

    bot = _BotWithPersistCount(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )

    async def consuming_handler():
        # Simulate auth + plugin consume.
        bot._auth_identity(_identity("alice", native_id="98765"))
        # Plugin returned True → handler returns early without role gate.
        return

    await bot._with_auth_persist(consuming_handler())
    assert persists == [1]
```

- [ ] **Step 7: Edit `bot.py` — gate state-changing command handlers**

State-changing handlers to gate (all currently auth'd via `_auth_identity` at the top of their bodies — locate each by name and add the guard immediately after the auth check):

`_on_run`, `_on_backend`, `_on_model`, `_on_effort`, `_on_thinking`, `_on_context` (when toggling, not displaying), `_on_permissions`, `_on_compact`, `_on_reset`, `_on_persona`, `_on_stop_persona`, `_on_create_persona`, `_on_delete_persona`, `_on_skills` activation branch (no separate `_on_use` exists today — list/pick UI is all `_on_skills` + `pick_skill_*` buttons), `_on_stop_skill`, `_on_create_skill`, `_on_delete_skill`, `_on_lang`, `_on_halt`, `_on_resume`, `_on_file_from_transport`, `_on_voice_from_transport`.

For each, after the existing `if not self._auth_identity(...): return`, add:

```python
        if not await self._guard_executor(ci):  # or `incoming` for IncomingMessage handlers
            return
```

For `_on_text` specifically: the role gate goes **after** plugin `_dispatch_plugin_on_message` (added in Task 2 Step 11) and **before** any pending-skill / waiting-input / submit logic. Insert at the appropriate point — read lines 1023–1075 to find the right spot. Concretely, place it immediately after the `consumed = await self._dispatch_plugin_on_message(...)` block:

```python
        if not await self._guard_executor(incoming):
            return
```

For `_on_skills`: read the handler body. If it has separate list-skills (read-only) and activate-skill (state-changing) branches, gate only the activation branch. If it's a single "always-list-then-pick" UI, you may need to gate at the pick callback in `_on_button` instead — make a judgment call after reading.

- [ ] **Step 7b: Gate state-changing button branches in `_on_button`**

In [bot.py](src/link_project_to_chat/bot.py) `_on_button` (line 1960), the branch chain handles many state-changing button values. Each one needs a role gate. The complete state-changing prefix list (verified against the current `_on_button` body):

- `model_set_*` (line 1969) — changes the active model.
- `effort_set_*` (line 1989) — changes effort level.
- `thinking_set_*` (around `thinking_set_on`/`thinking_set_off`) — toggles thinking livestream.
- `permissions_set_*` (around line 1360 generator + handler) — changes permission mode.
- `backend_set_*` (line 1981) — switches the active backend.
- `reset_confirm` / `reset_cancel` (line 1487-1488) — session reset confirmation.
- `task_cancel_*` (in `_tasks_buttons`) — cancels a running task.
- `lang_set_*` — switches voice-message language.
- `skill_scope_*` ([bot.py:2085](src/link_project_to_chat/bot.py:2085)) — `/create_skill` scope picker → next message writes a skill.
- `pick_skill_*` ([bot.py:2091](src/link_project_to_chat/bot.py:2091)) — activates the picked skill on this project.
- `skill_delete_confirm_*` ([bot.py:2073](src/link_project_to_chat/bot.py:2073)) — destructive delete confirmation.
- `persona_scope_*` ([bot.py:2120](src/link_project_to_chat/bot.py:2120)) — `/create_persona` scope picker.
- `pick_persona_*` (around `persona_scope_*`) — activates the picked persona.
- `persona_delete_confirm_*` ([bot.py:2109](src/link_project_to_chat/bot.py:2109)) — destructive delete confirmation.
- `ask_*` (answers to `AskUserQuestion`) — the answer drives a Claude turn forward. Viewers can't push a turn, so this gates too.
- Any future `*_set_*` / `*_confirm_*` / destructive-action prefix should be added as it lands.

For each state-changing branch, wrap the body with the guard. Example pattern:

```python
        if value.startswith("model_set_"):
            if not await self._guard_executor(click):
                return
            # ... existing branch body unchanged ...
```

Read-only branches stay untouched (no gate added):
- `tasks_show_log_*` — display only.
- Any other read-only display prefix surfaced by the audit.
- Plugin-registered buttons (they flow through `_dispatch_plugin_button` and the plugin is responsible for its own gating per the spec's viewer policy).

After the audit, list every gated branch in the commit message so reviewers can verify nothing was missed. The parametrized test in Step 7c below covers every prefix in the gated set.

- [ ] **Step 7c: Add a parametrized test for every gated button prefix**

Add to `tests/test_auth_roles.py`:

```python
# All state-changing button-value examples — one per prefix from Step 7b.
# Tests are parametrized over this list so a missed gate fails loudly with
# a specific param name.
STATE_CHANGING_BUTTON_VALUES = [
    "model_set_haiku",
    "effort_set_medium",
    "thinking_set_on",
    "permissions_set_default",
    "backend_set_codex",
    "reset_confirm",
    "reset_cancel",
    "task_cancel_42",
    "lang_set_en",
    "skill_scope_project_test-skill",
    "pick_skill_test-skill",
    "skill_delete_confirm_project_test-skill",
    "persona_scope_project_test-persona",
    "pick_persona_test-persona",
    "persona_delete_confirm_project_test-persona",
    "ask_42_0_0",  # AskUserQuestion answer — task 42, q 0, option 0
]


@pytest.fixture
def _viewer_bot(tmp_path):
    """Minimal ProjectBot wired enough to call _on_button(click).

    Constructed via __new__ to skip the heavy real __init__ (which would
    spin up backends and a TaskManager). We hand-set every attribute that
    _on_button + _guard_executor reads.
    """
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import AllowedUser

    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.path = Path("/tmp/p")
    bot._allowed_users = [
        AllowedUser(username="viewer-user", role="viewer", locked_identities=["telegram:42"]),
    ]
    bot._auth_source = "project"
    bot._init_auth()
    bot._plugins = []
    bot._plugin_command_handlers = {}
    bot._shared_ctx = None
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()
    bot._effective_config_path = lambda: tmp_path / "config.json"

    # Stub task_manager so any task_cancel_* branch doesn't AttributeError
    # before the gate fires. (Belt and suspenders — the gate should run
    # first, but if it doesn't, the test fails loudly instead of erroring.)
    bot.task_manager = MagicMock()
    bot.task_manager.cancel = MagicMock()
    bot.task_manager.find_by_id = MagicMock(return_value=None)
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("value", STATE_CHANGING_BUTTON_VALUES)
async def test_state_changing_button_blocked_for_viewer(_viewer_bot, value):
    """Every state-changing button prefix from Step 7b must reply
    'Read-only access' to a viewer and NOT call its mutation path.

    A new state-changing button prefix added later but not gated will fail
    this parametrized test with the missing-value param name in the report.
    """
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    viewer = Identity(transport_id="telegram", native_id="42", display_name="V", handle="viewer-user", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=viewer, value=value)

    await _viewer_bot._on_button(click)

    # The gate fires → send_text was awaited with the Read-only reply.
    assert _viewer_bot._transport.send_text.await_count >= 1, (
        f"No Read-only reply for state-changing button {value!r}; gate missing?"
    )
    last_text = _viewer_bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in last_text, (
        f"Reply for {value!r} was {last_text!r}, expected 'Read-only access' text"
    )
    # No mutation path ran.
    _viewer_bot.task_manager.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_state_changing_button_passes_for_executor(_viewer_bot):
    """Executors get past the gate — sanity check the gate isn't accidentally
    rejecting authorized users."""
    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    # Swap the bot's role to executor for this test.
    _viewer_bot._allowed_users = [
        AllowedUser(username="exec-user", role="executor", locked_identities=["telegram:42"]),
    ]
    sender = Identity(transport_id="telegram", native_id="42", display_name="E", handle="exec-user", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="model_set_haiku")

    await _viewer_bot._on_button(click)

    # No Read-only reply was sent; the gate let the executor through.
    sends = _viewer_bot._transport.send_text.await_args_list
    for call in sends:
        text = call.args[1].lower()
        assert "read-only" not in text, f"Executor saw Read-only reply: {text!r}"
```

If the test fixture above gets too involved (the real `_on_button` body touches many sibling attributes), move these into `tests/test_auth_migration_e2e.py` and drive the click through `FakeTransport` end-to-end instead. The contract — every state-changing prefix denies viewers — is what matters; the test shape can be whichever is cleanest given how `ProjectBot.__new__` interacts with the rest of the body.

- [ ] **Step 8: Update existing auth tests for the field removal**

```bash
grep -rln "_allowed_usernames\|_trusted_user_ids\|allowed_usernames\|trusted_user_ids" tests/
```

Each match is a test referencing the legacy auth model. Rewrite to construct `AllowedUser` entries with `role="executor"` (the migration default — preserves equivalent legacy semantics). Don't delete tests; convert them. **Expected: tests pass with no behavioral change because executor-only allowed_users matches the legacy "everyone allowed has full access" behavior.**

- [ ] **Step 9: Verify `run_bots` uses `resolve_project_allowed_users`**

`run_bots` was updated in **Task 4 Step 6** to compute
`effective_allowed, auth_source = resolve_project_allowed_users(proj, config)`
and pass both into `run_bot`. Verify this is in place — if Task 4's edit hasn't landed yet (executing tasks out of order), apply the snippet from Task 4 Step 6 to `run_bots` now. The Task 2 transitional `getattr(proj, "plugins", None)` plumbing is **replaced**, not preserved.

```bash
grep -n "resolve_project_allowed_users\|auth_source=" src/link_project_to_chat/bot.py
```

Expected: matches inside the `run_bots` body. If only `getattr(proj, ...)` lingers, port the Task 4 Step 6 snippet now.

- [ ] **Step 10a: Add scope-aware persistence test**

Append to `tests/test_auth_roles.py`:

```python
@pytest.mark.asyncio
async def test_persist_writes_to_global_when_auth_source_is_global(tmp_path):
    """When the bot inherited users from Config.allowed_users via fallback,
    first-contact locks are written to the GLOBAL allow-list — NOT cloned
    into the project's empty list."""
    import json
    from link_project_to_chat.config import save_config, Config, ProjectConfig

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [AllowedUser(username="admin", role="executor")]
    cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(cfg, cfg_path)

    # Simulate a bot that inherited from global (auth_source="global").
    bot = _BotWithRoles(allowed_users=cfg.allowed_users)
    bot._auth_source = "global"
    bot._effective_config_path = lambda: cfg_path

    # Wire up _persist_auth_if_dirty's real implementation here. The test
    # construction may need to import ProjectBot's method into the test bot
    # via __get__ — adjust to whatever is cleanest given the mixin shape.

    bot._auth_identity(_identity("admin", native_id="99"))
    assert bot._auth_dirty is True
    # Test is async — await directly. Calling asyncio.run() inside an
    # already-running event loop raises RuntimeError.
    await ProjectBot._persist_auth_if_dirty(bot)

    # Re-read disk. The GLOBAL allow-list must show the locked identity;
    # the project's allowed_users must remain empty (not promoted to a copy).
    disk = json.loads(cfg_path.read_text())
    assert disk["allowed_users"][0]["locked_identities"] == ["telegram:99"]
    assert disk["projects"]["p"].get("allowed_users", []) == []


@pytest.mark.asyncio
async def test_persist_merges_per_user_not_replace(tmp_path):
    """Persisting changes for user A must not overwrite changes to user B
    made by another bot writing concurrently."""
    import json
    from link_project_to_chat.config import save_config, Config, ProjectConfig

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor"),
    ]
    cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(cfg, cfg_path)

    # Simulate: another bot wrote bob's identity to disk while ours was running.
    disk_cfg = Config()
    disk_cfg.allowed_users = [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor", locked_identities=["telegram:200"]),
    ]
    disk_cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(disk_cfg, cfg_path)

    # Our bot in-memory only has the original lists. Alice locks her ID.
    bot = _BotWithRoles(allowed_users=cfg.allowed_users)
    bot._auth_source = "global"
    bot._effective_config_path = lambda: cfg_path
    bot._auth_identity(_identity("alice", native_id="100"))
    # Async test — await directly.
    await ProjectBot._persist_auth_if_dirty(bot)

    disk = json.loads(cfg_path.read_text())
    by_user = {u["username"]: u for u in disk["allowed_users"]}
    assert by_user["alice"]["locked_identities"] == ["telegram:100"]
    assert by_user["bob"]["locked_identities"] == ["telegram:200"]  # NOT clobbered
```

- [ ] **Step 10b: Add multi-transport identity test**

Append to `tests/test_auth_roles.py`:

```python
def test_multi_transport_user_auths_from_both(tmp_path):
    """A user with locked_identities=["telegram:X", "web:web-session:Y"]
    auths successfully from EITHER transport."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345", "web:web-session:abc-def"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Telegram side.
    tg_ident = Identity(
        transport_id="telegram", native_id="12345",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(tg_ident) is True
    # Web side.
    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True


def test_username_fallback_appends_to_identities_per_transport(tmp_path):
    """A user with one telegram lock who messages from web for the first
    time gets the web identity APPENDED — telegram lock is preserved."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    web_ident = Identity(
        transport_id="web", native_id="web-session:new-session",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    assert au.locked_identities == ["telegram:12345", "web:web-session:new-session"]
    assert bot._auth_dirty is True
```

- [ ] **Step 10: Add end-to-end migration integration test**

Create `tests/test_auth_migration_e2e.py`:

```python
"""End-to-end auth migration through ProjectBot + FakeTransport.

Covers the full path: legacy config.json → load_config → ProjectBot.build()
with FakeTransport → first message lands → _auth_dirty triggers save →
on-disk file shows the populated locked_identities.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser, load_config, save_config
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _legacy_config(cfg_file: Path, project_path: Path) -> None:
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": {"alice": 12345},  # alice locked; bob not
            }
        }
    }))


@pytest.mark.asyncio
async def test_e2e_legacy_load_first_message_locks_id_and_persists(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    _legacy_config(cfg_file, project_path)

    # 1. Load and force-save (this is what `start` does on migration_pending).
    config = load_config(cfg_file)
    assert config.migration_pending is True
    save_config(config, cfg_file)

    on_disk = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in on_disk["projects"]["p"]
    assert "trusted_users" not in on_disk["projects"]["p"]
    users = on_disk["projects"]["p"]["allowed_users"]
    by_user = {u["username"]: u for u in users}
    assert by_user["alice"]["locked_identities"] == ["telegram:12345"]
    assert "locked_identities" not in by_user["bob"]  # not locked yet (omitted because empty)

    # 2. Build ProjectBot from the migrated config + FakeTransport.
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p",
        path=project_path,
        token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )
    # Inject FakeTransport directly (bypass real build()).
    transport = FakeTransport()
    bot._transport = transport
    bot._app = None

    # 3. Bob's first message — should auth (bob is in allowed_users) and lock his ID.
    bob_identity = Identity(
        transport_id="fake", native_id="67890",
        display_name="Bob", handle="bob", is_bot=False,
    )
    chat = ChatRef(transport_id="fake", native_id="bob-dm", kind=ChatKind.DM)
    msg = IncomingMessage(
        chat=chat,
        sender=bob_identity,
        text="hello",
        files=[],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="m1", chat=chat),
    )

    # Drive the dispatch directly: auth + persist.
    assert bot._auth_identity(bob_identity) is True
    assert bot._auth_dirty is True
    await bot._persist_auth_if_dirty()
    assert bot._auth_dirty is False

    # 4. On-disk file now shows bob with a populated locked_identities. Bob's
    #    first contact came through FakeTransport, so the identity is
    #    prefixed with "fake:" (not "telegram:" — Bob was NOT in the legacy
    #    trusted_users dict, so the migration didn't seed a telegram entry).
    on_disk_after = json.loads(cfg_file.read_text())
    users_after = on_disk_after["projects"]["p"]["allowed_users"]
    by_user_after = {u["username"]: u for u in users_after}
    assert by_user_after["bob"]["locked_identities"] == ["fake:67890"]
    # Alice's migrated telegram lock from the legacy config is preserved.
    assert "telegram:12345" in by_user_after["alice"]["locked_identities"]

    # 5. Second contact by bob: no extra save (identity already in the list).
    msg_count_before = len([f for f in tmp_path.iterdir() if f.is_file()])
    bot._auth_identity(bob_identity)
    await bot._persist_auth_if_dirty()
    assert bot._auth_dirty is False
    msg_count_after = len([f for f in tmp_path.iterdir() if f.is_file()])
    assert msg_count_before == msg_count_after


@pytest.mark.asyncio
async def test_e2e_username_spoof_blocked_after_lock(tmp_path: Path):
    """After a user's identity is locked, an attacker with the same username
    but a different native_id (SAME transport) is rejected."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    # The locked identity uses transport_id="fake" because this test drives
    # FakeTransport. (For a Telegram-transport test, the locked identity would
    # be "telegram:12345" — must match the transport_id of the running bot.)
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor", "locked_identities": ["fake:12345"]},
                ],
            }
        }
    }))
    config = load_config(cfg_file)
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p",
        path=project_path,
        token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )

    # Attacker: same username "alice", different native_id on the same transport.
    # identity_key would be "fake:11111" — not in alice's locked_identities.
    attacker = Identity(
        transport_id="fake", native_id="11111",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(attacker) is False

    # Real alice still works — her identity_key is "fake:12345", in the list.
    real = Identity(
        transport_id="fake", native_id="12345",
        display_name="Anyone", handle="not-the-real-alice", is_bot=False,
    )
    assert bot._auth_identity(real) is True


@pytest.mark.asyncio
async def test_e2e_multi_transport_user_locks_per_transport(tmp_path: Path):
    """A user authed first from Telegram-shape locks 'telegram:X'; same user
    first-contacting from Web appends 'web:web-session:Y' — both work."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor"},
                ],
            }
        }
    }))
    config = load_config(cfg_file)
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p", path=project_path, token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )

    tg_ident = Identity(
        transport_id="telegram", native_id="12345",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(tg_ident) is True
    await bot._persist_auth_if_dirty()

    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    await bot._persist_auth_if_dirty()

    on_disk = json.loads(cfg_file.read_text())
    alice = next(u for u in on_disk["projects"]["p"]["allowed_users"] if u["username"] == "alice")
    assert "telegram:12345" in alice["locked_identities"]
    assert "web:web-session:abc-def" in alice["locked_identities"]
```

Run:
```bash
pytest tests/test_auth_migration_e2e.py -v
```
Expected: All tests PASS. If any fail, the most likely cause is missing wiring — re-check that `ProjectBot.__init__` accepts `allowed_users` and `config_path`, and that `_persist_auth_if_dirty` was added per Step 4.

- [ ] **Step 11: Audit + rewrite ALL remaining call sites of legacy fields**

```bash
grep -rn "allowed_usernames\|trusted_users\|trusted_user_ids" src/link_project_to_chat/
```

Every match outside `_migrate_legacy_auth` and the loader's compatibility shim must be rewritten:
- Reads of `proj.allowed_usernames` / `proj.trusted_users` / `proj.trusted_user_ids` → `resolve_project_allowed_users(proj, config)`.
- Reads of `config.allowed_usernames` / `config.trusted_users` → `config.allowed_users`.
- `_get_trusted_user_ids()` / `_get_trusted_user_bindings()` / `_effective_trusted_users` / `_trust_user` / `_revoke_user` / `_coerce_trust_value` — delete these helpers from `_auth.py` if they exist; their behavior is subsumed by `_get_user_role` and `_persist_auth_if_dirty`.
- `add_trusted_user_id`, `add_project_trusted_user_id`, `bind_project_trusted_user` (in `config.py`) — rewrite to operate on `AllowedUser.locked_identities` (append the new identity to the list) or delete if `_persist_auth_if_dirty` covers them.
- `ManagerBot.__init__` legacy kwargs (`allowed_username`, `allowed_usernames`, `trusted_users`, `trusted_user_id`, `trusted_user_ids`) — Task 4 Step 6b made them dead defaults (no caller passes them anymore). Strip them now along with the assignment block (`if allowed_usernames is not None: self._allowed_usernames = ...` etc.) **AND the transition synthesis branch** (`elif allowed_users is not None: self._allowed_usernames = [u.username for u in allowed_users]`) added in Step 6b. Step 3 above rewrote `AuthMixin` to read exclusively from `self._allowed_users`, so the legacy `_allowed_usernames` synthesis is now dead code. The constructor's surviving signature is `(token, process_manager, allowed_users=None, project_config_path=None)`.
- The companion transition test `test_manager_bot_legacy_auth_works_with_allowed_users_only` (added in Task 4 Step 6b in [tests/test_cli.py](tests/test_cli.py)) — also strip. Step 3 deleted the legacy `_auth(user)` method, so the test would `AttributeError`. The post-rewrite equivalent ("authorized user authenticates via `_auth_identity`") is already covered by `tests/test_auth_roles.py` from Step 1; no replacement needed here.

After this step, **no source file outside `config.py`'s migration helper reads `allowed_usernames` / `trusted_users` / `trusted_user_ids`**. Verify by re-running the grep — only `_migrate_legacy_auth` and its tests should match.

- [ ] **Step 12: Remove legacy fields from `ProjectConfig` and `Config`**

In [src/link_project_to_chat/config.py](src/link_project_to_chat/config.py), now that no caller reads them:

- Delete `allowed_usernames`, `trusted_users`, `trusted_user_ids` from `ProjectConfig` (line ~45-47).
- Delete the same three from `Config` (line ~97-99).
- Remove any helpers that only existed to read them: `_migrate_usernames`, `_migrate_user_ids`, `_effective_trusted_users`, `_migrate_trusted_users`, `_write_raw_trusted_users` (audit case-by-case; some may still be used by the migration helper).
- The loader's `_migrate_legacy_auth` is the ONLY place that reads the legacy keys from the raw JSON dict; it doesn't touch the dataclass for those fields.

Update tests that referenced the legacy fields on the dataclass (they'd been kept as no-ops during Task 3; now they need to construct `AllowedUser` entries directly).

**Add the regression test that was deliberately deferred from Task 3** — append to `tests/test_config_allowed_users.py`:

```python
def test_legacy_fields_are_not_dataclass_attributes():
    """After Task 5 Step 12, the legacy fields are GONE from the dataclass.
    They can only be read by the loader's _migrate_legacy_auth, which sees
    the raw JSON dict — never the typed ProjectConfig / Config.
    Adding this test in Task 3 would fail by design, since legacy fields
    stay on the dataclass through Tasks 3–4 as transitional read-only inputs.
    """
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert not hasattr(p, "allowed_usernames")
    assert not hasattr(p, "trusted_users")
    assert not hasattr(p, "trusted_user_ids")

    c = Config()
    assert not hasattr(c, "allowed_usernames")
    assert not hasattr(c, "trusted_users")
    assert not hasattr(c, "trusted_user_ids")
```

- [ ] **Step 13: Run tests**

```bash
pytest tests/test_auth_roles.py tests/test_bot_plugin_hooks.py tests/test_auth_migration_e2e.py -v
pytest -q
```
Expected: All tests PASS. Handler tests that previously assumed "no role gate" now run with all-executor `allowed_users` lists from the migration default — behavior equivalent.

- [ ] **Step 14: Commit**

```bash
git add src/link_project_to_chat/_auth.py src/link_project_to_chat/bot.py \
        src/link_project_to_chat/config.py \
        src/link_project_to_chat/manager/bot.py \
        tests/test_auth_roles.py tests/test_auth_migration_e2e.py \
        tests/manager/test_guard_persistence.py \
        tests/  # for existing-test updates
git commit -m "$(cat <<'EOF'
feat(auth)!: AllowedUser is sole auth source; legacy fields removed

_auth_identity, _require_executor, and _get_user_role all read
self._allowed_users exclusively. Empty allowed_users fails closed (no
legacy allow-all path). Identity-locking via AllowedUser.locked_identities
(a "transport_id:native_id" string), populated on first contact.
Validates by identity, not username — preserves the username-spoof
protection and works for every transport (telegram:, web:, future
discord:/slack:).

Brute-force lockout (_failed_auth_counts) and rate-limit (_rate_limits)
dicts re-keyed on _identity_key for transport-uniform behavior.

First-contact locks set _auth_dirty; _persist_auth_if_dirty runs after
each handled message (including from _guard_executor on success) and
writes config.json once. Concurrent races are serialized by the
existing _config_lock (fcntl.flock / msvcrt.locking).

Manager bot's two PTB-native guards (_guard wizard shim,
_edit_field_save setup-text + pending-edit handler) rewritten to
identity_from_telegram_user → _auth_identity → _identity_key-keyed
_rate_limited, wrapped in try/finally calling a manager-side
_persist_auth_if_dirty. This rewrite happens in the same commit as the
AuthMixin rewrite so the manager never goes through a window where
_auth(user) is deleted but the PTB shims still call it.

State-changing command handlers AND state-changing button branches
(model_set_*, effort_set_*, thinking_set_*, permissions_set_*,
backend_set_*, reset_confirm, reset_cancel, task_cancel_*, lang_set_*,
ask_*, skill_scope_*, pick_skill_*, skill_delete_confirm_*,
persona_scope_*, pick_persona_*, persona_delete_confirm_*) gate via
_guard_executor; viewers see 'Read-only access' replies.

Legacy fields allowed_usernames / trusted_users / trusted_user_ids
removed from ProjectConfig and Config dataclasses now that every call
site uses resolve_project_allowed_users(project, config). Existing
handler tests adapted to the new schema with executor-only
allowed_users (preserves prior all-allowed semantics).

BREAKING CHANGE: AuthMixin no longer accepts allowed_usernames or
trusted_user_ids instance state; deployments must migrate their config
via Task 3's loader.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manager UI — plugin toggle + user-management commands

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/manager/test_bot_plugins.py`
- Create: `tests/manager/test_user_commands.py`

The manager bot was also ported to Transport (commands take `CommandInvocation`, button clicks come via the transport's `on_button`). This task adds:

1. **Plugin toggle UI** — per-project keyboard `Plugins` button.
2. **User-management commands** operating on the **global** `Config.allowed_users`:
   - `/users` — list rows formatted as `username (role) [ID locked: <id>|not yet]`.
   - `/add_user <username> [viewer|executor]` — default role `executor`.
   - `/remove_user <username>`.
   - `/promote_user <username>` and `/demote_user <username>` — toggle role.
   - `/reset_user_identity <username> [transport_id]` — clear `locked_identities` (recovery path). With a `transport_id` argument, clears only entries with that prefix.

All user-management commands persist via `save_config` and reply with the updated `/users` listing.

- [ ] **Step 1: Write the failing tests**

Create `tests/manager/test_bot_plugins.py`:
```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot


def _make_manager(monkeypatch, projects=None):
    from link_project_to_chat.config import AllowedUser
    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = None
    bot._allowed_users = [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])]
    bot._init_auth()
    monkeypatch.setattr(bot, "_load_projects", lambda: projects or {})
    return bot


def test_available_plugins_returns_entry_point_names(monkeypatch):
    bot = _make_manager(monkeypatch)
    fake_ep = MagicMock()
    fake_ep.name = "demo"
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.importlib.metadata.entry_points",
        lambda group: [fake_ep] if group == "lptc.plugins" else [],
    )
    assert bot._available_plugins() == ["demo"]


def test_plugins_buttons_marks_active_and_available(monkeypatch):
    projects = {"myp": {"plugins": [{"name": "demo"}]}}
    bot = _make_manager(monkeypatch, projects)
    a = MagicMock(); a.name = "demo"
    b = MagicMock(); b.name = "other"
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.importlib.metadata.entry_points",
        lambda group: [a, b] if group == "lptc.plugins" else [],
    )
    buttons = bot._plugins_buttons("myp")
    labels = [btn.label for row in buttons.rows for btn in row]
    assert any(l.startswith("✓ demo") for l in labels)
    assert any(l.startswith("+ other") for l in labels)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/manager/test_bot_plugins.py -v
```
Expected: FAIL — `_available_plugins` / `_plugins_buttons` not defined.

- [ ] **Step 3: Edit `manager/bot.py` — add imports**

At the top of `src/link_project_to_chat/manager/bot.py`, ensure `import importlib.metadata` is present (search the existing imports; add it among the stdlib imports if missing). Also confirm `from link_project_to_chat.transport.base import Button, Buttons` (or similar) is in scope — read the existing markup-helper code.

- [ ] **Step 4: Add a Plugins button to the project detail keyboard**

Find the per-project detail keyboard builder. It's structured similarly to:
```python
rows.append([Button(label="Edit", value=f"proj_edit_{name}")])
```

Add a row right before `Edit`:

```python
        rows.append([Button(label="Plugins", value=f"proj_plugins_{name}")])
```

- [ ] **Step 5: Add `_available_plugins` and `_plugins_buttons` to `ManagerBot`**

Add as methods on `ManagerBot`, near the other `_proj_*` markup helpers:

```python
    def _available_plugins(self) -> list[str]:
        eps = importlib.metadata.entry_points(group="lptc.plugins")
        return sorted(ep.name for ep in eps)

    def _plugins_buttons(self, name: str) -> Buttons:
        projects = self._load_projects()
        active = {p.get("name") for p in projects.get(name, {}).get("plugins", [])}
        available = self._available_plugins()
        rows: list[list[Button]] = []
        for plugin_name in available:
            label = f"✓ {plugin_name}" if plugin_name in active else f"+ {plugin_name}"
            rows.append([Button(label=label, value=f"proj_ptog_{plugin_name}|{name}")])
        rows.append([Button(label="« Back", value=f"proj_info_{name}")])
        return Buttons(rows=rows)
```

- [ ] **Step 6: Add button-click branches**

Find the manager bot's button-click dispatch (mirrors `_on_button` on the project bot — look for `def _on_button` or the per-prefix routing). Add branches for `proj_plugins_*` and `proj_ptog_*`:

```python
        elif click.value.startswith("proj_plugins_"):
            name = click.value[len("proj_plugins_"):]
            available = self._available_plugins()
            assert self._transport is not None
            if not available:
                await self._transport.edit_text(
                    click.message,
                    "No plugins installed.\n\n"
                    "Install the link-project-to-chat-plugins package to add plugins.",
                    buttons=Buttons(rows=[[Button(label="« Back", value=f"proj_info_{name}")]]),
                )
            else:
                await self._transport.edit_text(
                    click.message,
                    f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                    buttons=self._plugins_buttons(name),
                )

        elif click.value.startswith("proj_ptog_"):
            # Plugin toggle changes config state — gate to executor role.
            # Without this, a viewer with manager-bot access could enable
            # arbitrary plugin code on any project. Also: _auth_identity
            # earlier in the dispatch may have appended a first-contact lock
            # for this user; the surrounding _on_button_from_transport
            # try/finally (Step 6b mirror in manager) persists either way.
            if not self._require_executor(click.sender):
                assert self._transport is not None
                await self._transport.send_text(
                    click.chat,
                    "Read-only access — only executors can toggle plugins.",
                    reply_to=click.message,
                )
                return
            suffix = click.value[len("proj_ptog_"):]
            if "|" not in suffix:
                return
            plugin_name, name = suffix.rsplit("|", 1)
            projects = self._load_projects()
            if name not in projects:
                return
            plugins = projects[name].get("plugins", [])
            active_names = [p.get("name") for p in plugins]
            if plugin_name in active_names:
                plugins = [p for p in plugins if p.get("name") != plugin_name]
            else:
                plugins = plugins + [{"name": plugin_name}]
            projects[name]["plugins"] = plugins
            self._save_projects(projects)
            assert self._transport is not None
            await self._transport.edit_text(
                click.message,
                f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                buttons=self._plugins_buttons(name),
            )
```

Add a test for the gate in `tests/manager/test_bot_plugins.py`:

```python
@pytest.mark.asyncio
async def test_viewer_cannot_toggle_plugin(monkeypatch, tmp_path):
    """A viewer clicking the plugin toggle gets a Read-only reply; the
    project's plugins list is NOT modified."""
    import json
    from unittest.mock import AsyncMock, MagicMock
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [
        AllowedUser(username="viewer-admin", role="viewer", locked_identities=["telegram:9"]),
    ]
    save_config(cfg, cfg_path)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = cfg_path
    bot._allowed_users = list(cfg.allowed_users)
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()

    sender = Identity(transport_id="telegram", native_id="9", display_name="V", handle="viewer-admin", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="proj_ptog_demo|myp")

    monkeypatch.setattr(bot, "_load_projects", lambda: {"myp": {"plugins": []}})
    save_called = []
    monkeypatch.setattr(bot, "_save_projects", lambda p: save_called.append(p))

    # The manager bot's transport-native button entry point is
    # _on_button_from_transport (verified via grep on manager/bot.py — the
    # one registered via self._transport.on_button(...)). Use it directly
    # so the test exercises the real dispatch path.
    await bot._on_button_from_transport(click)

    assert save_called == []
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text
```

- [ ] **Step 7: Write failing tests — `tests/manager/test_user_commands.py`**

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.config import AllowedUser, Config, save_config
from link_project_to_chat.transport.base import ChatKind, ChatRef, CommandInvocation, Identity, MessageRef


def _make_manager(tmp_path: Path, users: list[AllowedUser] | None = None) -> ManagerBot:
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = list(users or [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])])
    save_config(cfg, cfg_file)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = cfg_file
    bot._allowed_users = list(cfg.allowed_users)
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    return bot


def _invocation(args: list[str], sender_handle: str = "admin", sender_id: str = "1") -> CommandInvocation:
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    sender = Identity(transport_id="telegram", native_id=sender_id, display_name=sender_handle, handle=sender_handle, is_bot=False)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    return CommandInvocation(chat=chat, sender=sender, name="cmd", args=args, raw_text=" ".join(args), message=msg)


@pytest.mark.asyncio
async def test_users_lists_current_state(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
        AllowedUser(username="bob", role="viewer"),
    ])
    await bot._on_users(_invocation([]))
    text = bot._transport.send_text.await_args.args[1]
    assert "alice" in text and "executor" in text and "12345" in text
    assert "bob" in text and "viewer" in text
    assert "not yet" in text.lower() or "—" in text  # bob has no locked id


@pytest.mark.asyncio
async def test_add_user_with_default_role_is_executor(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie"]))
    # Reload from disk to confirm persistence.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert any(u.username == "charlie" and u.role == "executor" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_add_user_with_explicit_role(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie", "viewer"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert any(u.username == "charlie" and u.role == "viewer" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_remove_user(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="viewer"),
    ])
    await bot._on_remove_user(_invocation(["bob"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert [u.username for u in cfg.allowed_users] == ["alice"]


@pytest.mark.asyncio
async def test_promote_user(tmp_path):
    bot = _make_manager(tmp_path, [AllowedUser(username="alice", role="viewer")])
    await bot._on_promote_user(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert cfg.allowed_users[0].role == "executor"


@pytest.mark.asyncio
async def test_demote_user(tmp_path):
    bot = _make_manager(tmp_path, [AllowedUser(username="alice", role="executor")])
    await bot._on_demote_user(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert cfg.allowed_users[0].role == "viewer"


@pytest.mark.asyncio
async def test_reset_user_identity_clears_locked_id(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
    ])
    await bot._on_reset_user_identity(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert cfg.allowed_users[0].locked_identities == []


@pytest.mark.asyncio
async def test_add_user_invalid_role_rejected(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie", "godmode"]))
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "invalid role" in text or "viewer" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_add_user(tmp_path):
    """Viewers must NOT be able to edit the allow-list — only executors."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-admin", role="viewer", locked_identities=["telegram:99"]),
    ])
    # Invocation from a viewer.
    inv = _invocation(["charlie"], sender_handle="viewer-admin", sender_id="99")
    await bot._on_add_user(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text
    # Confirm no write happened.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert not any(u.username == "charlie" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_viewer_can_list_users(tmp_path):
    """Viewers can use /users (read-only listing)."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_users(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "alice" in text
    assert "viewer-bob" in text


@pytest.mark.asyncio
async def test_promote_user_usage_message_says_promote_not_demote(tmp_path):
    """Regression test: _set_role used to compare new_role == 'promote' but
    callers pass the role string ('executor' / 'viewer'), so the usage
    message always said /demote_user even for /promote_user."""
    bot = _make_manager(tmp_path)
    # No args → usage message.
    await bot._on_promote_user(_invocation([]))
    text = bot._transport.send_text.await_args.args[1]
    assert "/promote_user" in text


@pytest.mark.asyncio
async def test_user_commands_work_without_explicit_config_path(monkeypatch, tmp_path):
    """When ManagerBot was constructed without a custom config path,
    `_load_config_for_users()` must fall back to DEFAULT_CONFIG instead of
    passing None to load_config (which would TypeError)."""
    from link_project_to_chat.config import DEFAULT_CONFIG

    # Redirect DEFAULT_CONFIG to a tmp file so the test doesn't touch the
    # user's home directory.
    cfg_path = tmp_path / "default-config.json"
    cfg_path.write_text(json.dumps({"allowed_users": []}))
    monkeypatch.setattr("link_project_to_chat.config.DEFAULT_CONFIG", cfg_path)
    monkeypatch.setattr("link_project_to_chat.manager.bot.DEFAULT_CONFIG", cfg_path)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = None       # ← this is the case the bug surfaced in
    bot._allowed_users = [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])]
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()

    inv = _invocation(["bob"], sender_handle="admin", sender_id="1")
    await bot._on_add_user(inv)
    # If _load_config_for_users passed None, this would have TypeError'd before
    # this line. Reaching here proves the fallback to DEFAULT_CONFIG worked.
    written = json.loads(cfg_path.read_text())
    assert any(u["username"] == "bob" for u in written.get("allowed_users", []))
```

- [ ] **Step 8: Run tests — expect failure**

```bash
pytest tests/manager/test_user_commands.py -v
```
Expected: FAIL — handlers don't exist yet.

- [ ] **Step 9: Implement user-management commands**

In `src/link_project_to_chat/manager/bot.py`, add the handlers. Register them via `transport.on_command(...)` in the existing command registration block (find where other manager commands like `/projects` are registered).

**REPLACE, not just add.** `manager/bot.py` already maps `users` / `add_user` / `remove_user` to legacy handlers (`_on_users_from_transport` / `_on_add_user_from_transport` / `_on_remove_user_from_transport`) inside its existing registration block (around [manager/bot.py:2285](src/link_project_to_chat/manager/bot.py:2285)). Those legacy handlers operate on the pre-v1.0 single-username-per-call shape and read/edit `allowed_usernames` / `trusted_users`. Leaving them in place would mean two handlers compete for the same command name — the registration dict's last writer wins, but the legacy handlers might still be referenced from elsewhere (e.g., the in-class ConversationHandler wizard at `_add_username`) and stale dead code is confusing for future readers.

Concrete edits:

1. **Find** the existing registration block in `manager/bot.py` (~line 2280 onwards):

   ```python
   ported_commands = {
       "projects": self._on_projects_from_transport,
       ...
       "users": self._on_users_from_transport,             # ← REMOVE
       ...
       "add_user": self._on_add_user_from_transport,       # ← REMOVE
       "remove_user": self._on_remove_user_from_transport, # ← REMOVE
       ...
   }
   for name, handler in ported_commands.items():
       self._transport.on_command(name, handler)
       app.add_handler(CommandHandler(name, self._transport.bridge_command(name)))
   ```

2. **Remove** the `users`, `add_user`, and `remove_user` lines from `ported_commands`. All three are replaced by the new role-aware handlers in step 3 below.

3. **Add** all the new user-mgmt registrations through `_wrap_with_persist` AND through `app.add_handler` so PTB actually dispatches them:

   ```python
   # Wrap every handler so a first-contact identity lock from the auth check
   # inside the handler always gets saved, regardless of which branch the
   # handler exits through (auth-failed, viewer-denied, normal). Apply this
   # to BOTH the new user-mgmt commands AND the existing manager commands
   # that go through _auth_identity (/projects, /add_project, etc.).
   def _wrap_with_persist(handler):
       async def _wrapped(ci):
           try:
               await handler(ci)
           finally:
               await self._persist_auth_if_dirty()
       return _wrapped

   # IMPORTANT: the manager bot does NOT use attach_telegram_routing — that
   # path is project-bot only. Each manager command needs BOTH calls:
   #   - self._transport.on_command(name, handler) updates the dispatch dict
   #   - app.add_handler(CommandHandler(name, self._transport.bridge_command(name)))
   #     registers a PTB handler that routes to the dispatch dict via _dispatch_command
   # Without the second call, PTB drops the command at the filter level even
   # though the dispatch dict knows about it.
   _new_manager_commands = {
       "users": self._on_users,
       "add_user": self._on_add_user,
       "remove_user": self._on_remove_user,
       "promote_user": self._on_promote_user,
       "demote_user": self._on_demote_user,
       "reset_user_identity": self._on_reset_user_identity,
   }
   for _name, _handler in _new_manager_commands.items():
       self._transport.on_command(_name, _wrap_with_persist(_handler))
       app.add_handler(CommandHandler(_name, self._transport.bridge_command(_name)))
   ```

4. **Wrap each existing handler** in the same registration block with `_wrap_with_persist` (replace `self._transport.on_command(name, handler)` with `self._transport.on_command(name, _wrap_with_persist(handler))` in the loop). Don't leave any unwrapped path: any manager command that calls `_auth_identity` and could lock a first-contact identity needs the persist tail.

5. **Verify nothing references the deleted legacy handlers**:

   ```bash
   grep -n "_on_users_from_transport\|_on_add_user_from_transport\|_on_remove_user_from_transport" src/link_project_to_chat/manager/bot.py
   ```

   If any of the names still appear (e.g., in `_add_username` ConversationHandler), either delete the dead code or migrate it to the new handler. The legacy ConversationHandler wizard ([manager/bot.py:636](src/link_project_to_chat/manager/bot.py:636)) edits legacy `allowed_usernames` directly — wholesale-replace its terminal action with a call to `self._on_add_user` so it produces the new shape on disk. `_on_users_from_transport` is typically only the registration-block reference; deleting that line plus its method definition should clear the grep.

NOTE: Task 1's `TelegramTransport.on_command` post-routing fix applies to the **project bot** (which uses `attach_telegram_routing`). The **manager bot** has always used the explicit `app.add_handler(CommandHandler(name, self._transport.bridge_command(name)))` pattern (visible at [manager/bot.py:2295](src/link_project_to_chat/manager/bot.py:2295)) — keep using it for the new commands too. Both calls are required:

- `self._transport.on_command(name, handler)` → updates `_command_handlers` so `_dispatch_command(name, ...)` finds the right handler.
- `app.add_handler(CommandHandler(name, self._transport.bridge_command(name)))` → registers a PTB-level handler that routes the update into `_dispatch_command`.

Missing either call leaves the command silently undispatchable.

Also wrap the manager's button dispatch (`_on_button_from_transport`) with a try/finally that calls `_persist_auth_if_dirty`. The plugin-toggle viewer-denied path triggers the `_auth_identity` + `_require_executor` chain — both can append a first-contact lock. Edit the existing `_on_button_from_transport` to wrap its body:

```python
    async def _on_button_from_transport(self, click) -> None:
        try:
            # existing body unchanged
            ...
        finally:
            await self._persist_auth_if_dirty()
```

Add the handler methods on `ManagerBot`. **NOTE:** `_users_config_path` and `_persist_auth_if_dirty` were already added on `ManagerBot` back in **Task 5 Step 2b** (alongside the PTB-shim rewrites that needed the persist tail). Don't redefine them here — just reference them. The methods below are the user-management surface that depends on them.

```python
    def _load_config_for_users(self):
        """Helper: load the global config (uses _users_config_path from Task 5 Step 2b)."""
        from ..config import load_config
        return load_config(self._users_config_path())

    def _save_config_for_users(self, cfg) -> None:
        from ..config import save_config
        save_config(cfg, self._users_config_path())
        # Refresh our own in-memory allow-list to match.
        self._allowed_users = list(cfg.allowed_users)

    def _format_users_list(self, users) -> str:
        if not users:
            return "No users authorized."
        lines = ["Authorized users:"]
        for u in users:
            locked = f"[identities: {', '.join(u.locked_identities)}]" if u.locked_identities else "[not yet]"
            lines.append(f"  • {u.username} ({u.role}) {locked}")
        return "\n".join(lines)

    async def _require_executor_or_reply(self, ci) -> bool:
        """Common gate for write commands. Auth + executor role enforcement.

        Calling `_auth_identity` may append a first-contact identity to a
        user's locked_identities. The persist helper added in Task 5 Step 2b
        (`_persist_auth_if_dirty`) covers both allow and deny branches via
        the try/finally below.
        """
        try:
            if not self._auth_identity(ci.sender):
                return False
            if not self._require_executor(ci.sender):
                await self._transport.send_text(
                    ci.chat,
                    "Read-only access — only executors can edit the allow-list.",
                    reply_to=ci.message,
                )
                return False
            return True
        finally:
            # Always persist any first-contact lock from the _auth_identity
            # call above. Covers both allow and deny branches.
            await self._persist_auth_if_dirty()

    async def _on_users(self, ci) -> None:
        # /users LIST is viewer-allowed (read-only). But _auth_identity may
        # still append a first-contact lock; persist before returning.
        try:
            if not self._auth_identity(ci.sender):
                return
            cfg = self._load_config_for_users()
            await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)
        finally:
            await self._persist_auth_if_dirty()

    async def _on_add_user(self, ci) -> None:
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /add_user <username> [viewer|executor]", reply_to=ci.message)
            return
        from ..config import AllowedUser
        username = ci.args[0].lstrip("@").lower()
        role = ci.args[1] if len(ci.args) > 1 else "executor"
        if role not in ("viewer", "executor"):
            await self._transport.send_text(
                ci.chat, f"Invalid role {role!r}. Use 'viewer' or 'executor'.", reply_to=ci.message,
            )
            return
        cfg = self._load_config_for_users()
        existing = next((u for u in cfg.allowed_users if u.username == username), None)
        if existing:
            existing.role = role
        else:
            cfg.allowed_users.append(AllowedUser(username=username, role=role))
        self._save_config_for_users(cfg)
        await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)

    async def _on_remove_user(self, ci) -> None:
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /remove_user <username>", reply_to=ci.message)
            return
        username = ci.args[0].lstrip("@").lower()
        cfg = self._load_config_for_users()
        cfg.allowed_users = [u for u in cfg.allowed_users if u.username != username]
        self._save_config_for_users(cfg)
        await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)

    async def _set_role(self, ci, new_role: str) -> None:
        """Shared body for /promote_user and /demote_user.

        `new_role` is the role to assign — "executor" or "viewer". The
        command name for the usage hint is derived from new_role:
            executor → "/promote_user"
            viewer   → "/demote_user"
        (The earlier draft compared `new_role == "promote"`, which always
        fell through to the demote message because callers actually pass
        the role string, not the command name.)
        """
        if not await self._require_executor_or_reply(ci):
            return
        cmd_name = "promote_user" if new_role == "executor" else "demote_user"
        if not ci.args:
            await self._transport.send_text(
                ci.chat, f"Usage: /{cmd_name} <username>", reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        cfg = self._load_config_for_users()
        u = next((x for x in cfg.allowed_users if x.username == username), None)
        if not u:
            await self._transport.send_text(ci.chat, f"User {username!r} not found.", reply_to=ci.message)
            return
        u.role = new_role
        self._save_config_for_users(cfg)
        await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)

    async def _on_promote_user(self, ci) -> None:
        await self._set_role(ci, "executor")

    async def _on_demote_user(self, ci) -> None:
        await self._set_role(ci, "viewer")

    async def _on_reset_user_identity(self, ci) -> None:
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(
                ci.chat,
                "Usage: /reset_user_identity <username> [transport_id]",
                reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        transport_filter = ci.args[1] if len(ci.args) > 1 else None
        cfg = self._load_config_for_users()
        u = next((x for x in cfg.allowed_users if x.username == username), None)
        if not u:
            await self._transport.send_text(ci.chat, f"User {username!r} not found.", reply_to=ci.message)
            return
        if transport_filter:
            u.locked_identities = [
                ident for ident in u.locked_identities
                if not ident.startswith(f"{transport_filter}:")
            ]
        else:
            u.locked_identities = []
        self._save_config_for_users(cfg)
        await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)
```

- [ ] **Step 10: Run tests to verify they pass**

```bash
pytest tests/manager/test_user_commands.py tests/manager/test_bot_plugins.py -v
```
Expected: All tests PASS.

- [ ] **Step 11: Run the full suite**

```bash
pytest -q
```

- [ ] **Step 12: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py \
        tests/manager/test_bot_plugins.py \
        tests/manager/test_user_commands.py
git commit -m "$(cat <<'EOF'
feat(manager): plugin toggle UI + user-management commands

Per-project keyboard gains a Plugins button; toggle screen lists
installed lptc.plugins entry points; tap toggles active/inactive per
project. Restart-required hint shown after toggles.

New user-management commands operating on Config.allowed_users (global):
/users, /add_user <name> [viewer|executor] (default executor),
/remove_user, /promote_user, /demote_user, /reset_user_identity. All persist
via save_config and reply with the updated /users listing.

Implemented via the transport-ported Button/Buttons primitives and
transport.on_command registration.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Docs and final verification

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml` (major version bump — see Step 2)
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update README with a Plugins section**

Read the current `README.md` to find a good insertion point (after the existing feature documentation, before the Manager bot section). Add:

````markdown
## Plugins

Plugins extend the project bot with custom commands, message handlers,
task hooks, button handlers, and Claude prompt context. They are external
Python packages discovered via the `lptc.plugins` entry point group, and
they're **transport-portable**: the same plugin works on Telegram, on the
Web UI, and on any future Discord/Slack/Google Chat transport.

### Activating plugins for a project

Add them to the project's config entry:

```json
{
  "projects": {
    "myproject": {
      "path": "/path/to/project",
      "telegram_bot_token": "...",
      "plugins": [
        {"name": "in-app-web-server"},
        {"name": "diff-reviewer"}
      ]
    }
  }
}
```

Or toggle them in the manager bot: open a project → Plugins → tap a plugin.
Restart the bot after changes.

### Writing a plugin

```python
from link_project_to_chat.plugin import Plugin, BotCommand


class MyPlugin(Plugin):
    name = "my-plugin"
    depends_on = []

    async def start(self):
        ...

    async def stop(self):
        ...

    async def on_message(self, msg):
        # msg is an IncomingMessage — text, sender, chat, files all available
        return False  # True consumes; the agent (Claude/Codex) is skipped

    def get_context(self):
        # Only used when the active backend is Claude; ignored for Codex/Gemini.
        return "Extra system-prompt context"

    def commands(self):
        async def hello(invocation):
            # invocation is a CommandInvocation
            from link_project_to_chat.transport.base import ChatRef
            await self._ctx.transport.send_text(invocation.chat, "hi")
        return [BotCommand(command="hello", description="say hi", handler=hello)]
```

Expose it via your plugin package's `pyproject.toml`:

```toml
[project.entry-points."lptc.plugins"]
my-plugin = "my_package:MyPlugin"
```

### Role-based access

Set `allowed_users` on a project to enable per-user roles:

```json
"allowed_users": [
  {"username": "alice", "role": "executor"},
  {"username": "bob", "role": "viewer"}
]
```

Viewers can use `/tasks`, `/log`, `/status`, `/help`, `/version`, `/skills`
(listing), `/context` (display), and any plugin command flagged `viewer_ok`.
Executors have the full command set. `allowed_users` is the sole auth source —
an empty list means no one is authorized (fail-closed). The first request
from each user atomically appends their identity to `locked_identities` and writes it back to the config so
subsequent requests validate by native ID rather than username (preserves the
username-spoof protection from the pre-v1.0 model).
````

- [ ] **Step 2: Bump version in BOTH pyproject.toml and __init__.py**

In `pyproject.toml`, change `version = "0.16.0"` to `version = "1.0.0"`.

In `src/link_project_to_chat/__init__.py`, change `__version__ = "0.16.0"` to `__version__ = "1.0.0"`.

The two must stay in sync — `pyproject.toml` is the source of truth for installs; `__init__.py` is what the bot reports via `/version`. Mismatched values produce confusing user-visible drift.

Add a regression test so this can't drift again. Append to `tests/test_cli.py`:

```python
def test_version_is_consistent_across_pyproject_and_init():
    """Version string in pyproject.toml must match src/.../__init__.py."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    pyproject_version = pyproject["project"]["version"]

    import link_project_to_chat
    assert link_project_to_chat.__version__ == pyproject_version, (
        f"Version drift: pyproject.toml={pyproject_version!r} vs "
        f"__init__.py={link_project_to_chat.__version__!r}"
    )
```

Rationale for the major bump: this release breaks the `config.json` schema (legacy auth keys removed) and removes deprecated CLI flags after one release. SemVer-major is correct.

- [ ] **Step 3: Update CHANGELOG**

Prepend to `docs/CHANGELOG.md` (read the existing top to match format):

```markdown
## 1.0.0 — 2026-05-14

### BREAKING CHANGES
- **`config.json` schema:** `allowed_usernames`, `trusted_users`, and
  `trusted_user_ids` are removed. The new `allowed_users` field is the sole
  auth source. Legacy fields are read once on load (synthesized into
  `AllowedUser{role="executor"}` entries), then stripped on next save.
  Operators upgrading need to verify the migrated `allowed_users` list before
  exposing the bot to traffic. An empty `allowed_users` list now fails closed
  (every request denied) — pre-v1.0 ambiguous behavior is gone.

### Added
- **Plugin framework** (`plugin.py`) with `Plugin` base, `PluginContext`,
  `BotCommand`, entry-point discovery via `lptc.plugins`. Hooks: `start`,
  `stop`, `on_message`, `on_button`, `on_task_complete`, `on_tool_use`,
  `get_context`. Plugins are transport-portable: same plugin runs on
  Telegram, Web, and future Discord/Slack/Google Chat transports.
- **`Transport.on_stop(callback)`** Protocol method, fired during shutdown
  before the platform tears down. Implemented in TelegramTransport,
  FakeTransport, and WebTransport.
- **`plugin-call <project> <plugin_name> <tool_name> <args_json>`** CLI
  subcommand for invoking plugin tools (used by Claude via Bash).
- **Plugin toggle UI** in the manager bot (per-project, restart-required).
- **`AllowedUser` role model** (`viewer` / `executor`) is the sole auth +
  authority source. Per-user `locked_identities` list — first contact from each transport appends a new entry;
  subsequent requests validate by native ID, not username.
- **CLI flags** `--add-user USER[:ROLE]`, `--remove-user USER`,
  `--reset-user-identity USER` on `configure`. Legacy `--username` /
  `--remove-username` aliased for this release; removed in 1.1.
- **Manager bot user-management commands** `/promote_user`, `/demote_user`,
  `/reset_user_identity` added; `/add_user` accepts an optional `[viewer|executor]`
  role argument (defaults to `executor`).
- **Operational scripts** `scripts/restart.sh` and `scripts/stop.sh` for the
  manager process.

### Notes
- The plugin framework is in this repo; specific plugins (e.g.,
  `in-app-web-server`, `diff-reviewer`) live in a separate
  `link-project-to-chat-plugins` package.
- `get_context()` is Claude-only by design; Codex/Gemini turns ignore it.
```

- [ ] **Step 4: Manual smoke test (run by hand, not pytest)**

Run each by hand against a real bot before considering the merge complete:

  1. **Pre-upgrade migration smoke**: copy a real pre-v1.0 `config.json` containing `allowed_usernames`/`trusted_users`/`trusted_user_ids` to a temp dir. Run `link-project-to-chat --config /tmp/copied/config.json start --project NAME` and immediately stop. Inspect the saved file: legacy keys gone, `allowed_users` populated with executor-role entries. Verify locked IDs are aligned.
  1a. With one `executor` user, no `plugins` configured, start the bot. Send messages, run `/tasks`, run `/model`. Verify identical behavior to pre-merge.
  2. Create a stub plugin in a separate directory:
     ```python
     # stub_plugin/__init__.py
     from link_project_to_chat.plugin import Plugin, BotCommand

     class StubPlugin(Plugin):
         name = "stub"
         async def start(self):
             print("STUB START")
         def commands(self):
             async def h(invocation):
                 await self._ctx.transport.send_text(invocation.chat, "stub OK")
             return [BotCommand(command="stub", description="stub", handler=h)]
     ```
     With `pyproject.toml`:
     ```toml
     [project]
     name = "stub-plugin"
     version = "0.0.1"
     [project.entry-points."lptc.plugins"]
     stub = "stub_plugin:StubPlugin"
     ```
     Install in the same venv: `pip install -e .`
  3. Add `"plugins": [{"name": "stub"}]` to one project. Start the bot. Verify `STUB START` in logs. Send `/stub` in Telegram. Expect reply "stub OK".
  4. Add `"allowed_users": [{"username": "<your-handle>", "role": "viewer"}]` to that same project. Restart. Send `/run echo hi` — expect "Read-only access". Send `/tasks` — expect normal listing.
  5. Change role to `"executor"` for the same user. Restart. Send `/run echo hi` — expect normal execution.
  6. Start the same project with `--transport web --port 8080`. Open `http://localhost:8080/...` in a browser. The same stub plugin should serve its `/stub` command via the browser UI (proves transport portability).

- [ ] **Step 5: Run the full suite once more**

```bash
pytest -q
```
Expected: All tests PASS.

- [ ] **Step 6: Final commit**

```bash
git add README.md pyproject.toml src/link_project_to_chat/__init__.py docs/CHANGELOG.md \
        docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md tests/test_cli.py
git commit -m "$(cat <<'EOF'
docs(release)!: plugin system + AllowedUser-sole auth, v1.0.0

README gains a Plugins section covering activation, transport-portable
plugin authoring, and role-based access. CHANGELOG entry summarizes the
release and the breaking config.json schema change. Version bumped to
1.0.0 in pyproject.toml AND src/link_project_to_chat/__init__.py;
test_version_is_consistent_across_pyproject_and_init prevents future drift.

BREAKING CHANGE: see the 1.0.0 CHANGELOG entry for the auth-model
migration. Operators upgrading must run the bot once under supervision
to verify the migrated allowed_users list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Push and open PR**

```bash
git push -u origin feat/plugin-system
gh pr create --title "Plugin system port + AllowedUser-sole auth (v1.0.0)" --body "$(cat <<'EOF'
## Summary
- Adds transport-portable plugin framework (`plugin.py`) with entry-point discovery, lifecycle hooks, command/button registration, Claude-prompt prepend
- Adds `Transport.on_stop` Protocol hook for clean plugin shutdown
- Adds manager-bot plugin toggle UI and `plugin-call` CLI subcommand
- **BREAKING:** `AllowedUser{username, role, locked_identities}` replaces `allowed_usernames` / `trusted_users` / `trusted_user_ids` as the sole auth source. `locked_identities` is a **list** of `"transport_id:native_id"` strings — same username works across Telegram + Web + future transports. Legacy fields auto-migrate on load and are stripped on next save.
- New CLI flags on `configure`: `--add-user USER[:ROLE]`, `--remove-user`, `--reset-user-identity`. Legacy `--username` / `--remove-username` aliased one release.
- New manager commands: `/promote_user`, `/demote_user`, `/reset_user_identity`; `/add_user` takes an optional role argument.
- Adds operational scripts (`restart.sh`, `stop.sh`)
- Bumps version to 1.0.0

Design doc: `docs/superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md`
Implementation plan: `docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md`

## Test plan
- [x] `pytest -q` green on every commit
- [ ] **Migration smoke**: pre-v1.0 config.json with all three legacy fields → loads, saves, legacy keys stripped, locked IDs aligned (see Task 7 Step 4 Item 1)
- [ ] Manual smoke test with stub plugin (see Task 7 Step 4 of the plan)
- [ ] Viewer/executor manual verification (see Task 7 Step 4 of the plan)
- [ ] Stub plugin works on `--transport web` (proves transport portability)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Verification gates

After every task:

1. `pytest -q` green. Baseline is whatever Task 0 recorded after `pip install -e ".[all]" && pytest -q`; tasks add new tests so the count grows monotonically.
2. New tests for the task pass.
3. No source files deleted EXCEPT for the legacy-field removal in Task 5 Step 12 (where `allowed_usernames` / `trusted_users` / `trusted_user_ids` are removed from `ProjectConfig` and `Config`).
4. If you observe a count below the baseline, first verify the venv: `.venv/bin/pip install -e ".[all]"` then re-run pytest. A stale editable install (e.g., leftover from a deleted worktree) shows as `0 passed` with 60+ collection errors — that's a venv problem, not a regression.

If a gate fails, **STOP** and reconcile before continuing.

## Out-of-scope reminders

Not in this plan; don't add without a new spec:
- Wire-compatibility with GitLab plugin packages written against `python-telegram-bot` directly. Plugin authors must use the transport-agnostic API.
- Migrating the primary fork's existing features (team_relay, livestream, personas, skills, voice) to the role model.
- Backend-aware plugin behavior beyond `get_context()` being Claude-only.
- Building any specific plugin (those live in the external `link-project-to-chat-plugins` package).
