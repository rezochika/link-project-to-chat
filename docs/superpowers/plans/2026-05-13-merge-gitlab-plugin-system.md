# Merge GitLab plugin system — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the GitLab fork's plugin framework, manager-UI toggle, `plugin-call` CLI, operational scripts, and optional `AllowedUser` role model into the primary fork without disturbing existing features (team_relay, livestream, personas, skills, voice, group filters).

**Architecture:** All changes are additive. `plugin.py` is dropped in verbatim with one tiny extension (`viewer_ok` on `BotCommand`). `ProjectBot` gains plugin lifecycle hooks alongside its existing setup. `ProjectConfig` gains a `plugins` field and an optional `allowed_users` field; legacy `allowed_usernames` + `trusted_user_ids` keep working unchanged.

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22`, `click>=8`, `pytest` with `asyncio_mode=auto`, plugin discovery via `importlib.metadata.entry_points(group="lptc.plugins")`.

**Reference design:** [`docs/superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md`](../specs/2026-05-13-merge-gitlab-plugin-system-design.md)

**Branch:** Create and work on `feat/plugin-system` off `main`. Commit at the end of each task.

---

## Task 0: Setup branch

**Files:**
- N/A (git only)

- [ ] **Step 1: Create the feature branch off `main`**

Run from the repo root:
```bash
git checkout main
git pull --ff-only
git checkout -b feat/plugin-system
git status
```
Expected: `On branch feat/plugin-system` with a clean working tree.

- [ ] **Step 2: Verify baseline test suite passes**

Run:
```bash
pytest -q
```
Expected: All tests pass. If anything fails, **STOP** and ask before proceeding — the plan assumes a green baseline.

---

## Task 1: Add `plugin.py` framework + operational scripts

**Files:**
- Create: `src/link_project_to_chat/plugin.py`
- Create: `scripts/restart.sh`
- Create: `scripts/stop.sh`
- Create: `tests/test_plugin_framework.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugin_framework.py`:
```python
from __future__ import annotations

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
    async def handler():
        return None

    cmd = BotCommand(command="x", description="d", handler=handler)
    assert cmd.viewer_ok is False


def test_botcommand_viewer_ok_can_be_set():
    async def handler():
        return None

    cmd = BotCommand(command="x", description="d", handler=handler, viewer_ok=True)
    assert cmd.viewer_ok is True


def test_plugin_context_send_message_proxies_to_send():
    send = AsyncMock()
    ctx = PluginContext(bot_name="b", project_path=Path("/tmp"), _send=send)

    import asyncio
    asyncio.run(ctx.send_message(42, "hi", reply_to=7))

    send.assert_awaited_once_with(42, "hi", reply_to=7)


def test_plugin_context_send_message_noop_without_send():
    ctx = PluginContext(bot_name="b", project_path=Path("/tmp"))

    import asyncio
    # Must not raise even when no _send is wired.
    asyncio.run(ctx.send_message(1, "hi"))


def test_plugin_data_dir_creates_directory(tmp_path):
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

- [ ] **Step 2: Run tests to verify they fail with ImportError**

Run:
```bash
pytest tests/test_plugin_framework.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'link_project_to_chat.plugin'`.

- [ ] **Step 3: Create `src/link_project_to_chat/plugin.py`**

```python
"""
Plugin base classes and PluginContext.

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
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class BotCommand:
    command: str
    description: str
    handler: Callable[..., Awaitable[Any]]
    viewer_ok: bool = False


@dataclass
class PluginContext:
    """Shared context for all plugins in a project. One instance per bot."""
    bot_name: str
    project_path: Path
    bot_token: str | None = None
    trusted_user_id: int | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    executor_user_ids: list[int] = field(default_factory=list)

    bot_username: str = ""
    data_dir: Path | None = None

    web_port: int | None = None
    public_url: str | None = None

    register_in_app_web_handler: Callable[[str, str, Callable[..., Awaitable[Any]]], None] | None = field(default=None, repr=False)

    _send: Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Any:
        """Send a Telegram message from a plugin without importing bot internals."""
        if self._send:
            return await self._send(chat_id, text, **kwargs)


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
        """Called when the bot starts. Perform setup here."""

    async def stop(self) -> None:
        """Called when the bot stops. Clean up resources here."""

    async def on_message(self, user_id: int, username: str, chat_id: int, message_id: int, text: str = "") -> bool:
        """Called for every authorized incoming text message. Return True to consume (skip Claude)."""
        return False

    async def on_task_complete(self, task) -> None:
        """Called after a task finishes (DONE or FAILED). Not called for CANCELLED."""

    async def on_tool_use(self, tool: str, path: str | None) -> None:
        """Called when Claude uses a tool during a task (e.g. Write, Edit)."""

    def get_context(self) -> str | None:
        """Text prepended to Claude's prompt before each task. Return None to skip."""
        return None

    def tools(self) -> list[dict]:
        """Tool definitions available to Claude (schema only, for documentation)."""
        return []

    async def call_tool(self, name: str, args: dict) -> str:
        """Execute a plugin tool. Called via CLI (claude uses Bash to invoke it)."""
        return f"Unknown tool: {name}"

    def commands(self) -> list[BotCommand]:
        """Additional Telegram bot commands this plugin registers."""
        return []

    def callbacks(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        """Callback query handlers keyed by callback_data prefix."""
        return {}


def load_plugin(name: str, context: PluginContext, config: dict) -> Plugin | None:
    """
    Instantiate a plugin by name using the 'lptc.plugins' entry point group.
    Returns None if the plugin is not installed.
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

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_plugin_framework.py -v
```
Expected: All 6 tests PASS.

- [ ] **Step 5: Add operational scripts**

Create `scripts/restart.sh` with exact content:
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

Create `scripts/stop.sh` with exact content:
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

Make them executable:
```bash
chmod +x scripts/restart.sh scripts/stop.sh
```

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/plugin.py tests/test_plugin_framework.py scripts/restart.sh scripts/stop.sh
git commit -m "$(cat <<'EOF'
feat(plugin): add plugin framework and operational scripts

