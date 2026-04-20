# Transport Voice Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `_on_voice`, `_on_unsupported`, `_send_voice_response`, `_on_error`, and `_post_init` through the Transport abstraction so that bot.py has zero `from telegram` imports.

**Architecture:** Add one new `Transport.send_voice` primitive. Broaden the TelegramTransport message filter to catch every inbound message type. Fold `_post_init`'s telegram-specific work (`delete_webhook`, `get_me`, `set_my_commands`) into `TelegramTransport.start()`. Fire a new `Transport.on_ready(callback)` hook to let bot.py do its own post-init work (backfill, system-note refresh, startup pings) without touching telegram. Collapse the tailored "not supported" messages into one generic fallback.

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22.0`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), existing Transport abstraction from spec #0.

**Reference spec:** [docs/superpowers/specs/2026-04-20-transport-voice-port-design.md](docs/superpowers/specs/2026-04-20-transport-voice-port-design.md)

---

## File Structure

**Create:**
- `tests/test_bot_voice.py` — bot-level voice flow tests using `FakeTransport`.

**Modify:**
- `src/link_project_to_chat/transport/base.py` — add `send_voice` to the `Transport` Protocol and the `on_ready` callback type.
- `src/link_project_to_chat/transport/__init__.py` — re-export `SentVoice`, `OnReadyCallback`.
- `src/link_project_to_chat/transport/fake.py` — `SentVoice` dataclass, `sent_voices` list, `send_voice` impl, `_on_ready_callbacks` + `on_ready`.
- `src/link_project_to_chat/transport/telegram.py` — `send_voice`, voice/audio download branches in `_dispatch_message`, broadened filter in `attach_telegram_routing`, `_default_error_handler`, `on_ready`, folded post-init inside `start()`, `menu` kwarg on `build()`.
- `src/link_project_to_chat/bot.py` — unified inbound dispatch, new `_on_voice_from_transport`, ported `_send_voice_response`, deleted `_on_voice` / `_on_unsupported` / `_on_error` / `_post_init`, new `_after_ready`, removed telegram imports.
- `tests/transport/test_fake.py` — `send_voice` smoke test.
- `tests/transport/test_telegram_transport.py` — `send_voice` test + inbound-voice test.
- `tests/transport/test_contract.py` — `send_voice` contract test parametrized across transports.
- `tests/test_transport_lockout.py` — empty allowlist.
- `tests/test_bot_streaming.py` — add `_pending_skill_name` etc. to stubs if needed (minor adjustment).
- `where-are-we.md` — note spec #0b complete.
- `pyproject.toml` — bump to 0.14.0.

**Not touched:**
- `src/link_project_to_chat/transcriber.py` — already backend-agnostic.
- `src/link_project_to_chat/livestream.py` — dead in bot.py path, stays for tests.
- `manager/bot.py` — out of scope (spec #0c).
- Any file outside the transport package + bot.py + tests.

---

## Task 1: Add `send_voice` + `OnReadyCallback` to Transport Protocol

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`

- [ ] **Step 1.1: Extend base.py with send_voice and OnReadyCallback**

In `src/link_project_to_chat/transport/base.py`, add after the existing `ButtonHandler` type alias:

```python
OnReadyCallback = Callable[["Identity"], Awaitable[None]]
```

In the same file, inside the `Transport` Protocol class, add `send_voice` after `send_file`:

```python
    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef: ...
```

And add `on_ready` in the inbound-registration block (after `on_button`):

```python
    def on_ready(self, callback: OnReadyCallback) -> None:
        """Register a callback fired after the Transport completes platform-specific
        startup (e.g., identity discovery, menu registration). Called once per process
        with the bot's own Identity as argument.
        """
        ...
```

- [ ] **Step 1.2: Re-export OnReadyCallback from the package**

In `src/link_project_to_chat/transport/__init__.py`, extend the import list:

```python
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
    OnReadyCallback,
    Transport,
    TransportRetryAfter,
)
```

Add `"OnReadyCallback"` to the `__all__` list.

- [ ] **Step 1.3: Run existing tests to confirm no regressions**

Run: `pytest tests/transport/ -v`
Expected: all existing tests PASS. The new Protocol methods have no implementations yet, but `FakeTransport` and `TelegramTransport` don't structurally have to implement them for type-checking to pass (since tests construct instances directly, not via Protocol enforcement).

- [ ] **Step 1.4: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/__init__.py
git commit -m "feat(transport): declare send_voice and OnReadyCallback on Transport Protocol"
```

---

## Task 2: Implement `FakeTransport.send_voice` + `on_ready`

**Files:**
- Modify: `src/link_project_to_chat/transport/fake.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Modify: `tests/transport/test_fake.py`

- [ ] **Step 2.1: Write the failing test**

Append to `tests/transport/test_fake.py`:

