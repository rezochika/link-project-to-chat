# Discord Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `DiscordTransport` using `discord.py` 2.x, wired to the existing project and manager bot surfaces via the shared primitives introduced in spec #1 (structured mentions, `PromptSpec`, `BotPeerRef`/`RoomBinding`).

**Architecture:** `DiscordTransport` wraps `discord.ext.commands.Bot` with an application command tree for `/lp2c` subcommands; `PromptSpec(TEXT/SECRET)` maps to Discord modals and `CHOICE/CONFIRM` to `discord.ui.View` buttons; structured snowflake IDs populate `IncomingMessage.mentions`; no relay layer — bot-to-bot is native `sender.is_bot=True` traffic.

**Tech Stack:** Python 3.11+, `discord.py` 2.x, `discord.ext.commands.Bot`, `discord.app_commands`, `discord.ui.Modal`, `discord.ui.View`

**Prerequisite:** Plan `2026-04-21-web-transport.md` must be complete (all shared primitives in `transport/base.py`, `config.py`, `group_filters.py`, and `FakeTransport` extended).

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/transport/discord.py` | **NEW**: `DiscordTransport` full implementation |
| `src/link_project_to_chat/transport/__init__.py` | Export `DiscordTransport` |
| `pyproject.toml` | Add `discord` optional dep group: `discord.py>=2.3` |
| `tests/transport/test_contract.py` | Add `DiscordTransport` to `transport` fixture |
| `tests/transport/test_discord_transport.py` | **NEW**: Discord-specific unit tests (modal submit, mentions, channel routing) |

---

### Task 1: Add `discord.py` dependency, config surface, and `DiscordTransport` skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/link_project_to_chat/transport/discord.py`
- Create: `tests/transport/test_discord_transport.py` (initial skeleton)

- [ ] **Step 1: Write the failing skeleton test**

```python
# tests/transport/test_discord_transport.py
def test_discord_transport_importable():
    from link_project_to_chat.transport.discord import DiscordTransport  # noqa: F401


def test_discord_transport_id():
    from unittest.mock import MagicMock
    from link_project_to_chat.transport.discord import DiscordTransport

    bot = MagicMock()
    bot.latency = 0.0
    t = DiscordTransport(bot)
    assert t.TRANSPORT_ID == "discord"
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_discord_transport.py::test_discord_transport_importable -v
```
Expected: `ModuleNotFoundError` — `link_project_to_chat.transport.discord` does not exist and `discord` package is not installed.

- [ ] **Step 3: Add `discord` optional dep to `pyproject.toml`**

```toml
[project.optional-dependencies]
discord = ["discord.py>=2.3"]
all = ["httpx>=0.27", "telethon>=1.36", "openai>=1.30", "fastapi[standard]>=0.111", "jinja2>=3.1", "aiosqlite>=0.19", "discord.py>=2.3"]
```

Install it:
```
pip install -e ".[discord]"
```

- [ ] **Step 4: Create `src/link_project_to_chat/transport/discord.py` skeleton**

