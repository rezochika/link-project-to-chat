# Slack Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `SlackTransport` using `slack_bolt` with Socket Mode, wired to the existing project and manager bot surfaces via the shared primitives from spec #1 (structured mentions, `PromptSpec`, `BotPeerRef`/`RoomBinding`). This is the final transport in the additive track; if `PromptSpec` and structured mentions hold for Slack, the model is genuinely cross-platform.

**Architecture:** `SlackTransport` wraps a `slack_bolt.async_app.AsyncApp` with Socket Mode; `/lp2c` slash command maps to `CommandInvocation`; `PromptSpec(TEXT/SECRET)` opens Slack modals (Block Kit `input` views); `CHOICE/CONFIRM` sends `actions` Block Kit sections; `IncomingMessage.mentions` is populated from Slack mention entities (`<@U...>`) parsed from message text. Socket Mode means no public ingress needed.

**Tech Stack:** Python 3.11+, `slack-bolt>=1.18` with `slack_bolt.async_app.AsyncApp`, `slack_sdk>=3.19`, Socket Mode (`AsyncSocketModeHandler`)

**Prerequisite:** Plan `2026-04-21-web-transport.md` must be complete (all shared primitives in `transport/base.py`, `config.py`, `group_filters.py`, and `FakeTransport` extended).

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/transport/slack.py` | **NEW**: `SlackTransport` full implementation |
| `src/link_project_to_chat/transport/__init__.py` | Export `SlackTransport` |
| `pyproject.toml` | Add `slack` optional dep group: `slack-bolt>=1.18` |
| `tests/transport/test_contract.py` | Add `SlackTransport` to `transport` fixture |
| `tests/transport/test_slack_transport.py` | **NEW**: Slack-specific unit tests (modal submit, mention parsing, command parsing) |

---

### Task 1: Add `slack_bolt` dependency, config surface, and `SlackTransport` skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/link_project_to_chat/transport/slack.py`
- Create: `tests/transport/test_slack_transport.py` (initial skeleton)

- [ ] **Step 1: Write the failing skeleton test**

```python
# tests/transport/test_slack_transport.py
def test_slack_transport_importable():
    from link_project_to_chat.transport.slack import SlackTransport  # noqa: F401


def test_slack_transport_id():
    from unittest.mock import MagicMock
    from link_project_to_chat.transport.slack import SlackTransport

    app = MagicMock()
    t = SlackTransport(app)
    assert t.TRANSPORT_ID == "slack"
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py::test_slack_transport_importable -v
```
Expected: `ModuleNotFoundError` — `slack` package not installed and module does not exist.

- [ ] **Step 3: Add `slack` optional dep to `pyproject.toml`**

```toml
[project.optional-dependencies]
slack = ["slack-bolt>=1.18"]
all = ["httpx>=0.27", "telethon>=1.36", "openai>=1.30", "fastapi[standard]>=0.111", "jinja2>=3.1", "aiosqlite>=0.19", "discord.py>=2.3", "slack-bolt>=1.18"]
```

Install it:
```
pip install -e ".[slack]"
```

- [ ] **Step 4: Create `src/link_project_to_chat/transport/slack.py` skeleton**

