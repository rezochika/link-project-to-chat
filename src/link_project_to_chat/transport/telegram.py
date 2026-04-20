"""TelegramTransport — python-telegram-bot adapter for the Transport Protocol.

This module is the ONLY place in the codebase that imports `telegram` after
spec #0 step 9 (lockout). bot.py talks to the interface; everything
Telegram-specific lives behind it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import (
    ButtonHandler,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    Identity,
    MessageHandler,
    MessageRef,
)

TRANSPORT_ID = "telegram"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _buttons_to_inline_keyboard(buttons: Buttons | None) -> Any:
    """Convert a Buttons primitive to telegram's InlineKeyboardMarkup (or None)."""
    if buttons is None:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=b.label, callback_data=b.value) for b in row]
        for row in buttons.rows
    ])


def chat_ref_from_telegram(chat: Any) -> ChatRef:
    """Map a telegram.Chat (or duck-typed equivalent with .id and .type) to ChatRef."""
    kind = ChatKind.DM if chat.type == "private" else ChatKind.ROOM
    return ChatRef(transport_id=TRANSPORT_ID, native_id=str(chat.id), kind=kind)


def identity_from_telegram_user(user: Any) -> Identity:
    """Map a telegram.User (or duck-typed equivalent) to Identity."""
    return Identity(
        transport_id=TRANSPORT_ID,
        native_id=str(user.id),
        display_name=user.full_name,
        handle=user.username,
        is_bot=user.is_bot,
    )


def message_ref_from_telegram(msg: Any) -> MessageRef:
    """Map a telegram.Message to MessageRef."""
    return MessageRef(
        transport_id=TRANSPORT_ID,
        native_id=str(msg.message_id),
        chat=chat_ref_from_telegram(msg.chat),
    )


