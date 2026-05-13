# Plugin system port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the GitLab fork's plugin design onto the Transport+Backend architecture. Plugins become transport-portable (one plugin works on Telegram, Web, future Discord/Slack). Add an optional `AllowedUser` viewer/executor role model alongside the existing flat allow-list.

**Architecture:** Plugins are external Python packages discovered via `lptc.plugins` entry points. `Plugin` base class with transport-agnostic handler signatures (`CommandInvocation`, `IncomingMessage`, `ButtonClick`). Lifecycle wired through `ProjectBot._after_ready` and an `on_stop` Transport callback. Role enforcement is `Identity`-keyed and layers on top of the existing `_auth_identity` flow.

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
pytest -q
```
Expected: **1003 passed, 5 skipped** (or whatever the current count on `main` is). If anything fails, **STOP** and ask before proceeding.

---

## Task 1: Add `plugin.py` framework + operational scripts + `on_stop` Transport hook

**Files:**
- Create: `src/link_project_to_chat/plugin.py`
- Create: `scripts/restart.sh`
- Create: `scripts/stop.sh`
- Create: `tests/test_plugin_framework.py`
- Modify: `src/link_project_to_chat/transport/base.py` (add `on_stop` to Protocol)
- Modify: `src/link_project_to_chat/transport/telegram.py` (fire on_stop callbacks in `post_stop`)
- Modify: `src/link_project_to_chat/transport/fake.py` (fire on_stop callbacks in `stop`)
- Modify: `src/link_project_to_chat/web/transport.py` (fire on_stop callbacks during shutdown)

The Transport gains a tiny new hook so plugins can stop cleanly before the transport tears down — parallels `on_ready`. Implementing it across all 3 transports keeps the Protocol uniform.

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

    trusted_user_id: int | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    executor_user_ids: list[int] = field(default_factory=list)

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
        """Per-plugin persistent storage: <meta_dir>/<bot_name>/plugins/<plugin_name>/"""
        base = self._ctx.data_dir or (Path.home() / ".link-project-to-chat" / "meta" / self._ctx.bot_name)
        path = base / "plugins" / self.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def start(self) -> None:
        """Called after the bot's Transport is ready. Perform setup here."""

    async def stop(self) -> None:
        """Called before the bot stops. Clean up resources here."""

    async def on_message(self, msg: "IncomingMessage") -> bool:
        """Called for every authorized incoming text message. Return True to consume (skip backend)."""
        return False

    async def on_button(self, click: "ButtonClick") -> bool:
        """Called for every button click. Return True to consume (skip primary handlers)."""
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

- [ ] **Step 8: Run tests to verify they pass**

```bash
pytest tests/test_plugin_framework.py tests/transport/test_on_stop.py -v
```
Expected: All tests PASS.

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
        scripts/restart.sh scripts/stop.sh
git commit -m "$(cat <<'EOF'
feat(plugin): plugin framework + Transport.on_stop hook

Adds plugin.py (transport-agnostic Plugin base, PluginContext, BotCommand,
load_plugin via entry points) and operational scripts. Extends the
Transport Protocol with on_stop(callback) so plugins can shutdown cleanly
before the platform tears down. All three transports (Telegram, Fake, Web)
implement it.

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
        # Role enforcement: empty list means legacy/no-roles mode (allow all).
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
            trusted_user_id=(self._get_trusted_user_ids()[0] if self._get_trusted_user_ids() else None),
            allowed_user_ids=list(self._get_trusted_user_ids()),
            executor_user_ids=list(self._get_trusted_user_ids()),
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

## Task 3: Config schema — `plugins` field and `AllowedUser` model

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/test_config_allowed_users.py`