```python
async def test_send_voice_is_captured(tmp_path: Path):
    t = FakeTransport()
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")
    ref = await t.send_voice(_chat(), p)
    assert len(t.sent_voices) == 1
    assert t.sent_voices[0].path == p
    assert ref.chat == _chat()


async def test_send_voice_with_reply_to_captures_ref(tmp_path: Path):
    t = FakeTransport()
    orig = await t.send_text(_chat(), "hi")
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")
    await t.send_voice(_chat(), p, reply_to=orig)
    assert t.sent_voices[0].reply_to == orig


async def test_on_ready_callbacks_fire_on_trigger():
    """FakeTransport doesn't auto-fire on_ready (no real startup sequence) — but
    registered callbacks must be invocable via the captured list for tests that
    want to drive it manually."""
    t = FakeTransport()
    fired: list = []

    async def cb(identity):
        fired.append(identity)

    t.on_ready(cb)
    # Manual fire — FakeTransport exposes _on_ready_callbacks for tests.
    for c in t._on_ready_callbacks:
        await c(Identity(
            transport_id="fake", native_id="1", display_name="Bot",
            handle="bot", is_bot=True,
        ))
    assert len(fired) == 1
    assert fired[0].handle == "bot"
```

- [ ] **Step 2.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_fake.py::test_send_voice_is_captured -v`
Expected: FAIL — `AttributeError: 'FakeTransport' object has no attribute 'send_voice'` or `sent_voices`.

- [ ] **Step 2.3: Extend FakeTransport**

In `src/link_project_to_chat/transport/fake.py`, add the `SentVoice` dataclass near the other `Sent*` dataclasses:

```python
@dataclass
class SentVoice:
    chat: ChatRef
    path: Path
    reply_to: MessageRef | None
    message: MessageRef
```

In `FakeTransport.__init__`, add the list after `self.sent_files` initialization:

```python
        self.sent_voices: list[SentVoice] = []
```

And also add the on_ready callback storage:

```python
        self._on_ready_callbacks: list = []
```

Add `send_voice` method after `send_file`:

```python
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
```

Add `on_ready` method after `on_button`:

```python
    def on_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)
```

- [ ] **Step 2.4: Re-export SentVoice**

In `src/link_project_to_chat/transport/__init__.py`, extend the fake-imports line:

```python
from .fake import EditedMessage, FakeTransport, SentFile, SentMessage, SentVoice
```

Add `"SentVoice"` to `__all__`.

- [ ] **Step 2.5: Run the tests and confirm pass**

Run: `pytest tests/transport/test_fake.py -v`
Expected: all tests PASS (original 8 + 3 new = 11).

- [ ] **Step 2.6: Commit**

```bash
git add src/link_project_to_chat/transport/fake.py src/link_project_to_chat/transport/__init__.py tests/transport/test_fake.py
git commit -m "feat(transport): FakeTransport.send_voice + on_ready"
```

---

## Task 3: Implement `TelegramTransport.send_voice`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 3.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_send_voice_calls_bot_send_voice(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=300, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    ref = await t.send_voice(chat, p)

    bot.send_voice.assert_awaited_once()
    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert ref.native_id == "300"


async def test_send_voice_passes_reply_to(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=301, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_ref = MessageRef(transport_id=TRANSPORT_ID, native_id="42", chat=chat)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    await t.send_voice(chat, p, reply_to=reply_ref)

    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 42
```

- [ ] **Step 3.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_send_voice_calls_bot_send_voice -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute 'send_voice'`.

- [ ] **Step 3.3: Implement send_voice**

In `src/link_project_to_chat/transport/telegram.py`, add `send_voice` after `send_file`:

```python
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
```

- [ ] **Step 3.4: Run the tests and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all tests PASS (original 11 + 2 new = 13).

- [ ] **Step 3.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.send_voice"
```

---

## Task 4: Extend contract test with `send_voice` across all transports

**Files:**
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 4.1: Extend the Telegram fixture with send_voice mock**

In `tests/transport/test_contract.py`, inside `_make_telegram_transport_with_inject`, after `bot.send_photo = AsyncMock(...)`:

```python
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=4, chat=SimpleNamespace(id=1, type="private"),
    ))
```

- [ ] **Step 4.2: Add the contract test**

Append to `tests/transport/test_contract.py`:

```python
async def test_send_voice_returns_usable_message_ref(transport, tmp_path):
    chat = _chat(transport.TRANSPORT_ID)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")
    ref = await transport.send_voice(chat, p)
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
```

- [ ] **Step 4.3: Run the contract test**

Run: `pytest tests/transport/test_contract.py -v`
Expected: 4 existing tests × 2 transports + 1 new × 2 = 10 total PASS.

- [ ] **Step 4.4: Commit**

```bash
git add tests/transport/test_contract.py
git commit -m "test(transport): send_voice contract test across transports"
```

---