class TelegramTransport:
    """python-telegram-bot adapter. All Protocol methods raise NotImplementedError
    until populated in subsequent tasks (spec strangler steps 3–8).
    """

    TRANSPORT_ID = TRANSPORT_ID

    def __init__(self, application: Any) -> None:
        """Construct from an already-built telegram.ext.Application.

        Prefer the `build()` classmethod for production use; this constructor
        exists so tests can inject a mocked Application.
        """
        self._app = application
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []

    @classmethod
    def build(cls, token: str, *, concurrent_updates: bool = True, post_init: Any = None) -> "TelegramTransport":
        """Construct a TelegramTransport with a polling-mode Application.

        Caller passes the bot token and optional post_init hook. The telegram
        Application is created and wrapped in one step.
        """
        from telegram.ext import ApplicationBuilder
        builder = ApplicationBuilder().token(token).concurrent_updates(concurrent_updates)
        if post_init is not None:
            builder = builder.post_init(post_init)
        app = builder.build()
        return cls(app)

    def attach_telegram_routing(
        self,
        *,
        group_mode: bool,
        command_names: list[str],
    ) -> None:
        """Wire telegram's MessageHandler/CommandHandler/CallbackQueryHandler
        so all incoming updates route through our _dispatch_* methods.

        Called by the bot once during setup. Encapsulates every remaining
        telegram import (filters, handler types) inside this module.
        """
        from telegram.ext import (
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        if group_mode:
            chat_filter = filters.ChatType.GROUPS
            incoming_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
        else:
            chat_filter = filters.ChatType.PRIVATE
            incoming_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & (filters.TEXT | filters.Document.ALL | filters.PHOTO)
                & ~filters.COMMAND
            )

        self._app.add_handler(MessageHandler(incoming_filter, self._dispatch_message))

        for name in command_names:
            self._app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._dispatch_command(_n, u, c),
                filters=chat_filter,
            ))

        self._app.add_handler(CallbackQueryHandler(self._dispatch_button))

    @property
    def app(self) -> Any:
        """Direct access to the underlying telegram.ext.Application.

        For legacy bot.py code that still needs to call bot methods directly
        (e.g., set_my_commands at post_init time). New code should go through
        the Transport interface.
        """
        return self._app

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    # ── Outbound ──────────────────────────────────────────────────────────
    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        # buttons handled in Task 17; ignore here.
        kwargs: dict[str, Any] = {
            "chat_id": int(chat.native_id),
            "text": text,
        }
        if html:
            kwargs["parse_mode"] = "HTML"
        if reply_to is not None:
            kwargs["reply_to_message_id"] = int(reply_to.native_id)
        if buttons is not None:
            kwargs["reply_markup"] = _buttons_to_inline_keyboard(buttons)
        try:
            native_msg = await self._app.bot.send_message(**kwargs)
        except Exception as e:
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                from .base import TransportRetryAfter
                raise TransportRetryAfter(float(retry_after)) from e
            raise
        return message_ref_from_telegram(native_msg)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None:
        # buttons handled in Task 17; ignore here.
        kwargs: dict[str, Any] = {
            "chat_id": int(msg.chat.native_id),
            "message_id": int(msg.native_id),
            "text": text,
        }
        if html:
            kwargs["parse_mode"] = "HTML"
        if buttons is not None:
            kwargs["reply_markup"] = _buttons_to_inline_keyboard(buttons)
        try:
            await self._app.bot.edit_message_text(**kwargs)
        except Exception as e:
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                from .base import TransportRetryAfter
                raise TransportRetryAfter(float(retry_after)) from e
            raise

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        suffix = path.suffix.lower()
        chat_id = int(chat.native_id)
        if suffix in _IMAGE_SUFFIXES:
            with path.open("rb") as fh:
                native = await self._app.bot.send_photo(
                    chat_id=chat_id, photo=fh, caption=caption,
                )
        else:
            with path.open("rb") as fh:
                native = await self._app.bot.send_document(
                    chat_id=chat_id, document=fh, caption=caption, filename=display_name,
                )
        return message_ref_from_telegram(native)

    async def send_typing(self, chat: ChatRef) -> None:
        try:
            await self._app.bot.send_chat_action(
                chat_id=int(chat.native_id), action="typing",
            )
        except Exception:
            # Typing indicators are best-effort; never fatal.
            pass

    # ── Inbound dispatch ──────────────────────────────────────────────────
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        """Convert a telegram Update into IncomingMessage and invoke handlers.

        Downloads photo/document attachments to a per-handler temp directory.
        The tempdir is stashed on the native message so it lives until handler
        completion; GC then cleans it up.
        """
        import tempfile

        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        from .base import IncomingFile, IncomingMessage

        files: list[IncomingFile] = []

        photo = getattr(msg, "photo", None)
        if photo:
            largest = photo[-1]
            tmpdir = tempfile.TemporaryDirectory()
            path = Path(tmpdir.name) / "photo.jpg"
            tg_file = await largest.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name="photo.jpg",
                mime_type="image/jpeg",
                size_bytes=getattr(largest, "file_size", 0) or 0,
            ))
            msg._transport_tmpdirs = getattr(msg, "_transport_tmpdirs", []) + [tmpdir]

        doc = getattr(msg, "document", None)
        if doc is not None:
            tmpdir = tempfile.TemporaryDirectory()
            name = getattr(doc, "file_name", None) or "document"
            path = Path(tmpdir.name) / name
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=name,
                mime_type=getattr(doc, "mime_type", None),
                size_bytes=getattr(doc, "file_size", 0) or 0,
            ))
            msg._transport_tmpdirs = getattr(msg, "_transport_tmpdirs", []) + [tmpdir]

        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=msg.text or getattr(msg, "caption", None) or "",
            files=files,
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
        )
        for h in self._message_handlers:
            await h(incoming)

    async def _dispatch_button(self, update: Any, ctx: Any) -> None:
        """Convert a telegram CallbackQuery into ButtonClick and invoke handlers.

        Also answers the query to dismiss the client-side loading spinner.
        """
        query = update.callback_query
        if query is None:
            return
        try:
            await query.answer()
        except Exception:
            pass  # already answered or expired; not fatal
        from .base import ButtonClick
        click = ButtonClick(
            chat=chat_ref_from_telegram(query.message.chat),
            message=message_ref_from_telegram(query.message),
            sender=identity_from_telegram_user(query.from_user),
            value=query.data or "",
        )
        for h in self._button_handlers:
            await h(click)

    async def _dispatch_command(self, name: str, update: Any, ctx: Any) -> None:
        """Convert a telegram command Update into CommandInvocation and invoke the handler."""
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        from .base import CommandInvocation
        ci = CommandInvocation(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            name=name,
            args=list(getattr(ctx, "args", []) or []),
            raw_text=msg.text or "",
            message=message_ref_from_telegram(msg),
            native=(update, ctx),
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            await handler(ci)

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)
