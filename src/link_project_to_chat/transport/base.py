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


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
CommandHandler = Callable[[CommandInvocation], Awaitable[None]]
ButtonHandler = Callable[[ButtonClick], Awaitable[None]]


class Transport(Protocol):
    """A concrete chat platform. See spec #0 for implementation rules."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_text(
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef: ...

    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None: ...

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef: ...

    def on_message(self, handler: MessageHandler) -> None: ...
    def on_command(self, name: str, handler: CommandHandler) -> None: ...
    def on_button(self, handler: ButtonHandler) -> None: ...
