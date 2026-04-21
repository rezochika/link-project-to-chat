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


async def test_on_command_handler_fires_for_telegram_command():
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(ci):
        captured.append(ci)

    t.on_command("help", handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=77,
        chat=tg_chat,
        from_user=tg_user,
        text="/help",
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)
    ctx = SimpleNamespace(args=[])

    await t._dispatch_command("help", update, ctx)

    assert len(captured) == 1
    assert captured[0].name == "help"
    assert captured[0].args == []
    assert captured[0].raw_text == "/help"


async def test_edit_text_calls_edit_message_text():
    t, bot = _make_transport_with_mock_bot()
    bot.edit_message_text = AsyncMock()

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    ref = MessageRef(transport_id=TRANSPORT_ID, native_id="99", chat=chat)

    await t.edit_text(ref, "updated text")

    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["message_id"] == 99
    assert kwargs["text"] == "updated text"


async def test_send_text_with_buttons_passes_inline_keyboard():
    t, bot = _make_transport_with_mock_bot()
    from link_project_to_chat.transport import Button, Buttons

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    buttons = Buttons(rows=[[Button(label="Go", value="go"), Button(label="Stop", value="stop")]])

    await t.send_text(chat, "pick one", buttons=buttons)

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    markup = kwargs["reply_markup"]
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    row = markup.inline_keyboard[0]
    assert len(row) == 2
    assert row[0].text == "Go"
    assert row[0].callback_data == "go"


async def test_edit_text_with_buttons_passes_inline_keyboard():
    t, bot = _make_transport_with_mock_bot()
    bot.edit_message_text = AsyncMock()
    from link_project_to_chat.transport import Button, Buttons

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    ref = MessageRef(transport_id=TRANSPORT_ID, native_id="99", chat=chat)
    buttons = Buttons(rows=[[Button(label="Ok", value="ok")]])

    await t.edit_text(ref, "new text", buttons=buttons)

    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["reply_markup"] is not None