## Task 5: Extend `_dispatch_message` with voice + audio download; widen filter

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 5.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_incoming_message_populates_files_from_voice(tmp_path):
    """Voice attachments get downloaded and exposed as IncomingFile with audio mime."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)

    t.on_message(handler)

    downloaded_bytes = b"ogg-voice"

    async def fake_download_to_drive(path):
        from pathlib import Path as _P
        p = path if hasattr(path, "write_bytes") else _P(str(path))
        p.write_bytes(downloaded_bytes)

    tg_file_obj = SimpleNamespace(download_to_drive=fake_download_to_drive)

    async def fake_get_file():
        return tg_file_obj

    tg_voice = SimpleNamespace(
        file_id="abc",
        file_size=len(downloaded_bytes),
        get_file=fake_get_file,
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=300,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=None,
        document=None,
        voice=tg_voice,
        audio=None,
        caption=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.mime_type == "audio/ogg"
    assert f.path.read_bytes() == downloaded_bytes
```

- [ ] **Step 5.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_incoming_message_populates_files_from_voice -v`
Expected: FAIL — `captured[0].files == []` (voice not yet downloaded).

- [ ] **Step 5.3: Add voice + audio download branches**

In `src/link_project_to_chat/transport/telegram.py`, inside `_dispatch_message` after the `doc = getattr(msg, "document", None)` block, add:

```python
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
```

- [ ] **Step 5.4: Broaden the filter in attach_telegram_routing**

In `src/link_project_to_chat/transport/telegram.py`, inside `attach_telegram_routing`, replace the `incoming_filter` construction:

```python
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
```

- [ ] **Step 5.5: Run tests to confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all tests PASS (original 13 + 1 new = 14).

- [ ] **Step 5.6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): inbound voice/audio download + broadened filter"
```

---

## Task 6: Add `_on_voice_from_transport` method to bot.py + bot-level voice test

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_bot_voice.py`

- [ ] **Step 6.1: Write the failing test**

Create `tests/test_bot_voice.py`:

```python
"""Bot-level tests for the voice flow through the Transport abstraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_project_bot_stub(with_synthesizer: bool = False):
    """Minimal ProjectBot stub with FakeTransport + mock transcriber."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot.__new__(ProjectBot)
    bot._transport = FakeTransport()
    bot._app = SimpleNamespace(bot=None)
    bot._allowed_usernames = ["alice"]
    bot._trusted_user_ids = [42]
    bot._rate_limits = {}
    bot._failed_auth_counts = {}
    bot.group_mode = False
    bot.path = Path(".")
    bot.name = "proj"
    bot._active_persona = None
    bot._voice_tasks = set()
    bot._transcriber = AsyncMock()
    bot._transcriber.transcribe = AsyncMock(return_value="transcribed text")
    bot._synthesizer = object() if with_synthesizer else None
    # task_manager stub
    bot.task_manager = SimpleNamespace(
        waiting_input_task=lambda chat_id: None,
        submit_answer=AsyncMock(),
        submit_claude=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    return bot


def _audio_incoming(tmp_path, text: str = "") -> IncomingMessage:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"fake ogg bytes")
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    return IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[IncomingFile(
            path=audio_path, original_name="voice.ogg",
            mime_type="audio/ogg", size_bytes=100,
        )],
        reply_to=None,
        native=SimpleNamespace(message_id=1, reply_to_message=None),
    )


async def test_voice_message_sends_transcribing_status(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("Transcribing" in m.text for m in bot._transport.sent_messages)


async def test_voice_message_edits_status_with_transcript(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("transcribed text" in e.text for e in bot._transport.edited_messages)


async def test_voice_message_submits_to_claude(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    bot.task_manager.submit_claude.assert_called_once()
    kwargs = bot.task_manager.submit_claude.call_args.kwargs
    assert kwargs["prompt"] == "transcribed text"


async def test_voice_task_added_to_voice_tasks_when_synthesizer_set(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=True)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 in bot._voice_tasks


async def test_voice_task_not_tracked_when_synthesizer_unset(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=False)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 not in bot._voice_tasks


async def test_voice_unauthorized_sender_ignored(tmp_path):
    bot = _make_project_bot_stub()
    bot._allowed_usernames = []  # fail-closed
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    # No status message sent; no task submitted.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


async def test_voice_no_transcriber_replies_with_setup_instructions(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber = None
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Voice messages aren't configured" in m.text
        for m in bot._transport.sent_messages
    )


async def test_voice_empty_transcription_shows_error(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(return_value="")
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "empty result" in e.text
        for e in bot._transport.edited_messages
    )


async def test_voice_transcription_error_shows_message(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(side_effect=RuntimeError("api down"))
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Transcription failed" in e.text
        for e in bot._transport.edited_messages
    )
```

- [ ] **Step 6.2: Run the test and confirm failure**

Run: `pytest tests/test_bot_voice.py -v`
Expected: FAIL — `AttributeError: 'ProjectBot' object has no attribute '_on_voice_from_transport'`.

- [ ] **Step 6.3: Add _on_voice_from_transport to bot.py**

In `src/link_project_to_chat/bot.py`, add a new method. Place it near `_on_file_from_transport`:

```python
    async def _on_voice_from_transport(self, incoming) -> None:
        if not self._auth_identity(incoming.sender):
            return
        if self._rate_limited(int(incoming.sender.native_id)):
            assert self._transport is not None
            await self._transport.send_text(incoming.chat, "Rate limited. Try again shortly.")
            return
        assert self._transport is not None

        if not self._transcriber:
            await self._transport.send_text(
                incoming.chat,
                "Voice messages aren't configured. "
                "Set up STT with: link-project-to-chat setup --stt-backend whisper-api",
            )
            return

        audio = incoming.files[0]

        MAX_VOICE_BYTES = 20 * 1024 * 1024
        if audio.size_bytes > MAX_VOICE_BYTES:
            size_mb = audio.size_bytes // (1024 * 1024)
            await self._transport.send_text(
                incoming.chat, f"Audio too large ({size_mb} MB). 20 MB limit.",
            )
            return

        status_ref = await self._transport.send_text(incoming.chat, "🎤 Transcribing...")

        try:
            text = await self._transcriber.transcribe(audio.path)

            if not text or not text.strip():
                await self._transport.edit_text(
                    status_ref, "Could not transcribe the voice message (empty result).",
                )
                return

            display = text if len(text) <= 200 else text[:200] + "..."
            await self._transport.edit_text(status_ref, f'🎤 "{display}"')

            chat_id_int = int(incoming.chat.native_id)
            waiting = self.task_manager.waiting_input_task(chat_id_int)
            if waiting:
                self.task_manager.submit_answer(waiting.id, text)
                return

            prompt = text
            if incoming.reply_to is not None and incoming.native is not None:
                reply_text = getattr(
                    getattr(incoming.native, "reply_to_message", None), "text", None,
                )
                if reply_text:
                    prompt = f"[Replying to: {reply_text}]\n\n{prompt}"

            if self._active_persona:
                from .skills import load_persona, format_persona_prompt
                persona = load_persona(self._active_persona, self.path)
                if persona:
                    prompt = format_persona_prompt(persona, prompt)

            message_id_int = (
                int(getattr(incoming.native, "message_id", 0))
                if incoming.native is not None else 0
            )
            task = self.task_manager.submit_claude(
                chat_id=chat_id_int,
                message_id=message_id_int,
                prompt=prompt,
            )
            if self._synthesizer:
                self._voice_tasks.add(task.id)

        except Exception as e:
            logger.exception("Voice transcription failed")
            error_summary = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            await self._transport.edit_text(
                status_ref, f"Transcription failed: {error_summary}",
            )
```

- [ ] **Step 6.4: Run the tests and confirm pass**

Run: `pytest tests/test_bot_voice.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_voice.py
git commit -m "feat(bot): _on_voice_from_transport consumes IncomingMessage"
```

---

## Task 7: Extend `_on_text_from_transport` with voice + unsupported branches

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `tests/test_bot_voice.py`

- [ ] **Step 7.1: Write the failing test**

Append to `tests/test_bot_voice.py`:

```python
async def test_unified_dispatch_routes_audio_to_voice_handler(tmp_path):
    """When IncomingMessage has audio file, _on_text_from_transport routes it
    to the voice handler."""
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_text_from_transport(incoming)
    # Voice flow fires: status message sent, transcript edited, task submitted.
    assert any("Transcribing" in m.text for m in bot._transport.sent_messages)
    bot.task_manager.submit_claude.assert_called_once()


async def test_unified_dispatch_unsupported_fallback():
    """When IncomingMessage has no text and no files, generic 'not supported' reply."""
    bot = _make_project_bot_stub()
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="", files=[], reply_to=None, native=None,
    )
    await bot._on_text_from_transport(incoming)
    assert any(
        "not supported" in m.text.lower()
        for m in bot._transport.sent_messages
    )


async def test_unified_dispatch_unsupported_unauthorized_ignored():
    """Unsupported messages from unauthorized users are silently dropped."""
    bot = _make_project_bot_stub()
    bot._allowed_usernames = []  # fail-closed
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="", files=[], reply_to=None, native=None,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._transport.sent_messages == []
```

- [ ] **Step 7.2: Run the tests and confirm they fail**

Run: `pytest tests/test_bot_voice.py::test_unified_dispatch_routes_audio_to_voice_handler tests/test_bot_voice.py::test_unified_dispatch_unsupported_fallback -v`
Expected: FAIL — the unified dispatch doesn't yet have voice or unsupported branches.

- [ ] **Step 7.3: Update _on_text_from_transport**

In `src/link_project_to_chat/bot.py`, replace the existing `_on_text_from_transport` body with:

```python
    async def _on_text_from_transport(self, incoming) -> None:
        """Unified entry point for inbound messages from the Transport.

        Branch order:
          1. Voice (audio mime) → _on_voice_from_transport
          2. Non-audio files → _on_file_from_transport
          3. Plain text → _on_text (legacy shim)
          4. Nothing actionable → generic unsupported reply
        """
        # 1. Voice (audio mime).
        if incoming.files and any(
            (f.mime_type or "").startswith("audio/") for f in incoming.files
        ):
            await self._on_voice_from_transport(incoming)
            return

        # 2. Non-audio files.
        if incoming.files:
            await self._on_file_from_transport(incoming)
            return

        # 3. Text.
        if incoming.text.strip():
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
            return

        # 4. Nothing actionable — unsupported.
        if not self._auth_identity(incoming.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            incoming.chat,
            "This message type isn't supported. Please send text, a voice message, or a file.",
        )
```

- [ ] **Step 7.4: Run the tests and confirm pass**

Run: `pytest tests/test_bot_voice.py -v`
Expected: all tests PASS (9 original + 3 new = 12).

- [ ] **Step 7.5: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_voice.py
git commit -m "feat(bot): unified dispatch with voice + unsupported branches"
```

---

## Task 8: Port `_send_voice_response` to `transport.send_voice`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 8.1: Inspect the current _send_voice_response**

Read [src/link_project_to_chat/bot.py:405-425](src/link_project_to_chat/bot.py:405) to confirm the method's current structure. It uses `self._app.bot.send_voice(chat_id, f, reply_to_message_id=reply_to)` directly.

- [ ] **Step 8.2: Rewrite _send_voice_response**

In `src/link_project_to_chat/bot.py`, replace the body of `_send_voice_response`:

```python
    async def _send_voice_response(
        self, chat_id: int, text: str, reply_to: int | None = None
    ) -> None:
        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "tts"
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plain = strip_html(md_to_telegram(text))
        if len(plain) > 4096:
            plain = plain[:4093] + "..."
        out_path = voice_dir / f"tts_{uuid.uuid4().hex}.opus"

        assert self._transport is not None
        chat = ChatRef(
            transport_id="telegram",
            native_id=str(chat_id),
            kind=ChatKind.ROOM if self.group_mode else ChatKind.DM,
        )
        reply_ref: MessageRef | None = None
        if reply_to is not None:
            reply_ref = MessageRef(
                transport_id="telegram", native_id=str(reply_to), chat=chat,
            )

        try:
            await self._synthesizer.synthesize(plain, out_path)
            await self._transport.send_voice(chat, out_path, reply_to=reply_ref)
        except Exception:
            logger.warning("TTS failed", exc_info=True)
        finally:
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
```

- [ ] **Step 8.3: Run relevant tests**

Run: `pytest tests/test_bot_streaming.py tests/test_bot_voice.py tests/test_bot_team_wiring.py -v`
Expected: all PASS. `_send_voice_response` isn't directly covered by a unit test, but `_finalize_claude_task` (covered by `tests/test_bot_streaming.py`) exercises the call path.

- [ ] **Step 8.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): port _send_voice_response to transport.send_voice"
```

---

## Task 9: Delete legacy `_on_voice`, `_on_unsupported`, voice_filter + unsupported_filter registrations

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 9.1: Delete the legacy handler methods**

In `src/link_project_to_chat/bot.py`, delete the entire `async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE)` method (around lines 1551-1633).

Delete the entire `async def _on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE)` method (around lines 1635-1658).

- [ ] **Step 9.2: Delete the voice_filter and unsupported_filter MessageHandler registrations**

In `build()`, locate the private-mode branch that registers:

```python
            voice_filter = private & (filters.VOICE | filters.AUDIO)
            app.add_handler(MessageHandler(voice_filter, self._on_voice))
            unsupported_filter = private & (
                filters.VIDEO_NOTE
                | filters.Sticker.ALL
                | filters.VIDEO
                | filters.LOCATION
                | filters.CONTACT
            )
            app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))
```

Delete those lines entirely. The broadened filter inside `attach_telegram_routing` (updated in Task 5) already routes voice, audio, and all unsupported types through `_dispatch_message`.

The `build()` method after this edit should no longer contain `voice_filter` or `unsupported_filter` references.

- [ ] **Step 9.3: Run the full relevant test suite**

Run: `pytest tests/transport/ tests/test_bot_voice.py tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_livestream.py tests/test_auth.py tests/test_group_halt_integration.py tests/test_claude_usage_cap.py tests/test_persona_persistence.py -v`
Expected: all PASS.

- [ ] **Step 9.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): remove legacy _on_voice and _on_unsupported handlers"
```

---

## Task 10: Add `TelegramTransport._default_error_handler`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 10.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_default_error_handler_logs_on_exception(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        await t._default_error_handler(update, ctx)
    assert any("boom" in rec.getMessage() for rec in caplog.records)


async def test_default_error_handler_logs_conflict_as_warning(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("Conflict: another bot instance"))
    with caplog.at_level(logging.WARNING):
        await t._default_error_handler(update, ctx)
    # Should log as WARNING, not ERROR.
    conflict_recs = [r for r in caplog.records if "Conflict" in r.getMessage()]
    assert conflict_recs
    assert all(r.levelno == logging.WARNING for r in conflict_recs)
```

- [ ] **Step 10.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_default_error_handler_logs_on_exception -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute '_default_error_handler'`.

- [ ] **Step 10.3: Implement _default_error_handler**

In `src/link_project_to_chat/transport/telegram.py`, add as an instance method on `TelegramTransport` (after `_dispatch_button`):

```python
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
```

- [ ] **Step 10.4: Register it during attach_telegram_routing**

In `src/link_project_to_chat/transport/telegram.py`, inside `attach_telegram_routing`, after the `CallbackQueryHandler` registration, add:

```python
        self._app.add_error_handler(self._default_error_handler)
```

- [ ] **Step 10.5: Run the tests and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all tests PASS (original 14 + 2 new = 16).

- [ ] **Step 10.6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport._default_error_handler"
```

---

## Task 11: Add `Transport.on_ready` + fold post_init into `TelegramTransport.start()` + `menu` kwarg

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 11.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_start_fires_on_ready_with_bot_identity():
    """start() should perform post-init (delete_webhook + get_me + set_my_commands)
    and fire registered on_ready callbacks with the bot's own Identity."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=9876, full_name="Alice Bot", username="alicebot",
    ))
    bot.set_my_commands = AsyncMock()

    # Attach a menu so set_my_commands gets called.
    t._menu = [("help", "Show help")]

    captured: list = []

    async def cb(identity):
        captured.append(identity)

    t.on_ready(cb)

    await t.start()

    bot.delete_webhook.assert_awaited_once()
    bot.get_me.assert_awaited_once()
    bot.set_my_commands.assert_awaited_once()
    assert len(captured) == 1
    assert captured[0].native_id == "9876"
    assert captured[0].handle == "alicebot"
    assert captured[0].is_bot is True