```python
"""DiscordTransport — Transport Protocol implementation for Discord.

Uses discord.py 2.x with application commands for /lp2c subcommands,
discord.ui.Modal for TEXT/SECRET prompts, and discord.ui.View buttons
for CHOICE/CONFIRM prompts. Structured snowflake IDs populate
IncomingMessage.mentions for ID-based team routing.
"""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Awaitable, Callable

from link_project_to_chat.transport.base import (
    ButtonClick,
    ButtonHandler,
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
    OnReadyCallback,
    PromptHandler,
    PromptRef,
    PromptSpec,
    PromptSubmission,
    TransportRetryAfter,
)


def _identity_from_discord_user(user: Any) -> Identity:
    return Identity(
        transport_id="discord",
        native_id=str(user.id),
        display_name=getattr(user, "display_name", str(user)),
        handle=getattr(user, "name", None),
        is_bot=getattr(user, "bot", False),
    )


def _chat_ref_from_discord_channel(channel: Any) -> ChatRef:
    import discord as _discord
    kind = ChatKind.DM if isinstance(channel, _discord.DMChannel) else ChatKind.ROOM
    return ChatRef(transport_id="discord", native_id=str(channel.id), kind=kind)


def _message_ref_from_discord_message(msg: Any) -> MessageRef:
    chat = _chat_ref_from_discord_channel(msg.channel)
    return MessageRef(transport_id="discord", native_id=str(msg.id), chat=chat)


class DiscordTransport:
    TRANSPORT_ID = "discord"

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._prompt_handlers: list[PromptHandler] = []
        self._msg_counter = itertools.count(1)
        self._prompt_counter = itertools.count(1)

    @classmethod
    def build(cls, token: str) -> "DiscordTransport":
        import discord
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        bot = discord.ext.commands.Bot(command_prefix="!", intents=intents)
        return cls(bot)

    async def start(self) -> None:
        pass  # caller drives bot.start(token)

    async def stop(self) -> None:
        await self._bot.close()

    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def on_ready(self, callback: OnReadyCallback) -> None:
        self._on_ready_callbacks.append(callback)

    def on_prompt_submit(self, handler: PromptHandler) -> None:
        self._prompt_handlers.append(handler)

    async def send_text(self, chat: ChatRef, text: str, *, buttons: Buttons | None = None, html: bool = False, reply_to: MessageRef | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def edit_text(self, msg: MessageRef, text: str, *, buttons: Buttons | None = None, html: bool = False) -> None:
        raise NotImplementedError("implemented in Task 2")

    async def send_file(self, chat: ChatRef, path: Path, *, caption: str | None = None, display_name: str | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def send_voice(self, chat: ChatRef, path: Path, *, reply_to: MessageRef | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def send_typing(self, chat: ChatRef) -> None:
        raise NotImplementedError("implemented in Task 2")

    async def open_prompt(self, chat: ChatRef, spec: PromptSpec, *, reply_to: MessageRef | None = None) -> PromptRef:
        raise NotImplementedError("implemented in Task 4")

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        raise NotImplementedError("implemented in Task 4")

    async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
        raise NotImplementedError("implemented in Task 4")
```

- [ ] **Step 5: Run to confirm skeleton tests pass**

```
pytest tests/transport/test_discord_transport.py::test_discord_transport_importable tests/transport/test_discord_transport.py::test_discord_transport_id -v
```
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/link_project_to_chat/transport/discord.py tests/transport/test_discord_transport.py
git commit -m "feat: add DiscordTransport skeleton and discord.py dependency"
```

---

### Task 2: Implement outbound methods

**Files:**
- Modify: `src/link_project_to_chat/transport/discord.py`
- Modify: `tests/transport/test_discord_transport.py`

The outbound methods interact with the Discord API. In tests, the Discord bot is mocked.

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_discord_transport.py`:

```python
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.discord import DiscordTransport


def _make_mock_transport() -> DiscordTransport:
    """DiscordTransport with a mocked discord.Bot for unit tests."""
    import discord

    channel = MagicMock()
    channel.id = 100
    channel.__class__ = discord.TextChannel  # marks as ROOM
    channel.send = AsyncMock(return_value=SimpleNamespace(id=200, channel=channel))
    channel.typing = MagicMock(return_value=AsyncMock().__aenter__)

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)

    t = DiscordTransport(bot)
    t._channel_cache = {100: channel}
    return t


def _room() -> ChatRef:
    return ChatRef(transport_id="discord", native_id="100", kind=ChatKind.ROOM)


async def test_send_text_returns_message_ref():
    t = _make_mock_transport()
    ref = await t.send_text(_room(), "hello")
    assert isinstance(ref, MessageRef)
    assert ref.transport_id == "discord"
    assert ref.chat == _room()


async def test_send_text_calls_channel_send():
    t = _make_mock_transport()
    await t.send_text(_room(), "hello discord")
    t._channel_cache[100].send.assert_called_once()
    call_kwargs = t._channel_cache[100].send.call_args
    assert "hello discord" in str(call_kwargs)


async def test_edit_text_does_not_raise():
    t = _make_mock_transport()
    msg_mock = SimpleNamespace(id=200, edit=AsyncMock())
    t._message_cache = {"200": msg_mock}
    ref = MessageRef(transport_id="discord", native_id="200", chat=_room())
    await t.edit_text(ref, "updated")
    msg_mock.edit.assert_called_once()


async def test_send_file_returns_message_ref(tmp_path):
    t = _make_mock_transport()
    f = tmp_path / "test.txt"
    f.write_bytes(b"data")
    ref = await t.send_file(_room(), f)
    assert isinstance(ref, MessageRef)


async def test_send_voice_returns_message_ref(tmp_path):
    t = _make_mock_transport()
    f = tmp_path / "voice.opus"
    f.write_bytes(b"fake opus")
    ref = await t.send_voice(_room(), f)
    assert isinstance(ref, MessageRef)
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_discord_transport.py -k "send or edit" -v
```
Expected: all fail with `NotImplementedError`.