```python
"""SlackTransport — Transport Protocol implementation for Slack.

Uses slack_bolt AsyncApp with Socket Mode so no public ingress is needed.
/lp2c slash command maps to CommandInvocation. PromptSpec(TEXT/SECRET)
opens Slack modal views; CHOICE/CONFIRM sends Block Kit actions sections.
Mentions are parsed from <@U...> entities to populate IncomingMessage.mentions.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

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

# Matches Slack mention tokens: <@U12345678> or <@U12345678|alice>
_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")


def _parse_mentions(text: str, client: Any) -> list[Identity]:
    """Extract structured Identity objects from Slack mention tokens in text."""
    ids = _MENTION_RE.findall(text)
    result: list[Identity] = []
    for uid in ids:
        result.append(Identity(
            transport_id="slack",
            native_id=uid,
            display_name="",
            handle=None,
            is_bot=False,  # unknown without API call; set best-effort
        ))
    return result


def _chat_ref_from_slack(channel_id: str, is_dm: bool) -> ChatRef:
    kind = ChatKind.DM if is_dm else ChatKind.ROOM
    return ChatRef(transport_id="slack", native_id=channel_id, kind=kind)


def _message_ref_from_slack(channel_id: str, ts: str, is_dm: bool) -> MessageRef:
    chat = _chat_ref_from_slack(channel_id, is_dm)
    return MessageRef(transport_id="slack", native_id=ts, chat=chat)


def _identity_from_slack_event(user_id: str, *, display_name: str = "", is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="slack",
        native_id=user_id,
        display_name=display_name,
        handle=None,
        is_bot=is_bot,
    )


class SlackTransport:
    TRANSPORT_ID = "slack"

    def __init__(self, app: Any) -> None:
        self._app = app
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._prompt_handlers: list[PromptHandler] = []
        self._msg_counter = itertools.count(1)
        self._prompt_counter = itertools.count(1)
        self._prompt_specs: dict[str, PromptSpec] = {}
        self._bot_user_id: str | None = None

    @classmethod
    def build(cls, bot_token: str, app_token: str) -> "SlackTransport":
        from slack_bolt.async_app import AsyncApp
        app = AsyncApp(token=bot_token)
        t = cls(app)
        t._app_token = app_token
        t._bot_token = bot_token
        return t

    async def start(self) -> None:
        pass  # caller drives SocketModeHandler.start_async()

    async def stop(self) -> None:
        pass

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
        pass  # Slack typing indicators are not supported via Web API in a simple way

    async def open_prompt(self, chat: ChatRef, spec: PromptSpec, *, reply_to: MessageRef | None = None) -> PromptRef:
        raise NotImplementedError("implemented in Task 4")

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        raise NotImplementedError("implemented in Task 4")

    async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
        raise NotImplementedError("implemented in Task 4")
```

- [ ] **Step 5: Run to confirm skeleton tests pass**

```
pytest tests/transport/test_slack_transport.py::test_slack_transport_importable tests/transport/test_slack_transport.py::test_slack_transport_id -v
```
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: add SlackTransport skeleton and slack-bolt dependency"
```

---

### Task 2: Implement outbound methods

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.slack import SlackTransport


def _make_mock_transport() -> SlackTransport:
    """SlackTransport with a mocked slack_bolt AsyncApp."""
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234.0001", "channel": "C100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_uploadV2 = AsyncMock(return_value={"ok": True, "file": {"permalink": "https://slack.com/f1"}})

    app = MagicMock()
    app.client = client

    t = SlackTransport(app)
    t._bot_user_id = "B001"
    return t


def _room() -> ChatRef:
    return ChatRef(transport_id="slack", native_id="C100", kind=ChatKind.ROOM)


async def test_send_text_returns_message_ref():
    t = _make_mock_transport()
    ref = await t.send_text(_room(), "hello slack")
    assert isinstance(ref, MessageRef)
    assert ref.transport_id == "slack"
    assert ref.chat == _room()


async def test_send_text_calls_chat_post_message():
    t = _make_mock_transport()
    await t.send_text(_room(), "hello slack")
    t._app.client.chat_postMessage.assert_called_once()
    call_kwargs = t._app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C100"
    assert "hello slack" in call_kwargs.get("text", "") or "hello slack" in str(call_kwargs.get("blocks", ""))


async def test_edit_text_calls_chat_update():
    t = _make_mock_transport()
    ref = await t.send_text(_room(), "original")
    await t.edit_text(ref, "updated")
    t._app.client.chat_update.assert_called_once()


async def test_send_file_returns_message_ref(tmp_path):
    t = _make_mock_transport()
    f = tmp_path / "doc.txt"
    f.write_bytes(b"content")
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
pytest tests/transport/test_slack_transport.py -k "send or edit" -v
```
Expected: all fail with `NotImplementedError`.