`config.py` is now 1379 LOC with `BotPeerRef`, `RoomBinding`, `backend_state`, etc. We add fields to `ProjectConfig` without touching existing primitives.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_allowed_users.py`:
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


def test_project_config_has_plugins_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.plugins == []


def test_project_config_has_allowed_users_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.allowed_users == []


def test_save_load_roundtrip(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["myp"] = ProjectConfig(
        path="/tmp/p",
        telegram_bot_token="t",
        allowed_users=[
            AllowedUser(username="alice", role="executor"),
            AllowedUser(username="bob", role="viewer"),
        ],
        plugins=[{"name": "in-app-web-server"}, {"name": "diff", "option": 1}],
    )
    save_config(cfg, cfg_file)
    loaded = load_config(cfg_file)
    p = loaded.projects["myp"]
    assert {(u.username, u.role) for u in p.allowed_users} == {
        ("alice", "executor"),
        ("bob", "viewer"),
    }
    assert p.plugins == [{"name": "in-app-web-server"}, {"name": "diff", "option": 1}]


def test_legacy_allowed_usernames_synthesize_executor_on_load(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    assert p.allowed_usernames == ["alice"]
    assert any(u.username == "alice" and u.role == "executor" for u in p.allowed_users)


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
    assert p.allowed_users == [AllowedUser(username="x", role="viewer")]


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


def test_save_does_not_persist_synthesized_allowed_users(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    p = written["projects"]["p"]
    assert p["allowed_usernames"] == ["alice"]
    assert "allowed_users" not in p
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_config_allowed_users.py -v
```
Expected: FAIL — `AllowedUser` not importable; `ProjectConfig.plugins` / `.allowed_users` not defined.

- [ ] **Step 3: Edit `config.py` — add `AllowedUser` and helpers**

In `src/link_project_to_chat/config.py`, find the `BotPeerRef` dataclass (line 28). Right before it, add:

```python
_VALID_ROLES = ("viewer", "executor")


@dataclass
class AllowedUser:
    username: str
    role: str = "viewer"


def _parse_allowed_users(raw_list) -> list[AllowedUser]:
    out: list[AllowedUser] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        if not username:
            continue
        role = entry.get("role", "viewer")
        if role not in _VALID_ROLES:
            logger.warning("unknown role %r for %s; defaulting to viewer", role, username)
            role = "viewer"
        out.append(AllowedUser(username=str(username), role=role))
    return out


def _serialize_allowed_users(users: list[AllowedUser]) -> list[dict]:
    return [{"username": u.username, "role": u.role} for u in users]


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
```

- [ ] **Step 4: Edit `config.py` — add fields to `ProjectConfig`**

`ProjectConfig` definition. Find its `@dataclass` (search for `class ProjectConfig:`). Add to the end of the field list (after the last existing field — read the dataclass first to find the right place):

```python
    plugins: list[dict] = field(default_factory=list)
    allowed_users: list[AllowedUser] = field(default_factory=list)
```

- [ ] **Step 5: Edit `config.py` — load `plugins` and `allowed_users`, synthesize legacy**

Find the project parsing logic inside `load_config` (search for `ProjectConfig(` around line 100–200). The dataclass is constructed from `proj` dict — add to the constructor kwargs:

```python
                plugins=_parse_plugins(proj.get("plugins", [])),
                allowed_users=_parse_allowed_users(proj.get("allowed_users", [])),
```

Then add a small block just after the `ProjectConfig` is appended/assigned, in the same loop iteration:

```python
                # In-memory legacy migration: synthesize allowed_users from
                # allowed_usernames when allowed_users is empty. Don't write back.
                if not config.projects[name_iter].allowed_users and config.projects[name_iter].allowed_usernames:
                    config.projects[name_iter].allowed_users = [
                        AllowedUser(username=u, role="executor")
                        for u in config.projects[name_iter].allowed_usernames
                    ]
```

(Read the actual loop to determine the iteration variable name and use it.)

- [ ] **Step 6: Edit `config.py` — write `plugins`, conditionally write `allowed_users`**

Find the project serialization logic inside `save_config` or `_merge_project_entry` (search for `proj["allowed_usernames"]` or similar). After the existing per-project field writes, add:

```python
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)

        synthesized = (
            p.allowed_usernames
            and len(p.allowed_users) == len(p.allowed_usernames)
            and all(
                u.role == "executor" and u.username == name
                for u, name in zip(p.allowed_users, p.allowed_usernames)
            )
        )
        if p.allowed_users and not synthesized:
            proj["allowed_users"] = _serialize_allowed_users(p.allowed_users)
        else:
            proj.pop("allowed_users", None)
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/test_config_allowed_users.py -v
pytest tests/test_config.py -q
```
Expected: All tests PASS.

