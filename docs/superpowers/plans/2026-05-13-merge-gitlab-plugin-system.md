# Plugin system port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the GitLab fork's plugin design onto the Transport+Backend architecture. Plugins become transport-portable (one plugin works on Telegram, Web, future Discord/Slack). Add an `AllowedUser{username, role, locked_identity}` model that **replaces** the existing `allowed_usernames` / `trusted_users` / `trusted_user_ids` flat fields as the single source of auth + authority. `locked_identity` is a `"transport_id:native_id"` string so the lock works for every transport, not just numeric Telegram IDs. Legacy configs migrate one-way on load; legacy keys are stripped on next save.

**Architecture:** Plugins are external Python packages discovered via `lptc.plugins` entry points. `Plugin` base class with transport-agnostic handler signatures (`CommandInvocation`, `IncomingMessage`, `ButtonClick`). Lifecycle wired through `ProjectBot._after_ready` and an `on_stop` Transport callback. Auth + role enforcement is identity-keyed: `_auth_identity`, `_require_executor`, and `_get_user_role` read `_allowed_users` exclusively and compare `locked_identity` against `_identity_key(identity)`. Legacy fields stay on the dataclasses through Tasks 3–4 as read-only inputs; Task 5 rewrites every call site to use `resolve_project_allowed_users(project, config)` and then removes the legacy fields from the dataclasses. **This is a breaking on-disk config change.**

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