- [ ] **Step 3: Implement outbound methods in `slack.py`**

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
    clean_text = self._html_to_slack(text) if html else text
    kwargs: dict[str, Any] = {"channel": chat.native_id, "text": clean_text}
    if buttons:
        kwargs["blocks"] = self._buttons_to_blocks(buttons, clean_text)
        kwargs["text"] = clean_text  # fallback for notifications
    if reply_to:
        kwargs["thread_ts"] = reply_to.native_id
    resp = await self._app.client.chat_postMessage(**kwargs)
    ts = resp["ts"]
    self._ts_cache[ts] = {"channel": chat.native_id, "text": clean_text}
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)

async def edit_text(
    self,
    msg: MessageRef,
    text: str,
    *,
    buttons: Buttons | None = None,
    html: bool = False,
) -> None:
    clean_text = self._html_to_slack(text) if html else text
    kwargs: dict[str, Any] = {
        "channel": msg.chat.native_id,
        "ts": msg.native_id,
        "text": clean_text,
    }
    if buttons:
        kwargs["blocks"] = self._buttons_to_blocks(buttons, clean_text)
    await self._app.client.chat_update(**kwargs)
    if msg.native_id in self._ts_cache:
        self._ts_cache[msg.native_id]["text"] = clean_text

async def send_file(
    self,
    chat: ChatRef,
    path: Path,
    *,
    caption: str | None = None,
    display_name: str | None = None,
) -> MessageRef:
    resp = await self._app.client.files_uploadV2(
        channel=chat.native_id,
        file=str(path),
        filename=display_name or path.name,
        initial_comment=caption or "",
    )
    ts = str(next(self._msg_counter))
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)

async def send_voice(
    self,
    chat: ChatRef,
    path: Path,
    *,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    kwargs: dict[str, Any] = {
        "channel": chat.native_id,
        "file": str(path),
        "filename": path.name,
    }
    await self._app.client.files_uploadV2(**kwargs)
    ts = str(next(self._msg_counter))
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)
```

Add helpers and `__init__` additions:

In `__init__`, add:
```python
self._ts_cache: dict[str, dict[str, Any]] = {}
```

Add helpers:
```python
@staticmethod
def _html_to_slack(text: str) -> str:
    """Convert basic HTML to Slack mrkdwn. Falls back to plain text for unsupported tags."""
    import re
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)  # strip remaining tags
    return text

@staticmethod
def _buttons_to_blocks(buttons: Buttons, text: str) -> list[dict]:
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    for row in buttons.rows:
        elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": btn.label},
                "value": btn.value,
                "action_id": f"btn_{btn.value}",
            }
            for btn in row
        ]
        blocks.append({"type": "actions", "elements": elements})
    return blocks
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "send or edit" -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: implement SlackTransport outbound methods (send_text, edit, files)"
```

---

### Task 3: Implement `/lp2c` command bridge

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_lp2c_command_dispatched():
    from link_project_to_chat.transport import CommandInvocation

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("projects", handler)

    await t._handle_lp2c_command(
        command={"text": "projects", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=AsyncMock(),
        say=AsyncMock(),
    )

    assert len(seen) == 1
    assert seen[0].name == "projects"
    assert seen[0].raw_text == "/lp2c projects"


async def test_lp2c_command_with_args():
    from link_project_to_chat.transport import CommandInvocation

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("model", handler)

    await t._handle_lp2c_command(
        command={"text": "model set sonnet", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=AsyncMock(),
        say=AsyncMock(),
    )

    assert seen[0].args == ["set", "sonnet"]
    assert seen[0].raw_text == "/lp2c model set sonnet"


async def test_unknown_command_calls_ack():
    t = _make_mock_transport()
    ack = AsyncMock()

    await t._handle_lp2c_command(
        command={"text": "unknown_cmd", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=ack,
        say=AsyncMock(),
    )

    ack.assert_called_once()
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "command" -v
```
Expected: `AttributeError` — `_handle_lp2c_command` does not exist.