- [ ] **Step 3: Implement outbound methods in `discord.py`**

Replace the `raise NotImplementedError` stubs for outbound methods:

```python
async def send_text(
    self,
    chat: ChatRef,
    text: str,
    *,
    buttons: Buttons | None = None,
    html: bool = False,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    import discord as _discord
    channel = await self._get_channel(chat)
    view = self._buttons_to_view(buttons) if buttons else None
    # Discord does not use HTML; strip basic tags to plain text
    clean_text = self._strip_html(text) if html else text
    kwargs: dict[str, Any] = {"content": clean_text}
    if view:
        kwargs["view"] = view
    if reply_to:
        ref_msg = self._message_cache.get(reply_to.native_id)
        if ref_msg:
            kwargs["reference"] = ref_msg
    sent = await channel.send(**kwargs)
    msg_ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(sent.id), chat=chat)
    self._message_cache[str(sent.id)] = sent
    return msg_ref

async def edit_text(
    self,
    msg: MessageRef,
    text: str,
    *,
    buttons: Buttons | None = None,
    html: bool = False,
) -> None:
    cached = self._message_cache.get(msg.native_id)
    if cached is None:
        return
    clean_text = self._strip_html(text) if html else text
    view = self._buttons_to_view(buttons) if buttons else None
    kwargs: dict[str, Any] = {"content": clean_text}
    if view is not None:
        kwargs["view"] = view
    await cached.edit(**kwargs)

async def send_file(
    self,
    chat: ChatRef,
    path: Path,
    *,
    caption: str | None = None,
    display_name: str | None = None,
) -> MessageRef:
    import discord as _discord
    channel = await self._get_channel(chat)
    sent = await channel.send(
        content=caption or "",
        file=_discord.File(path, filename=display_name or path.name),
    )
    ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(sent.id), chat=chat)
    self._message_cache[str(sent.id)] = sent
    return ref

async def send_voice(
    self,
    chat: ChatRef,
    path: Path,
    *,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    import discord as _discord
    channel = await self._get_channel(chat)
    sent = await channel.send(file=_discord.File(path, filename=path.name))
    ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(sent.id), chat=chat)
    self._message_cache[str(sent.id)] = sent
    return ref

async def send_typing(self, chat: ChatRef) -> None:
    channel = await self._get_channel(chat)
    async with channel.typing():
        pass
```

Also add these private helpers and `__init__` additions:

In `__init__`, add:
```python
self._channel_cache: dict[int, Any] = {}
self._message_cache: dict[str, Any] = {}
```

Add helpers:
```python
async def _get_channel(self, chat: ChatRef) -> Any:
    channel_id = int(chat.native_id)
    if channel_id in self._channel_cache:
        return self._channel_cache[channel_id]
    channel = await self._bot.fetch_channel(channel_id)
    self._channel_cache[channel_id] = channel
    return channel

def _buttons_to_view(self, buttons: Buttons) -> Any:
    import discord as _discord

    transport = self

    class _BotView(_discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            for row_idx, row in enumerate(buttons.rows):
                for btn in row:
                    b = _discord.ui.Button(label=btn.label, custom_id=btn.value, row=row_idx)
                    async def callback(interaction, v=btn.value):
                        msg_ref = MessageRef(
                            transport_id=transport.TRANSPORT_ID,
                            native_id=str(interaction.message.id),
                            chat=_chat_ref_from_discord_channel(interaction.channel),
                        )
                        sender = _identity_from_discord_user(interaction.user)
                        click = ButtonClick(chat=msg_ref.chat, message=msg_ref, sender=sender, value=v)
                        for h in transport._button_handlers:
                            await h(click)
                        await interaction.response.defer()
                    b.callback = callback
                    self.add_item(b)
    return _BotView()

@staticmethod
def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_discord_transport.py -k "send or edit" -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/discord.py tests/transport/test_discord_transport.py
git commit -m "feat: implement DiscordTransport outbound methods (send_text, edit, send_file, send_voice)"
```