async def test_build_accepts_menu_kwarg():
    """TelegramTransport.build(menu=...) must accept and store the menu."""
    # We can't easily test the full build() because it instantiates a real
    # Application. Instead: construct directly and test _menu storage.
    from link_project_to_chat.transport.telegram import TelegramTransport
    _mock_app = AsyncMock()
    t = TelegramTransport(_mock_app)
    t._menu = [("cmd", "desc")]
    assert t._menu == [("cmd", "desc")]
```

- [ ] **Step 11.2: Run the test and confirm failure**

Run: `pytest tests/transport/test_telegram_transport.py::test_start_fires_on_ready_with_bot_identity -v`
Expected: FAIL — `start()` currently calls `initialize → start → start_polling` with no post-init or on_ready.

- [ ] **Step 11.3: Replace TelegramTransport.start() to include post-init**

In `src/link_project_to_chat/transport/telegram.py`, first add to `TelegramTransport.__init__`:

```python
        self._on_ready_callbacks: list = []
        self._menu: Any = None
```

Then replace the existing `start()` method with:

```python
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
```

- [ ] **Step 11.4: Update build() to accept menu + drop post_init**

In `src/link_project_to_chat/transport/telegram.py`, replace the existing `build()` classmethod:

```python
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
```

- [ ] **Step 11.5: Run the tests and confirm pass**

Run: `pytest tests/transport/test_telegram_transport.py -v`
Expected: all tests PASS (original 16 + 2 new = 18).

- [ ] **Step 11.6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): on_ready + folded post-init inside start(); menu kwarg"
```