- [ ] **Step 8: Run the full suite**

```bash
pytest -q
```

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config_allowed_users.py
git commit -m "$(cat <<'EOF'
feat(config): add plugins field and AllowedUser role model

ProjectConfig gains plugins (list[dict]) and allowed_users
(list[AllowedUser{username, role}]). Legacy allowed_usernames
synthesize executor-role entries in-memory on load; on-disk form is
preserved. Unknown roles fall back to viewer; malformed plugins entries
are dropped.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI — `plugin-call` subcommand

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/test_cli.py` (append)

`run_bot` / `run_bots` already accept `plugins` after Task 2; `proj.plugins` is read in `run_bots` via `proj.plugins or None`. Once Task 3 lands, the chain is live without further CLI changes. The remaining CLI work is the standalone `plugin-call` subcommand.

- [ ] **Step 1: Append failing test to `tests/test_cli.py`**

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cli.py -v
```
Expected: All tests PASS.

- [ ] **Step 5: Run the full suite**

```bash
pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): add plugin-call subcommand

New `plugin-call <project> <plugin_name> <tool_name> <args_json>` CLI
subcommand instantiates a plugin standalone (no bot) and invokes its
call_tool() — used by Claude via Bash within a task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Role enforcement on `AuthMixin`

**Files:**
- Modify: `src/link_project_to_chat/_auth.py`
- Modify: `src/link_project_to_chat/bot.py` (gates on state-changing handlers)
- Create: `tests/test_auth_roles.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_roles.py`:
```python
from __future__ import annotations

import pytest

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


class _BotWithRoles(AuthMixin):
    def __init__(self, allowed_users=None, allowed_usernames=None, trusted_user_ids=None):
        self._allowed_users = allowed_users or []
        self._allowed_usernames = allowed_usernames or []
        self._trusted_user_ids = trusted_user_ids or []
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


def test_require_executor_legacy_path_allows_when_empty():
    bot = _BotWithRoles(allowed_users=[])
    assert bot._require_executor(_identity("alice")) is True


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: FAIL — `_get_user_role` / `_require_executor` not defined.

- [ ] **Step 3: Edit `_auth.py` — add role helpers**

In `src/link_project_to_chat/_auth.py`, at the end of `class AuthMixin`, append:

```python
    # ── optional role-based access ────────────────────────────────────────────

    # Set by ProjectBot.__init__ when the project has populated `allowed_users`.
    # Empty → legacy flat allow-list semantics (no role enforcement).
    _allowed_users: list = []

    def _get_user_role(self, identity) -> str | None:
        """Return 'executor', 'viewer', or None for this identity.

        Matches the identity's handle against AllowedUser.username
        case- and @-insensitively.
        """
        if not self._allowed_users:
            return None
        uname = self._normalize_username(getattr(identity, "handle", ""))
        for au in self._allowed_users:
            if self._normalize_username(au.username) == uname:
                return au.role
        return None

    def _require_executor(self, identity) -> bool:
        """True if this identity may execute state-changing actions.

        - No allowed_users → legacy, allow.
        - Role 'executor' → allow.
        - Role 'viewer' or not listed → deny.
        """
        if not self._allowed_users:
            return True
        return self._get_user_role(identity) == "executor"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: All tests PASS.

- [ ] **Step 5: Edit `bot.py` — add `_guard_executor` helper**

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

- [ ] **Step 6: Edit `bot.py` — gate state-changing command handlers**

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

- [ ] **Step 7: Pipe `allowed_users` through `run_bots`**

`run_bots` already passes `allowed_users=proj.allowed_users or None` (added in Task 2 Step 14). After Task 3 lands, `proj.allowed_users` exists — verify by running the suite.

- [ ] **Step 8: Run tests**

```bash
pytest tests/test_auth_roles.py tests/test_bot_plugin_hooks.py -v
pytest -q
```
Expected: All tests PASS. Pay special attention to the existing handler tests — they were written assuming no role gate, so they should still pass because the default state has `_allowed_users = []` (legacy path).

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/_auth.py src/link_project_to_chat/bot.py tests/test_auth_roles.py
git commit -m "$(cat <<'EOF'
feat(auth): optional viewer/executor role enforcement

AuthMixin gains _get_user_role(identity) and _require_executor(identity)
(Identity-keyed, matches the post-transport-port auth model). When a
project populates allowed_users, state-changing handlers reply
'Read-only access' to viewers. Legacy projects (empty allowed_users)
unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manager UI — plugin toggle

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/manager/test_bot_plugins.py`