---

### Task 3: Implement `/lp2c` application command bridge

**Files:**
- Modify: `src/link_project_to_chat/transport/discord.py`
- Modify: `tests/transport/test_discord_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_discord_transport.py`:

```python
async def test_lp2c_command_dispatched_as_command_invocation():
    from link_project_to_chat.transport import CommandInvocation
    from types import SimpleNamespace

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("projects", handler)

    # Simulate the /lp2c slash command interaction
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=5, display_name="Alice", name="alice", bot=False),
        channel=SimpleNamespace(id=100, __class__=__import__("discord").TextChannel),
        channel_id=100,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )
    await t._handle_lp2c_interaction(interaction, args="projects")

    assert len(seen) == 1
    assert seen[0].name == "projects"
    assert seen[0].raw_text == "/lp2c projects"


async def test_lp2c_command_with_extra_args():
    from link_project_to_chat.transport import CommandInvocation
    from types import SimpleNamespace

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("model", handler)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=5, display_name="Alice", name="alice", bot=False),
        channel=SimpleNamespace(id=100, __class__=__import__("discord").TextChannel),
        channel_id=100,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )
    await t._handle_lp2c_interaction(interaction, args="model set sonnet")

    assert seen[0].args == ["set", "sonnet"]
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_discord_transport.py -k "command" -v
```
Expected: `AttributeError` — no `_handle_lp2c_interaction` method.

- [ ] **Step 3: Add `_handle_lp2c_interaction` and `attach_discord_routing` to `DiscordTransport`**

```python
async def _handle_lp2c_interaction(self, interaction: Any, args: str = "") -> None:
    """Parse /lp2c <name> [args...] and dispatch to registered command handler."""
    import discord as _discord
    parts = args.strip().split()
    name = parts[0] if parts else "help"
    extra_args = parts[1:] if len(parts) > 1 else []
    raw_text = f"/lp2c {args}".strip()

    chat = _chat_ref_from_discord_channel(interaction.channel)
    sender = _identity_from_discord_user(interaction.user)

    msg_ref = MessageRef(
        transport_id=self.TRANSPORT_ID,
        native_id=str(next(self._msg_counter)),
        chat=chat,
    )
    ci = CommandInvocation(
        chat=chat, sender=sender, name=name,
        args=extra_args, raw_text=raw_text, message=msg_ref,
    )
    handler = self._command_handlers.get(name)
    if handler:
        await handler(ci)
    else:
        await interaction.response.send_message(f"Unknown command: {name}", ephemeral=True)

def attach_discord_routing(self) -> None:
    """Register discord.py event handlers and slash commands on the bot."""
    import discord as _discord

    transport = self

    @self._bot.event
    async def on_ready():
        identity = _identity_from_discord_user(self._bot.user)
        for cb in self._on_ready_callbacks:
            await cb(identity)
        await self._bot.tree.sync()

    @self._bot.event
    async def on_message(message):
        if message.author == self._bot.user:
            return
        mentions = [_identity_from_discord_user(u) for u in message.mentions]
        chat = _chat_ref_from_discord_channel(message.channel)
        sender = _identity_from_discord_user(message.author)
        incoming = IncomingMessage(
            chat=chat, sender=sender, text=message.content or "",
            files=[], reply_to=None, mentions=mentions,
            is_relayed_bot_to_bot=False,
        )
        for h in transport._message_handlers:
            await h(incoming)

    @self._bot.tree.command(name="lp2c", description="link-project-to-chat commands")
    async def lp2c_slash(
        interaction: _discord.Interaction,
        args: str = "",
    ):
        await transport._handle_lp2c_interaction(interaction, args=args)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_discord_transport.py -k "command" -v
```
Expected: both command tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/discord.py tests/transport/test_discord_transport.py
git commit -m "feat: add /lp2c application command dispatch to DiscordTransport"
```

---

### Task 4: Implement prompt mapping (modals + buttons)

**Files:**
- Modify: `src/link_project_to_chat/transport/discord.py`
- Modify: `tests/transport/test_discord_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_discord_transport.py`:

```python
async def test_open_prompt_text_returns_prompt_ref():
    from types import SimpleNamespace
    from link_project_to_chat.transport import PromptKind, PromptRef, PromptSpec

    t = _make_mock_transport()
    chat = _room()
    spec = PromptSpec(key="name", title="Enter name", body="Type your name", kind=PromptKind.TEXT)

    # Simulate having an interaction context for the TEXT prompt
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_modal=AsyncMock()),
    )
    t._pending_interaction = interaction

    ref = await t.open_prompt(chat, spec)
    assert isinstance(ref, PromptRef)
    assert ref.key == "name"


