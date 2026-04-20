# Transport Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a `Transport` Protocol from the python-telegram-bot coupling in [src/link_project_to_chat/bot.py](src/link_project_to_chat/bot.py), porting the project bot's DM feature set (text, commands, streaming edits, inline buttons, file uploads) behind the interface with no behavior change.

**Architecture:** Strangler-fig refactor. A new `transport/` package defines a `Transport` Protocol plus normalized event/primitive dataclasses. A `TelegramTransport` implementation wraps python-telegram-bot. A `FakeTransport` double powers a parametrized contract test that enforces Protocol conformance across all implementations. [bot.py](src/link_project_to_chat/bot.py) is migrated feature-by-feature over 9 spec steps, each landing as an independent commit with no behavior change. Exit criteria: zero `from telegram` / `import telegram` imports remain in [bot.py](src/link_project_to_chat/bot.py).

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22.0` (existing), `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), `dataclasses`, `typing.Protocol`.

**Reference spec:** [docs/superpowers/specs/2026-04-20-transport-abstraction-design.md](docs/superpowers/specs/2026-04-20-transport-abstraction-design.md)

---

## File Structure

**Create:**
- `src/link_project_to_chat/transport/__init__.py` — package marker, re-exports public API from `base.py`.
- `src/link_project_to_chat/transport/base.py` — `Transport` Protocol + `ChatRef`, `ChatKind`, `Identity`, `MessageRef`, `Button`, `ButtonStyle`, `Buttons`, `ButtonClick`, `IncomingMessage`, `IncomingFile`, `CommandInvocation`.
- `src/link_project_to_chat/transport/telegram.py` — `TelegramTransport` implementation (wraps `telegram.ext.Application`) + native-object mapping helpers.
- `src/link_project_to_chat/transport/fake.py` — `FakeTransport` in-memory test double.
- `src/link_project_to_chat/transport/streaming.py` — `StreamingMessage` throttled-edit helper (introduced at Task 16).
- `tests/transport/__init__.py` — test-package marker.
- `tests/transport/test_fake.py` — smoke tests for `FakeTransport`.
- `tests/transport/test_contract.py` — parametrized Protocol contract test over `[FakeTransport, TelegramTransport]`.
- `tests/transport/test_streaming.py` — `StreamingMessage` unit tests (introduced at Task 16).

**Modify:**
- `src/link_project_to_chat/bot.py` — progressively migrated per strangler steps 3–9. Final state has zero `from telegram` / `import telegram` imports.