async def test_incoming_message_populates_files_from_document(tmp_path):
    """Document attachments get downloaded and exposed as IncomingFile."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)

    t.on_message(handler)

    downloaded_bytes = b"hello doc"
    target_path: list = []

    async def fake_download_to_drive(path):
        # Simulate telegram's download by writing bytes to the given path.
        p = path if hasattr(path, "write_bytes") else __import__("pathlib").Path(str(path))
        p.write_bytes(downloaded_bytes)
        target_path.append(p)

    tg_file_obj = SimpleNamespace(download_to_drive=fake_download_to_drive)

    async def fake_get_file():
        return tg_file_obj

    tg_document = SimpleNamespace(
        file_name="notes.txt",
        mime_type="text/plain",
        file_size=len(downloaded_bytes),
        get_file=fake_get_file,
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=200,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=None,
        document=tg_document,
        voice=None,
        audio=None,
        caption="see this file",
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.original_name == "notes.txt"
    assert f.mime_type == "text/plain"
    assert f.path.read_bytes() == downloaded_bytes
    # Caption becomes the message text when no text is set.
    assert captured[0].text == "see this file"


async def test_send_file_calls_send_document_for_non_image(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(
        message_id=200, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    path = tmp_path / "notes.txt"
    path.write_text("x")

    ref = await t.send_file(chat, path, caption="see")

    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["caption"] == "see"
    assert ref.native_id == "200"


async def test_send_file_calls_send_photo_for_image(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(
        message_id=201, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    path = tmp_path / "pic.png"
    path.write_bytes(b"\x89PNG\r\n")

    await t.send_file(chat, path)

    bot.send_photo.assert_awaited_once()


async def test_on_button_fires_for_telegram_callback_query():
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(click):
        captured.append(click)

    t.on_button(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(message_id=99, chat=tg_chat)
    tg_query = SimpleNamespace(
        data="confirm_reset",
        from_user=tg_user,
        message=tg_msg,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=tg_query, effective_user=tg_user)

    await t._dispatch_button(update, ctx=None)

    assert len(captured) == 1
    assert captured[0].value == "confirm_reset"
    assert captured[0].sender.handle == "alice"
    tg_query.answer.assert_awaited_once()


async def test_send_voice_calls_bot_send_voice(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=300, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    ref = await t.send_voice(chat, p)

    bot.send_voice.assert_awaited_once()
    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert ref.native_id == "300"


async def test_send_voice_passes_reply_to(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=301, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_ref = MessageRef(transport_id=TRANSPORT_ID, native_id="42", chat=chat)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    await t.send_voice(chat, p, reply_to=reply_ref)

    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 42


async def test_incoming_message_populates_files_from_voice(tmp_path):
    """Voice attachments get downloaded and exposed as IncomingFile with audio mime."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)

    t.on_message(handler)

    downloaded_bytes = b"ogg-voice"

    async def fake_download_to_drive(path):
        from pathlib import Path as _P
        p = path if hasattr(path, "write_bytes") else _P(str(path))
        p.write_bytes(downloaded_bytes)

    tg_file_obj = SimpleNamespace(download_to_drive=fake_download_to_drive)

    async def fake_get_file():
        return tg_file_obj

    tg_voice = SimpleNamespace(
        file_id="abc",
        file_size=len(downloaded_bytes),
        get_file=fake_get_file,
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=300,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=None,
        document=None,
        voice=tg_voice,
        audio=None,
        caption=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.mime_type == "audio/ogg"
    assert f.path.read_bytes() == downloaded_bytes


async def test_default_error_handler_logs_on_exception(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        await t._default_error_handler(update, ctx)
    assert any("boom" in rec.getMessage() for rec in caplog.records)


async def test_default_error_handler_logs_conflict_as_warning(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("Conflict: another bot instance"))
    with caplog.at_level(logging.WARNING):
        await t._default_error_handler(update, ctx)
    conflict_recs = [r for r in caplog.records if "Conflict" in r.getMessage()]
    assert conflict_recs
    assert all(r.levelno == logging.WARNING for r in conflict_recs)


async def test_start_fires_on_ready_with_bot_identity():
    """start() should perform post-init (delete_webhook + get_me + set_my_commands)
    and fire registered on_ready callbacks with the bot's own Identity."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=9876, full_name="Alice Bot", username="alicebot",
    ))
    bot.set_my_commands = AsyncMock()

    # Attach a menu so set_my_commands gets called.
    t._menu = [("help", "Show help")]

    captured: list = []

    async def cb(identity):
        captured.append(identity)

    t.on_ready(cb)

    await t.start()

    bot.delete_webhook.assert_awaited_once()
    bot.get_me.assert_awaited_once()
    bot.set_my_commands.assert_awaited_once()
    assert len(captured) == 1
    assert captured[0].native_id == "9876"
    assert captured[0].handle == "alicebot"
    assert captured[0].is_bot is True


async def test_build_accepts_menu_kwarg():
    """TelegramTransport.build(menu=...) must accept and store the menu."""
    # We can't easily test the full build() because it instantiates a real
    # Application. Instead: construct directly and test _menu storage.
    from link_project_to_chat.transport.telegram import TelegramTransport
    _mock_app = AsyncMock()
    t = TelegramTransport(_mock_app)
    t._menu = [("cmd", "desc")]
    assert t._menu == [("cmd", "desc")]


async def test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix():
    """Inbound text starting with '[auto-relay from <handle>]' is marked as relayed
    and the prefix is stripped from the dispatched IncomingMessage.text."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=-100123, type="supergroup")
    tg_user = SimpleNamespace(id=42, full_name="Rezo", username="rezo", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="[auto-relay from bot_a]\n\n@bot_b go do X",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert captured[0].is_relayed_bot_to_bot is True
    assert captured[0].text == "@bot_b go do X"


async def test_dispatch_non_relay_text_unchanged():
    """Messages without the relay prefix have is_relayed_bot_to_bot=False and text unchanged."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hello world",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert captured[0].is_relayed_bot_to_bot is False
    assert captured[0].text == "hello world"


async def test_enable_team_relay_lifecycle():
    """enable_team_relay stashes config; start() starts the relay; stop() stops it."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    mock_client = MagicMock()
    mock_client.add_event_handler = MagicMock(return_value=object())
    mock_client.remove_event_handler = MagicMock()

    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    await t.start()
    mock_client.add_event_handler.assert_called_once()

    await t.stop()
    mock_client.remove_event_handler.assert_called_once()


async def test_build_without_enable_team_relay_starts_and_stops_cleanly():
    """TelegramTransport without a team relay starts/stops without touching relay code."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    await t.start()
    await t.stop()


async def test_app_property_returns_underlying_application():
    """TelegramTransport.app exposes the underlying telegram.ext.Application
    so the manager bot can attach ConversationHandlers directly."""
    t, _bot = _make_transport_with_mock_bot()
    app = t.app
    assert app is t._app  # exposes the same instance
    assert hasattr(app, "add_handler")  # quacks like an Application