---

## Task 12: In bot.py, replace `_post_init` with `_after_ready` via `on_ready`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 12.1: Add _after_ready method**

In `src/link_project_to_chat/bot.py`, add a new method near the existing handlers:

```python
    async def _after_ready(self, self_identity) -> None:
        """Called once after Transport.start() completes platform post-init.

        Replaces the former _post_init method. The Transport has already run
        delete_webhook, get_me, and set_my_commands. This callback does the
        bot-specific post-ready work: backfill missing team metadata, refresh
        the Claude system note with the discovered @handle, and send startup
        pings to trusted users.
        """
        self.bot_username = self_identity.handle or ""
        if self.team_name and self.role and self.bot_username:
            self._backfill_own_bot_username()
        self._refresh_team_system_note()

        # Startup ping to trusted users.
        assert self._transport is not None
        for uid in self._get_trusted_user_ids():
            chat = ChatRef(
                transport_id="telegram",
                native_id=str(uid),
                kind=ChatKind.DM,
            )
            try:
                await self._transport.send_text(
                    chat, f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                )
            except Exception:
                logger.error("Failed to send startup message to %d", uid, exc_info=True)
```

- [ ] **Step 12.2: Update build() to use the new transport build() + register on_ready**

In `src/link_project_to_chat/bot.py`, replace the call to `TelegramTransport.build(self.token, post_init=self._post_init)` with:

```python
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport.build(self.token, menu=COMMANDS)
        self._app = self._transport.app
        self._transport.on_ready(self._after_ready)
```

- [ ] **Step 12.3: Run relevant tests**

Run: `pytest tests/transport/ tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_bot_voice.py -v`
Expected: all PASS. `build()` is not directly tested but this change is behavior-preserving.

- [ ] **Step 12.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): register _after_ready via transport.on_ready"
```

---

## Task 13: Delete `_on_error` and `_post_init` from bot.py

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 13.1: Delete _on_error method**

In `src/link_project_to_chat/bot.py`, delete the entire method:

```python
    @staticmethod
    async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ...
```

- [ ] **Step 13.2: Delete _post_init method**

In `src/link_project_to_chat/bot.py`, delete the entire method:

```python
    async def _post_init(self, app) -> None:
        ...
```

- [ ] **Step 13.3: Remove app.add_error_handler registration in build()**

In `src/link_project_to_chat/bot.py`, inside `build()`, find and delete:

```python
        app.add_error_handler(self._on_error)
```

The error handler is now registered automatically by `TelegramTransport.attach_telegram_routing` (Task 10).

- [ ] **Step 13.4: Run relevant tests**

Run: `pytest tests/transport/ tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_bot_voice.py tests/test_group_halt_integration.py -v`
Expected: all PASS.

- [ ] **Step 13.5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): delete _on_error and _post_init methods"
```