**Not touched by this plan (belongs to follow-up specs):**
- `src/link_project_to_chat/manager/bot.py` (spec #0c)
- `src/link_project_to_chat/transcriber.py`, voice handling in `bot.py` (spec #0b)
- `src/link_project_to_chat/group_filters.py`, `src/link_project_to_chat/group_state.py`, `src/link_project_to_chat/manager/team_relay.py`, `src/link_project_to_chat/manager/telegram_group.py` (spec #0a)
- All tests outside `tests/transport/` — they test seams below the transport line, don't import `telegram`, and will continue working unchanged.

---

## Task 1: Scaffold the `transport/` package

**Files:**
- Create: `src/link_project_to_chat/transport/__init__.py`

- [ ] **Step 1.1: Create the package marker**

Write `src/link_project_to_chat/transport/__init__.py`:

```python
"""Transport abstraction — see docs/superpowers/specs/2026-04-20-transport-abstraction-design.md.

Public API is re-exported here so callers can write:
    from link_project_to_chat.transport import Transport, ChatRef, ...
"""
# Re-exports populated as subsequent tasks add types.
```

- [ ] **Step 1.2: Create the tests subpackage marker**

Write `tests/transport/__init__.py` (empty file).

- [ ] **Step 1.3: Commit**

```bash
git add src/link_project_to_chat/transport/__init__.py tests/transport/__init__.py
git commit -m "chore(transport): scaffold transport package"
```

---

## Task 2: Define primitive types and the `Transport` Protocol in `base.py`

**Files:**
- Create: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Test: `tests/transport/test_base_types.py`

- [ ] **Step 2.1: Write the failing smoke test**

Create `tests/transport/test_base_types.py`:

```python
"""Smoke tests for transport primitive types.

These don't exercise behavior (dataclasses have none); they verify the shape
so later refactors can't silently drop or rename a field.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    Button,
    ButtonClick,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
    Transport,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="test", native_id="123", kind=ChatKind.DM)


def _sender(is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="test",
        native_id="42",
        display_name="Alice",
        handle="alice",
        is_bot=is_bot,
    )


def test_chat_ref_fields():
    c = _chat()
    assert c.transport_id == "test"
    assert c.native_id == "123"
    assert c.kind is ChatKind.DM


def test_chat_kind_has_dm_and_room():
    assert {ChatKind.DM, ChatKind.ROOM} == set(ChatKind)


def test_identity_fields():
    i = _sender(is_bot=True)
    assert i.is_bot is True
    assert i.handle == "alice"


def test_message_ref_carries_chat():
    m = MessageRef(transport_id="test", native_id="m1", chat=_chat())
    assert m.chat.kind is ChatKind.DM


def test_button_defaults_to_default_style():
    b = Button(label="Go", value="go")
    assert b.style is ButtonStyle.DEFAULT


def test_buttons_is_rows_of_buttons():
    bs = Buttons(rows=[[Button(label="A", value="a")], [Button(label="B", value="b")]])
    assert len(bs.rows) == 2
    assert bs.rows[0][0].label == "A"


def test_button_click_carries_value():
    click = ButtonClick(chat=_chat(), message=MessageRef("test", "m", _chat()), sender=_sender(), value="go")
    assert click.value == "go"


def test_incoming_file_carries_path():
    f = IncomingFile(path=Path("/tmp/x"), original_name="x", mime_type="text/plain", size_bytes=10)
    assert f.path == Path("/tmp/x")


def test_incoming_message_has_empty_files_by_default():
    m = IncomingMessage(chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None, native=None)
    assert m.files == []


def test_command_invocation_has_args_list():
    ci = CommandInvocation(
        chat=_chat(),
        sender=_sender(),
        name="run",
        args=["ls", "-la"],
        raw_text="/run ls -la",
        message=MessageRef("test", "m", _chat()),
    )
    assert ci.args == ["ls", "-la"]


def test_transport_is_a_protocol():
    # Protocol runtime-check: a stub class missing a method should not be considered a Transport.
    class Stub:
        async def start(self) -> None: ...
    # Stub is missing most methods — the following must NOT raise a false positive.
    # We don't use isinstance(Stub(), Transport) because Protocol isn't runtime-checkable
    # by default; instead we import as a type. The test here is a compile-time check
    # performed by mypy/pyright in CI, so we just assert the symbol is importable.
    assert Transport is not None
```

- [ ] **Step 2.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_base_types.py -v`
Expected: FAIL — `ImportError: cannot import name 'Button' from 'link_project_to_chat.transport'`.

- [ ] **Step 2.3: Implement `base.py`**

Create `src/link_project_to_chat/transport/base.py`:

```python
"""Transport Protocol and primitive types.

See docs/superpowers/specs/2026-04-20-transport-abstraction-design.md section 4.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
```

- [ ] **Step 2.4: Populate the package's public API**

Overwrite `src/link_project_to_chat/transport/__init__.py`:

```python
"""Transport abstraction — see docs/superpowers/specs/2026-04-20-transport-abstraction-design.md."""
from .base import (
    Button,
    ButtonClick,
    ButtonHandler,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    Transport,
)

__all__ = [
    "Button",
    "ButtonClick",
    "ButtonHandler",
    "ButtonStyle",
    "Buttons",
    "ChatKind",
    "ChatRef",
    "CommandHandler",
    "CommandInvocation",
    "Identity",
    "IncomingFile",
    "IncomingMessage",
    "MessageHandler",
    "MessageRef",
    "Transport",
]
```

- [ ] **Step 2.5: Run the test and confirm pass**

Run: `pytest tests/transport/test_base_types.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 2.6: Commit**

```bash
git add src/link_project_to_chat/transport/__init__.py src/link_project_to_chat/transport/base.py tests/transport/test_base_types.py
git commit -m "feat(transport): define Transport Protocol and primitive types"
```

---

## Task 3: Implement `FakeTransport`

**Files:**
- Create: `src/link_project_to_chat/transport/fake.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Test: `tests/transport/test_fake.py`

- [ ] **Step 3.1: Write the failing test**

Create `tests/transport/test_fake.py`:

```python
"""Smoke tests for FakeTransport — ensures the test double works before anything else relies on it."""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


def _alice() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


async def test_send_text_is_captured():
    t = FakeTransport()
    ref = await t.send_text(_chat(), "hello")
    assert len(t.sent_messages) == 1
    assert t.sent_messages[0].text == "hello"
    assert ref.chat == _chat()


async def test_edit_text_is_captured():
    t = FakeTransport()
    ref = await t.send_text(_chat(), "hello")
    await t.edit_text(ref, "updated")
    assert len(t.edited_messages) == 1
    assert t.edited_messages[0].text == "updated"
    assert t.edited_messages[0].message == ref


async def test_send_file_is_captured(tmp_path: Path):
    t = FakeTransport()
    p = tmp_path / "x.txt"
    p.write_text("hi")
    ref = await t.send_file(_chat(), p, caption="see")
    assert len(t.sent_files) == 1
    assert t.sent_files[0].path == p
    assert t.sent_files[0].caption == "see"
    assert ref.chat == _chat()


async def test_inject_message_fires_on_message_handler():
    t = FakeTransport()
    captured: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        captured.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _alice(), "hi")

    assert len(captured) == 1
    assert captured[0].text == "hi"
    assert captured[0].sender == _alice()


async def test_inject_command_fires_on_command_handler():
    t = FakeTransport()
    seen: list[str] = []

    async def handler(ci):
        seen.append(ci.name)

    t.on_command("help", handler)
    await t.inject_command(_chat(), _alice(), "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_inject_button_click_fires_handler():
    t = FakeTransport()
    seen: list[str] = []

    async def handler(click):
        seen.append(click.value)

    t.on_button(handler)
    ref = await t.send_text(_chat(), "pick one")
    await t.inject_button_click(ref, _alice(), value="go")

    assert seen == ["go"]


async def test_unknown_command_is_noop():
    """Injecting a command with no registered handler doesn't raise — just no-op."""
    t = FakeTransport()
    # No handler registered for 'help'.
    await t.inject_command(_chat(), _alice(), "help", args=[], raw_text="/help")
    # No assertion beyond "didn't raise" — this is the contract.


async def test_start_and_stop_are_idempotent():
    t = FakeTransport()
    await t.start()
    await t.start()  # second start must not raise
    await t.stop()
    await t.stop()  # second stop must not raise
```

- [ ] **Step 3.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_fake.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'link_project_to_chat.transport.fake'`.

- [ ] **Step 3.3: Implement `FakeTransport`**

Create `src/link_project_to_chat/transport/fake.py`:

```python
"""In-memory Transport for tests. Implements the full Protocol.

Handlers invoked via inject_* are awaited synchronously so tests can assert
state after a single await with no timer-settling hacks.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import (
    Button,
    ButtonClick,
    ButtonHandler,
    Buttons,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
)


@dataclass
class SentMessage:
    chat: ChatRef
    text: str
    buttons: Buttons | None
    message: MessageRef


@dataclass
class EditedMessage:
    message: MessageRef
    text: str
    buttons: Buttons | None


@dataclass
class SentFile:
    chat: ChatRef
    path: Path
    caption: str | None
    display_name: str | None
    message: MessageRef


class FakeTransport:
    """In-memory implementation of the Transport Protocol."""

    TRANSPORT_ID = "fake"

    def __init__(self) -> None:
        self.sent_messages: list[SentMessage] = []
        self.edited_messages: list[EditedMessage] = []
        self.sent_files: list[SentFile] = []
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._msg_counter = itertools.count(1)
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    # ── Outbound ──────────────────────────────────────────────────────────
    async def send_text(
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef:
        ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(next(self._msg_counter)), chat=chat)
        self.sent_messages.append(SentMessage(chat=chat, text=text, buttons=buttons, message=ref))
        return ref

    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None:
        self.edited_messages.append(EditedMessage(message=msg, text=text, buttons=buttons))

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

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    # ── Test injection ────────────────────────────────────────────────────
    async def inject_message(
        self,
        chat: ChatRef,
        sender: Identity,
        text: str,
        *,
        files: list[IncomingFile] | None = None,
        reply_to: MessageRef | None = None,
    ) -> None:
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=files or [],
            reply_to=reply_to,
            native=None,
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
```

- [ ] **Step 3.4: Export `FakeTransport` helpers from the package**

Edit `src/link_project_to_chat/transport/__init__.py` — append to `__all__` and add the import:

```python
from .fake import EditedMessage, FakeTransport, SentFile, SentMessage
```

Add each name to `__all__`.

- [ ] **Step 3.5: Run the test and confirm pass**

Run: `pytest tests/transport/test_fake.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 3.6: Commit**

```bash
git add src/link_project_to_chat/transport/fake.py src/link_project_to_chat/transport/__init__.py tests/transport/test_fake.py
git commit -m "feat(transport): in-memory FakeTransport for tests"
```

---

## Task 4: Create `TelegramTransport` skeleton + native-object mapping helpers

**Files:**
- Create: `src/link_project_to_chat/transport/telegram.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Test: `tests/transport/test_telegram_mapping.py`

- [ ] **Step 4.1: Write the failing test**

Create `tests/transport/test_telegram_mapping.py`:

```python
"""Unit tests for telegram-native ↔ transport-primitive mapping helpers.

The full TelegramTransport wiring is tested via the contract test in test_contract.py.
These tests isolate the pure mapping functions so they can be debugged in isolation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from link_project_to_chat.transport import ChatKind
from link_project_to_chat.transport.telegram import (
    chat_ref_from_telegram,
    identity_from_telegram_user,
    message_ref_from_telegram,
)


def test_private_chat_maps_to_dm():
    fake_chat = SimpleNamespace(id=12345, type="private")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.native_id == "12345"
    assert ref.kind is ChatKind.DM
    assert ref.transport_id == "telegram"


def test_group_chat_maps_to_room():
    fake_chat = SimpleNamespace(id=-100123, type="group")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.kind is ChatKind.ROOM


def test_supergroup_chat_maps_to_room():
    fake_chat = SimpleNamespace(id=-100123, type="supergroup")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.kind is ChatKind.ROOM


def test_identity_from_user():
    fake_user = SimpleNamespace(
        id=42, full_name="Alice Bee", username="alice", is_bot=False
    )
    i = identity_from_telegram_user(fake_user)
    assert i.native_id == "42"
    assert i.display_name == "Alice Bee"
    assert i.handle == "alice"
    assert i.is_bot is False


def test_identity_with_no_username():
    fake_user = SimpleNamespace(id=7, full_name="Bot McBotface", username=None, is_bot=True)
    i = identity_from_telegram_user(fake_user)
    assert i.handle is None
    assert i.is_bot is True


def test_message_ref_from_telegram():
    fake_chat = SimpleNamespace(id=12345, type="private")
    fake_msg = SimpleNamespace(message_id=99, chat=fake_chat)
    m = message_ref_from_telegram(fake_msg)
    assert m.native_id == "99"
    assert m.chat.native_id == "12345"
    assert m.chat.kind is ChatKind.DM
```

- [ ] **Step 4.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'link_project_to_chat.transport.telegram'`.

- [ ] **Step 4.3: Implement the skeleton and mapping helpers**

Create `src/link_project_to_chat/transport/telegram.py`:

```python
"""TelegramTransport — python-telegram-bot adapter for the Transport Protocol.

This module is the ONLY place in the codebase that imports `telegram` after
spec #0 step 9 (lockout). bot.py talks to the interface; everything
Telegram-specific lives behind it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import (
    Buttons,
    ButtonHandler,
    ChatKind,
    ChatRef,
    CommandHandler,
    Identity,
    MessageHandler,
    MessageRef,
)

TRANSPORT_ID = "telegram"

# Type aliases — kept as Any to avoid importing `telegram` types at module level
# when only the helpers are called. The actual telegram types are used inside
# function bodies.


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
        raise NotImplementedError("Wired in Task 16")

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
```

- [ ] **Step 4.4: Export `TelegramTransport` from the package**

Edit `src/link_project_to_chat/transport/__init__.py` — add:

```python
from .telegram import TelegramTransport
```

And add `"TelegramTransport"` to `__all__`.

- [ ] **Step 4.5: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_mapping.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 4.6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/transport/__init__.py tests/transport/test_telegram_mapping.py
git commit -m "feat(transport): TelegramTransport skeleton + native-type mapping helpers"
```

---

## Task 5: Implement `TelegramTransport.start/stop/send_text` + inbound `on_message`

**Spec step 3 — port one outbound path and the inbound text flow.**

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Test: `tests/transport/test_telegram_transport.py`

- [ ] **Step 5.1: Write the failing test**

Create `tests/transport/test_telegram_transport.py`:

```python
"""Integration tests for TelegramTransport using a lightweight Application stub.

We don't require a live Telegram connection — `telegram.ext.Application` accepts
a mock `Bot` and we can drive it via its message-handling entry points.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef
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
```

- [ ] **Step 5.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: FAIL — `NotImplementedError: Wired in Task 5`.

- [ ] **Step 5.3: Implement start/stop/send_text**

In `src/link_project_to_chat/transport/telegram.py`, replace the body of `start`, `stop`, and `send_text`:

```python
    async def start(self) -> None:
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def send_text(
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef:
        # buttons handled in Task 20; ignore here.
        native_msg = await self._app.bot.send_message(
            chat_id=int(chat.native_id),
            text=text,
        )
        return message_ref_from_telegram(native_msg)
```

- [ ] **Step 5.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.start/stop/send_text"
```

---

## Task 6: Wire inbound text path — `IncomingMessage` dispatch from `telegram.Update`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 6.1: Add the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
```

- [ ] **Step 6.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_on_message_handler_fires_on_telegram_update -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute '_dispatch_message'`.

- [ ] **Step 6.3: Implement inbound dispatch**

In `src/link_project_to_chat/transport/telegram.py`, add:

```python
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        """Convert a telegram Update into IncomingMessage and invoke handlers.

        Called from the MessageHandler wired on the Application by bot.py
        during the strangler port (Task 7). For now, tests call this directly.
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
            files=[],  # populated in Task 23
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
        )
        for h in self._message_handlers:
            await h(incoming)
```

- [ ] **Step 6.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py::test_on_message_handler_fires_on_telegram_update -v`
Expected: PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): inbound-message dispatch from telegram.Update"
```

---

## Task 7: Set up parametrized contract test

**Files:**
- Create: `tests/transport/test_contract.py`

- [ ] **Step 7.1: Write the contract test**

Create `tests/transport/test_contract.py`:

```python
"""Parametrized Protocol contract test — every Transport must pass.

Initial parameter list: [FakeTransport]. TelegramTransport added in Task 8 once
we have a working test fixture for it.
"""
from __future__ import annotations

from pathlib import Path

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


@pytest.fixture(params=[FakeTransport])
def transport(request) -> Transport:
    """Yield a fresh Transport implementation per test.

    New transports added to `params` when implemented.
    """
    cls = request.param
    t = cls()
    yield t


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
    # transports provide a test fixture that wires one in (see Task 8 for Telegram).
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
```

- [ ] **Step 7.2: Run the contract test**

Run: `pytest tests/transport/test_contract.py -v`
Expected: 4 tests PASS against `FakeTransport`.

- [ ] **Step 7.3: Commit**

```bash
git add tests/transport/test_contract.py
git commit -m "test(transport): parametrized Protocol contract test"
```

---

## Task 8: Wire `bot.py` unsupported-type reply through `transport.send_text`

**Spec step 3 (outbound half) — prove the outbound adapter on a low-stakes path.**

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (constructor, `_build_app`, `_on_unsupported`)

- [ ] **Step 8.1: Inspect the current wiring**

Read [src/link_project_to_chat/bot.py:1496-1513](src/link_project_to_chat/bot.py:1496) (the `_on_unsupported` handler) and [src/link_project_to_chat/bot.py:1609-1689](src/link_project_to_chat/bot.py:1609) (the `_build_app` method) to confirm the shape. No code changes in this step.

- [ ] **Step 8.2: Add a `self._transport` attribute**

In `bot.py`, in `ProjectBot.__init__`, after `self._app: Any = None` (search for `self._app =` or the constructor body), add:

```python
        self._transport = None  # TelegramTransport — set in _build_app
```

- [ ] **Step 8.3: Construct the `TelegramTransport` in `_build_app`**

In `bot.py`'s `_build_app`, immediately after `self._app = app`:

```python
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport(app)
```

- [ ] **Step 8.4: Route the unsupported reply through the transport**

Replace the body of `_on_unsupported` ([src/link_project_to_chat/bot.py:1496-1512](src/link_project_to_chat/bot.py:1496)) with:

```python
    async def _on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        if msg.video_note:
            text = "Video notes aren't supported yet. Please type your message or send a voice message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        from .transport.telegram import chat_ref_from_telegram
        chat = chat_ref_from_telegram(msg.chat)
        assert self._transport is not None
        await self._transport.send_text(chat, text)
```

- [ ] **Step 8.5: Run the full test suite to confirm no regressions**

Run: `pytest -v`
Expected: all existing tests PASS. No test should break — `_on_unsupported` isn't covered by existing tests, so the change is behaviorally transparent.

- [ ] **Step 8.6: Manual smoke test**

Start the bot manually and send a sticker to confirm the reply arrives as "Stickers aren't supported. Please type your message." The outbound path is now via `TelegramTransport.send_text`.

- [ ] **Step 8.7: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): route unsupported-type reply through TelegramTransport.send_text"
```

---

## Task 9: Wire `bot.py` main text handler through `transport.on_message`

**Spec step 3 (inbound half) — main text flow dispatches via the Transport.**

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (`_build_app` and `_on_text`)

- [ ] **Step 9.1: Add an adapter method that consumes `IncomingMessage`**

In `bot.py`, find `async def _on_text` (search for `_on_text`). Add a new sibling method immediately above it:

```python
    async def _on_text_from_transport(self, incoming) -> None:
        """Bridge handler registered on TelegramTransport.on_message.

        The heavy lifting stays in _on_text for now (which consumes telegram.Update).
        This method converts IncomingMessage back into the Update shape expected
        by _on_text — a temporary shim that goes away when _on_text is fully
        ported to consume IncomingMessage directly (future task).
        """
        native = incoming.native  # the original telegram.Message
        if native is None:
            return
        # Reconstruct the Update-like object the existing _on_text expects.
        from types import SimpleNamespace
        fake_update = SimpleNamespace(
            effective_message=native,
            effective_user=native.from_user,
            effective_chat=native.chat,
        )
        await self._on_text(fake_update, None)
```

- [ ] **Step 9.2: Register the transport handler and remove the telegram MessageHandler for text**

In `bot.py` `_build_app`, replace the text-filter registration block:

```python
            text_filter = (
                private
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
            app.add_handler(MessageHandler(text_filter, self._on_text))
```

with:

```python
            text_filter = (
                private
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
            # Register the transport-level handler on the filtered MessageHandler
            # so we keep the private/text filtering while dispatching via Transport.
            app.add_handler(MessageHandler(text_filter, self._transport._dispatch_message))
            assert self._transport is not None
            self._transport.on_message(self._on_text_from_transport)
```

Apply the same change inside the `if self.group_mode:` branch (keep the `chat_filter` variant) — replace the text `add_handler(MessageHandler(text_filter, self._on_text))` with the same two lines.

- [ ] **Step 9.3: Run the full test suite**

Run: `pytest -v`
Expected: all existing tests PASS. `_on_text` is covered by `tests/test_bot_streaming.py`, `tests/test_bot_team_wiring.py`, and others — verify these still pass.

- [ ] **Step 9.4: Manual smoke test**

Send a normal text message to the bot. Verify Claude replies normally and streaming edits still work. The inbound path is now `telegram Update → TelegramTransport._dispatch_message → _on_text_from_transport → _on_text`.

- [ ] **Step 9.5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): route inbound text through TelegramTransport.on_message"
```

---

## Task 10: Implement `TelegramTransport.on_command` dispatch

**Spec step 4 (part 1) — add the command-dispatch infrastructure.**

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 10.1: Add the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
```

- [ ] **Step 10.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_on_command_handler_fires_for_telegram_command -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute '_dispatch_command'`.

- [ ] **Step 10.3: Implement `_dispatch_command`**

In `src/link_project_to_chat/transport/telegram.py`, add:

```python
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
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            await handler(ci)
```

- [ ] **Step 10.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all tests PASS (including the new one).

- [ ] **Step 10.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport command dispatch"
```

---

## Task 11: Port `/help`, `/version`, `/status` through `transport.on_command`

**Spec step 4 (part 2) — port three commands via the new dispatch.**

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (`_build_app` registration + three handlers)

- [ ] **Step 11.1: Add transport-native versions of the three commands**

Each ported command is rewritten to consume `CommandInvocation` directly via the transport — the legacy `Update`-based version stays side-by-side for now (removed in Task 13 after all commands are ported).

Find `async def _on_help` in `bot.py`:

```python
    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ...existing body...
```

Add a sibling:

```python
    async def _on_help_t(self, ci) -> None:
        """Transport-native /help — CommandInvocation in, transport.send_text out."""
        assert self._transport is not None
        await self._transport.send_text(ci.chat, _CMD_HELP)
```

Repeat for `_on_version` and `_on_status`:

```python
    async def _on_version_t(self, ci) -> None:
        from . import __version__ as _ver  # if a __version__ exists; otherwise use the same string as the legacy handler
        assert self._transport is not None
        await self._transport.send_text(ci.chat, f"link-project-to-chat v{_ver}")

    async def _on_status_t(self, ci) -> None:
        """Transport-native /status. Replicate the existing _on_status string."""
        assert self._transport is not None
        # Build status exactly as _on_status does. If _on_status calls self._status_text()
        # (or similar helper), reuse that.
        text = self._compose_status()  # Replace with the exact helper or inline text the legacy handler uses.
        await self._transport.send_text(ci.chat, text)
```

**Note to implementer:** before this step, open [bot.py](src/link_project_to_chat/bot.py), locate `_on_help`, `_on_version`, `_on_status`. Extract the text each produces (often a single string or a small composition). Paste that text into the `_t` variants. Do NOT reinvent the content.

- [ ] **Step 11.2: Register the three commands via the transport**

In `bot.py` `_build_app`, find the handlers dict (around [src/link_project_to_chat/bot.py:1623-1648](src/link_project_to_chat/bot.py:1623)). In both branches (group_mode and private), after the telegram CommandHandler registration loop, add:

```python
        # Transport-ported commands — register on the transport and DROP the
        # corresponding telegram CommandHandler registration above.
        assert self._transport is not None
        self._transport.on_command("help", self._on_help_t)
        self._transport.on_command("version", self._on_version_t)
        self._transport.on_command("status", self._on_status_t)

        # Wire telegram's CommandHandler → transport dispatcher for the ported names.
        for name in ("help", "version", "status"):
            app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._transport._dispatch_command(_n, u, c),
                filters=chat_filter if self.group_mode else private,
            ))
```

**And remove** the three corresponding entries from the `handlers` dict that loops `add_handler(CommandHandler(name, handler, filters=...))`. Specifically, delete these three lines from the dict:

```python
            "status": self._on_status,
            "version": self._on_version,
            "help": self._on_help,
```

- [ ] **Step 11.3: Run the test suite**

Run: `pytest -v`
Expected: all PASS. The ported commands have no existing unit tests; no regressions elsewhere.

- [ ] **Step 11.4: Manual smoke test**

Start the bot, send `/help`, `/version`, `/status`. Verify each returns the same output as before.

- [ ] **Step 11.5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): port /help /version /status through TelegramTransport.on_command"
```

---

## Task 12: Port remaining commands through `transport.on_command`

**Spec step 5 — mechanical port of all remaining commands.**

**Commands to port:** `/run`, `/tasks`, `/model`, `/effort`, `/thinking`, `/permissions`, `/compact`, `/reset`, `/skills`, `/stop_skill`, `/create_skill`, `/delete_skill`, `/persona`, `/stop_persona`, `/create_persona`, `/delete_persona`, `/voice`, `/lang`, `/halt`, `/resume`, `/start`.

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 12.1: Port each command handler to a `_t` variant**

For EACH command in the list above, apply the same pattern as Task 11:

1. Find `async def _on_<name>(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:`.
2. Add a sibling `async def _on_<name>_t(self, ci) -> None:` that consumes `CommandInvocation`.
3. Replicate the existing handler's behavior, using `self._transport.send_text(ci.chat, ...)` in place of `await msg.reply_text(...)`.

**Example — /model (handler reads args, validates, sets state, replies):**

Before (existing):
```python
    async def _on_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not self._auth(update.effective_user):
            return
        arg = (ctx.args or [None])[0]
        if arg not in MODELS:
            await msg.reply_text(f"Usage: /model {'/'.join(MODELS)}")
            return
        self._model = arg
        await msg.reply_text(f"Model set to {arg}.")
```

After (added):
```python
    async def _on_model_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        arg = (ci.args or [None])[0]
        assert self._transport is not None
        if arg not in MODELS:
            await self._transport.send_text(ci.chat, f"Usage: /model {'/'.join(MODELS)}")
            return
        self._model = arg
        await self._transport.send_text(ci.chat, f"Model set to {arg}.")
```

**Auth note:** the legacy handlers use `self._auth(update.effective_user)` which takes a telegram.User. Add a parallel helper `self._auth_identity(identity)` that takes our `Identity`. Add it once, use it everywhere:

```python
    def _auth_identity(self, identity) -> bool:
        """Authorize based on transport Identity. Mirrors _auth but consumes Identity."""
        # Find the existing _auth body; it checks username + user_id. Adapt to use
        # identity.handle (username) and int(identity.native_id) (user_id).
        # Exact body depends on _auth in _auth.py — reuse that function's logic via
        # constructing a minimal object, OR expose the underlying check with a new
        # helper in _auth.py. Recommended: add a helper `_is_authorized(user_id, username)`
        # in _auth.py and call it from both _auth and _auth_identity.
        from ._auth import _is_authorized  # new helper — add in this step if missing
        return _is_authorized(
            int(identity.native_id),
            identity.handle or "",
            self.allowed_usernames,
            self.trusted_user_ids,
        )
```

If `_is_authorized` doesn't exist in [src/link_project_to_chat/_auth.py](src/link_project_to_chat/_auth.py), **add it** in this step by extracting the auth logic from the existing `AuthMixin._auth` method. Both `_auth` (telegram-typed) and `_auth_identity` then call `_is_authorized`.

- [ ] **Step 12.2: Register each ported command via `self._transport.on_command`**

Extend the registration block added in Task 11:

```python
        for name, handler in (
            ("help", self._on_help_t),
            ("version", self._on_version_t),
            ("status", self._on_status_t),
            ("start", self._on_start_t),
            ("run", self._on_run_t),
            ("tasks", self._on_tasks_t),
            ("model", self._on_model_t),
            ("effort", self._on_effort_t),
            ("thinking", self._on_thinking_t),
            ("permissions", self._on_permissions_t),
            ("compact", self._on_compact_t),
            ("reset", self._on_reset_t),
            ("skills", self._on_skills_t),
            ("stop_skill", self._on_stop_skill_t),
            ("create_skill", self._on_create_skill_t),
            ("delete_skill", self._on_delete_skill_t),
            ("persona", self._on_persona_t),
            ("stop_persona", self._on_stop_persona_t),
            ("create_persona", self._on_create_persona_t),
            ("delete_persona", self._on_delete_persona_t),
            ("voice", self._on_voice_status_t),
            ("lang", self._on_lang_t),
            ("halt", self._on_halt_t),
            ("resume", self._on_resume_t),
        ):
            self._transport.on_command(name, handler)
            app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._transport._dispatch_command(_n, u, c),
                filters=chat_filter if self.group_mode else private,
            ))
```

- [ ] **Step 12.3: Empty the legacy `handlers` dict**

Delete the old `handlers = { ... }` dict and its loop entirely (the block at [src/link_project_to_chat/bot.py:1623-1665](src/link_project_to_chat/bot.py:1623)). All commands are now registered via the transport.

- [ ] **Step 12.4: Run the full test suite**

Run: `pytest -v`
Expected: ALL pass. Commands covered by tests are in `tests/manager/test_bot_commands.py` (manager bot, untouched), `tests/test_skills.py`, `tests/test_persona_persistence.py`, `tests/test_claude_usage_cap.py` — verify the existing behavior is preserved.

- [ ] **Step 12.5: Manual smoke test**

Exercise 4–5 different commands: `/model sonnet`, `/effort high`, `/persona`, `/tasks`, `/compact`. Confirm responses match pre-port behavior.

- [ ] **Step 12.6: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/_auth.py
git commit -m "refactor(bot): port all commands through TelegramTransport.on_command"
```

---

## Task 13: Delete legacy Update-based command handlers

Now that every command has a `_t` variant and is registered via the transport, the legacy `_on_<name>` handlers (that take `Update, ContextTypes.DEFAULT_TYPE`) are dead code.

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 13.1: Delete each legacy handler**

For each name in the list from Task 12, delete `async def _on_<name>(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:` and its body. Keep the `_t` variants.

**Exception:** the legacy `_on_text`, `_on_file`, `_on_voice`, `_on_unsupported`, `_on_callback` are NOT command handlers — leave them for later tasks.

- [ ] **Step 13.2: Rename `_t` variants to canonical names**

Rename every `_on_<name>_t` back to `_on_<name>` via find-and-replace:

```bash
# Example — adjust editor to do this across bot.py:
#  _on_help_t → _on_help
#  _on_version_t → _on_version
#  ...
```

Update the registration block in `_build_app` to match.

- [ ] **Step 13.3: Run the full test suite**

Run: `pytest -v`
Expected: all PASS.

- [ ] **Step 13.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): remove legacy Update-based command handlers"
```

---

## Task 14: Implement `StreamingMessage` helper (telegram-backed)

**Spec step 6 (part 1) — extract streaming logic from bot.py into a dedicated module, still using python-telegram-bot under the hood.**

**Files:**
- Create: `src/link_project_to_chat/transport/streaming.py`
- Test: `tests/transport/test_streaming.py`

- [ ] **Step 14.1: Review the existing streaming code**

Read [src/link_project_to_chat/livestream.py](src/link_project_to_chat/livestream.py) (288 lines) — this is the current `LiveMessage` helper that already owns streaming-edit throttling. It takes telegram objects directly. Task 14 creates a `StreamingMessage` in `transport/` that will eventually replace it; for now we mirror its interface with no behavior change.

- [ ] **Step 14.2: Write the failing test**

Create `tests/transport/test_streaming.py`:

```python
"""Unit tests for StreamingMessage — transport-agnostic streaming-edit helper."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef
from link_project_to_chat.transport.fake import FakeTransport
from link_project_to_chat.transport.streaming import StreamingMessage


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


async def test_open_sends_initial_text():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("starting...")
    assert len(t.sent_messages) == 1
    assert t.sent_messages[0].text == "starting..."


async def test_update_edits_existing_message():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("v1")
    await sm.update("v2")
    await sm.close()
    assert len(t.edited_messages) >= 1
    assert t.edited_messages[-1].text == "v2"


async def test_close_with_final_text_performs_final_edit():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("working...")
    await sm.close(final_text="done")
    assert any(e.text == "done" for e in t.edited_messages)


async def test_throttle_defers_interim_updates():
    """Back-to-back updates inside the throttle window coalesce."""
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=10)  # huge window
    await sm.open("v0")
    await sm.update("v1")
    await sm.update("v2")
    await sm.update("v3")
    # None of v1/v2/v3 should be sent yet — only the initial open.
    assert len(t.edited_messages) == 0
    # close() must flush the final text.
    await sm.close()
    assert t.edited_messages[-1].text == "v3"


async def test_overflow_sends_new_message_and_continues_editing_tail():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0, max_chars=10)
    await sm.open("0123456789")  # exactly at cap
    await sm.update("0123456789ABCDE")  # overflow by 5 chars
    await sm.close()
    # At least 2 messages sent total: the original plus the overflow chunk.
    assert len(t.sent_messages) >= 2
```

- [ ] **Step 14.3: Run the test and confirm failure**

Run: `pytest tests/transport/test_streaming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'link_project_to_chat.transport.streaming'`.

- [ ] **Step 14.4: Implement `StreamingMessage`**

Create `src/link_project_to_chat/transport/streaming.py`:

```python
"""Transport-agnostic streaming-edit helper.

Owns one editable message and throttles updates. Called by the bot when
streaming Claude's output; the throttling + chunking logic lives here so
every Transport behaves identically.
"""
from __future__ import annotations

import asyncio
import time

from .base import Buttons, ChatRef, MessageRef, Transport


class StreamingMessage:
    def __init__(
        self,
        transport: Transport,
        chat: ChatRef,
        *,
        min_interval_s: float = 2.0,
        max_chars: int = 4000,
    ) -> None:
        self._transport = transport
        self._chat = chat
        self._min_interval_s = min_interval_s
        self._max_chars = max_chars
        self._current_ref: MessageRef | None = None
        self._current_text = ""
        self._pending_text: str | None = None
        self._last_edit_ts = 0.0
        self._closed = False

    async def open(self, initial_text: str) -> None:
        self._current_text = initial_text[: self._max_chars]
        self._current_ref = await self._transport.send_text(self._chat, self._current_text)
        self._last_edit_ts = time.monotonic()

    async def update(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("StreamingMessage is closed")
        if self._current_ref is None:
            raise RuntimeError("open() must be called before update()")
        self._pending_text = text
        now = time.monotonic()
        if now - self._last_edit_ts < self._min_interval_s:
            return
        await self._flush()

    async def close(self, final_text: str | None = None) -> None:
        if self._closed:
            return
        if final_text is not None:
            self._pending_text = final_text
        if self._pending_text is not None:
            await self._flush()
        self._closed = True

    async def _flush(self) -> None:
        if self._pending_text is None or self._current_ref is None:
            return
        text = self._pending_text
        self._pending_text = None

        if len(text) <= self._max_chars:
            await self._transport.edit_text(self._current_ref, text)
            self._current_text = text
            self._last_edit_ts = time.monotonic()
            return

        # Overflow: chunk the prefix into new messages; keep the tail on the
        # current message so the stream continues to edit-in-place.
        keep_last = text[-self._max_chars :]
        prefix = text[: -self._max_chars]
        # Finalize the current message with the last full chunk of prefix.
        while len(prefix) > self._max_chars:
            head, prefix = prefix[: self._max_chars], prefix[self._max_chars :]
            await self._transport.edit_text(self._current_ref, head)
            self._current_ref = await self._transport.send_text(self._chat, "...")
        # Flush the final piece of prefix onto the current message; start a new
        # message for the tail so future updates have somewhere to land.
        if prefix:
            await self._transport.edit_text(self._current_ref, prefix)
            self._current_ref = await self._transport.send_text(self._chat, keep_last)
        else:
            await self._transport.edit_text(self._current_ref, keep_last)
        self._current_text = keep_last
        self._last_edit_ts = time.monotonic()
```

- [ ] **Step 14.5: Run the test and confirm pass**

Run: `pytest tests/transport/test_streaming.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 14.6: Commit**

```bash
git add src/link_project_to_chat/transport/streaming.py tests/transport/test_streaming.py
git commit -m "feat(transport): StreamingMessage throttled-edit helper"
```

---

## Task 15: Implement `TelegramTransport.edit_text`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 15.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
```

Import `MessageRef` from the transport package at the top of the file if not already present.

- [ ] **Step 15.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_edit_text_calls_edit_message_text -v`
Expected: FAIL — `NotImplementedError: Wired in Task 16`.

- [ ] **Step 15.3: Implement `edit_text`**

In `src/link_project_to_chat/transport/telegram.py`, replace `edit_text`'s body:

```python
    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None:
        # buttons handled in Task 20; ignore here.
        await self._app.bot.edit_message_text(
            chat_id=int(msg.chat.native_id),
            message_id=int(msg.native_id),
            text=text,
        )
```

- [ ] **Step 15.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all PASS.

- [ ] **Step 15.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.edit_text"
```

---

## Task 16: Swap `bot.py` Claude streaming to `StreamingMessage`

**Spec step 6 (part 2) — the validation-moment task. If the Transport interface is wrong, it shows up here.**

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 16.1: Locate the streaming call site**

Search [src/link_project_to_chat/bot.py](src/link_project_to_chat/bot.py) for `LiveMessage(`. This is the current streaming helper from [src/link_project_to_chat/livestream.py](src/link_project_to_chat/livestream.py). Identify every construction site — typically in `_finalize_claude_task` or the Claude chat flow.

- [ ] **Step 16.2: Add a compatibility wrapper**

The existing `LiveMessage` has methods `open`, `update`, `close` that match `StreamingMessage`. But `LiveMessage` takes a telegram `Bot` and chat_id; `StreamingMessage` takes a `Transport` and `ChatRef`. Create a minimal shim — if the public API is the same, we can do a mechanical class-swap:

In `bot.py`, replace:

```python
from .livestream import LiveMessage
```

with:

```python
from .transport.streaming import StreamingMessage
```

Then at every `LiveMessage(bot=..., chat_id=...)` construction site:

Before:
```python
live = LiveMessage(bot=self._app.bot, chat_id=msg.chat.id)
```

After:
```python
from .transport.telegram import chat_ref_from_telegram
chat_ref = chat_ref_from_telegram(msg.chat) if hasattr(msg, "chat") else ci.chat
assert self._transport is not None
live = StreamingMessage(self._transport, chat_ref)
```

**Note:** any `LiveMessage`-specific methods that `StreamingMessage` does NOT have must be identified during this task. If `LiveMessage.append(...)` exists but `StreamingMessage.update(...)` doesn't cover it, extend `StreamingMessage` first (with a test). **Do this in a separate commit within Task 16** to isolate the API extension from the callsite swap.

- [ ] **Step 16.3: Reconcile `LiveMessage` API surface**

Compare the public methods of `LiveMessage` (in [livestream.py](src/link_project_to_chat/livestream.py)) against `StreamingMessage`. If any method names differ (e.g., `LiveMessage.append` vs `StreamingMessage.update`), there are two options:

a) Rename the callers to use `StreamingMessage`'s names. Preferred if the call sites are few.
b) Add aliases on `StreamingMessage`. Preferred if a dozen call sites would need edits.

Pick the lighter approach and apply it.

- [ ] **Step 16.4: Run the full test suite**

Run: `pytest -v`
Expected: all PASS. Critical tests: `tests/test_livestream.py` (must continue passing unchanged — `LiveMessage` still exists, we just aren't using it from bot.py), `tests/test_bot_streaming.py` (may need adjustment if it tests streaming behavior through bot.py).

If `tests/test_bot_streaming.py` fails, read each failure: the test expects specific streaming behavior; verify `StreamingMessage` produces equivalent output. Adjust the test expectations only if the spec explicitly changes behavior; otherwise fix `StreamingMessage`.

- [ ] **Step 16.5: Manual smoke test**

Chat with Claude. Verify:
- Streaming edits appear at ~2s throttle.
- Long responses chunk into multiple messages.
- Final content matches pre-port behavior.

- [ ] **Step 16.6: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/transport/streaming.py
git commit -m "refactor(bot): stream Claude output via StreamingMessage and Transport"
```

- [ ] **Step 16.7: Remove `livestream.py` if fully unused**

Run: `grep -rn "from .*livestream" src/`
If [src/link_project_to_chat/livestream.py](src/link_project_to_chat/livestream.py) has no remaining imports (manager bot doesn't use it either), delete the file:

```bash
git rm src/link_project_to_chat/livestream.py tests/test_livestream.py
git commit -m "chore: remove unused livestream module — superseded by StreamingMessage"
```

If there ARE remaining imports, leave it in place and skip this sub-step.

---

## Task 17: Implement button send/edit in `TelegramTransport`

**Spec step 7 (part 1) — buttons on outbound messages.**

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 17.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
    # Confirm two buttons made it through in one row.
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
```

- [ ] **Step 17.2: Run the tests and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_send_text_with_buttons_passes_inline_keyboard tests/transport/test_telegram_transport.py::test_edit_text_with_buttons_passes_inline_keyboard -v`
Expected: FAIL (markup is None or not checked).

- [ ] **Step 17.3: Add the conversion helper and update send/edit**

In `src/link_project_to_chat/transport/telegram.py`, add:

```python
def _buttons_to_inline_keyboard(buttons: Buttons | None) -> Any:
    """Convert a Buttons primitive into telegram's InlineKeyboardMarkup."""
    if buttons is None:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=b.label, callback_data=b.value) for b in row]
        for row in buttons.rows
    ])