async def test_prompt_submit_fires_on_modal_callback():
    from types import SimpleNamespace
    from link_project_to_chat.transport import (
        PromptKind, PromptSpec, PromptSubmission
    )

    t = _make_mock_transport()
    chat = _room()
    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)

    seen: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        seen.append(sub)

    t.on_prompt_submit(on_submit)

    interaction = SimpleNamespace(
        response=SimpleNamespace(send_modal=AsyncMock()),
        user=SimpleNamespace(id=5, display_name="Alice", name="alice", bot=False),
        channel=SimpleNamespace(id=100, __class__=__import__("discord").TextChannel),
    )
    t._pending_interaction = interaction

    ref = await t.open_prompt(chat, spec)

    # Simulate modal submission
    await t._on_modal_submit(ref, "Alice", interaction)

    assert len(seen) == 1
    assert seen[0].text == "Alice"


async def test_open_prompt_choice_sends_view_message():
    from link_project_to_chat.transport import ButtonStyle, PromptKind, PromptOption, PromptSpec

    t = _make_mock_transport()
    spec = PromptSpec(
        key="pick",
        title="Pick model",
        body="Choose",
        kind=PromptKind.CHOICE,
        options=[PromptOption(value="sonnet", label="Sonnet"), PromptOption(value="opus", label="Opus")],
    )
    ref = await t.open_prompt(_room(), spec)
    # send_text on the channel was called (choice prompts send a message with buttons)
    t._channel_cache[100].send.assert_called_once()
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_discord_transport.py -k "prompt" -v
```
Expected: fail with `NotImplementedError` (from the stubs).

- [ ] **Step 3: Implement prompt methods in `discord.py`**

Replace the `open_prompt`, `update_prompt`, and `close_prompt` stubs:

```python
async def open_prompt(
    self,
    chat: ChatRef,
    spec: PromptSpec,
    *,
    reply_to: MessageRef | None = None,
) -> PromptRef:
    import discord as _discord
    from link_project_to_chat.transport.base import PromptKind

    native_id = str(next(self._prompt_counter))
    ref = PromptRef(
        transport_id=self.TRANSPORT_ID,
        native_id=native_id,
        chat=chat,
        key=spec.key,
    )
    self._prompt_specs[native_id] = spec

    if spec.kind in (PromptKind.TEXT, PromptKind.SECRET):
        interaction = getattr(self, "_pending_interaction", None)
        if interaction:
            modal = self._build_modal(ref, spec)
            await interaction.response.send_modal(modal)
        else:
            # Fallback: send a plain message asking for input
            await self.send_text(chat, f"**{spec.title}**\n{spec.body}")
    else:
        # CHOICE / CONFIRM / DISPLAY: send message with buttons
        view = self._build_prompt_view(ref, spec)
        channel = await self._get_channel(chat)
        body = f"**{spec.title}**\n{spec.body}" if spec.body else spec.title
        sent = await channel.send(content=body, view=view)
        self._message_cache[str(sent.id)] = sent
        self._prompt_message_ids[native_id] = str(sent.id)

    return ref

async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
    msg_id = self._prompt_message_ids.get(prompt.native_id)
    if msg_id and msg_id in self._message_cache:
        view = self._build_prompt_view(prompt, spec)
        await self._message_cache[msg_id].edit(
            content=f"**{spec.title}**\n{spec.body}", view=view
        )

async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
    msg_id = self._prompt_message_ids.pop(prompt.native_id, None)
    if msg_id and msg_id in self._message_cache:
        await self._message_cache[msg_id].edit(
            content=final_text or "✓", view=None
        )
    self._prompt_specs.pop(prompt.native_id, None)
