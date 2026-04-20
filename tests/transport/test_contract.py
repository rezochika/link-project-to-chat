"""Parametrized Protocol contract test — every Transport must pass."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
    Transport,
)
from link_project_to_chat.transport.fake import FakeTransport
from link_project_to_chat.transport.telegram import TelegramTransport


def _chat(transport_id: str) -> ChatRef:
    return ChatRef(transport_id=transport_id, native_id="1", kind=ChatKind.DM)


def _sender(transport_id: str) -> Identity:
    return Identity(
        transport_id=transport_id,
        native_id="1",
        display_name="Alice",
        handle="alice",
        is_bot=False,
    )


def _make_telegram_transport_with_inject() -> TelegramTransport:
    """Return a TelegramTransport wired with fake inject_* methods for testing.

    Mirrors the shape contract tests expect (inject_message/inject_command/
    inject_button_click), translating into _dispatch_* under the hood.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(
        message_id=1, chat=SimpleNamespace(id=1, type="private"),
    ))
    bot.edit_message_text = AsyncMock()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(
        message_id=2, chat=SimpleNamespace(id=1, type="private"),
    ))
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(
        message_id=3, chat=SimpleNamespace(id=1, type="private"),
    ))
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=4, chat=SimpleNamespace(id=1, type="private"),
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

    t = TelegramTransport(app)

    async def inject_message(chat, sender, text, *, files=None, reply_to=None):
        tg_chat = SimpleNamespace(id=int(chat.native_id), type="private")
        tg_user = SimpleNamespace(
            id=int(sender.native_id), full_name=sender.display_name,
            username=sender.handle, is_bot=sender.is_bot,
        )
        tg_msg = SimpleNamespace(
            message_id=100, chat=tg_chat, from_user=tg_user,
            text=text, photo=None, document=None, voice=None, audio=None, caption=None,
            reply_to_message=None,
        )
        update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)
        await t._dispatch_message(update, ctx=None)

    async def inject_command(chat, sender, name, *, args, raw_text):
        tg_chat = SimpleNamespace(id=int(chat.native_id), type="private")
        tg_user = SimpleNamespace(
            id=int(sender.native_id), full_name=sender.display_name,
            username=sender.handle, is_bot=sender.is_bot,
        )
        tg_msg = SimpleNamespace(
            message_id=101, chat=tg_chat, from_user=tg_user, text=raw_text,
            reply_to_message=None,
        )
        update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)
        ctx = SimpleNamespace(args=args)
        await t._dispatch_command(name, update, ctx)

    async def inject_button_click(message, sender, *, value):
        tg_chat = SimpleNamespace(id=int(message.chat.native_id), type="private")
        tg_user = SimpleNamespace(
            id=int(sender.native_id), full_name=sender.display_name,
            username=sender.handle, is_bot=sender.is_bot,
        )
        tg_msg = SimpleNamespace(message_id=int(message.native_id), chat=tg_chat)
        tg_query = SimpleNamespace(
            data=value, from_user=tg_user, message=tg_msg, answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=tg_query, effective_user=tg_user)
        await t._dispatch_button(update, ctx=None)

    t.inject_message = inject_message  # type: ignore[attr-defined]
    t.inject_command = inject_command  # type: ignore[attr-defined]
    t.inject_button_click = inject_button_click  # type: ignore[attr-defined]
    return t


@pytest.fixture(params=["fake", "telegram"])
def transport(request) -> Transport:
    """Yield a fresh Transport implementation per test."""
    if request.param == "fake":
        yield FakeTransport()
    elif request.param == "telegram":
        yield _make_telegram_transport_with_inject()
    else:
        pytest.fail(f"Unknown param: {request.param}")


async def test_send_text_returns_usable_message_ref(transport):
    chat = _chat(transport.TRANSPORT_ID)
    ref = await transport.send_text(chat, "hello")
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
    # edit_text on the returned ref must not raise.
    await transport.edit_text(ref, "updated")


async def test_on_message_fires_for_injected_text(transport):
    # This test requires an inject_message method — all Transports used in
    # contract tests must expose one. FakeTransport has it natively; new
    # transports provide a test fixture that wires one in (see Task 24 for Telegram).
    if not hasattr(transport, "inject_message"):
        pytest.skip(f"{type(transport).__name__} does not support inject_message")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    captured: list[IncomingMessage] = []

    async def handler(msg):
        captured.append(msg)

    transport.on_message(handler)
    await transport.inject_message(chat, sender, "ping")

    assert len(captured) == 1
    assert captured[0].text == "ping"


async def test_on_command_fires_for_injected_command(transport):
    if not hasattr(transport, "inject_command"):
        pytest.skip(f"{type(transport).__name__} does not support inject_command")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    seen: list[str] = []

    async def handler(ci):
        seen.append(ci.name)

    transport.on_command("help", handler)
    await transport.inject_command(chat, sender, "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_on_button_fires_for_injected_click(transport):
    if not hasattr(transport, "inject_button_click"):
        pytest.skip(f"{type(transport).__name__} does not support inject_button_click")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    seen: list[str] = []

    async def handler(click):
        seen.append(click.value)

    transport.on_button(handler)
    ref = await transport.send_text(chat, "pick")
    await transport.inject_button_click(ref, sender, value="go")

    assert seen == ["go"]


async def test_send_voice_returns_usable_message_ref(transport, tmp_path):
    chat = _chat(transport.TRANSPORT_ID)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")
    ref = await transport.send_voice(chat, p)
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