The manager bot was also ported to Transport (commands take `CommandInvocation`, button clicks come via the transport's `on_button`). Plugin toggle UI uses the same primitives.

- [ ] **Step 1: Write the failing tests**

Create `tests/manager/test_bot_plugins.py`:
```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot


def _make_manager(monkeypatch, projects=None):
    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = None
    bot._allowed_usernames = ["admin"]
    bot._trusted_user_ids = [1]
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

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/manager/test_bot_plugins.py -v
```
Expected: All tests PASS.

- [ ] **Step 8: Run the full suite**

```bash
pytest -q
```

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/manager/test_bot_plugins.py
git commit -m "$(cat <<'EOF'
feat(manager): plugin toggle UI for projects

Per-project keyboard gains a Plugins button. Toggle screen lists
installed lptc.plugins entry points; tap toggles active/inactive per
project. Restart-required hint shown after toggles. Implemented via the
transport-ported Button/Buttons primitives.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Docs and final verification

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml` (version bump to 0.17.0)
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
Executors have the full command set. When `allowed_users` is unset, the legacy
`allowed_usernames` model applies (every authorized user is effectively an
executor).
````

- [ ] **Step 2: Bump version**

In `pyproject.toml`, change `version = "0.16.0"` to `version = "0.17.0"`.

- [ ] **Step 3: Update CHANGELOG**

Prepend to `docs/CHANGELOG.md` (read the existing top to match format):

```markdown
## 0.17.0 — 2026-05-13

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
- **Optional `AllowedUser` role model** (`viewer` / `executor`) — opt-in per
  project via the new `allowed_users` field. Legacy `allowed_usernames` keep
  working unchanged.
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

  1. With no `plugins` configured and no `allowed_users`, start the bot. Send messages, run `/tasks`, run `/model`. Verify identical behavior to pre-merge.
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
git add README.md pyproject.toml docs/CHANGELOG.md docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md
git commit -m "$(cat <<'EOF'
docs: plugin system + role-based access, v0.17.0

README gains a Plugins section covering activation, transport-portable
plugin authoring, and role-based access. CHANGELOG entry summarizes the
release. Version bump to 0.17.0.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Push and open PR**

```bash
git push -u origin feat/plugin-system
gh pr create --title "Plugin system port + AllowedUser role model" --body "$(cat <<'EOF'
## Summary
- Adds transport-portable plugin framework (`plugin.py`) with entry-point discovery, lifecycle hooks, command/button registration, Claude-prompt prepend
- Adds `Transport.on_stop` Protocol hook for clean plugin shutdown
- Adds manager-bot plugin toggle UI and `plugin-call` CLI subcommand
- Adds optional `AllowedUser` viewer/executor role model (opt-in per project; legacy `allowed_usernames` untouched)
- Adds operational scripts (`restart.sh`, `stop.sh`)
- Bumps version to 0.17.0

Design doc: `docs/superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md`
Implementation plan: `docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md`

## Test plan
- [x] `pytest -q` green on every commit
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

1. `pytest -q` green.
2. New tests for the task pass.
3. No existing source files deleted.
4. No regressions in the 1003-test baseline.

If a gate fails, **STOP** and reconcile before continuing.

## Out-of-scope reminders

Not in this plan; don't add without a new spec:
- Wire-compatibility with GitLab plugin packages written against `python-telegram-bot` directly. Plugin authors must use the transport-agnostic API.
- Migrating the primary fork's existing features (team_relay, livestream, personas, skills, voice) to the role model.
- Replacing `allowed_usernames` / `trusted_users` on team bots.
- Backend-aware plugin behavior beyond `get_context()` being Claude-only.
- Building any specific plugin (those live in the external `link-project-to-chat-plugins` package).
