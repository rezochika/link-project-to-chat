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

        bot.py owns the ApplicationBuilder; this class just uses the Application.
        """
        self._app = application
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []

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
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef:
        # buttons handled in Task 17; ignore here.
        native_msg = await self._app.bot.send_message(
            chat_id=int(chat.native_id),
            text=text,
        )
        return message_ref_from_telegram(native_msg)

    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None:
        # buttons handled in Task 17; ignore here.
        await self._app.bot.edit_message_text(
            chat_id=int(msg.chat.native_id),
            message_id=int(msg.native_id),
            text=text,
        )

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        raise NotImplementedError("Wired in Task 22")

    # ── Inbound dispatch ──────────────────────────────────────────────────
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        """Convert a telegram Update into IncomingMessage and invoke handlers.

        Called from the MessageHandler wired on the Application by bot.py
        during the strangler port (Task 9). For now, tests call this directly.
        """
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        from .base import IncomingMessage
        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=msg.text or "",
            files=[],  # populated in Task 21
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
        )
        for h in self._message_handlers:
            await h(incoming)

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