```

Update `send_text`:

```python
    async def send_text(
        self, chat: ChatRef, text: str, *, buttons: Buttons | None = None
    ) -> MessageRef:
        native_msg = await self._app.bot.send_message(
            chat_id=int(chat.native_id),
            text=text,
            reply_markup=_buttons_to_inline_keyboard(buttons),
        )
        return message_ref_from_telegram(native_msg)
```

Update `edit_text`:

```python
    async def edit_text(
        self, msg: MessageRef, text: str, *, buttons: Buttons | None = None
    ) -> None:
        await self._app.bot.edit_message_text(
            chat_id=int(msg.chat.native_id),
            message_id=int(msg.native_id),
            text=text,
            reply_markup=_buttons_to_inline_keyboard(buttons),
        )
```

- [ ] **Step 17.4: Run the tests and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all PASS.

- [ ] **Step 17.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): inline-button send/edit via TelegramTransport"
```

---

## Task 18: Implement `TelegramTransport.on_button`

**Spec step 7 (part 2) — receive button clicks through the Transport.**

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 18.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
    # Telegram requires answering the callback query to dismiss the loading spinner.
    tg_query.answer.assert_awaited_once()
```

- [ ] **Step 18.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_on_button_fires_for_telegram_callback_query -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute '_dispatch_button'`.