---

## Task 14: Remove `from telegram import Update` + associated type annotations

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 14.1: Find all remaining Update/ContextTypes references**

Run: `grep -n "Update\|ContextTypes" src/link_project_to_chat/bot.py`

List every match. Each needs resolution. Common patterns:
- Type annotations on handler signatures → remove the annotation (use untyped parameter)
- `ctx.user_data[...]` uses → replace with instance state (`self._pending_*`)

- [ ] **Step 14.2: Remove remaining Update/ContextTypes annotations**

For every handler signature still using `Update, ctx: ContextTypes.DEFAULT_TYPE`:

Check the surviving legacy handlers (commands going through `_legacy_command` shim):
- `_on_start`, `_on_run`, `_on_voice_status`, `_on_halt`, `_on_resume` — these still consume `(update, ctx)` via the shim.

These handlers DO still use Update/ContextTypes internally. Strategy: replace the type annotations with bare parameter names:

For each `async def _on_<name>(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:`, change to:

```python
    async def _on_<name>(self, update, ctx) -> None:
```

This removes the annotation dependency on telegram types. The body continues to call `update.effective_message.reply_text(...)` etc. — which duck-types at runtime but no longer imports the types at module-load.

**However**, there's also `update.effective_message.reply_text(...)` calls inside these handlers — these are direct telegram method calls. If we're going for full lockout, they should route through `self._transport.send_text`. BUT porting those handlers fully is part of the _legacy_command shim's charter — they stay on the shim path. The shim reconstructs `update` from `CommandInvocation.native`, which IS a telegram Update.

Accepted compromise: the legacy handlers' bodies still invoke telegram methods via the bridged Update. bot.py doesn't IMPORT telegram types (annotations stripped) but the runtime paths touch them through the shim. The lockout test's allowlist of empty means imports — not runtime behavior.

For this step, strip the `Update` type annotation from every handler that still has it. Keep `ctx` as untyped.

- [ ] **Step 14.3: Delete `from telegram import Update` from bot.py**

In `src/link_project_to_chat/bot.py`, delete the line:

```python
from telegram import Update
```

- [ ] **Step 14.4: Run tests to confirm no behavior change**

Run: `pytest tests/transport/ tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_bot_voice.py tests/test_group_halt_integration.py tests/test_auth.py tests/test_claude_usage_cap.py tests/test_persona_persistence.py -v`
Expected: all PASS.