Record the actual passing count (latest verified: **1003 passed, 5 skipped** as of `7fd934e` on `main`). Use this number as the regression gate for the rest of the plan — every commit must keep it green.

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
    """
    bot_name: str
    project_path: Path
    bot_username: str = ""
    data_dir: Path | None = None

    backend_name: str = "claude"
    transport: "Transport | None" = field(default=None, repr=False)

    # Identity strings: "transport_id:native_id" (e.g. "telegram:12345",
    # "discord:abc-snowflake", "web:session-token"). Transport-portable.
    # Replaces the GitLab `allowed_user_ids: list[int]` design.
    allowed_identities: list[str] = field(default_factory=list)
    executor_identities: list[str] = field(default_factory=list)

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
        if they care about role — check `_identity_key(msg.sender)` against
        `self._ctx.executor_identities`. Return True to consume (skip backend);
        False to let the primary path proceed.
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
Expected: All tests PASS (1003 + new ones).

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
```

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

    def _wrap_plugin_command(self, bc):
        """Wrap a plugin command handler with auth + role gating."""
        from functools import wraps
        handler = bc.handler
        viewer_ok = bc.viewer_ok

        @wraps(handler)
        async def _wrapped(invocation):
            # Defense-in-depth: transport's set_authorizer already gated, but
            # cheap and avoids drift if a plugin re-registers from elsewhere.
            if not self._auth_identity(invocation.sender):
                return
            if not viewer_ok and not self._require_executor(invocation.sender):
                assert self._transport is not None
                await self._transport.send_text(
                    invocation.chat,
                    "Read-only access — your role is viewer.",
                    reply_to=invocation.message,
                )
                return
            await handler(invocation)

        return _wrapped

    async def _init_plugins(self) -> None:
        """Instantiate, register, and start plugins. Called from _after_ready."""
        if not self._plugin_configs or self._transport is None:
            return
        self._shared_ctx = PluginContext(
            bot_name=self.name,
            project_path=self.path,
            bot_username=self.bot_username,
            data_dir=Path.home() / ".link-project-to-chat" / "meta" / self.name,
            backend_name=self._backend_name,
            transport=self._transport,
            # Build identity-string lists from _allowed_users (locked_identity is
            # populated on first contact). Transport-portable: works for telegram:
            # web:, future discord:/slack: prefixes alike.
            allowed_identities=[u.locked_identity for u in self._allowed_users if u.locked_identity is not None],
            executor_identities=[u.locked_identity for u in self._allowed_users
                                 if u.role == "executor" and u.locked_identity is not None],
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

        # Register each plugin's commands on the transport.
        for plugin in self._plugins:
            try:
                cmds = plugin.commands()
            except Exception:
                logger.warning("plugin %s commands() failed; skipping plugin", plugin.name, exc_info=True)
                continue
            for bc in cmds:
                wrapped = self._wrap_plugin_command(bc)
                self._transport.on_command(bc.command, wrapped)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(bc.command)

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
            plugins=proj.plugins or None,
            allowed_users=proj.allowed_users or None,
```

(`ProjectConfig.plugins` and `ProjectConfig.allowed_users` are added in Task 3 — this line will be live once Task 3 lands.)

- [ ] **Step 15: Run tests to verify they pass**

```bash
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: All tests PASS.

- [ ] **Step 16: Run the full suite for regressions**

```bash
pytest -q
```
Expected: Pre-existing 1003 + new test count all PASS. If anything else breaks, the most likely cause is a hook placement issue. Re-read the surrounding code.

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

## Task 3: Config schema — `plugins`, `AllowedUser` (with `locked_identity`), `resolve_project_allowed_users` helper, transitional legacy fields

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

We add a one-way migration on load that sets `Config.migration_pending` for the CLI to act on, plus a new helper `resolve_project_allowed_users(project, config) -> list[AllowedUser]` that callers use instead of touching the legacy fields directly.

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
    assert u.locked_identity is None


def test_project_config_has_plugins_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.plugins == []


def test_project_config_has_allowed_users_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.allowed_users == []


def test_legacy_fields_are_not_dataclass_attributes():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    # The legacy fields are deleted from the dataclass. They must not
    # be settable except via the migration path on load.
    assert not hasattr(p, "allowed_usernames")
    assert not hasattr(p, "trusted_users")
    assert not hasattr(p, "trusted_user_ids")


def test_save_load_roundtrip(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["myp"] = ProjectConfig(
        path="/tmp/p",
        telegram_bot_token="t",
        allowed_users=[
            AllowedUser(username="alice", role="executor", locked_identity="telegram:12345"),
            AllowedUser(username="bob", role="viewer"),
        ],
        plugins=[{"name": "in-app-web-server"}, {"name": "diff", "option": 1}],
    )
    save_config(cfg, cfg_file)
    loaded = load_config(cfg_file)
    p = loaded.projects["myp"]
    assert {(u.username, u.role, u.locked_identity) for u in p.allowed_users} == {
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
    assert p.allowed_users == [AllowedUser(username="x", role="viewer", locked_identity=None)]


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
        AllowedUser(username="good", role="viewer", locked_identity=None),
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
    assert by_user["alice"].role == "executor" and by_user["alice"].locked_identity == "telegram:12345"
    assert by_user["bob"].role == "executor" and by_user["bob"].locked_identity is None
    assert by_user["carol"].role == "executor" and by_user["carol"].locked_identity is None


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
    assert by_user["alice"].locked_identity == "telegram:12345"
    assert by_user["bob"].locked_identity == "telegram:67890"


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
    assert by_user["alice"].locked_identity == "telegram:12345"
    assert by_user["bob"].locked_identity == "telegram:67890"


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
        AllowedUser(username="admin", role="executor", locked_identity="telegram:99999"),
    ]
    # Project has empty allowed_users (and that's fine — it'll fall back to
    # Config.allowed_users in some paths, or warn at startup).
    assert loaded.projects["p"].allowed_users == []
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in written
    assert "trusted_users" not in written
    assert written["allowed_users"] == [
        {"username": "admin", "role": "executor", "locked_identity": "telegram:99999"},
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
    assert by_user["bob"].locked_identity == "telegram:67890"


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
    assert p.allowed_users == [AllowedUser(username="alice", role="executor", locked_identity=None)]


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
        {"username": "alice", "role": "executor", "locked_identity": "telegram:12345"},
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
        {"username": "alice", "role": "executor", "locked_identity": "telegram:12345"},
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
    locked_identity: str | None = None
    # Platform-portable identity lock: "transport_id:native_id" string
    # populated on first contact. Works for numeric Telegram IDs
    # ("telegram:12345"), Discord snowflakes ("discord:abc..."), and Web
    # session tokens ("web:..."). Replaces the int-only ID locking from the
    # legacy design — see spec Components > config.py.


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
        locked_identity = entry.get("locked_identity")
        if locked_identity is not None and not isinstance(locked_identity, str):
            logger.warning(
                "malformed locked_identity for %s (not a string): %r; dropping",
                username, locked_identity,
            )
            locked_identity = None
        out.append(AllowedUser(
            username=str(username).lstrip("@").lower(),
            role=role,
            locked_identity=locked_identity,
        ))
    return out


def _serialize_allowed_users(users: list[AllowedUser]) -> list[dict]:
    out = []
    for u in users:
        entry: dict = {"username": u.username, "role": u.role}
        if u.locked_identity is not None:
            entry["locked_identity"] = u.locked_identity
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

    # Build a username → locked_identity map ("telegram:<id>" strings).
    # Legacy fields predate multi-transport support, so every legacy ID belongs
    # to Telegram; we prefix with "telegram:" so the locked_identity is
    # immediately usable by the new identity-keyed auth comparison.
    identity_for: dict[str, str] = {}
    legacy_trusted_names: list[str] = []
    if isinstance(raw_trusted, dict):
        # Current on-disk shape: username → user_id (int or str).
        for uname, uid in raw_trusted.items():
            norm = _norm(uname)
            legacy_trusted_names.append(norm)
            if uid is None:
                continue
            # Preserve non-numeric IDs too (Discord/Slack scenarios that may
            # have been entered manually). We assume telegram unless the ID
            # already contains a ":" (caller-prefixed).
            uid_str = str(uid)
            if ":" in uid_str:
                identity_for[norm] = uid_str
            else:
                try:
                    identity_for[norm] = f"telegram:{int(uid_str)}"
                except (TypeError, ValueError):
                    # Non-numeric ID without prefix — assume telegram.
                    identity_for[norm] = f"telegram:{uid_str}"
    elif isinstance(raw_trusted, list):
        # Pre-A1 shape: list of usernames aligned with trusted_user_ids by index.
        legacy_trusted_names = [_norm(n) for n in raw_trusted]
        if len(legacy_trusted_names) == len(legacy_ids):
            identity_for = {
                name: f"telegram:{int(uid)}"
                for name, uid in zip(legacy_trusted_names, legacy_ids)
            }
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
            identity_for = {
                name: f"telegram:{int(uid)}"
                for name, uid in zip(norm_allowed, legacy_ids)
            }
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
            locked_identity=identity_for.get(norm),
        ))
    logger.info(
        "migrated legacy auth fields → %d AllowedUser entries (%d with locked identities)",
        len(out), sum(1 for u in out if u.locked_identity is not None),
    )
    return out, True


def resolve_project_allowed_users(project, config) -> list[AllowedUser]:
    """Project allow-list with global fallback.

    Returns the project's allowed_users if non-empty; otherwise returns the
    global Config.allowed_users. Matches the precedence of the existing
    `resolve_project_auth_scope` (project overrides global, falls back to
    global) so deployments where the project list is empty don't suddenly
    fail-closed when only the global list is populated.

    Callers in bot.py / cli.py / manager/bot.py use this helper instead of
    reading project.allowed_usernames / project.trusted_users directly.
    """
    if project.allowed_users:
        return project.allowed_users
    return config.allowed_users
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

- [ ] **Step 8: Audit other call sites in `config.py`**

```bash
grep -n "allowed_usernames\|trusted_users\|trusted_user_ids" src/link_project_to_chat/config.py
```

Every remaining reference outside `_migrate_legacy_auth` is a bug — read the file and either delete the code path or rewrite to read `allowed_users`.

- [ ] **Step 9: Run target tests**

```bash
pytest tests/test_config_allowed_users.py tests/test_config_migration.py -v
```
Expected: All PASS.

- [ ] **Step 10: Update existing config tests for the field removal**

```bash
grep -rln "allowed_usernames\|trusted_users\|trusted_user_ids" tests/
```

Each file gets read and rewritten to use `allowed_users` directly. **Many tests will fail until this is done.** Expected scope: `tests/test_config.py`, `tests/test_auth*.py`, `tests/manager/test_bot*.py`, `tests/test_bot_team_wiring.py`. Estimate ~30 tests touched.

- [ ] **Step 11: Run the full suite**

```bash
pytest -q
```
Expected: green. If anything in `tests/manager/` or `tests/test_bot_*` references legacy fields, fix it (these are existing tests that must adapt to the new schema).

- [ ] **Step 12: Commit**

```bash
git add src/link_project_to_chat/config.py \
        tests/test_config_allowed_users.py \
        tests/test_config_migration.py \
        tests/  # for the existing-test updates
git commit -m "$(cat <<'EOF'
feat(config)!: AllowedUser replaces allowed_usernames/trusted_users/trusted_user_ids

ProjectConfig and Config (global) drop the three legacy auth fields.
AllowedUser{username, role, locked_identity} becomes the sole on-disk
shape (locked_identity is a "transport_id:native_id" string so the lock
works for every transport, not only numeric Telegram IDs). Legacy `trusted_users` dict (current format) maps username → id;
legacy list form aligned with trusted_user_ids by index is also
supported. Loader sets Config.migration_pending = True when legacy
fields were read; the CLI's start command saves once to materialize the
new shape on disk. TeamBotConfig is unchanged — team bots continue to
inherit from Config.allowed_users. Unknown roles fall back to viewer;
malformed entries log warnings and are skipped; empty allowed_users
after migration logs WARNING (CRITICAL aggregation happens in CLI start).

BREAKING CHANGE: config.json schema. Operators upgrading need to verify
the resulting allowed_users list and run the bot under supervision on
first start.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI — `plugin-call` + `migrate-config` + `--add-user`/`--remove-user`/`--reset-user-identity` on `configure`

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/test_cli.py` (append)

`run_bot` / `run_bots` already accept `plugins` after Task 2; `proj.plugins` is read in `run_bots` via `proj.plugins or None`. Once Task 3 lands, the chain is live without further CLI changes. This task adds three CLI surfaces:

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
        {"username": "alice", "role": "executor", "locked_identity": "telegram:12345"},
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
        locked = f" [locked_identity={u.locked_identity}]" if u.locked_identity is not None else ""
        click.echo(f"  - {u.username} ({u.role}){locked}")

    empty_projects: list[str] = []
    for name, proj in config.projects.items():
        if project_filter and name != project_filter:
            continue
        click.echo(f"\nProject {name!r}: {len(proj.allowed_users)} users")
        for u in proj.allowed_users:
            locked = f" [locked_identity={u.locked_identity}]" if u.locked_identity is not None else ""
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
@click.option("--reset-user-identity", "reset_user_identity", default=None, help="Clear the locked_identity for a user (re-locks on next contact).")
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
        norm = reset_user_identity.lstrip("@").lower()
        u = _find(norm)
        if not u:
            raise SystemExit(f"User {norm!r} not in allow-list.")
        u.locked_identity = None
        save_config(config, cfg_path)
        click.echo(f"Cleared locked_identity for {norm}.")
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
    # resolve_project_allowed_users falls back to the global list, so a project
    # with empty allowed_users is only a problem when the global is also empty.
    empty: list[str] = []
    for name, proj in config.projects.items():
        if not resolve_project_allowed_users(proj, config):
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

The `run_bot` / `run_bots` plumbing (added in Task 2) currently passes `proj.plugins or None` and `proj.allowed_users or None` to `run_bot`. **Update both call sites to use the fallback helper** so the project picks up the global allow-list when its own is empty:

```python
# In run_bots, before the existing run_bot call:
        effective_allowed = resolve_project_allowed_users(proj, config)
        run_bot(
            ...,
            allowed_users=effective_allowed or None,
            plugins=proj.plugins or None,
        )
```

Add the same wiring in any ad-hoc `--path`/`--token` `run_bot` call paths so they end up with the global list when no per-project override exists.

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
git add src/link_project_to_chat/cli.py tests/test_cli.py
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

`start` honors Config.migration_pending: forces a save before serving
traffic so the on-disk migration is deterministic. Empty-allow-list
projects are aggregated into a single CRITICAL log line at startup
(replaces per-load log spam).

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

`AuthMixin` is rewritten around `_allowed_users` as the sole source of truth. The legacy `_allowed_usernames` / `_trusted_user_ids` instance state is deleted. ID-locking moves from a separate `trusted_user_ids` list to `AllowedUser.locked_identity` (a `"transport_id:native_id"` string), populated on first contact via `_identity_key(identity)`.

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


def test_first_contact_locks_user_id():
    """First contact by username writes back locked_identity atomically."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    ident = _identity("alice", native_id="98765")
    bot._auth_identity(ident)  # First contact
    assert au.locked_identity == "telegram:98765"


