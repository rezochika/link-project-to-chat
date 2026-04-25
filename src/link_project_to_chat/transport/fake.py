"""In-memory Transport for tests. Implements the full Protocol.

Handlers invoked via inject_* are awaited synchronously so tests can assert
state after a single await with no timer-settling hacks.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

from .base import (
    AuthorizerCallback,
    ButtonHandler,
    Buttons,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    ButtonClick,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
)


@dataclass
class SentMessage:
    chat: ChatRef
    text: str
    buttons: Buttons | None
    message: MessageRef
    html: bool = False
    reply_to: MessageRef | None = None


@dataclass
class EditedMessage:
    message: MessageRef
    text: str
    buttons: Buttons | None
    html: bool = False


@dataclass
class SentFile:
    chat: ChatRef
    path: Path
    caption: str | None
    display_name: str | None
    message: MessageRef


@dataclass
class SentVoice:
    chat: ChatRef
    path: Path
    reply_to: MessageRef | None
    message: MessageRef


class FakeTransport:
    """In-memory implementation of the Transport Protocol."""

    TRANSPORT_ID = "fake"
    max_text_length: int = 4096  # Match the most-restrictive transport for test parity.

    def __init__(self) -> None:
        self.sent_messages: list[SentMessage] = []
        self.edited_messages: list[EditedMessage] = []
        self.sent_files: list[SentFile] = []
        self.sent_voices: list[SentVoice] = []
        self.typing_signals: list[ChatRef] = []
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._authorizer: AuthorizerCallback | None = None
        self._msg_counter = itertools.count(1)
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        """Fake transport: no-op (tests drive dispatch synchronously)."""
        return

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
        ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat)
        self.sent_messages.append(SentMessage(
            chat=chat, text=text, buttons=buttons, message=ref, html=html, reply_to=reply_to,
        ))
        return ref

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None:
        self.edited_messages.append(EditedMessage(message=msg, text=text, buttons=buttons, html=html))

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat)
        self.sent_files.append(SentFile(chat=chat, path=path, caption=caption, display_name=display_name, message=ref))
        return ref

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat)
        self.sent_voices.append(SentVoice(chat=chat, path=path, reply_to=reply_to, message=ref))
        return ref

    async def send_typing(self, chat: ChatRef) -> None:
        self.typing_signals.append(chat)

    def render_markdown(self, md: str) -> str:
        """Return the input unchanged — FakeTransport has no rendering semantics."""
        return md

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def on_ready(self, callback: OnReadyCallback) -> None:
        self._on_ready_callbacks.append(callback)

    def set_authorizer(self, authorizer: AuthorizerCallback | None) -> None:
        # FakeTransport has no native media-download path (files arrive pre-built
        # via inject_message), so the authorizer here only gates handler dispatch.
        self._authorizer = authorizer

    # ── Test injection ────────────────────────────────────────────────────
    async def inject_message(
        self,
        chat: ChatRef,
        sender: Identity,
        text: str,
        *,
        files: list[IncomingFile] | None = None,
        reply_to: MessageRef | None = None,
        reply_to_text: str | None = None,
        reply_to_sender: Identity | None = None,
    ) -> None:
        msg_ref = MessageRef(
            transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat,
        )
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=files or [],
            reply_to=reply_to,
            native=None,
            message=msg_ref,
            reply_to_text=reply_to_text,
            reply_to_sender=reply_to_sender,
        )
        for h in self._message_handlers:
            await h(msg)

    async def inject_command(
        self,
        chat: ChatRef,
        sender: Identity,
        name: str,
        *,
        args: list[str],
        raw_text: str,
    ) -> None:
        msg_ref = MessageRef(
            transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat
        )
        ci = CommandInvocation(
            chat=chat,
            sender=sender,
            name=name,
            args=args,
            raw_text=raw_text,
            message=msg_ref,
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            await handler(ci)

    async def inject_button_click(
        self, message: MessageRef, sender: Identity, *, value: str
    ) -> None:
        click = ButtonClick(chat=message.chat, message=message, sender=sender, value=value)
        for h in self._button_handlers:
            await h(click)