- [ ] **Step 18.3: Implement `_dispatch_button`**

In `src/link_project_to_chat/transport/telegram.py`, add:

```python
    async def _dispatch_button(self, update: Any, ctx: Any) -> None:
        """Convert a telegram CallbackQuery into ButtonClick and invoke handlers.

        Also answers the query to dismiss the client-side loading spinner.
        """
        query = update.callback_query
        if query is None:
            return
        await query.answer()  # dismiss the spinner; no-op if already answered
        from .base import ButtonClick
        click = ButtonClick(
            chat=chat_ref_from_telegram(query.message.chat),
            message=message_ref_from_telegram(query.message),
            sender=identity_from_telegram_user(query.from_user),
            value=query.data or "",
        )
        for h in self._button_handlers:
            await h(click)
```

- [ ] **Step 18.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all PASS.

- [ ] **Step 18.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport button-click dispatch"
```

---

## Task 19: Port `bot.py` inline-button send sites to `Buttons`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 19.1: Inventory the inline-button construction sites**

Search `bot.py` for `InlineKeyboardMarkup(` and `InlineKeyboardButton(`. Expected sites:
- `/tasks` — per-task action buttons
- `/reset` confirmation — yes/no buttons
- `/persona` picker — one button per available persona
- `/skills` / `/use` picker — one button per available skill

- [ ] **Step 19.2: Port each site**

For each site, replace the telegram-native construction:

```python
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Cancel", callback_data=f"cancel_task_{task.id}")]
])
await msg.reply_text("Running...", reply_markup=keyboard)
```

with `Buttons` + `transport.send_text`:

```python
from .transport import Button, Buttons
buttons = Buttons(rows=[[Button(label="Cancel", value=f"cancel_task_{task.id}")]])
await self._transport.send_text(ci.chat, "Running...", buttons=buttons)
```

- [ ] **Step 19.3: Port edit sites that update buttons**

For any site that calls `edit_message_reply_markup` or `edit_message_text` with `reply_markup=`, convert to `self._transport.edit_text(message_ref, new_text, buttons=Buttons(...))`.

If a call site doesn't have a `MessageRef` available (e.g., it's constructing from a raw `telegram.Update`), use `chat_ref_from_telegram` + `message_ref_from_telegram` helpers from `transport.telegram`.

- [ ] **Step 19.4: Run the full test suite**

Run: `pytest -v`
Expected: all PASS. Tests for button behavior are in `tests/test_bot_streaming.py`, `tests/test_skills.py`, `tests/test_persona_persistence.py`.

- [ ] **Step 19.5: Manual smoke test**

Exercise: `/tasks` → click Cancel; `/reset` → click Yes/No; `/persona` → pick one from the list.

- [ ] **Step 19.6: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): port inline-button sends to Buttons primitive"
```