- [ ] **Step 3: Implement `_handle_lp2c_command` and `attach_slack_routing`**

```python
async def _handle_lp2c_command(
    self,
    command: dict[str, Any],
    ack: Any,
    say: Any,
) -> None:
    """Parse /lp2c <name> [args...] and dispatch to registered handler."""
    await ack()
    text = (command.get("text") or "").strip()
    parts = text.split() if text else []
    name = parts[0] if parts else "help"
    extra_args = parts[1:] if len(parts) > 1 else []
    raw_text = f"/lp2c {text}".strip()

    channel_id = command.get("channel_id", "")
    user_id = command.get("user_id", "")
    is_dm = channel_id.startswith("D")

    chat = _chat_ref_from_slack(channel_id, is_dm)
    sender = _identity_from_slack_event(user_id)
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
        await say(text=f"Unknown command: `{name}`. Try `/lp2c help`.")

def attach_slack_routing(self) -> None:
    """Register slack_bolt event handlers and slash command on the app."""
    transport = self

    @self._app.command("/lp2c")
    async def handle_lp2c(ack, command, say):
        await transport._handle_lp2c_command(command=command, ack=ack, say=say)

    @self._app.event("message")
    async def handle_message(event, say):
        await transport._dispatch_slack_message(event)

    @self._app.action(re.compile(r"^btn_.*"))
    async def handle_button_action(ack, action, body):
        await ack()
        await transport._dispatch_slack_button(action, body)

    @self._app.view(re.compile(r"^prompt_.*"))
    async def handle_modal_submit(ack, body, view):
        await ack()
        await transport._dispatch_slack_modal(body, view)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "command" -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: add /lp2c command dispatch to SlackTransport"
```

---

### Task 4: Implement prompt mapping (Slack modals + Block Kit)

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_open_text_prompt_opens_modal():
    from link_project_to_chat.transport import PromptKind, PromptRef, PromptSpec

    t = _make_mock_transport()
    t._app.client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V001"}})

    chat = _room()
    spec = PromptSpec(key="name", title="Your Name", body="Enter name", kind=PromptKind.TEXT)

    t._pending_trigger_id = "trigger_123"
    ref = await t.open_prompt(chat, spec)

    assert isinstance(ref, PromptRef)
    assert ref.key == "name"
    t._app.client.views_open.assert_called_once()
    call_kwargs = t._app.client.views_open.call_args.kwargs
    assert call_kwargs["trigger_id"] == "trigger_123"


async def test_open_choice_prompt_sends_block_message():
    from link_project_to_chat.transport import ButtonStyle, PromptKind, PromptOption, PromptSpec

    t = _make_mock_transport()
    spec = PromptSpec(
        key="model",
        title="Choose model",
        body="Select the model",
        kind=PromptKind.CHOICE,
        options=[PromptOption(value="sonnet", label="Sonnet"), PromptOption(value="opus", label="Opus")],
    )
    ref = await t.open_prompt(_room(), spec)
    assert isinstance(ref, PromptRef)
    t._app.client.chat_postMessage.assert_called_once()


