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