---

## Task 20: Port `bot.py` callback-query handler to `on_button`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 20.1: Add a transport-native callback handler**

Find `async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:` in bot.py. Read its full body — note every `if query.data == X` / `if query.data.startswith(X)` branch. Each branch becomes a ported branch in the new handler.

Add a sibling method `_on_button` that replicates every branch of `_on_callback` using the `ButtonClick` shape. Transformation rules — apply mechanically:

| Legacy (`_on_callback`)                                   | Ported (`_on_button`)                                                                |
|-----------------------------------------------------------|--------------------------------------------------------------------------------------|
| `query = update.callback_query`                           | (remove — `click` is the parameter)                                                   |
| `user = update.effective_user`                            | `user = click.sender`                                                                 |
| `if query.data == "confirm_reset":`                       | `if click.value == "confirm_reset":`                                                  |
| `if query.data.startswith("cancel_task_"):`               | `if click.value.startswith("cancel_task_"):`                                          |
| `task_id = _parse_task_id(query.data)`                    | `task_id = _parse_task_id(click.value)`                                               |
| `await query.answer()`                                    | (remove — `_dispatch_button` already calls `query.answer()`)                          |
| `await query.edit_message_text(text)`                     | `await self._transport.edit_text(click.message, text)`                                |
| `await query.edit_message_text(text, reply_markup=kb)`    | `await self._transport.edit_text(click.message, text, buttons=<Buttons equivalent>)`  |
| `await query.edit_message_reply_markup(reply_markup=kb)`  | See note below — re-send full text+buttons                                            |
| `await query.message.reply_text(text)`                    | `await self._transport.send_text(click.chat, text)`                                   |
| `if not self._auth(user):`                                | `if not self._auth_identity(user):`                                                   |