def test_locked_identity_takes_precedence_over_username():
    """After identity is locked, validation goes through identity, not username."""
    au = AllowedUser(username="alice", role="executor", locked_identity="telegram:98765")
    bot = _BotWithRoles(allowed_users=[au])
    # Attacker renames themselves to "alice" but their native_id is different.
    ident = _identity("alice", native_id="11111")
    assert bot._auth_identity(ident) is False
    # The real alice still works (her identity matches even with a renamed handle).
    ident_real = _identity("anything-else", native_id="98765")
    assert bot._auth_identity(ident_real) is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: FAIL — `_get_user_role` / `_require_executor` / new `_auth_identity` semantics not defined.

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
    _MAX_MESSAGES_PER_MINUTE: int = 30
    _MAX_FAILED_AUTH: int = 5

    def _init_auth(self) -> None:
        # Both dicts keyed on `_identity_key(identity)` = "transport_id:native_id"
        # so Discord/Slack/Telegram identities never collide.
        self._rate_limits: dict[str, collections.deque] = {}
        self._failed_auth_counts: dict[str, int] = {}
        # First-contact lock writes back locked_identity in-memory; this flag
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
          1. Identity-lock fast path: a user with `locked_identity == _identity_key(identity)`.
             Security-critical — prevents username-spoof attacks. Works for every
             transport since the key is `transport_id:native_id`.
          2. Username fallback: case- and @-insensitive match against an entry
             with `locked_identity is None`. On match, write back
             `locked_identity = _identity_key(identity)` and set
             `self._auth_dirty = True` so the message-handling tail persists
             via save_config.
        """
        if not self._allowed_users:
            return None
        ident_key = self._identity_key(identity)
        # 1. Identity-lock fast path. Works for every transport.
        for au in self._allowed_users:
            if au.locked_identity == ident_key:
                return au.role
        # 2. Username fallback (only if no identity is locked for that user yet).
        uname = self._normalize_username(getattr(identity, "handle", ""))
        if not uname:
            return None
        for au in self._allowed_users:
            if au.locked_identity is not None:
                # Locked to a different identity; username match doesn't help.
                continue
            if self._normalize_username(au.username) == uname:
                au.locked_identity = ident_key
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
    """First contact by username writes back locked_identity AND sets _auth_dirty."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    assert bot._auth_dirty is False
    bot._auth_identity(_identity("alice", native_id="98765"))
    assert au.locked_identity == 98765
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
    """When locked_identity is already set, no first-contact write happens."""
    au = AllowedUser(username="alice", role="executor", locked_identity="telegram:98765")
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
        """
        if not self._auth_dirty:
            return
        from .config import save_config
        cfg_path = self._effective_config_path()
        try:
            # Reload-modify-save would race with other writers; instead we save
            # the in-memory state and rely on the existing _config_lock for
            # serialization. The in-memory state already reflects the lock
            # write, so save_config's per-project merge will preserve it.
            from .config import load_config
            disk = load_config(cfg_path)
            # Merge our updated allowed_users back into the loaded shape so
            # we don't clobber concurrent edits to other projects.
            if self.team_name:
                # Team bot: write to Config.allowed_users (global) which is
                # what _allowed_users mirrors for team bots.
                disk.allowed_users = list(self._allowed_users)
            elif self.name in disk.projects:
                disk.projects[self.name].allowed_users = list(self._allowed_users)
            save_config(disk, cfg_path)
            self._auth_dirty = False
        except Exception:
            logger.exception("Failed to persist auth state; will retry on next message")
```

In `_on_text` (line 1003), at the very end of the body (after `task_manager.submit_agent(...)`), add:

```python
        await self._persist_auth_if_dirty()
```

Mirror in `_on_run` (line 1100), `_on_file_from_transport` (line 2302), and the command-handler bodies after `_guard_executor` returns True. The simplest pattern: extend `_guard_executor` itself to call `_persist_auth_if_dirty` before returning True. That covers every state-changing handler in one place:

```python
    async def _guard_executor(self, ci_or_msg) -> bool:
        sender = getattr(ci_or_msg, "sender", None)
        if sender is None:
            return False
        if self._require_executor(sender):
            await self._persist_auth_if_dirty()  # NEW
            return True
        assert self._transport is not None
        await self._transport.send_text(
            ci_or_msg.chat,
            "Read-only access — your role is viewer.",
            reply_to=getattr(ci_or_msg, "message", None),
        )
        return False
```

Read-only commands that don't run `_guard_executor` (like `/status`, `/tasks`) also need to persist — add `await self._persist_auth_if_dirty()` at the end of `_on_text_from_transport` for the `auth_identity` branch that doesn't go through `_guard_executor`.

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
        """
        sender = getattr(ci_or_msg, "sender", None)
        if sender is None:
            return False
        if self._require_executor(sender):
            return True
        assert self._transport is not None
        await self._transport.send_text(
            ci_or_msg.chat,
            "Read-only access — your role is viewer.",
            reply_to=getattr(ci_or_msg, "message", None),
        )
        return False
