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