**Note on markup-only edits:** `MessageRef` doesn't carry current text. If the legacy handler only updates the inline keyboard (via `edit_message_reply_markup`), promote the branch to track the current text in bot state, or convert to `send_text` with new buttons and `edit_text` the old message to a terminal marker. Pick whichever keeps behavior closest — inline notes in the ported handler should document the choice.

**Skeleton for the ported method** (fill in each branch per the rules above; preserve ordering):

```python
    async def _on_button(self, click) -> None:
        if not self._auth_identity(click.sender):
            return
        value = click.value
        # Branch 1: <name> — ported from _on_callback line ~<N>
        if value == "confirm_reset":
            # ... original branch body, transformed ...
            return
        if value == "cancel_reset":
            # ... original branch body, transformed ...
            return
        if value.startswith("cancel_task_"):
            # ... original branch body, transformed ...
            return
        # ... one return-guarded branch per legacy _on_callback branch ...
```

- [ ] **Step 20.2: Register the transport handler; remove the telegram one**

In `_build_app`, delete:

```python
        app.add_handler(CallbackQueryHandler(self._on_callback))
```

Add:

```python
        assert self._transport is not None
        app.add_handler(CallbackQueryHandler(self._transport._dispatch_button))
        self._transport.on_button(self._on_button)
```

- [ ] **Step 20.3: Delete the legacy `_on_callback` method**

Remove the entire `async def _on_callback(...)` method from bot.py.

- [ ] **Step 20.4: Run the full test suite**

Run: `pytest -v`
Expected: all PASS. Callback dispatch is covered by `tests/test_skills.py` and `tests/test_persona_persistence.py` among others.

- [ ] **Step 20.5: Manual smoke test**

Re-exercise every button: `/tasks` cancel, `/reset` yes/no, `/persona` pick, `/skills` pick, `/use` pick.

- [ ] **Step 20.6: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): port button-click handler to TelegramTransport.on_button"
```

---

## Task 21: Implement `TelegramTransport` incoming-file download + `IncomingMessage.files`

**Spec step 8 (part 1) — inbound file normalization.**

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 21.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_incoming_message_populates_files_from_photo(tmp_path):
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)

    t.on_message(handler)

    # Mock a telegram photo: tg_msg.photo is a list of PhotoSize objects.
    downloaded = tmp_path / "photo.jpg"
    downloaded.write_bytes(b"\x89PNG\r\n")

    photo_size = SimpleNamespace(
        file_id="abc",
        file_unique_id="u",
        width=100,
        height=100,
        file_size=len(downloaded.read_bytes()),
        get_file=AsyncMock(return_value=SimpleNamespace(
            download_to_drive=AsyncMock(return_value=None, side_effect=lambda p: downloaded.rename(p) or None),
            file_path=str(downloaded),
        )),
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=1,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=[photo_size],
        document=None,
        voice=None,
        audio=None,
        caption="see this",
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.mime_type and f.mime_type.startswith("image/")
    # Caption becomes the message text.
    assert captured[0].text == "see this"
```

