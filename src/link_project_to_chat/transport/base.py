"""Transport Protocol and primitive types.

See docs/superpowers/specs/2026-04-20-transport-abstraction-design.md section 4.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ChatKind(Enum):
    DM = "dm"
    ROOM = "room"


@dataclass(frozen=True)
class ChatRef:
    """Opaque reference to a conversation target."""
    transport_id: str
    native_id: str
    kind: ChatKind


@dataclass(frozen=True)
class Identity:
    """Who sent a message. Transport-agnostic."""
    transport_id: str
    native_id: str
    display_name: str
    handle: str | None
    is_bot: bool


@dataclass(frozen=True)
class MessageRef:
    """Opaque reference to a sent message."""
    transport_id: str
    native_id: str
    chat: ChatRef


class ButtonStyle(Enum):
    DEFAULT = "default"
    PRIMARY = "primary"
    DANGER = "danger"


@dataclass(frozen=True)
class Button:
    label: str
    value: str
    style: ButtonStyle = ButtonStyle.DEFAULT


@dataclass(frozen=True)
class Buttons:
    rows: list[list[Button]]


@dataclass(frozen=True)
class ButtonClick:
    chat: ChatRef
    message: MessageRef
    sender: Identity
    value: str


@dataclass(frozen=True)
class IncomingFile:
    """An attachment already downloaded to local disk.

    Lifetime: cleaned up by the Transport after the IncomingMessage handler returns.
    """
    path: Path
    original_name: str
    mime_type: str | None
    size_bytes: int


@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    native: Any = None


@dataclass(frozen=True)
class CommandInvocation:
    chat: ChatRef
    sender: Identity
    name: str
    args: list[str]
    raw_text: str
    message: MessageRef
    native: Any = None


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
CommandHandler = Callable[[CommandInvocation], Awaitable[None]]
ButtonHandler = Callable[[ButtonClick], Awaitable[None]]
OnReadyCallback = Callable[["Identity"], Awaitable[None]]


class TransportRetryAfter(Exception):
    """Raised when a Transport's underlying platform asks the caller to back off.

    Transports should catch platform-specific rate-limit errors internally and
    re-raise as this if the caller needs to know (e.g., for throttle adaptation).
    `retry_after` is in seconds.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class Transport(Protocol):
    """A concrete chat platform. See spec #0 for implementation rules.

    `html` on send_text/edit_text is a portable rich-text hint: when True, the
    text contains platform-agnostic HTML-like markup (a subset telegram supports
    natively — see formatting.md_to_telegram). Transports that don't natively
    render HTML strip or convert it. Required floor: every Transport MUST accept
    html=True without error, even if the rendering degrades.

    `reply_to` on send_text attaches the new message as a reply to an earlier
    one. Transports that don't support thread-style replies MAY ignore the hint.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef: ...

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None: ...

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef: ...

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        """Send an audio file as a platform-appropriate voice message.

        Telegram renders as a voice note with waveform UI; Discord as an audio
        attachment; other transports render per their platform conventions.
        Transports that don't distinguish voice from file MAY delegate to send_file.
        """
        ...

    async def send_typing(self, chat: ChatRef) -> None:
        """Emit a typing indicator. One-shot; caller loops if a sustained
        indicator is needed. Transports that don't support it MAY no-op.
        """
        ...

    def on_message(self, handler: MessageHandler) -> None: ...
    def on_command(self, name: str, handler: CommandHandler) -> None: ...
    def on_button(self, handler: ButtonHandler) -> None: ...

    def on_ready(self, callback: OnReadyCallback) -> None:
        """Register a callback fired after the Transport completes platform-specific
        startup (e.g., identity discovery, menu registration). Called once per process
        with the bot's own Identity as argument.
        """
        ...
