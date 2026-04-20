"""Integration tests for TelegramTransport using a lightweight Application stub.

We don't require a live Telegram connection — `telegram.ext.Application` accepts
a mock `Bot` and we can drive it via its message-handling entry points.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.telegram import (
    TRANSPORT_ID,
    TelegramTransport,
)


def _make_transport_with_mock_bot() -> tuple[TelegramTransport, MagicMock]:
    """Return (transport, mock_bot) where mock_bot has async send_message/etc."""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(
        message_id=42,
        chat=SimpleNamespace(id=12345, type="private"),
    ))
    app = MagicMock()
    app.bot = bot
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    return TelegramTransport(app), bot


async def test_send_text_calls_bot_send_message():
    t, bot = _make_transport_with_mock_bot()
    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)

    ref = await t.send_text(chat, "hello")

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["text"] == "hello"
    assert ref.native_id == "42"
    assert ref.chat == chat


async def test_start_and_stop_delegate_to_application():
    t, _bot = _make_transport_with_mock_bot()
    await t.start()
    t._app.initialize.assert_awaited_once()
    t._app.start.assert_awaited_once()
    t._app.updater.start_polling.assert_awaited_once()

    await t.stop()
    t._app.updater.stop.assert_awaited_once()
    t._app.stop.assert_awaited_once()
    t._app.shutdown.assert_awaited_once()


async def test_on_message_handler_fires_on_telegram_update():
    """Inbound text message from telegram lands as IncomingMessage on the handler."""
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    # Build a minimal telegram.Update-shaped object.
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="hi there",
        photo=None,
        document=None,
        voice=None,
        audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    # Drive the transport's internal message dispatcher directly.
    await t._dispatch_message(update, ctx=None)

    assert len(received) == 1
    assert received[0].text == "hi there"
    assert received[0].sender.handle == "alice"
    assert received[0].chat.native_id == "12345"