- [ ] **Step 21.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_incoming_message_populates_files_from_photo -v`
Expected: FAIL — `captured[0].files == []` (currently hardcoded).

- [ ] **Step 21.3: Extend `_dispatch_message` to download attachments**

In `src/link_project_to_chat/transport/telegram.py`, update `_dispatch_message`:

```python
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        import tempfile
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        files: list = []
        if getattr(msg, "photo", None):
            # Telegram returns multiple sizes; take the largest (last).
            largest = msg.photo[-1]
            tmpdir = tempfile.TemporaryDirectory()
            path = Path(tmpdir.name) / "photo.jpg"
            tg_file = await largest.get_file()
            await tg_file.download_to_drive(path)
            from .base import IncomingFile
            files.append(IncomingFile(
                path=path,
                original_name="photo.jpg",
                mime_type="image/jpeg",
                size_bytes=getattr(largest, "file_size", 0) or 0,
            ))
            # Tmpdir stays alive via closure; capture on the message so GC
            # releases it after the handler finishes.
            msg._transport_tmpdir = tmpdir

        doc = getattr(msg, "document", None)
        if doc is not None:
            tmpdir = tempfile.TemporaryDirectory()
            path = Path(tmpdir.name) / (doc.file_name or "document")
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(path)
            from .base import IncomingFile
            files.append(IncomingFile(
                path=path,
                original_name=doc.file_name or "document",
                mime_type=doc.mime_type,
                size_bytes=doc.file_size or 0,
            ))
            msg._transport_tmpdir = tmpdir

        from .base import IncomingMessage
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
```

- [ ] **Step 21.4: Run the test and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all PASS.

- [ ] **Step 21.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): populate IncomingMessage.files for photos and documents"
```

---

## Task 22: Implement `TelegramTransport.send_file`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 22.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
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
```

- [ ] **Step 22.2: Run the tests and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_send_file_calls_send_document_for_non_image tests/transport/test_telegram_transport.py::test_send_file_calls_send_photo_for_image -v`
Expected: FAIL — `NotImplementedError: Wired in Task 22`.

- [ ] **Step 22.3: Implement `send_file`**

In `src/link_project_to_chat/transport/telegram.py`, replace `send_file`'s body:

```python
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class TelegramTransport:
    ...

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
                    chat_id=chat_id, photo=fh, caption=caption
                )
        else:
            with path.open("rb") as fh:
                native = await self._app.bot.send_document(
                    chat_id=chat_id, document=fh, caption=caption, filename=display_name,
                )
        return message_ref_from_telegram(native)
```

Place `_IMAGE_SUFFIXES` as a module-level constant above the class.

- [ ] **Step 22.4: Run the tests and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all PASS.

- [ ] **Step 22.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.send_file (photo vs document by suffix)"
```

---

## Task 23: Port `bot.py` file-receive and outbound-image paths

**Spec step 8 (bot-side).**

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 23.1: Port `_on_file` / `_on_photo` / `_on_document` to consume `IncomingMessage.files`**

In bot.py, find the existing file-handler methods (`_on_file` — the handler registered for `filters.Document.ALL | filters.PHOTO`). Add a transport-native variant:

```python
    async def _on_file_from_transport(self, incoming) -> None:
        if not self._auth_identity(incoming.sender):
            return
        if not incoming.files:
            return  # defensive: no files on this message
        for f in incoming.files:
            # Existing logic in _on_file: save to {project}/uploads/<name>
            dest = self.path / "uploads" / f.original_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Copy from the temp path the transport downloaded to.
            dest.write_bytes(f.path.read_bytes())
            # Then reply with the original confirmation message from _on_file.
            assert self._transport is not None
            await self._transport.send_text(
                incoming.chat,
                f"Saved {f.original_name} to uploads/",
            )
```

The exact reply text and upload-directory structure MUST match `_on_file`'s existing behavior. Read the legacy `_on_file` and port its logic verbatim.

- [ ] **Step 23.2: Register the transport file handler in the main message-handler path**

Since files arrive via `on_message` with `IncomingMessage.files` populated, the existing `_on_text_from_transport` shim should branch on `incoming.files`:

```python
    async def _on_text_from_transport(self, incoming) -> None:
        if incoming.files:
            await self._on_file_from_transport(incoming)
            return
        native = incoming.native
        if native is None:
            return
        from types import SimpleNamespace
        fake_update = SimpleNamespace(
            effective_message=native,
            effective_user=native.from_user,
            effective_chat=native.chat,
        )
        await self._on_text(fake_update, None)
```

- [ ] **Step 23.3: Remove the legacy `_on_file` MessageHandler registration**

In `_build_app`, delete:

```python
            file_filter = private & (filters.Document.ALL | filters.PHOTO)
            app.add_handler(MessageHandler(file_filter, self._on_file))