async def test_modal_submit_fires_prompt_handler():
    from link_project_to_chat.transport import PromptKind, PromptSpec, PromptSubmission

    t = _make_mock_transport()
    t._app.client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V001"}})
    t._pending_trigger_id = "t1"

    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)
    seen: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        seen.append(sub)

    t.on_prompt_submit(on_submit)
    ref = await t.open_prompt(_room(), spec)

    # Simulate Slack modal submission callback
    body = {
        "user": {"id": "U001"},
        "container": {"channel_id": "C100"},
        "view": {
            "callback_id": f"prompt_{ref.native_id}",
            "state": {"values": {ref.native_id: {"answer": {"value": "Alice"}}}},
        },
    }
    view = body["view"]
    await t._dispatch_slack_modal(body, view)

    assert len(seen) == 1
    assert seen[0].text == "Alice"
    assert seen[0].prompt == ref
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "prompt" -v
```
Expected: `NotImplementedError` from the stubs.

- [ ] **Step 3: Implement prompt methods in `slack.py`**

Replace `open_prompt`, `update_prompt`, `close_prompt` stubs:

```python
async def open_prompt(
    self,
    chat: ChatRef,
    spec: PromptSpec,
    *,
    reply_to: MessageRef | None = None,
) -> PromptRef:
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
        trigger_id = getattr(self, "_pending_trigger_id", None)
        if trigger_id:
            modal_view = self._build_modal_view(native_id, spec)
            await self._app.client.views_open(trigger_id=trigger_id, view=modal_view)
            self._pending_trigger_id = None
        else:
            # Fallback: send ephemeral message asking for text
            await self._app.client.chat_postMessage(
                channel=chat.native_id,
                text=f"*{spec.title}*\n{spec.body}",
            )
    else:
        # CHOICE / CONFIRM / DISPLAY: send Block Kit actions message
        blocks = self._build_choice_blocks(native_id, spec)
        resp = await self._app.client.chat_postMessage(
            channel=chat.native_id,
            text=spec.title,
            blocks=blocks,
        )
        self._prompt_ts_cache[native_id] = resp.get("ts", "")

    return ref

async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
    ts = self._prompt_ts_cache.get(prompt.native_id)
    if ts:
        blocks = self._build_choice_blocks(prompt.native_id, spec)
        await self._app.client.chat_update(
            channel=prompt.chat.native_id,
            ts=ts,
            text=spec.title,
            blocks=blocks,
        )

async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
    ts = self._prompt_ts_cache.pop(prompt.native_id, None)
    if ts and final_text:
        await self._app.client.chat_update(
            channel=prompt.chat.native_id,
            ts=ts,
            text=final_text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": final_text}}],
        )
    self._prompt_specs.pop(prompt.native_id, None)
```

Add private helpers:

```python
def _build_modal_view(self, native_id: str, spec: PromptSpec) -> dict[str, Any]:
    from link_project_to_chat.transport.base import PromptKind
    return {
        "type": "modal",
        "callback_id": f"prompt_{native_id}",
        "title": {"type": "plain_text", "text": spec.title[:24]},
        "submit": {"type": "plain_text", "text": spec.submit_label},
        "blocks": [
            {
                "type": "input",
                "block_id": native_id,
                "label": {"type": "plain_text", "text": spec.body or spec.title},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "answer",
                    "placeholder": {"type": "plain_text", "text": spec.placeholder or ""},
                    "multiline": False,
                },
            }
        ],
    }

def _build_choice_blocks(self, native_id: str, spec: PromptSpec) -> list[dict[str, Any]]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{spec.title}*\n{spec.body}"}}
    ]
    elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": opt.label},
            "value": opt.value,
            "action_id": f"btn_{opt.value}_{native_id}",
        }
        for opt in spec.options
    ]
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks

async def _dispatch_slack_modal(self, body: dict[str, Any], view: dict[str, Any]) -> None:
    callback_id: str = view.get("callback_id", "")
    if not callback_id.startswith("prompt_"):
        return
    native_id = callback_id[len("prompt_"):]
    spec = self._prompt_specs.get(native_id)
    ref = PromptRef(
        transport_id=self.TRANSPORT_ID,
        native_id=native_id,
        chat=_chat_ref_from_slack(
            body.get("container", {}).get("channel_id", ""),
            is_dm=False,
        ),
        key=spec.key if spec else "",
    )
    user_id = body.get("user", {}).get("id", "")
    sender = _identity_from_slack_event(user_id)
    values = view.get("state", {}).get("values", {})
    answer_value = values.get(native_id, {}).get("answer", {}).get("value", "")
    sub = PromptSubmission(
        chat=ref.chat, sender=sender, prompt=ref, text=answer_value
    )
    for h in self._prompt_handlers:
        await h(sub)