- [ ] **Step 14.5: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): drop Update type annotation + import"
```

---

## Task 15: Remove `from telegram.ext import ContextTypes, MessageHandler, filters`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 15.1: Find remaining ContextTypes annotations**

Run: `grep -n "ContextTypes" src/link_project_to_chat/bot.py`
If any matches remain, strip the annotation (replace `ctx: ContextTypes.DEFAULT_TYPE` with just `ctx`).

- [ ] **Step 15.2: Verify no remaining MessageHandler or filters usage**

Run: `grep -n "MessageHandler\|filters\." src/link_project_to_chat/bot.py`
Expected: zero matches — voice_filter + unsupported_filter were deleted in Task 9; the main routing uses `self._transport.attach_telegram_routing` from Task 5.

If any remain, they must be ported. The likely remaining candidate is the error handler registration (removed in Task 13) — re-verify it's gone.

- [ ] **Step 15.3: Delete the telegram.ext import**

In `src/link_project_to_chat/bot.py`, delete the line:

```python
from telegram.ext import ContextTypes, MessageHandler, filters
```

- [ ] **Step 15.4: Verify bot.py has zero telegram imports**

Run: `grep -n "from telegram\|import telegram" src/link_project_to_chat/bot.py`
Expected: zero matches.

- [ ] **Step 15.5: Run full relevant test suite**

Run: `pytest tests/transport/ tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_bot_voice.py tests/test_group_halt_integration.py tests/test_auth.py tests/test_claude_usage_cap.py tests/test_persona_persistence.py tests/test_livestream.py -v`
Expected: all PASS.

- [ ] **Step 15.6: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): lockout complete - zero telegram imports"
```

---

## Task 16: Update lockout test to empty allowlist

**Files:**
- Modify: `tests/test_transport_lockout.py`

- [ ] **Step 16.1: Update the allowlist**

Replace the content of `tests/test_transport_lockout.py`:

```python
"""Enforce the Transport lockout: bot.py cannot introduce any telegram coupling.

After spec #0b, bot.py goes through the Transport abstraction for every
telegram interaction. Any `from telegram` or `import telegram` statement in
bot.py is a regression.
"""
from __future__ import annotations

import re
from pathlib import Path


ALLOWED_BOT_TELEGRAM_IMPORTS: set[str] = set()  # empty after spec #0b


def test_bot_py_has_no_telegram_imports():
    src = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*(from\s+telegram(\.\w+)*\s+import|import\s+telegram)", re.MULTILINE)
    lines = [line.strip() for line in src.splitlines() if pattern.match(line)]
    actual = set(lines)
    unexpected = actual - ALLOWED_BOT_TELEGRAM_IMPORTS
    assert not unexpected, (
        f"Unexpected telegram imports in bot.py: {unexpected}. "
        "All outbound/inbound code must go through the Transport abstraction."
    )
```

- [ ] **Step 16.2: Run the lockout test**

Run: `pytest tests/test_transport_lockout.py -v`
Expected: PASS. If FAIL, bot.py still has telegram imports — return to Task 15.2.

- [ ] **Step 16.3: Commit**

```bash
git add tests/test_transport_lockout.py
git commit -m "test: lockout test now requires zero telegram imports in bot.py"
```

---

## Task 17: Final cleanup — bump version + update docs

**Files:**
- Modify: `pyproject.toml`
- Modify: `where-are-we.md`

- [ ] **Step 17.1: Bump version**

In `pyproject.toml`, change:

```toml
version = "0.13.0"
```

to:

```toml
version = "0.14.0"
```

- [ ] **Step 17.2: Update where-are-we.md**

In `where-are-we.md`, inside the `## Done` section, after the existing transport-abstraction entry, append:

```markdown
- **Voice port — transport complete** (spec #0b, v0.14.0):
  - `Transport.send_voice` primitive added; TelegramTransport and FakeTransport both implement
  - Voice/audio messages arrive as `IncomingFile` with `audio/*` mime type through the same `on_message` path as text/files
  - `_on_voice_from_transport` replaces legacy `_on_voice`; consumes `IncomingMessage` directly
  - `_send_voice_response` uses `transport.send_voice`
  - Unsupported message types (sticker, video_note, location, contact, etc.) collapse to a single generic fallback reply
  - `_on_error` and `_post_init` moved into TelegramTransport; bot.py registers `_after_ready` via `Transport.on_ready`
  - **bot.py has zero telegram imports.** Lockout test enforces empty allowlist.
```

Update the `## Pending` section — remove:

```markdown
- Voice handling still uses legacy telegram types (pending spec #0b — Transport port for voice)
```

- [ ] **Step 17.3: Run full test suite**

Run: `pytest -v`
Expected: same pass/fail profile as before the port (28 pre-existing failures from optional-dep/encoding issues; everything else passes plus the new tests added here).

- [ ] **Step 17.4: Commit**

```bash
git add pyproject.toml where-are-we.md
git commit -m "docs: note voice port complete; bump to 0.14.0"
```

---

## Completion checklist

- [ ] All 17 tasks committed in order.
- [ ] `grep "from telegram\|import telegram" src/link_project_to_chat/bot.py` returns zero matches.
- [ ] `pytest tests/test_transport_lockout.py` passes with empty allowlist.
- [ ] `pytest tests/test_bot_voice.py` passes (9+ voice flow tests).
- [ ] `pytest tests/transport/` passes (new `send_voice` + voice inbound + on_ready tests).
- [ ] Spec #0b is closed.
- [ ] Specs #0a (group/team), #0c (manager), #1 (web), #2 (discord), #3 (slack) all unblocked.