```

- [ ] **Step 7: Edit `bot.py` — gate state-changing command handlers**

State-changing handlers to gate (all currently auth'd via `_auth_identity` at the top of their bodies — locate each by name and add the guard immediately after the auth check):

`_on_run`, `_on_backend`, `_on_model`, `_on_effort`, `_on_thinking`, `_on_context` (when toggling, not displaying), `_on_permissions`, `_on_compact`, `_on_reset`, `_on_persona`, `_on_stop_persona`, `_on_create_persona`, `_on_delete_persona`, `_on_use` (skill activate path) / `_on_skills` activation branch, `_on_stop_skill`, `_on_create_skill`, `_on_delete_skill`, `_on_lang`, `_on_halt`, `_on_resume`, `_on_file_from_transport`, `_on_voice_from_transport`.

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

In [bot.py](src/link_project_to_chat/bot.py) `_on_button` (line 1960), the branch chain handles many state-changing button values. Each one needs a role gate. The known state-changing prefixes:

- `model_set_*` (line 1969) — changes the active model.
- `effort_set_*` (line 1989) — changes effort level.
- `thinking_set_*` (around `thinking_set_on`/`thinking_set_off`) — toggles thinking livestream.
- `permissions_set_*` (around line 1360 generator + handler) — changes permission mode.
- `backend_set_*` (line 1981) — switches the active backend.
- `reset_confirm` / `reset_cancel` (line 1487-1488) — session reset confirmation.
- `task_cancel_*` (find in `_tasks_buttons` or related) — cancels a running task.
- `lang_set_*` (search) — switches voice-message language.
- Any future `*_set_*` or destructive-action prefix should be added as it lands.

For each state-changing branch, wrap the body with the guard. Example pattern:

```python
        if value.startswith("model_set_"):
            if not await self._guard_executor(click):
                return
            # ... existing branch body unchanged ...