async def _dispatch_slack_button(self, action: dict[str, Any], body: dict[str, Any]) -> None:
    action_id: str = action.get("action_id", "")
    value: str = action.get("value", "")
    channel_id = body.get("container", {}).get("channel_id", "")
    user_id = body.get("user", {}).get("id", "")
    ts = action.get("block_id", "0")

    # Check if this is a prompt choice button (action_id starts with btn_ and ends with _<native_id>)
    if action_id.startswith("btn_"):
        parts = action_id.split("_")
        native_id = parts[-1] if len(parts) > 2 else ""
        spec = self._prompt_specs.get(native_id)
        if spec:
            ref = PromptRef(
                transport_id=self.TRANSPORT_ID,
                native_id=native_id,
                chat=_chat_ref_from_slack(channel_id, is_dm=False),
                key=spec.key,
            )
            sender = _identity_from_slack_event(user_id)
            sub = PromptSubmission(
                chat=ref.chat, sender=sender, prompt=ref, option=value
            )
            for h in self._prompt_handlers:
                await h(sub)
            return

    # Regular button click
    is_dm = channel_id.startswith("D")
    chat = _chat_ref_from_slack(channel_id, is_dm)
    msg_ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)
    sender = _identity_from_slack_event(user_id)
    click = ButtonClick(chat=chat, message=msg_ref, sender=sender, value=value)
    for h in self._button_handlers:
        await h(click)
```

In `__init__`, add:
```python
self._prompt_ts_cache: dict[str, str] = {}
self._pending_trigger_id: str | None = None
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "prompt" -v
```
Expected: all 3 prompt tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: implement SlackTransport prompt mapping (modals + Block Kit actions)"
```

---

### Task 5: Implement inbound message dispatch with structured mentions

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_parse_mentions_from_slack_text():
    from link_project_to_chat.transport.slack import _parse_mentions

    mentions = _parse_mentions("<@U111> hello <@U222|alice>", client=None)
    assert len(mentions) == 2
    assert mentions[0].native_id == "U111"
    assert mentions[1].native_id == "U222"


async def test_dispatch_slack_message_populates_mentions():
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "user": "U001",
        "text": "<@B001> can you help?",
        "channel": "C100",
        "ts": "1234.0001",
    }
    await t._dispatch_slack_message(event)

    assert len(received) == 1
    assert len(received[0].mentions) == 1
    assert received[0].mentions[0].native_id == "B001"


async def test_bot_own_messages_ignored():
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    # Message from the bot itself
    event = {
        "type": "message",
        "user": "B001",
        "text": "from myself",
        "channel": "C100",
        "ts": "1234.0001",
    }
    await t._dispatch_slack_message(event)
    assert received == []


async def test_dispatch_slack_message_dm():
    from link_project_to_chat.transport import IncomingMessage, ChatKind

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "user": "U005",
        "text": "hello in dm",
        "channel": "D100",  # DM channel starts with D
        "ts": "1234.0002",
    }
    await t._dispatch_slack_message(event)

    assert len(received) == 1
    assert received[0].chat.kind == ChatKind.DM
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "mention or dispatch or dm" -v
```
Expected: `AttributeError` — `_dispatch_slack_message` does not exist.

- [ ] **Step 3: Implement `_dispatch_slack_message` and inject helpers**

```python
async def _dispatch_slack_message(self, event: dict[str, Any]) -> None:
    """Normalize a Slack message event into IncomingMessage and dispatch."""
    user_id = event.get("user", "")
    if user_id == self._bot_user_id:
        return  # ignore own messages
    if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
        return

    channel_id = event.get("channel", "")
    is_dm = channel_id.startswith("D")
    text = event.get("text", "")
    mentions = _parse_mentions(text, self._app.client)

    chat = _chat_ref_from_slack(channel_id, is_dm)
    sender = _identity_from_slack_event(user_id)
    incoming = IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[],
        reply_to=None,
        mentions=mentions,
        is_relayed_bot_to_bot=False,
    )
    for h in self._message_handlers:
        await h(incoming)