```

Add these private helpers:

```python
def _build_modal(self, prompt_ref: PromptRef, spec: PromptSpec) -> Any:
    import discord as _discord
    from link_project_to_chat.transport.base import PromptKind

    transport = self

    class _Modal(_discord.ui.Modal, title=spec.title):
        answer: _discord.ui.TextInput = _discord.ui.TextInput(
            label=spec.body or spec.title,
            placeholder=spec.placeholder,
            style=_discord.TextStyle.short,
        )

        async def on_submit(self_modal, interaction: _discord.Interaction):
            await transport._on_modal_submit(prompt_ref, self_modal.answer.value, interaction)

    return _Modal()

def _build_prompt_view(self, prompt_ref: PromptRef, spec: PromptSpec) -> Any:
    import discord as _discord
    from link_project_to_chat.transport.base import PromptKind, PromptSubmission

    transport = self

    class _PromptView(_discord.ui.View):
        def __init__(self_view):
            super().__init__(timeout=None)
            for opt in spec.options:
                btn = _discord.ui.Button(label=opt.label, custom_id=opt.value)

                async def cb(interaction, v=opt.value):
                    sender = _identity_from_discord_user(interaction.user)
                    sub = PromptSubmission(
                        chat=prompt_ref.chat, sender=sender, prompt=prompt_ref, option=v
                    )
                    for h in transport._prompt_handlers:
                        await h(sub)
                    await interaction.response.defer()

                btn.callback = cb
                self_view.add_item(btn)

    return _PromptView()

async def _on_modal_submit(self, prompt_ref: PromptRef, value: str, interaction: Any) -> None:
    sender = _identity_from_discord_user(interaction.user)
    sub = PromptSubmission(
        chat=prompt_ref.chat, sender=sender, prompt=prompt_ref, text=value
    )
    for h in self._prompt_handlers:
        await h(sub)
```

In `__init__`, add:
```python
self._prompt_specs: dict[str, PromptSpec] = {}
self._prompt_message_ids: dict[str, str] = {}
self._pending_interaction: Any = None
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_discord_transport.py -k "prompt" -v
```
Expected: all 3 prompt tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/discord.py tests/transport/test_discord_transport.py
git commit -m "feat: implement DiscordTransport prompt mapping (modals + button views)"
```

---

### Task 5: Implement inbound message dispatch with structured mentions

**Files:**
- Modify: `src/link_project_to_chat/transport/discord.py`
- Modify: `tests/transport/test_discord_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_discord_transport.py`:

```python
async def test_structured_mentions_populated():
    from types import SimpleNamespace
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)

    # Simulate discord on_message with a mention
    import discord
    bot_user = SimpleNamespace(id=99, display_name="MyBot", name="mybot", bot=True)
    author = SimpleNamespace(id=5, display_name="Alice", name="alice", bot=False)
    channel = SimpleNamespace(
        id=100,
        __class__=discord.TextChannel,
    )
    message = SimpleNamespace(
        author=author,
        channel=channel,
        content="<@99> hello",
        mentions=[bot_user],
        attachments=[],
    )
    # Simulate the bot user so we can skip self-messages
    t._bot.user = SimpleNamespace(id=1)

    await t._dispatch_discord_message(message)

    assert len(received) == 1
    assert len(received[0].mentions) == 1
    assert received[0].mentions[0].native_id == "99"
    assert received[0].mentions[0].is_bot is True


async def test_self_messages_ignored():
    from types import SimpleNamespace
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))

    import discord
    bot_self = SimpleNamespace(id=1, display_name="Bot", name="bot", bot=True)
    message = SimpleNamespace(
        author=bot_self,
        channel=SimpleNamespace(id=100, __class__=discord.TextChannel),
        content="from myself",
        mentions=[],
        attachments=[],
    )
    t._bot.user = bot_self

    await t._dispatch_discord_message(message)
    assert received == []
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_discord_transport.py -k "mention or self" -v
```
Expected: `AttributeError` — `_dispatch_discord_message` does not exist.

- [ ] **Step 3: Add `_dispatch_discord_message` to `DiscordTransport`**

```python
async def _dispatch_discord_message(self, message: Any) -> None:
    """Normalize a discord.Message into IncomingMessage and dispatch."""
    if message.author == self._bot.user:
        return
    mentions = [_identity_from_discord_user(u) for u in (message.mentions or [])]
    chat = _chat_ref_from_discord_channel(message.channel)
    sender = _identity_from_discord_user(message.author)
    incoming = IncomingMessage(
        chat=chat,
        sender=sender,
        text=message.content or "",
        files=[],
        reply_to=None,
        mentions=mentions,
        is_relayed_bot_to_bot=False,
    )
    for h in self._message_handlers:
        await h(incoming)
```