Drops plugin.py from the GitLab fork into the primary fork with one
additive field (`viewer_ok` on BotCommand). Adds restart.sh and stop.sh.
No wiring yet — the framework is unused until Task 2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire plugin lifecycle into `ProjectBot`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_bot_plugin_hooks.py`

This is the largest task. It adds:
- Constructor parameter, instance state, topo sort
- `_init_plugins()` called from `_post_init` (after `bot.get_me()`)
- `_shutdown_plugins()` called when bot stops
- `on_message` invocation in `_on_text`
- `get_context()` aggregation when assembling Claude prompts
- `on_tool_use` invocation in `_on_stream_event`
- `on_task_complete` invocation in `_on_task_complete`
- Callback dispatcher prefix-match against `_plugin_callbacks`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot_plugin_hooks.py`:
```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.bot import ProjectBot, _topo_sort
from link_project_to_chat.plugin import BotCommand, Plugin, PluginContext
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.stream import TextDelta, ToolUse


class _RecordingPlugin(Plugin):
    name = "rec"

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.events: list[tuple[str, Any]] = []
        self.consume_message = False
        self.start_raises = False
        self.context_text: str | None = None

    async def start(self) -> None:
        if self.start_raises:
            raise RuntimeError("boom")
        self.events.append(("start", None))

    async def stop(self) -> None:
        self.events.append(("stop", None))

    async def on_message(self, user_id, username, chat_id, message_id, text=""):
        self.events.append(("on_message", text))
        return self.consume_message

    async def on_task_complete(self, task) -> None:
        self.events.append(("on_task_complete", task.id))

    async def on_tool_use(self, tool, path) -> None:
        self.events.append(("on_tool_use", (tool, path)))

    def get_context(self):
        return self.context_text

    def commands(self):
        async def _h():
            return None
        return [BotCommand(command="rec_cmd", description="rec", handler=_h)]

    def callbacks(self):
        async def _cb(query, suffix):
            self.events.append(("cb", suffix))
        return {"rec_": _cb}


def _make_bot(plugins: list[Plugin] | None = None) -> ProjectBot:
    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.path = Path("/tmp/p")
    bot.token = "t"
    bot._allowed_usernames = ["alice"]
    bot._trusted_user_ids = [123]
    bot._plugins = plugins or []
    bot._plugin_configs = []
    bot._plugin_callbacks = {}
    bot._shared_ctx = None
    bot._plugin_command_handlers: dict[str, Any] = {}
    return bot


def test_topo_sort_orders_by_depends_on():
    a = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    a.name = "a"
    a.depends_on = ["b"]
    b = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    b.name = "b"

    ordered = _topo_sort([a, b])
    assert [p.name for p in ordered] == ["b", "a"]


def test_topo_sort_missing_dep_still_returns_plugin():
    p = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p.name = "p"
    p.depends_on = ["unknown"]
    ordered = _topo_sort([p])
    # Missing dep is logged but does not drop the plugin
    assert [x.name for x in ordered] == ["p"]


@pytest.mark.asyncio
async def test_on_tool_use_fires_for_each_plugin():
    p1 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p2 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    bot = _make_bot([p1, p2])

    task = Task.__new__(Task)
    task.id = 1
    task.chat_id = 1
    task.message_id = 1
    task.type = TaskType.CLAUDE
    task.pending_questions = []
    task._compact = False

    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))

    assert ("on_tool_use", ("Write", "/x")) in p1.events
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_tool_use_logs_and_continues_when_plugin_raises():
    p1 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})

    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p1.on_tool_use = boom  # type: ignore[assignment]

    p2 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    bot = _make_bot([p1, p2])

    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))

    # p2 still ran despite p1 raising
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_task_complete_fires_for_done_and_failed_not_cancelled():
    p = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    bot = _make_bot([p])

    for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task = Task.__new__(Task)
        task.id = int(status.value) if hasattr(status, "value") else 1
        task.status = status
        await bot._dispatch_plugin_task_complete(task)

    fired = [e for e in p.events if e[0] == "on_task_complete"]
    assert len(fired) == 2  # DONE + FAILED, not CANCELLED


@pytest.mark.asyncio
async def test_on_message_consumes_when_any_plugin_returns_true():
    p1 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p2 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p2.consume_message = True
    bot = _make_bot([p1, p2])

    consumed = await bot._dispatch_plugin_on_message(
        user_id=123, username="alice", chat_id=1, message_id=1, text="hi"
    )
    assert consumed is True
    # both plugins still saw it (p1 ran before p2 short-circuited)
    assert ("on_message", "hi") in p1.events
    assert ("on_message", "hi") in p2.events


@pytest.mark.asyncio
async def test_on_message_does_not_consume_when_plugin_raises():
    p = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})

    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p.on_message = boom  # type: ignore[assignment]

    bot = _make_bot([p])
    consumed = await bot._dispatch_plugin_on_message(
        user_id=123, username="alice", chat_id=1, message_id=1, text="hi"
    )
    assert consumed is False


def test_plugin_context_concatenation_inserts_separator():
    p1 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p1.context_text = "FIRST"
    p2 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p2.context_text = "SECOND"
    p3 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p3.context_text = None
    bot = _make_bot([p1, p2, p3])

    prepend = bot._plugin_context_prepend("USER_PROMPT")
    # Plugin contexts joined with \n\n, then followed by separator + user prompt
    assert prepend.startswith("FIRST\n\nSECOND")
    assert "\n\n---\n\nUSER_PROMPT" in prepend


def test_plugin_context_concatenation_empty_when_no_contexts():
    p = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p.context_text = None
    bot = _make_bot([p])

    prepend = bot._plugin_context_prepend("USER_PROMPT")
    assert prepend == "USER_PROMPT"


@pytest.mark.asyncio
async def test_plugin_callback_dispatch_by_prefix():
    p = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    bot = _make_bot([p])
    bot._plugin_callbacks = p.callbacks()

    handled = await bot._dispatch_plugin_callback(MagicMock(), "rec_open")
    assert handled is True
    assert ("cb", "rec_open") in p.events


@pytest.mark.asyncio
async def test_plugin_callback_dispatch_returns_false_when_no_match():
    bot = _make_bot([])
    handled = await bot._dispatch_plugin_callback(MagicMock(), "no_match_x")
    assert handled is False


@pytest.mark.asyncio
async def test_shutdown_calls_stop_in_reverse_order():
    order: list[str] = []

    class P(_RecordingPlugin):
        async def stop(self):
            order.append(self.name)

    p1 = P(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p1.name = "first"
    p2 = P(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    p2.name = "second"
    bot = _make_bot([p1, p2])

    await bot._shutdown_plugins()
    assert order == ["second", "first"]


@pytest.mark.asyncio
async def test_shutdown_continues_when_a_stop_raises():
    p1 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    async def boom():
        raise RuntimeError("boom")
    p1.stop = boom  # type: ignore[assignment]
    p2 = _RecordingPlugin(PluginContext(bot_name="b", project_path=Path("/tmp")), {})
    bot = _make_bot([p1, p2])

    await bot._shutdown_plugins()
    # p2.stop was reached
    assert ("stop", None) in p2.events
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: FAIL — `_topo_sort` not importable, `_dispatch_plugin_*` and `_shutdown_plugins` not defined on `ProjectBot`.

- [ ] **Step 3: Edit `bot.py` — add import**

In `src/link_project_to_chat/bot.py` after line 45 (`from .task_manager import ...`):

```python
from .plugin import Plugin, PluginContext, load_plugin
```

- [ ] **Step 4: Edit `bot.py` — add module-level `_topo_sort` helper**

Insert before `class ProjectBot(AuthMixin):` (which is at line 82). Place it right after the `_parse_task_id` helper:

```python
def _topo_sort(plugins: list[Plugin]) -> list[Plugin]:
    """Order plugins so that each comes after the plugins it depends_on.

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

- [ ] **Step 5: Edit `bot.py` — add constructor parameter and state**

In `ProjectBot.__init__` (starts at line 83), add a new keyword parameter `plugins: list[dict] | None = None` to the signature (after `peer_bot_username` on line 104), then in the body (after the existing `self._group_state = GroupStateRegistry(max_bot_rounds=20)` line at line 160 — find it precisely with grep first), insert:

```python
        # Plugin framework state. Populated in _post_init after bot.get_me().
        self._plugin_configs: list[dict] = list(plugins or [])
        self._plugins: list[Plugin] = []
        self._plugin_callbacks: dict[str, Any] = {}
        self._plugin_command_handlers: dict[str, Any] = {}
        self._shared_ctx: PluginContext | None = None
```

Note: import `Any` from `typing` at the top of the file if it's not already imported (check the current imports at lines 1–11).

- [ ] **Step 6: Edit `bot.py` — add plugin dispatch helpers**

Add these methods on `ProjectBot`. Place them as a block right above the `_post_init` method (`_post_init` is currently at line 1690 — locate it first with grep):

```python
    async def _dispatch_plugin_on_message(
        self, user_id: int, username: str, chat_id: int, message_id: int, text: str
    ) -> bool:
        """Fire on_message for each plugin. Return True if ANY plugin consumed."""
        consumed = False
        for plugin in self._plugins:
            try:
                if await plugin.on_message(user_id, username, chat_id, message_id, text):
                    consumed = True
            except Exception:
                logger.warning("plugin %s on_message failed", plugin.name, exc_info=True)
        return consumed

    async def _dispatch_plugin_tool_use(self, event: ToolUse) -> None:
        for plugin in self._plugins:
            try:
                await plugin.on_tool_use(event.tool, event.path)
            except Exception:
                logger.warning("plugin %s on_tool_use failed", plugin.name, exc_info=True)

    async def _dispatch_plugin_task_complete(self, task: Task) -> None:
        if task.status == TaskStatus.CANCELLED:
            return
        for plugin in self._plugins:
            try:
                await plugin.on_task_complete(task)
            except Exception:
                logger.warning("plugin %s on_task_complete failed", plugin.name, exc_info=True)

    async def _dispatch_plugin_callback(self, query, data: str) -> bool:
        """Return True if a plugin callback prefix matched and was invoked."""
        for prefix, handler in self._plugin_callbacks.items():
            if data.startswith(prefix):
                try:
                    suffix = data
                    await handler(query, suffix)
                except Exception:
                    logger.warning("plugin callback %s failed", prefix, exc_info=True)
                return True
        return False

    def _plugin_context_prepend(self, prompt: str) -> str:
        """Prepend non-empty plugin get_context() outputs to a Claude prompt."""
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

    async def _init_plugins(self, app) -> None:
        """Instantiate, register, and start plugins. Called from _post_init after get_me()."""
        if not self._plugin_configs:
            return
        self._shared_ctx = PluginContext(
            bot_name=self.name,
            project_path=self.path,
            bot_token=self.token,
            trusted_user_id=(self._get_trusted_user_ids()[0] if self._get_trusted_user_ids() else None),
            allowed_user_ids=list(self._get_trusted_user_ids()),
            executor_user_ids=list(self._get_trusted_user_ids()),
            bot_username=self.bot_username,
            data_dir=Path.home() / ".link-project-to-chat" / "meta" / self.name,
            _send=self._app.bot.send_message if self._app else None,
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

        # Register commands and callbacks
        for plugin in self._plugins:
            try:
                cmds = plugin.commands()
            except Exception:
                logger.warning("plugin %s commands() failed; skipping plugin", plugin.name, exc_info=True)
                continue
            try:
                cbs = plugin.callbacks()
            except Exception:
                logger.warning("plugin %s callbacks() failed", plugin.name, exc_info=True)
                cbs = {}

            for bc in cmds:
                handler = CommandHandler(bc.command, bc.handler)
                app.add_handler(handler)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(handler)
            for prefix, handler in cbs.items():
                self._plugin_callbacks[prefix] = handler

        # Start plugins in dependency order; on failure, unregister that plugin's
        # commands/callbacks so it cannot serve stale handlers.
        for plugin in _topo_sort(self._plugins):
            try:
                await plugin.start()
            except Exception:
                logger.warning("plugin %s start failed; unregistering", plugin.name, exc_info=True)
                for h in self._plugin_command_handlers.pop(plugin.name, []):
                    try:
                        app.remove_handler(h)
                    except Exception:
                        logger.debug("remove_handler failed", exc_info=True)
                try:
                    for prefix in list(plugin.callbacks().keys()):
                        self._plugin_callbacks.pop(prefix, None)
                except Exception:
                    pass

    async def _shutdown_plugins(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.warning("plugin %s stop failed", plugin.name, exc_info=True)
```

- [ ] **Step 7: Edit `bot.py` — call `_init_plugins` from `_post_init`**

`_post_init` is around line 1690. After the existing `await app.bot.set_my_commands(COMMANDS)` line (around line 1710), insert:

```python
        await self._init_plugins(app)
```

- [ ] **Step 8: Edit `bot.py` — fire `on_tool_use` from `_on_stream_event`**

`_on_stream_event` is around line 201. Find the `elif isinstance(event, ToolUse):` branch (around line 265). After the existing image-handling block (the `if event.path and self._is_image(event.path):` lines around 266–269), add:

```python
            await self._dispatch_plugin_tool_use(event)
```

- [ ] **Step 9: Edit `bot.py` — fire `on_task_complete` from `_on_task_complete`**

`_on_task_complete` is around line 492. At the very end of the method (after the existing branching between `_finalize_claude_task` and `_finalize_command_task`), add:

```python
        await self._dispatch_plugin_task_complete(task)
```

- [ ] **Step 10: Edit `bot.py` — fire `on_message` and prepend context in `_on_text`**

`_on_text` is around line 570. Locate the block immediately before `self.task_manager.submit_claude(...)` (around line 655). The current code is:

```python
        prompt = msg.text
        if msg.reply_to_message and msg.reply_to_message.text:
            prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"
        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)
        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )
```

Before the `prompt = msg.text` line, add the plugin-consume check (the `_auth` check above this block already gates entry):

```python
        consumed = await self._dispatch_plugin_on_message(
            user_id=update.effective_user.id,
            username=(update.effective_user.username or ""),
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=msg.text or "",
        )
        if consumed:
            return
```

Then, just before the `self.task_manager.submit_claude(...)` call, prepend plugin context:

```python
        prompt = self._plugin_context_prepend(prompt)
        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )
```

(Keep the original arguments to `submit_claude`; only `prompt` is re-bound right above.)

- [ ] **Step 11: Edit `bot.py` — route callbacks through plugins**

`_on_callback` is at line 1204. Right after the auth/rate-limit checks at the top (find the first place the method does real work — typically after `await query.answer()` if present, or after `if not self._auth(...)` returns), insert:

```python
        if await self._dispatch_plugin_callback(query, query.data or ""):
            return
```

Place this **after** the existing `ask_` (AskUserQuestion) dispatch if there's an early-return for `ask_` callbacks, so primary's own callback prefixes still win. The cleanest placement is immediately before the chain of `elif data.startswith(...)` branches that handle primary's own callbacks. Read lines 1204–1260 first to find the correct insertion point.

- [ ] **Step 12: Edit `bot.py` — hook shutdown**

Telegram-bot v22 emits a `post_stop` hook from `ApplicationBuilder`. In `build()` (line 1726), change:

```python
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
```

to:

```python
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .build()
        )
```

And add this method on `ProjectBot` immediately above `build()`:

```python
    async def _post_stop(self, app) -> None:
        await self._shutdown_plugins()
```

- [ ] **Step 13: Edit `bot.py` — accept `plugins` in `run_bot()`**

`run_bot` is at line 1803. Add to the signature, right after `peer_bot_username: str = "",`:

```python
    plugins: list[dict] | None = None,
```

Inside the body, the `bot = ProjectBot(...)` call (around line 1836) — add to its kwargs:

```python
        plugins=plugins,
```

Similarly, `run_bots` is at line 1866. Add `plugins=proj.plugins or None,` to the `run_bot(...)` call inside it (around line 1898) — see Task 3 first so `proj.plugins` exists, then this step will already pass; for now, just add the parameter to `run_bot`'s signature and `bot = ProjectBot` call. The `run_bots` wiring is finalized in Task 4.

- [ ] **Step 14: Run tests to verify they pass**

```bash
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: All tests PASS.

- [ ] **Step 15: Run the full suite for regressions**

```bash
pytest -q
```
Expected: All previously-passing tests still pass. If anything breaks, the most likely cause is the placement of the plugin-dispatch hooks inside `_on_text` or `_on_callback`. Re-read the surrounding code and adjust.

- [ ] **Step 16: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_plugin_hooks.py
git commit -m "$(cat <<'EOF'
feat(plugin): wire plugin lifecycle into ProjectBot

Adds plugin constructor param, _topo_sort helper, plugin dispatch helpers
(_dispatch_plugin_on_message, _dispatch_plugin_tool_use,
_dispatch_plugin_task_complete, _dispatch_plugin_callback,
_plugin_context_prepend), _init_plugins in post_init, _shutdown_plugins in
post_stop. Failed start() unregisters that plugin's commands/callbacks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Config schema — `plugins` field and `AllowedUser` model

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/test_config_allowed_users.py`

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


def test_save_then_load_roundtrip_allowed_users(tmp_path: Path):
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


def test_legacy_allowed_usernames_synthesize_executor_role_on_load(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "allowed_usernames": [],
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
            }
        },
    }
    cfg_file.write_text(json.dumps(raw))

    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    # On-disk format preserved; in-memory `allowed_users` reflects legacy
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
        },
    }
    cfg_file.write_text(json.dumps(raw))

    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    assert p.allowed_users == [AllowedUser(username="x", role="viewer")]


def test_malformed_plugin_entry_is_skipped(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "plugins": [{"name": "good"}, {"not_a_name": "bad"}, "string-not-dict"],
            }
        },
    }
    cfg_file.write_text(json.dumps(raw))

    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    assert p.plugins == [{"name": "good"}]


def test_save_does_not_write_back_synthesized_allowed_users(tmp_path: Path):
    """Loading legacy then saving must not overwrite on-disk form."""
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
            }
        },
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
Expected: FAIL — `AllowedUser` not importable; `ProjectConfig.plugins` and `ProjectConfig.allowed_users` not defined.

- [ ] **Step 3: Edit `config.py` — add `AllowedUser` dataclass**

In `src/link_project_to_chat/config.py`, just before `class ProjectConfig:` (line 26), insert:

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
        if "name" not in entry or not entry.get("name"):
            continue
        out.append(entry)
    return out
```

- [ ] **Step 4: Edit `config.py` — add fields to `ProjectConfig`**

`ProjectConfig` is defined at line 27. Add these two fields. Place them right after `show_thinking: bool = False` (the current last field):

```python
    plugins: list[dict] = field(default_factory=list)
    allowed_users: list[AllowedUser] = field(default_factory=list)
```

- [ ] **Step 5: Edit `config.py` — load `plugins` and synthesize `allowed_users` in project parser**

Find the project-parsing loop inside `load_config` (around line 190 — search for `allowed_usernames=_migrate_usernames(proj, "allowed_usernames", "username"),`). The current `ProjectConfig(...)` call enumerates fields. Add to its kwargs:

```python
                plugins=_parse_plugins(proj.get("plugins", [])),
                allowed_users=_parse_allowed_users(proj.get("allowed_users", [])),
```

After this `ProjectConfig(...)` is constructed and assigned (currently around line 190–210), and just before the next project iteration, add a small block that synthesizes legacy users **only when `allowed_users` is empty**:

```python
                if not config.projects[name_iter].allowed_users and config.projects[name_iter].allowed_usernames:
                    config.projects[name_iter].allowed_users = [
                        AllowedUser(username=u, role="executor")
                        for u in config.projects[name_iter].allowed_usernames
                    ]
```

Note: the iteration variable in the existing code may be named differently — read lines 175–225 of `config.py` carefully, identify the actual project name binding inside the loop, and use that.

- [ ] **Step 6: Edit `config.py` — write `plugins` and `allowed_users` only when explicitly populated**

In `save_config`, find the place where each project is serialized back to a dict (search for `proj["allowed_usernames"] = p.allowed_usernames`, around line 228). After the existing serialization lines for that project, add:

```python
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)

        # Only persist allowed_users if it was set explicitly (not synthesized from legacy).
        explicit_au = [u for u in p.allowed_users] if p.allowed_users and not p.allowed_usernames else p.allowed_users
        # Avoid writing back synthesized values: when allowed_usernames is set and allowed_users
        # matches a 1:1 executor synthesis, leave the on-disk allowed_users key absent.
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
```
Expected: All tests PASS.

- [ ] **Step 8: Run the full suite for regressions**

```bash
pytest -q
```
Expected: All previously-passing tests still pass. Pay particular attention to `tests/test_config.py` and `tests/test_config_m11.py` — they exercise config save/load and must remain green.

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config_allowed_users.py
git commit -m "$(cat <<'EOF'
feat(config): add plugins field and AllowedUser role model

ProjectConfig gains optional plugins (list[dict]) and allowed_users
(list[AllowedUser{username, role}]). Legacy allowed_usernames synthesize
executor-role AllowedUser entries in-memory on load; on-disk form is
preserved (synthesized values are not written back). Unknown roles fall
back to viewer; malformed plugins entries are dropped.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI — `plugin-call` subcommand and `start` wiring

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `src/link_project_to_chat/bot.py` (finalize `run_bots` from Task 2)
- Modify: `tests/test_cli.py` (extend, don't recreate)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (add at the bottom of the file):

```python
def test_plugin_call_unknown_plugin_exits_nonzero(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text('{"projects": {"p": {"path": "/tmp", "telegram_bot_token": "t"}}}')

    result = runner.invoke(
        main,
        ["--config", str(cfg), "plugin-call", "p", "does-not-exist", "tool", "{}"],
    )
    assert result.exit_code != 0
    assert "does-not-exist" in (result.output or "") or "not found" in (result.output or "").lower() or result.exception is not None
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
pytest tests/test_cli.py::test_plugin_call_unknown_plugin_exits_nonzero -v
```
Expected: FAIL — `No such command 'plugin-call'`.

- [ ] **Step 3: Edit `cli.py` — add the `plugin-call` subcommand**

At the bottom of `src/link_project_to_chat/cli.py`, before the `if __name__ == "__main__":` block if any (otherwise just at the end), add:

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

- [ ] **Step 4: Edit `cli.py` — pass `plugins` from project config to `run_bot`**

In the `start` command (definition at line 236), the call path goes through `run_bots(config, ...)` (for the configured-projects branch) and `run_bot(...)` (for the ad-hoc `--path/--token` branch). The ad-hoc branch has no project config and therefore no plugins — leave it alone.

For the configured branch, the actual wiring happens in `bot.py`'s `run_bots` (line 1866). Locate the `run_bot(...)` call inside `run_bots` (around line 1887) and add to its kwargs:

```python
            plugins=proj.plugins or None,
```

Also locate the team-bot startup path (where `team` is truthy, around line 310 in cli.py — read it first). Team bots do not yet support plugins; leave them alone for this task. If the team bot path eventually calls `run_bot`, do **not** thread `plugins` through to it. Plugins are project-only for this merge.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cli.py -v
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: All tests PASS. (The bot-plugin test suite must continue to pass — `run_bots` change does not affect it.)

- [ ] **Step 6: Run the full suite**

```bash
pytest -q
```
Expected: All previously-passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/cli.py src/link_project_to_chat/bot.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): add plugin-call subcommand and wire plugins from config

New `plugin-call <project> <plugin_name> <tool_name> <args_json>` CLI
subcommand instantiates a plugin without a bot and invokes its
call_tool() — used by Claude via Bash. run_bots() now passes
ProjectConfig.plugins to run_bot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Role enforcement — read-only viewer mode

**Files:**
- Modify: `src/link_project_to_chat/_auth.py`
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_auth_roles.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_roles.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser


class _BotWithRoles(AuthMixin):
    def __init__(self, allowed_users=None, allowed_usernames=None, trusted_user_ids=None):
        self._allowed_users = allowed_users or []
        self._allowed_usernames = allowed_usernames or []
        self._trusted_user_ids = trusted_user_ids or []
        self._init_auth()


def test_get_user_role_returns_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(user_id=1, username="alice") == "executor"


def test_get_user_role_returns_viewer():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="bob", role="viewer")],
    )
    assert bot._get_user_role(user_id=2, username="bob") == "viewer"


def test_get_user_role_returns_none_when_not_listed():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(user_id=2, username="bob") is None


def test_role_check_legacy_path_allows_when_allowed_users_empty():
    bot = _BotWithRoles(allowed_users=[])
    # Legacy mode: no role enforcement; treat as executor.
    assert bot._require_executor(user_id=1, username="alice") is True


def test_role_check_blocks_viewer_for_state_changing_commands():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="bob", role="viewer")],
    )
    assert bot._require_executor(user_id=2, username="bob") is False


def test_role_check_allows_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(user_id=1, username="alice") is True


def test_role_check_blocks_unknown_user():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(user_id=3, username="charlie") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: FAIL — `_get_user_role` and `_require_executor` not defined on `AuthMixin`.

- [ ] **Step 3: Edit `_auth.py` — add role helpers**

In `src/link_project_to_chat/_auth.py`, append two methods to `AuthMixin` (after `_rate_limited` at line 90):

```python
    # ── role-based access (optional) ───────────────────────────────────────────

    # Set in __init__ when the project has populated `allowed_users`. When empty
    # or absent, the bot uses legacy flat allow-list semantics (no role enforcement).
    _allowed_users: list = []  # list[AllowedUser]

    def _get_user_role(self, user_id: int, username: str) -> str | None:
        """Return 'executor', 'viewer', or None for this user, based on _allowed_users.

        Username match is case-insensitive. Returns None if the user is not listed
        in _allowed_users at all (regardless of whether _allowed_usernames / _trusted_user_ids
        would have allowed them — when roles are active, allowed_users is authoritative).
        """
        if not self._allowed_users:
            return None
        uname = (username or "").strip().lower().lstrip("@")
        for au in self._allowed_users:
            if au.username.strip().lower().lstrip("@") == uname:
                return au.role
        return None

    def _require_executor(self, user_id: int, username: str) -> bool:
        """Return True when this user may execute state-changing actions.

        Behavior:
          - No allowed_users configured → legacy path, allow.
          - Role is 'executor' → allow.
          - Role is 'viewer' or user not in allowed_users → deny.
        """
        if not self._allowed_users:
            return True
        return self._get_user_role(user_id, username) == "executor"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_auth_roles.py -v
```
Expected: All tests PASS.

- [ ] **Step 5: Edit `bot.py` — populate `_allowed_users` on the bot**

`ProjectBot.__init__` (line 83) currently accepts `allowed_usernames` and `trusted_user_ids` but not `allowed_users`. Add to the signature (right after `trusted_user_ids: list[int] | None = None,` at line 96):

```python
        allowed_users: list | None = None,  # list[AllowedUser]
```

And in the body, right after the existing `_allowed_usernames` / `_trusted_user_ids` block (around line 109–116), add:

```python
        if allowed_users is not None:
            self._allowed_users = list(allowed_users)
        else:
            self._allowed_users = []
```

- [ ] **Step 6: Edit `bot.py` — apply role gate in state-changing handlers**

State-changing entry points to gate: `_on_text` (plain Claude messages), `_on_run`, `_on_model`, `_on_effort`, `_on_thinking`, `_on_permissions`, `_on_compact`, `_on_reset`, `_on_skills` (when activating, not when listing), `_on_stop_skill`, `_on_create_skill`, `_on_delete_skill`, `_on_persona`, `_on_stop_persona`, `_on_create_persona`, `_on_delete_persona`, `_on_voice` (the actual voice handler, not `_on_voice_status`), `_on_lang`, `_on_halt`, `_on_resume`, `_on_file`.

Add this helper on `ProjectBot` right above `_on_start` (line 562):

```python
    async def _guard_executor(self, update: Update) -> bool:
        """Return True if the user is allowed to execute state-changing actions.

        Replies 'Read-only access' when blocked. Returns False so the caller can
        early-return.
        """
        user = update.effective_user
        if not user:
            return False
        if self._require_executor(user.id, user.username or ""):
            return True
        msg = update.effective_message
        if msg:
            await msg.reply_text("Read-only access — your role is viewer.")
        return False
```

For each state-changing handler, insert immediately after the existing `_auth` check:

```python
        if not await self._guard_executor(update):
            return
```

For `_on_text` specifically, the gate must come **after** the plugin `on_message` dispatch (so plugins can serve viewer interactions) and **before** `task_manager.submit_claude(...)`. Concretely, place this block immediately before the `prompt = msg.text` line you added in Task 2 Step 10:

```python
        if not self._require_executor(update.effective_user.id, update.effective_user.username or ""):
            return await msg.reply_text("Read-only access — your role is viewer.")
```

For `_on_skills` (line 865), gate **only** the activation path. Read the handler — if it both lists skills and activates one based on arguments, split the gate so listing is allowed for viewers. If the handler's first behavior is "always list/show picker", viewers can use it; only the actual `save` / activation should be gated. Refer to the existing handler logic; the gate goes just before any code that mutates project state (e.g., calls to `save_skill`, `_patch_config`, `self._active_skill = ...`).

- [ ] **Step 7: Edit `bot.py` — pass `allowed_users` through `run_bot` and `run_bots`**

`run_bot` signature (line 1803) — add right after `trusted_user_ids: list[int] | None = None,`:

```python
    allowed_users: list | None = None,
```

Inside `run_bot`, the `bot = ProjectBot(...)` call (around line 1836) gets:

```python
        allowed_users=allowed_users,
```

`run_bots` (line 1866) — when computing `effective_trusted_ids` etc., add:

```python
        effective_allowed_users = proj.allowed_users or []
```

and pass into `run_bot(...)`:

```python
            allowed_users=effective_allowed_users,
```

- [ ] **Step 8: Plugin commands gating**

After Task 2 added plugin commands registration via `app.add_handler(CommandHandler(bc.command, bc.handler))` in `_init_plugins`, wrap the plugin command handler so it gates on the executor role unless `bc.viewer_ok` is True.

Inside `_init_plugins`, replace this fragment:

```python
            for bc in cmds:
                handler = CommandHandler(bc.command, bc.handler)
                app.add_handler(handler)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(handler)
```

with:

```python
            for bc in cmds:
                wrapped_handler = self._wrap_plugin_command(bc)
                handler = CommandHandler(bc.command, wrapped_handler)
                app.add_handler(handler)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(handler)
```

And add this helper on `ProjectBot`, near the other plugin helpers:

```python
    def _wrap_plugin_command(self, bc):
        """Wrap a plugin command handler with auth + role gating."""
        from functools import wraps
        handler = bc.handler
        viewer_ok = bc.viewer_ok

        @wraps(handler)
        async def _wrapped(update, ctx):
            user = update.effective_user
            if not user or not self._auth(user):
                return
            if not viewer_ok:
                if not self._require_executor(user.id, user.username or ""):
                    msg = update.effective_message
                    if msg:
                        await msg.reply_text("Read-only access — your role is viewer.")
                    return
            await handler(update, ctx)

        return _wrapped
```

- [ ] **Step 9: Run tests to verify they pass**

```bash
pytest tests/test_auth_roles.py -v
pytest tests/test_bot_plugin_hooks.py -v
```
Expected: All tests PASS.

- [ ] **Step 10: Run the full suite for regressions**

```bash
pytest -q
```
Expected: All previously-passing tests still pass. The role gate is inert for any project with empty `allowed_users` (the default), so existing tests should not see behavior changes.

- [ ] **Step 11: Commit**

```bash
git add src/link_project_to_chat/_auth.py src/link_project_to_chat/bot.py tests/test_auth_roles.py
git commit -m "$(cat <<'EOF'
feat(auth): add optional viewer/executor role enforcement

AuthMixin gains _get_user_role and _require_executor. When a project has
populated allowed_users, state-changing handlers (Claude messages, /run,
/model, /effort, /thinking, /permissions, /compact, /reset, skill/persona
mutations, /voice, /lang, /halt, /resume, file uploads, plugin commands
without viewer_ok) reply 'Read-only access' to viewers. Legacy projects
(empty allowed_users) are unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manager UI — plugin toggle

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/manager/test_bot_plugins.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/manager/test_bot_plugins.py`:
```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot


def _make_manager(monkeypatch, projects: dict | None = None) -> ManagerBot:
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


def test_plugins_markup_marks_active_and_available(monkeypatch):
    projects = {"myp": {"plugins": [{"name": "demo"}]}}
    bot = _make_manager(monkeypatch, projects)
    fake_active = MagicMock(); fake_active.name = "demo"
    fake_other = MagicMock(); fake_other.name = "other"
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.importlib.metadata.entry_points",
        lambda group: [fake_active, fake_other] if group == "lptc.plugins" else [],
    )
    markup = bot._plugins_markup("myp")
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any(l.startswith("✓ demo") for l in labels)
    assert any(l.startswith("+ other") for l in labels)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/manager/test_bot_plugins.py -v
```
Expected: FAIL — `_available_plugins` / `_plugins_markup` not defined.

- [ ] **Step 3: Edit `manager/bot.py` — add `importlib` import**

At the top of `src/link_project_to_chat/manager/bot.py`, ensure `import importlib.metadata` is present (search the existing imports first; if absent, add it among the stdlib imports).

- [ ] **Step 4: Edit `manager/bot.py` — add Plugins button to project detail keyboard**

`_proj_detail_markup` is at line 1516. Insert a new button row right before the `Edit` button row (around line 1523):

```python
        rows.append([InlineKeyboardButton("Plugins", callback_data=f"proj_plugins_{name}")])
```

- [ ] **Step 5: Edit `manager/bot.py` — add `_available_plugins` and `_plugins_markup`**

Add these methods on `ManagerBot`, near the other `_proj_*` helpers (e.g., right before `_proj_detail_markup` at line 1516):

```python
    def _available_plugins(self) -> list[str]:
        eps = importlib.metadata.entry_points(group="lptc.plugins")
        return sorted(ep.name for ep in eps)

    def _plugins_markup(self, name: str) -> InlineKeyboardMarkup:
        projects = self._load_projects()
        active = {p.get("name") for p in projects.get(name, {}).get("plugins", [])}
        available = self._available_plugins()
        rows = []
        for plugin_name in available:
            label = f"✓ {plugin_name}" if plugin_name in active else f"+ {plugin_name}"
            rows.append([InlineKeyboardButton(label, callback_data=f"proj_ptog_{plugin_name}|{name}")])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")])
        return InlineKeyboardMarkup(rows)
```

- [ ] **Step 6: Edit `manager/bot.py` — add callback handlers**

`_on_callback` is at line 1528. Add these two `elif` branches before the final fall-through (place them with the other `proj_*` branches, e.g., right after the `proj_remove_` branch at line 1656):

```python
        elif data.startswith("proj_plugins_"):
            name = data[len("proj_plugins_"):]
            available = self._available_plugins()
            if not available:
                await query.edit_message_text(
                    "No plugins installed.\n\n"
                    "Install the link-project-to-chat-plugins package to add plugins.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")]]
                    ),
                )
            else:
                await query.edit_message_text(
                    f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                    reply_markup=self._plugins_markup(name),
                )

        elif data.startswith("proj_ptog_"):
            suffix = data[len("proj_ptog_"):]
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
            await query.edit_message_text(
                f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                reply_markup=self._plugins_markup(name),
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
Expected: All previously-passing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/manager/test_bot_plugins.py
git commit -m "$(cat <<'EOF'
feat(manager): add Plugins toggle UI for projects

Per-project keyboard gains a Plugins button. The toggle screen lists
installed lptc.plugins entry points and lets the user activate/deactivate
each one per-project. Restart-required hint shown after toggles.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Docs and final verification

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml` (version bump)
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update README with a Plugins section**

Open `README.md` and insert a new section after the existing feature documentation (a good anchor is after the "Skills" section, before any "Manager bot" section — locate it by reading the file). Add:

````markdown
## Plugins

Plugins extend the project bot with custom Telegram commands, message
handlers, task hooks, and Claude prompt context. They live in separate
Python packages and are discovered via the `lptc.plugins` entry point group.

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
    depends_on = []  # other plugin names that must start first

    async def start(self):
        ...

    async def stop(self):
        ...

    async def on_message(self, user_id, username, chat_id, message_id, text=""):
        return False  # True consumes the message; Claude is skipped

    def get_context(self):
        return "Extra system-prompt context"

    def commands(self):
        async def hello(update, ctx):
            await update.message.reply_text("hi")
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
(listing only), and any plugin command flagged `viewer_ok`. Executors have
the full command set. When `allowed_users` is unset, the legacy
`allowed_usernames` model applies — all authorized users are effectively
executors.
````

- [ ] **Step 2: Bump version**

In `pyproject.toml`, find `version = "0.12.0"` and change to:

```toml
version = "0.13.0"
```

- [ ] **Step 3: Update CHANGELOG**

Open `docs/CHANGELOG.md` and prepend a new entry. Read the existing top of the file first to match its format. Add an entry like:

```markdown
## 0.13.0 — 2026-05-13

### Added
- Plugin framework (`plugin.py`) with `Plugin` base class, `PluginContext`,
  entry-point-based discovery via `lptc.plugins`, topological start order,
  lifecycle hooks (`start`/`stop`/`on_message`/`on_task_complete`/`on_tool_use`),
  Claude prompt prepend via `get_context()`, and command/callback registration.
- `plugin-call <project> <plugin_name> <tool_name> <args_json>` CLI subcommand
  for invoking plugin tools (used by Claude via Bash).
- Plugin toggle UI in the manager bot (per-project, restart-required).
- Optional `AllowedUser` role model (`viewer` / `executor`) — opt-in per
  project via the new `allowed_users` field. Legacy `allowed_usernames` keep
  working unchanged.
- Operational scripts `scripts/restart.sh` and `scripts/stop.sh` for the
  manager process.

### Notes
- Plugins are external Python packages. The framework is in this repo; example
  plugins (e.g., `in-app-web-server`, `diff-reviewer`) live in the separate
  `link-project-to-chat-plugins` package.
```

- [ ] **Step 4: Manual smoke test (run by hand, not part of pytest)**

Run each of these by hand against a real bot before considering the merge complete:

  1. With no `plugins` configured on any project and no `allowed_users`, start the bot. Send messages, run `/tasks`, run `/model`. Verify identical behavior to pre-merge.
  2. Create a small stub plugin in a separate directory:
     ```python
     # stub_plugin/__init__.py
     from link_project_to_chat.plugin import Plugin, BotCommand

     class StubPlugin(Plugin):
         name = "stub"
         async def start(self):
             print("STUB START")
         def commands(self):
             async def h(update, ctx):
                 await update.message.reply_text("stub OK")
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
  3. Add `"plugins": [{"name": "stub"}]` to one project. Start the bot. Verify "STUB START" in logs. Send `/stub` in Telegram. Expect reply "stub OK".
  4. Add `"allowed_users": [{"username": "<your-handle>", "role": "viewer"}]` to that same project. Restart. Send `/run echo hi` — expect "Read-only access". Send `/tasks` — expect normal listing.
  5. Change role to `"executor"` for the same user. Restart. Send `/run echo hi` — expect normal execution.

- [ ] **Step 5: Run the full suite once more**

```bash
pytest -q
```
Expected: All tests PASS.

- [ ] **Step 6: Final commit**

```bash
git add README.md pyproject.toml docs/CHANGELOG.md docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md
git commit -m "$(cat <<'EOF'
docs: plugin system, role-based access, version 0.13.0

README gains a Plugins section covering activation, plugin authoring,
and role-based access. CHANGELOG entry summarizes the merge. Version
bump to 0.13.0.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Open the branch for review**

The implementation is complete. Decide with the user whether to:
- Open a PR for review (recommended), or
- Merge to `main` directly after a self-review of `git log main..feat/plugin-system`.

For a PR:
```bash
git push -u origin feat/plugin-system
gh pr create --title "Merge GitLab plugin system into primary fork" --body "$(cat <<'EOF'
## Summary
- Adds plugin framework (`plugin.py`) with entry-point discovery, lifecycle hooks, command/callback registration, and Claude prompt prepend
- Adds manager-bot plugin toggle UI and `plugin-call` CLI subcommand
- Adds optional `AllowedUser` viewer/executor role model (opt-in per project; legacy `allowed_usernames` untouched)
- Adds operational scripts (`restart.sh`, `stop.sh`)
- Bumps version to 0.13.0

Design doc: `docs/superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md`
Implementation plan: `docs/superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md`

## Test plan
- [x] `pytest -q` passes locally on every commit
- [ ] Manual smoke test with stub plugin (see plan Step 4 of Task 7)
- [ ] Manual viewer/executor verification (see plan Step 4 of Task 7)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Verification gates

After every task, the following must hold:

1. `pytest -q` is green.
2. New tests for the task pass.
3. `git diff main..feat/plugin-system -- src/link_project_to_chat/ | wc -l` grows monotonically (no accidental reverts).
4. No existing source files are deleted.

If any gate fails, **STOP** and reconcile before continuing to the next task.

## Out-of-scope reminders

The following are intentionally NOT in this plan and should NOT be added without a new spec:
- Replacing `allowed_usernames` / `trusted_user_ids` on team bots.
- Bringing in GitLab's `dual auth`, `config write lock`, or `_auth.py` rewrite.
- Bringing in GitLab's `task_manager.py` changes.
- Importing the external `link-project-to-chat-plugins` package or its plugin implementations.
- Writing a plugin (`in-app-web-server`, `diff-reviewer`, etc.) — those live in a separate package.