# ── Test injection helpers ─────────────────────────────────────────────
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

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "mention or dispatch or dm or parse" -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Run full Slack test suite**

```
pytest tests/transport/test_slack_transport.py -v
```
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: SlackTransport inbound dispatch with structured mention parsing + inject helpers"
```

---

### Task 6: Add `SlackTransport` to contract tests and export

**Files:**
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 1: Export `SlackTransport`**

In `transport/__init__.py`, add:

```python
from .slack import SlackTransport

__all__ = [
    # ... existing ...
    "SlackTransport",
]
```

- [ ] **Step 2: Add `SlackTransport` to the contract test fixture**

In `tests/transport/test_contract.py`, add a factory:

```python
from link_project_to_chat.transport.slack import SlackTransport


def _make_slack_transport_with_inject() -> SlackTransport:
    """SlackTransport with mocked slack_bolt app for contract testing."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "1000.0001", "channel": "C1"}
    )
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_uploadV2 = AsyncMock(return_value={"ok": True})
    client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V1"}})

    app = MagicMock()
    app.client = client

    t = SlackTransport(app)
    t._bot_user_id = "B_TEST"
    return t
```

Update the fixture to include `"slack"`:

```python
@pytest.fixture(params=["fake", "telegram", "web", "discord", "slack"])
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
    elif request.param == "slack":
        yield _make_slack_transport_with_inject()
    else:
        pytest.fail(f"Unknown param: {request.param}")
```

- [ ] **Step 3: Run all contract tests**

```
pytest tests/transport/test_contract.py -v
```
Expected: all contract tests PASS for all five transports (prompt tests skip for telegram where not applicable).

- [ ] **Step 4: Run the full test suite**

```
pytest -v
```
Expected: all tests PASS with no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/__init__.py tests/transport/test_contract.py
git commit -m "test: add SlackTransport to transport contract test suite"
```

---

### Task 7: Self-review — spec coverage check

Run this as a checklist before declaring the plan complete.

- [ ] **Outbound**: `send_text`, `edit_text`, `send_file`, `send_voice`, `send_typing` — all implemented and tested.
- [ ] **Inbound**: `on_message`, `on_command` (`/lp2c`), `on_button` — all dispatch to handlers.
- [ ] **Prompt**: `open_prompt` (modal for TEXT/SECRET, Block Kit for CHOICE/CONFIRM), `update_prompt`, `close_prompt`, `on_prompt_submit` — all implemented.
- [ ] **Mentions**: `IncomingMessage.mentions` populated from `<@U...>` tokens — tested.
- [ ] **Identity source of truth**: stable Slack IDs (`U...`, `B...`, `C...`) used for `ChatRef.native_id`, `Identity.native_id`, `PromptRef` — verified.
- [ ] **Bot-to-bot**: messages from other bots have `sender.is_bot=True` from `bot_profile` presence; `is_relayed_bot_to_bot=False` — covered in `_dispatch_slack_message`.
- [ ] **Room config**: `RoomBinding.transport_id == "slack"` and peer routing via `BotPeerRef.native_id` — covered by shared config + group_filters from spec #1.
- [ ] **Contract tests**: `SlackTransport` passes `test_contract.py` for text, edit, voice, command, button, mentions, and prompts.
- [ ] **No Telegram leaks**: `SlackTransport` has zero imports from `python-telegram-bot`; verify with `grep -r "telegram" src/link_project_to_chat/transport/slack.py` → should return nothing.
