from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport import ChatKind, ChatRef, Identity
from link_project_to_chat.web.transport import WebTransport


async def test_projectbot_web_transport_auth_dispatches_command(tmp_path: Path):
    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="WEB",
        allowed_usernames=["alice"],
        config_path=tmp_path / "config.json",
        transport_kind="web",
        web_port=0,
    )
    bot.build()
    assert isinstance(bot._transport, WebTransport)
    transport = bot._transport
    await transport.start()
    try:
        chat = ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)
        sender = Identity(
            transport_id="web",
            native_id="web-session:abc",
            display_name="Browser Alice",
            handle="mallory",
            is_bot=False,
        )

        await transport.inject_command(chat, sender, "status", args=[], raw_text="/status")

        assert transport._store is not None
        messages = await transport._store.get_messages("default")
        assert any("Project: demo" in message["text"] for message in messages)
        assert bot._trusted_users["alice"] == "web-session:abc"
    finally:
        await transport.stop()
