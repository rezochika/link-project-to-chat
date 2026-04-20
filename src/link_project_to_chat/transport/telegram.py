"""TelegramTransport — python-telegram-bot adapter for the Transport Protocol.

This module is the ONLY place in the codebase that imports `telegram` after
spec #0 step 9 (lockout). bot.py talks to the interface; everything
Telegram-specific lives behind it.
"""
from __future__ import annotations

import re
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

# Matches the prefix the Telethon relay prepends to forwarded bot-to-bot messages.
# Format: "[auto-relay from <handle>]\n\n" — note there is no '@' on the handle.
# That is intentional: '@handle' would make peer bots re-process the relayed
# message as a self-mention. See manager/team_relay.py (moves to
# transport/_telegram_relay.py in a later task) for where the prefix is written.
_RELAY_PREFIX_RE = re.compile(r"^\[auto-relay from [A-Za-z][A-Za-z0-9_]*\]\n\n")


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
        self._on_ready_callbacks: list = []
        self._menu: Any = None

    @classmethod
    def build(
        cls,
        token: str,
        *,
        concurrent_updates: bool = True,
        menu: Any = None,
    ) -> "TelegramTransport":
        """Construct a TelegramTransport with a polling-mode Application.

        Post-init work (delete_webhook, get_me, set_my_commands) runs inside
        start() — see TelegramTransport.start().
        """
        from telegram.ext import ApplicationBuilder
        app = ApplicationBuilder().token(token).concurrent_updates(concurrent_updates).build()
        instance = cls(app)
        instance._menu = menu
        return instance

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
                & (
                    filters.TEXT
                    | filters.Document.ALL
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.VIDEO_NOTE
                    | filters.Sticker.ALL
                    | filters.VIDEO
                    | filters.LOCATION
                    | filters.CONTACT
                )
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
        self._app.add_error_handler(self._default_error_handler)

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

        # Platform post-init: drain pending updates + discover own identity +
        # register /commands menu. Runs between initialize() and start() so the
        # Application is configured before polling begins.
        try:
            await self._app.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass  # non-fatal
        try:
            me = await self._app.bot.get_me()
            from .base import Identity
            self_identity = Identity(
                transport_id=TRANSPORT_ID,
                native_id=str(me.id),
                display_name=me.full_name or me.username or "bot",
                handle=(me.username or "").lower() or None,
                is_bot=True,
            )
        except Exception:
            from .base import Identity
            self_identity = Identity(
                transport_id=TRANSPORT_ID, native_id="0",
                display_name="bot", handle=None, is_bot=True,
            )
        if self._menu:
            try:
                await self._app.bot.set_my_commands(self._menu)
            except Exception:
                pass

        # Fire caller-registered callbacks with the bot's identity.
        for cb in self._on_ready_callbacks:
            await cb(self_identity)

        await self._app.start()
        await self._app.updater.start_polling()

    def on_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

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

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        kwargs: dict[str, Any] = {
            "chat_id": int(chat.native_id),
        }
        if reply_to is not None:
            kwargs["reply_to_message_id"] = int(reply_to.native_id)
        with path.open("rb") as fh:
            native = await self._app.bot.send_voice(voice=fh, **kwargs)
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

        voice = getattr(msg, "voice", None)
        if voice is not None:
            tmpdir = tempfile.TemporaryDirectory()
            path = Path(tmpdir.name) / "voice.ogg"
            tg_file = await voice.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name="voice.ogg",
                mime_type="audio/ogg",
                size_bytes=getattr(voice, "file_size", 0) or 0,
            ))
            msg._transport_tmpdirs = getattr(msg, "_transport_tmpdirs", []) + [tmpdir]

        audio = getattr(msg, "audio", None)
        if audio is not None:
            tmpdir = tempfile.TemporaryDirectory()
            name = getattr(audio, "file_name", None) or "audio"
            path = Path(tmpdir.name) / name
            tg_file = await audio.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=name,
                mime_type=getattr(audio, "mime_type", None) or "audio/mpeg",
                size_bytes=getattr(audio, "file_size", 0) or 0,
            ))
            msg._transport_tmpdirs = getattr(msg, "_transport_tmpdirs", []) + [tmpdir]

        text = msg.text or getattr(msg, "caption", None) or ""
        is_relayed = False
        # The Telethon relay posts messages with this prefix (no '@' on the handle —
        # intentional, see manager/team_relay.py (moves to transport/_telegram_relay.py
        # in a later task) comment). Detect and strip.
        relay_match = _RELAY_PREFIX_RE.match(text)
        if relay_match:
            is_relayed = True
            text = text[relay_match.end():]

        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=text,
            files=files,
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
            is_relayed_bot_to_bot=is_relayed,
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

    async def _default_error_handler(self, update: Any, ctx: Any) -> None:
        """Default handler for unhandled telegram update errors.

        Specially treats 'Conflict' errors (another bot instance) as WARNING
        rather than ERROR since they're usually operational, not bugs.
        """
        import logging
        logger_ = logging.getLogger(__name__)
        err = str(ctx.error)
        if "Conflict" in err:
            logger_.warning(
                "Conflict error (another instance?): %s | update=%s", err, update,
            )
        else:
            logger_.error("Update error: %s | update=%s", ctx.error, update)

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