Update `attach_discord_routing` to call this method from `on_message`:

```python
@self._bot.event
async def on_message(message):
    await transport._dispatch_discord_message(message)
```

- [ ] **Step 4: Add inject helpers for contract tests**

```python
async def inject_message(
    self,
    chat: ChatRef,
    sender: Identity,
    text: str,
    *,
    files: list[IncomingFile] | None = None,
    reply_to: MessageRef | None = None,
    mentions: list[Identity] | None = None,
) -> None:
    msg = IncomingMessage(
        chat=chat, sender=sender, text=text,
        files=files or [], reply_to=reply_to, mentions=mentions or [],
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
        transport_id=self.TRANSPORT_ID,
        native_id=str(next(self._msg_counter)),
        chat=chat,
    )
    ci = CommandInvocation(
        chat=chat, sender=sender, name=name,
        args=args, raw_text=raw_text, message=msg_ref,
    )
    handler = self._command_handlers.get(name)
    if handler:
        await handler(ci)

async def inject_button_click(self, message: MessageRef, sender: Identity, *, value: str) -> None:
    click = ButtonClick(chat=message.chat, message=message, sender=sender, value=value)
    for h in self._button_handlers:
        await h(click)

async def inject_prompt_submit(
    self,
    prompt: PromptRef,
    sender: Identity,
    *,
    text: str | None = None,
    option: str | None = None,
) -> None:
    sub = PromptSubmission(
        chat=prompt.chat, sender=sender, prompt=prompt, text=text, option=option
    )
    for h in self._prompt_handlers:
        await h(sub)
```

- [ ] **Step 5: Run to confirm pass**

```
pytest tests/transport/test_discord_transport.py -k "mention or self" -v
```
Expected: both tests PASS.

- [ ] **Step 6: Run full Discord test suite**

```
pytest tests/transport/test_discord_transport.py -v
```
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/transport/discord.py tests/transport/test_discord_transport.py
git commit -m "feat: DiscordTransport inbound dispatch with structured mentions + inject helpers"
```

---

### Task 6: Add `DiscordTransport` to contract tests and export

**Files:**
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 1: Export `DiscordTransport`**

In `transport/__init__.py`, add:

```python
from .discord import DiscordTransport

__all__ = [
    # ... existing ...
    "DiscordTransport",
]
```

- [ ] **Step 2: Add `DiscordTransport` to the contract test fixture**

In `tests/transport/test_contract.py`, add a factory:

```python
from link_project_to_chat.transport.discord import DiscordTransport


def _make_discord_transport_with_inject() -> DiscordTransport:
    """DiscordTransport with mocked bot for contract testing."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    import discord

    channel = MagicMock()
    channel.id = 1
    channel.__class__ = discord.TextChannel
    channel.send = AsyncMock(return_value=SimpleNamespace(id=200, channel=channel))

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.user = SimpleNamespace(id=999)

    t = DiscordTransport(bot)
    t._channel_cache = {1: channel}
    return t
```

Update the fixture:

```python
@pytest.fixture(params=["fake", "telegram", "web", "discord"])
async def transport(request, tmp_path):
    if request.param == "fake":
        yield FakeTransport()
    elif request.param == "telegram":
        yield _make_telegram_transport_with_inject()
    elif request.param == "web":
        from link_project_to_chat.transport import Identity
        from link_project_to_chat.web.transport import WebTransport
        db_path = tmp_path / "contract.db"
        bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
        t = WebTransport(db_path=db_path, bot_identity=bot, port=18181)
        await t.start()
        yield t
        await t.stop()
    elif request.param == "discord":
        yield _make_discord_transport_with_inject()
    else:
        pytest.fail(f"Unknown param: {request.param}")
```

- [ ] **Step 3: Run all contract tests**

```
pytest tests/transport/test_contract.py -v
```
Expected: all existing and new contract tests PASS for all four transports (prompt tests skip for telegram).

- [ ] **Step 4: Run full suite for regressions**

```
pytest -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/__init__.py tests/transport/test_contract.py
git commit -m "test: add DiscordTransport to transport contract test suite"
```