```

The transport's `_dispatch_message` already catches photo/document updates as `IncomingMessage.files`, because the *main* MessageHandler filter (currently `text_filter`) needs to be broadened to include files. Update that filter:

Change:

```python
            text_filter = (
                private
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
```

to:

```python
            incoming_filter = (
                private
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & (filters.TEXT | filters.Document.ALL | filters.PHOTO)
                & ~filters.COMMAND
            )
```

And update the `MessageHandler(text_filter, ...)` registration to use `incoming_filter`.

- [ ] **Step 23.4: Port `_send_image` to `transport.send_file`**

Find every call to `self._send_image(...)` in bot.py. Replace each:

Before:
```python
await self._send_image(chat_id=msg.chat.id, file_path=path_str, reply_to=msg.message_id)
```

After:
```python
from .transport.telegram import chat_ref_from_telegram
chat_ref = chat_ref_from_telegram(msg.chat)
assert self._transport is not None
await self._transport.send_file(chat_ref, Path(path_str))
```

Then delete the `_send_image` method.

- [ ] **Step 23.5: Delete `_on_file`**

Remove the legacy `_on_file` method from bot.py (all its logic now lives in `_on_file_from_transport`).

- [ ] **Step 23.6: Run the full test suite**

Run: `pytest -v`
Expected: all PASS. File-handling tests to watch: any in `tests/test_bot_streaming.py` that involves attachments, plus manual smoke below.

- [ ] **Step 23.7: Manual smoke test**

- Upload a photo to the bot — verify it's saved to `{project}/uploads/`.
- Upload a PDF to the bot — verify it's saved.
- Trigger a Claude tool that screenshots a file — verify the image arrives back in chat.

- [ ] **Step 23.8: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): port file receive and image send through Transport"
```

---

## Task 24: Add `TelegramTransport` to the contract test

**Files:**
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 24.1: Add a fixture that produces a driveable TelegramTransport**

Edit `tests/transport/test_contract.py`. At the top, add:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from link_project_to_chat.transport.telegram import TelegramTransport


def _make_telegram_transport_with_inject() -> TelegramTransport:
    """Return a TelegramTransport whose inject_* methods drive _dispatch_* internals.

    The contract test's inject_* calls are aliased to the dispatch helpers so
    the same test body works against both FakeTransport (native inject_*) and
    TelegramTransport (inject_* → _dispatch_*).
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

    # Adapters so the parametrized contract test can use the same inject_* calls:
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

    t.inject_message = inject_message
    t.inject_command = inject_command
    t.inject_button_click = inject_button_click
    return t
```

- [ ] **Step 24.2: Add `TelegramTransport` as a second parametrize option**

Replace the fixture in `tests/transport/test_contract.py`:

```python
@pytest.fixture(params=["fake", "telegram"])
def transport(request) -> Transport:
    if request.param == "fake":
        t = FakeTransport()
    elif request.param == "telegram":
        t = _make_telegram_transport_with_inject()
    else:
        pytest.fail(f"Unknown param: {request.param}")
    yield t
```

- [ ] **Step 24.3: Run the contract test**

Run: `pytest tests/transport/test_contract.py -v`
Expected: all 4 tests × 2 transports = 8 test cases PASS.

- [ ] **Step 24.4: Commit**

```bash
git add tests/transport/test_contract.py
git commit -m "test(transport): contract test parametrized over TelegramTransport"
```

---

## Task 25: Lockout — remove remaining `from telegram` imports from `bot.py`

**Spec step 9.**

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 25.1: Identify remaining direct telegram imports**

Run:

```bash
grep -n "from telegram\|import telegram" src/link_project_to_chat/bot.py
```

Expected remaining hits: imports of `InlineKeyboardButton`, `InlineKeyboardMarkup`, `Update`, `ChatAction`, `ApplicationBuilder`, `CallbackQueryHandler`, `CommandHandler`, `ContextTypes`, `MessageHandler`, `filters`.

- [ ] **Step 25.2: Eliminate legitimate remaining uses**

Decide for each import:

- **`InlineKeyboardButton`, `InlineKeyboardMarkup`** — should be unused after Task 19. Delete the import.
- **`Update`, `ContextTypes`** — used in legacy handlers (text, voice, unsupported). `_on_text` and `_on_voice` still use them; `_on_unsupported` was ported in Task 8 but the method signature still has them. Two remaining handler signatures will need shims OR the signatures themselves stay because they're wired to telegram's `MessageHandler` directly.
- **`ApplicationBuilder`, `CommandHandler`, `MessageHandler`, `CallbackQueryHandler`, `filters`, `ChatAction`** — used inside `_build_app` to set up python-telegram-bot's routing. These are legitimate: the Transport wraps the Application, but **`bot.py` still needs to build it**. The lockout goal for `bot.py` is **no direct send/edit/receive calls**, not "no telegram import at all".

Revise the spec's lockout goal: the grep test allows `from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters` and `from telegram import Update, ChatAction` in `bot.py` because these are `Application` construction plumbing that `TelegramTransport` uses via its constructor. **All other** `from telegram` / `import telegram` usages must be gone.

- [ ] **Step 25.3: Relocate `ApplicationBuilder` construction into `TelegramTransport`**

Move the `ApplicationBuilder` setup out of `bot.py._build_app` and into a factory on `TelegramTransport`:

In `src/link_project_to_chat/transport/telegram.py`:

```python
    @classmethod
    def build(cls, token: str) -> "TelegramTransport":
        """Construct a TelegramTransport with a polling-mode Application."""
        from telegram.ext import ApplicationBuilder
        app = ApplicationBuilder().token(token).build()
        return cls(app)

    def attach_telegram_routing(
        self,
        *,
        private: bool,
        commands: list[str],
    ) -> None:
        """Wire python-telegram-bot handlers that delegate to _dispatch_* helpers.

        Called once by the bot after construction. Internally uses telegram's
        MessageHandler / CommandHandler / CallbackQueryHandler filters so
        bot.py doesn't need to import them.
        """
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters
        chat_filter = filters.ChatType.PRIVATE if private else filters.ChatType.GROUPS

        incoming_filter = (
            chat_filter
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
            & (filters.TEXT | filters.Document.ALL | filters.PHOTO)
            & ~filters.COMMAND
        )
        self._app.add_handler(MessageHandler(incoming_filter, self._dispatch_message))

        for name in commands:
            self._app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._dispatch_command(_n, u, c),
                filters=chat_filter,
            ))

        self._app.add_handler(CallbackQueryHandler(self._dispatch_button))
```

- [ ] **Step 25.4: Simplify `bot.py._build_app`**

Replace the whole `_build_app` method with:

```python
    def _build_app(self) -> Any:
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport.build(self.token)
        self._app = self._transport._app

        command_bindings = (
            ("help", self._on_help),
            ("version", self._on_version),
            ("status", self._on_status),
            ("start", self._on_start),
            ("run", self._on_run),
            ("tasks", self._on_tasks),
            ("model", self._on_model),
            ("effort", self._on_effort),
            ("thinking", self._on_thinking),
            ("permissions", self._on_permissions),
            ("compact", self._on_compact),
            ("reset", self._on_reset),
            ("skills", self._on_skills),
            ("stop_skill", self._on_stop_skill),
            ("create_skill", self._on_create_skill),
            ("delete_skill", self._on_delete_skill),
            ("persona", self._on_persona),
            ("stop_persona", self._on_stop_persona),
            ("create_persona", self._on_create_persona),
            ("delete_persona", self._on_delete_persona),
            ("voice", self._on_voice_status),
            ("lang", self._on_lang),
            ("halt", self._on_halt),
            ("resume", self._on_resume),
        )

        self._transport.on_message(self._on_text_from_transport)
        self._transport.on_button(self._on_button)
        for name, handler in command_bindings:
            self._transport.on_command(name, handler)

        self._transport.attach_telegram_routing(
            private=not self.group_mode,
            commands=[name for name, _ in command_bindings],
        )

        self._app.add_error_handler(self._on_error)
        return self._app
```

- [ ] **Step 25.5: Delete now-dead imports from `bot.py`**

At the top of `bot.py`, remove:

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
```

For any residual usage of `Update` / `ContextTypes` in remaining handler signatures that weren't ported (e.g., `_on_voice` if voice stays out of scope per spec #0b), either:
a) Leave those handlers + imports until spec #0b; OR
b) If no remaining handlers use them, delete them.

**Decision:** since voice is deferred to spec #0b, `_on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE)` stays. So `from telegram import Update` and `from telegram.ext import ContextTypes` **must stay until spec #0b lands**.

Reconcile the lockout criterion: after this step, bot.py should import ONLY `Update` and `ContextTypes` from telegram, and only for the voice handler path. Everything else is gone.

- [ ] **Step 25.6: Run the grep lockout**

Run:

```bash
grep -n "from telegram\|import telegram" src/link_project_to_chat/bot.py
```

Expected:

```
<line>: from telegram import Update
<line>: from telegram.ext import ContextTypes
```

Two lines total, both documented as held for spec #0b. If more than two lines match, repeat Step 25.5 for each residual.

- [ ] **Step 25.7: Run the full test suite**

Run: `pytest -v`
Expected: all PASS. This is the big-integration moment — if any existing test fails here, it's a sign a port step drifted behavior. Debug each failure individually.

- [ ] **Step 25.8: Manual end-to-end smoke test**

Full flow:
1. Start the bot.
2. Send `/help` — verify commands list.
3. Chat with Claude — verify streaming edits.
4. `/tasks` → click Cancel on a running task.
5. Send a photo — verify saved.
6. Send a sticker — verify unsupported reply.
7. `/reset` → confirm → verify session cleared.

- [ ] **Step 25.9: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/transport/telegram.py
git commit -m "refactor(bot): lockout — only telegram imports left are Update/ContextTypes (for voice, spec #0b)"
```

---

## Task 26: Add a grep-based lockout check to CI

**Goal:** prevent regressions where new `from telegram` imports creep back into `bot.py`.

**Files:**
- Create: `tests/test_transport_lockout.py`

- [ ] **Step 26.1: Write the lockout test**

Create `tests/test_transport_lockout.py`:

```python
"""Enforce the Transport lockout: bot.py cannot re-introduce telegram coupling.

The only telegram imports allowed in bot.py are `Update` and `ContextTypes`,
held for the voice handler path until spec #0b lands.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_bot_py_only_imports_update_and_context_types_from_telegram():
    src = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    # Match any line importing from telegram or telegram.*
    pattern = re.compile(r"^\s*(from\s+telegram(\.\w+)*\s+import|import\s+telegram)", re.MULTILINE)
    hits = pattern.findall(src)
    # Extract the actual matched lines for assertion messages.
    lines = [line for line in src.splitlines() if pattern.match(line)]
    # Allowed set — update only when deleting the two residuals in spec #0b.
    allowed = {
        "from telegram import Update",
        "from telegram.ext import ContextTypes",
    }
    actual = {line.strip() for line in lines}
    assert actual <= allowed, (
        f"Unexpected telegram imports in bot.py: {actual - allowed}. "
        "All new code must go through the Transport abstraction."
    )
```

- [ ] **Step 26.2: Run the test and confirm pass**

Run: `pytest tests/test_transport_lockout.py -v`
Expected: PASS. If FAIL, `bot.py` has more telegram imports than allowed — return to Task 25.5.

- [ ] **Step 26.3: Commit**

```bash
git add tests/test_transport_lockout.py
git commit -m "test: lockout test prevents telegram coupling from creeping back into bot.py"
```

---

## Task 27: Final cleanup + update `where-are-we.md`

**Files:**
- Modify: `where-are-we.md`

- [ ] **Step 27.1: Update the project state document**

Append to `where-are-we.md` under the `## Done` section:

```markdown
- **Transport abstraction** (spec #0, v0.13.0):
  - `Transport` Protocol + primitive types (`ChatRef`, `Identity`, `MessageRef`, `Buttons`, `IncomingMessage`, `IncomingFile`, `CommandInvocation`) in `src/link_project_to_chat/transport/`
  - `TelegramTransport` implementation wraps python-telegram-bot
  - `FakeTransport` for tests; parametrized contract test over all transports
  - `StreamingMessage` transport-agnostic streaming-edit helper (replaces `livestream.LiveMessage` in the project bot)
  - `bot.py` only imports `Update` and `ContextTypes` from telegram (voice path, pending spec #0b)
  - Lockout test prevents future direct-telegram coupling
```

Under the `## Pending` section, remove any items now resolved:
- "Open file handles in `_send_image`" — resolved (Transport opens/closes files internally).
- "File uploads stored permanently in project dir — consider `/tmp/{project_name}/` for temp files" — resolved for inbound (Transport temp-dirs); still applies to the bot's chosen `uploads/` dir policy, leave it.

- [ ] **Step 27.2: Bump version**

Edit `pyproject.toml`:

```toml
version = "0.13.0"
```

- [ ] **Step 27.3: Commit**

```bash
git add where-are-we.md pyproject.toml
git commit -m "docs: note transport abstraction complete; bump to 0.13.0"
```

---

## Completion checklist

- [ ] All 27 tasks committed in order.
- [ ] `pytest -v` passes green.
- [ ] Manual smoke test (Task 25.8) covers all major flows.
- [ ] `tests/test_transport_lockout.py` passes — no rogue telegram imports.
- [ ] Spec #0 is closed; follow-up specs #0a (group/team port), #0b (voice port), #1 (Web UI transport) are unblocked.
