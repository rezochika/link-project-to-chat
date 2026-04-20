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
        raise NotImplementedError("Wired in Task 5")

    async def stop(self) -> None:
        raise NotImplementedError("Wired in Task 5")

    # ── Outbound ──────────────────────────────────────────────────────────
    async def send_text(
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef:
        raise NotImplementedError("Wired in Task 5")

    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None:
        raise NotImplementedError("Wired in Task 15")

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        raise NotImplementedError("Wired in Task 22")

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)