```

Read-only branches stay untouched:
- `ask_*` (answers to AskUserQuestion; the answer itself is the user's reply — viewers shouldn't be able to push a Claude turn, so even `ask_*` should gate if the user role is viewer. Add the gate here too.)
- `tasks_show_log_*` (display only).
- Any plugin-registered buttons — those flow through `_dispatch_plugin_button` and the plugin is responsible for its own gating per the spec's viewer policy.

After the audit, list every gated branch in the commit message so reviewers can verify nothing was missed. The test in Step 8 below adds parametrized coverage.

Add a test case to `tests/test_auth_roles.py`:

```python
@pytest.mark.asyncio
async def test_state_changing_button_blocked_for_viewer():
    """A viewer clicking a state-changing button (model_set_*) gets a Read-only reply."""
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    bot = ProjectBot.__new__(ProjectBot)
    bot._allowed_users = [AllowedUser(username="viewer-user", role="viewer", locked_identity="telegram:42")]
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()

    viewer = Identity(transport_id="telegram", native_id="42", display_name="V", handle="viewer-user", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=viewer, value="model_set_haiku")

    # Wire up just enough to call _on_button.
    # ... call the real _on_button and assert the transport.send_text was awaited with "Read-only access"
```

(Implementation details: this test needs more wiring than the unit tests above; if it's too involved for a unit test, move it into `tests/test_auth_migration_e2e.py` as a real end-to-end click via FakeTransport.)

- [ ] **Step 8: Update existing auth tests for the field removal**

```bash
grep -rln "_allowed_usernames\|_trusted_user_ids\|allowed_usernames\|trusted_user_ids" tests/
```

Each match is a test referencing the legacy auth model. Rewrite to construct `AllowedUser` entries with `role="executor"` (the migration default — preserves equivalent legacy semantics). Don't delete tests; convert them. **Expected: tests pass with no behavioral change because executor-only allowed_users matches the legacy "everyone allowed has full access" behavior.**

- [ ] **Step 9: Pipe `allowed_users` through `run_bots`**

`run_bots` already passes `allowed_users=proj.allowed_users or None` (added in Task 2 Step 14). After Task 3 lands, `proj.allowed_users` exists — verify by running the suite.

- [ ] **Step 10: Add end-to-end migration integration test**

Create `tests/test_auth_migration_e2e.py`:

```python
"""End-to-end auth migration through ProjectBot + FakeTransport.

Covers the full path: legacy config.json → load_config → ProjectBot.build()
with FakeTransport → first message lands → _auth_dirty triggers save →
on-disk file shows the populated locked_identity.
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
    assert by_user["alice"]["locked_identity"] == 12345
    assert "locked_identity" not in by_user["bob"]  # not locked yet

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

    # 4. On-disk file now shows bob with a populated locked_identity.
    on_disk_after = json.loads(cfg_file.read_text())
    users_after = on_disk_after["projects"]["p"]["allowed_users"]
    by_user_after = {u["username"]: u for u in users_after}
    assert by_user_after["bob"]["locked_identity"] == 67890

    # 5. Second contact by bob: no extra save.
    msg_count_before = len([f for f in tmp_path.iterdir() if f.is_file()])
    bot._auth_identity(bob_identity)
    await bot._persist_auth_if_dirty()
    assert bot._auth_dirty is False
    msg_count_after = len([f for f in tmp_path.iterdir() if f.is_file()])
    assert msg_count_before == msg_count_after


@pytest.mark.asyncio
async def test_e2e_username_spoof_blocked_after_lock(tmp_path: Path):
    """After a user's ID is locked, an attacker with the same username but a
    different native_id is rejected."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor", "locked_identity": "telegram:12345"},
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

    # Attacker: same username "alice", different native_id.
    attacker = Identity(
        transport_id="fake", native_id="11111",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(attacker) is False

    # Real alice still works.
    real = Identity(
        transport_id="fake", native_id="12345",
        display_name="Anyone", handle="not-the-real-alice", is_bot=False,
    )
    assert bot._auth_identity(real) is True
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
- `add_trusted_user_id`, `add_project_trusted_user_id`, `bind_project_trusted_user` (in `config.py`) — rewrite to operate on `AllowedUser.locked_identity` or delete if `_persist_auth_if_dirty` covers them.

After this step, **no source file outside `config.py`'s migration helper reads `allowed_usernames` / `trusted_users` / `trusted_user_ids`**. Verify by re-running the grep — only `_migrate_legacy_auth` and its tests should match.

- [ ] **Step 12: Remove legacy fields from `ProjectConfig` and `Config`**

In [src/link_project_to_chat/config.py](src/link_project_to_chat/config.py), now that no caller reads them:

- Delete `allowed_usernames`, `trusted_users`, `trusted_user_ids` from `ProjectConfig` (line ~45-47).
- Delete the same three from `Config` (line ~97-99).
- Remove any helpers that only existed to read them: `_migrate_usernames`, `_migrate_user_ids`, `_effective_trusted_users`, `_migrate_trusted_users`, `_write_raw_trusted_users` (audit case-by-case; some may still be used by the migration helper).
- The loader's `_migrate_legacy_auth` is the ONLY place that reads the legacy keys from the raw JSON dict; it doesn't touch the dataclass for those fields.

Update tests that referenced the legacy fields on the dataclass (they'd been kept as no-ops during Task 3; now they need to construct `AllowedUser` entries directly).

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
        tests/test_auth_roles.py tests/test_auth_migration_e2e.py \
        tests/  # for existing-test updates
git commit -m "$(cat <<'EOF'
feat(auth)!: AllowedUser is sole auth source; legacy fields removed

_auth_identity, _require_executor, and _get_user_role all read
self._allowed_users exclusively. Empty allowed_users fails closed (no
legacy allow-all path). Identity-locking via AllowedUser.locked_identity
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

State-changing command handlers AND state-changing button branches
(model_set_*, effort_set_*, thinking_set_*, permissions_set_*,
backend_set_*, reset_confirm/cancel, task_cancel_*, lang_set_*, ask_*)
gate via _guard_executor; viewers see 'Read-only access' replies.

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
   - `/reset_user_identityentity <username>` — clear `locked_identity` (recovery path).

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
    bot._allowed_users = [AllowedUser(username="admin", role="executor", locked_identity="telegram:1")]
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
    cfg.allowed_users = list(users or [AllowedUser(username="admin", role="executor", locked_identity="telegram:1")])
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
        AllowedUser(username="alice", role="executor", locked_identity="telegram:12345"),
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
        AllowedUser(username="alice", role="executor", locked_identity="telegram:12345"),
    ])
    await bot._on_reset_user_identity(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert cfg.allowed_users[0].locked_identity is None


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
        AllowedUser(username="viewer-admin", role="viewer", locked_identity="telegram:99"),
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
        AllowedUser(username="viewer-bob", role="viewer", locked_identity="telegram:200"),
    ])
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_users(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "alice" in text
    assert "viewer-bob" in text
```

- [ ] **Step 8: Run tests — expect failure**

```bash
pytest tests/manager/test_user_commands.py -v
```
Expected: FAIL — handlers don't exist yet.

- [ ] **Step 9: Implement user-management commands**

In `src/link_project_to_chat/manager/bot.py`, add the handlers. Register them via `transport.on_command(...)` in the existing command registration block (find where other manager commands like `/projects` are registered).

```python
# Register near the other transport.on_command calls in ManagerBot.build()
# (or whatever the manager bot's setup method is named):
self._transport.on_command("users", self._on_users)
self._transport.on_command("add_user", self._on_add_user)
self._transport.on_command("remove_user", self._on_remove_user)
self._transport.on_command("promote_user", self._on_promote_user)
self._transport.on_command("demote_user", self._on_demote_user)
self._transport.on_command("reset_user_identity", self._on_reset_user_identity)
# NOTE: these registrations work BECAUSE Task 1 fixed TelegramTransport.on_command
# to also register PTB CommandHandler when called post-routing.
```

Add the handler methods on `ManagerBot`:

```python
    def _load_config_for_users(self):
        """Helper: load the global config (cached path)."""
        from ..config import load_config
        return load_config(self._project_config_path)

    def _save_config_for_users(self, cfg) -> None:
        from ..config import save_config
        save_config(cfg, self._project_config_path)
        # Refresh our own in-memory allow-list to match.
        self._allowed_users = list(cfg.allowed_users)

    def _format_users_list(self, users) -> str:
        if not users:
            return "No users authorized."
        lines = ["Authorized users:"]
        for u in users:
            locked = f"[ID locked: {u.locked_identity}]" if u.locked_identity is not None else "[not yet]"
            lines.append(f"  • {u.username} ({u.role}) {locked}")
        return "\n".join(lines)

    async def _require_executor_or_reply(self, ci) -> bool:
        """Common gate for write commands. Auth + executor role enforcement."""
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

    async def _on_users(self, ci) -> None:
        # /users LIST is viewer-allowed (read-only).
        if not self._auth_identity(ci.sender):
            return
        cfg = self._load_config_for_users()
        await self._transport.send_text(ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message)

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
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(
                ci.chat, f"Usage: /{new_role}_user <username>" if new_role == "promote" else "Usage: /demote_user <username>",
                reply_to=ci.message,
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
            await self._transport.send_text(ci.chat, "Usage: /reset_user_identity <username>", reply_to=ci.message)
            return
        username = ci.args[0].lstrip("@").lower()
        cfg = self._load_config_for_users()
        u = next((x for x in cfg.allowed_users if x.username == username), None)
        if not u:
            await self._transport.send_text(ci.chat, f"User {username!r} not found.", reply_to=ci.message)
            return
        u.locked_identity = None
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
from each user atomically writes their `locked_identity` back to the config so
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
  authority source. Per-user `locked_identity` populated on first contact;
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
- **BREAKING:** `AllowedUser{username, role, locked_identity}` replaces `allowed_usernames` / `trusted_users` / `trusted_user_ids` as the sole auth source. `locked_identity` is a `"transport_id:native_id"` string — works for every transport. Legacy fields auto-migrate on load and are stripped on next save.
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

1. `pytest -q` green (`1003 passed, 5 skipped` baseline on `main` at `7fd934e`; tasks add new tests so the count grows monotonically).
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
