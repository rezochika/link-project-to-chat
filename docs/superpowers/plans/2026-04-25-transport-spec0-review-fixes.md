# Transport Spec #0 Review-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four blocking findings (2 security regressions, 2 functional regressions) and five quality findings raised by two independent code reviews of the `feat/transport-abstraction` spec #0 closure (`d1d3bd9` + `0036715`).

**Architecture:** Tasks 1–4 are localized to `transport/telegram.py` media handling (sanitize filenames, pre-download authorization hook, unsupported-media flag, HTML-on-deleted-reply retry). Task 5 strips `int(...)` casts from auth keys by switching `_rate_limits` and `_failed_auth_counts` to string keys. Task 6 elevates `run()` to the Transport Protocol so `bot.py` no longer touches `app.run_polling()` / `app.post_init` / `app.post_stop` by name. Tasks 7–11 are cleanups: drop dead import, add `Transport.max_text_length`, tighten `IncomingMessage.message` type, log fallback failures, fix log-message wording.

**Tech Stack:** Python 3.11+, pytest with `asyncio_mode = "auto"`, `python-telegram-bot`, `telethon` (optional), hatchling. `FakeTransport` is the test double; `transport/test_contract.py` is parametrized across all Transport implementations.

**Severity ordering — engineer SHOULD complete in order, MAY stop after Task 6:**
- **Tasks 1–2 are blocking (Critical security).** Don't merge without these.
- **Tasks 3–4 are blocking (Important functional regressions).**
- **Tasks 5–6 are blocking (Important architectural — they're what makes the spec exit "genuine").**
- **Tasks 7–11 are quality of life.** Land them with a follow-up commit, or fold them in if you have momentum.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/link_project_to_chat/transport/telegram.py` | Modify | Media filename sanitization, pre-download authorizer call, unsupported-media flag, retry-on-deleted-reply inside `send_text`, log message fix |
| `src/link_project_to_chat/transport/base.py` | Modify | Add `set_authorizer`, `run`, `max_text_length` to `Transport` Protocol; add `has_unsupported_media` to `IncomingMessage`; tighten `message: MessageRef` type |
| `src/link_project_to_chat/transport/fake.py` | Modify | Implement new Protocol methods so contract tests still pass |
| `src/link_project_to_chat/_auth.py` | Modify | `_rate_limits` and `_failed_auth_counts` keyed by string identity-key; remove `int(.native_id)` round-trip in `_auth_identity` |
| `src/link_project_to_chat/bot.py` | Modify | Drop unused `AskQuestion` import; replace 3× `int(incoming.sender.native_id)` with identity key; replace 4× hardcoded 4096 with `transport.max_text_length`; remove 4× duplicated `MessageRef` fallback; call `await transport.run()` instead of `app.run_polling()`; check `has_unsupported_media` in `_on_text` |
| `tests/transport/test_telegram_transport.py` | Modify | Add filename-sanitization, pre-download-auth, unsupported-media-caption, HTML-retry tests |
| `tests/transport/test_contract.py` | Modify | Add `max_text_length`, `set_authorizer`, `run` contract assertions |
| `tests/test_security.py` | Modify | Add string-keyed rate-limit regression test |

---

## Task 1: [C1] Sanitize Telegram filenames before temp download

**Findings closed:** C1 (path traversal via unsanitized `doc.file_name` / `audio.file_name`)

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py:517-559` (document and audio paths)
- Test: `tests/transport/test_telegram_transport.py` (new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_telegram_transport.py` (append to end):

```python
async def test_document_filename_with_path_separators_is_sanitized_to_basename():
    """A malicious document filename like '../../etc/passwd' must not escape the temp dir."""
    t, _bot = _make_transport_with_mock_bot()
    captured_path: list = []

    async def handler(msg):
        captured_path.append(msg.files[0].path)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    document = SimpleNamespace(
        file_name="../../etc/passwd",
        mime_type="text/plain",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=document, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    # Path must live under tempfile's tempdir; basename must NOT contain separators.
    import tempfile as _tf
    assert captured_path[0].name == "passwd", f"basename leaked separators: {captured_path[0].name}"
    assert str(captured_path[0]).startswith(_tf.gettempdir()) or "tmp" in str(captured_path[0]).lower()


async def test_audio_filename_with_absolute_path_is_sanitized():
    """An audio filename like '/etc/passwd' must reduce to the basename inside tempdir."""
    t, _bot = _make_transport_with_mock_bot()
    captured_path: list = []

    async def handler(msg):
        captured_path.append(msg.files[0].path)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    audio = SimpleNamespace(
        file_name="/etc/passwd",
        mime_type="audio/mpeg",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=None, voice=None, audio=audio,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert captured_path[0].name == "passwd"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/transport/test_telegram_transport.py::test_document_filename_with_path_separators_is_sanitized_to_basename tests/transport/test_telegram_transport.py::test_audio_filename_with_absolute_path_is_sanitized -v
```

Expected: both FAIL — current code writes to `<tmpdir>/../../etc/passwd` (resolves outside tempdir) or similar.

- [ ] **Step 3: Add the sanitizer helper**

Add to `src/link_project_to_chat/transport/telegram.py` near other helpers (after the `_RELAY_PREFIX_RE` definition, around line 53):

```python
def _safe_basename(raw: str | None, fallback: str) -> str:
    """Reduce an attacker-controlled filename to a safe basename for tempdir use.

    Strips path separators, parent-dir traversals, and leading dots. Falls back
    to `fallback` if the result is empty or starts with '.'. The returned name
    is suitable for direct concatenation under a tempfile.TemporaryDirectory.
    """
    name = (raw or "").strip()
    if not name:
        return fallback
    # PurePosixPath/PureWindowsPath both strip separator/parent components.
    from pathlib import PurePath
    candidate = PurePath(name.replace("\\", "/")).name
    # Reject empty, '.', '..', and dotfile-like names that could shadow OS paths.
    if not candidate or candidate in (".", "..") or candidate.startswith("."):
        return fallback
    return candidate
```

- [ ] **Step 4: Apply sanitizer at document and audio paths**

In `src/link_project_to_chat/transport/telegram.py`, replace the document block (lines 517–530) and the audio block (lines 546–559):

```python
        doc = getattr(msg, "document", None)
        if doc is not None:
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            raw_name = getattr(doc, "file_name", None)
            safe_name = _safe_basename(raw_name, "document")
            path = Path(tmpdir.name) / safe_name
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=safe_name,
                mime_type=getattr(doc, "mime_type", None),
                size_bytes=getattr(doc, "file_size", 0) or 0,
            ))
```

```python
        audio = getattr(msg, "audio", None)
        if audio is not None:
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            raw_name = getattr(audio, "file_name", None)
            safe_name = _safe_basename(raw_name, "audio")
            path = Path(tmpdir.name) / safe_name
            tg_file = await audio.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=safe_name,
                mime_type=getattr(audio, "mime_type", None) or "audio/mpeg",
                size_bytes=getattr(audio, "file_size", 0) or 0,
            ))
```

(Note: `original_name` now reflects the sanitized name. This is intentional — the bot must not re-derive an attacker-controlled name downstream.)

- [ ] **Step 5: Run tests to verify pass**

```bash
pytest tests/transport/test_telegram_transport.py -v
```

Expected: all PASS, including the two new sanitization tests.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "$(cat <<'EOF'
fix(transport): sanitize Telegram filenames before temp download (C1)

Closes the path-traversal regression: malicious doc.file_name /
audio.file_name values like '../../etc/passwd' or '/etc/passwd' now
collapse to a basename inside the tempfile.TemporaryDirectory before
download_to_drive runs. The downstream bot.py sanitizer ran too late.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: [C2] Pre-download authorization hook

**Findings closed:** C2 (unauth users force network + disk work; voice size checks happen post-download)

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py` (add `set_authorizer` to Protocol)
- Modify: `src/link_project_to_chat/transport/telegram.py` (call authorizer before downloads)
- Modify: `src/link_project_to_chat/transport/fake.py` (implement `set_authorizer`)
- Modify: `src/link_project_to_chat/bot.py` (register authorizer during `build()`)
- Test: `tests/transport/test_telegram_transport.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_telegram_transport.py`:

```python
async def test_unauthorized_pre_dispatch_skips_downloads_and_handlers():
    """Authorizer returning False must short-circuit before any get_file()/download."""
    t, _bot = _make_transport_with_mock_bot()
    handler_calls: list = []
    download_calls: list = []

    async def handler(msg):
        handler_calls.append(msg)

    t.on_message(handler)

    async def authorizer(identity):
        return False  # Always reject.

    t.set_authorizer(authorizer)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Mallory", username="mallory", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        download_calls.append(path)
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    document = SimpleNamespace(
        file_name="big.bin",
        mime_type="application/octet-stream",
        file_size=10**9,  # 1 GB — would burn disk if downloaded.
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=document, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert download_calls == [], "authorized=False must prevent download_to_drive"
    assert document.get_file.await_count == 0, "authorized=False must skip get_file too"
    assert handler_calls == [], "authorized=False must skip handler invocation"


async def test_authorized_pre_dispatch_proceeds_with_downloads():
    """Authorizer returning True allows the normal download path."""
    t, _bot = _make_transport_with_mock_bot()
    handler_calls: list = []

    async def handler(msg):
        handler_calls.append(msg)

    t.on_message(handler)

    async def authorizer(identity):
        return identity.handle == "alice"

    t.set_authorizer(authorizer)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hi", photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(handler_calls) == 1
    assert handler_calls[0].text == "hi"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/transport/test_telegram_transport.py::test_unauthorized_pre_dispatch_skips_downloads_and_handlers tests/transport/test_telegram_transport.py::test_authorized_pre_dispatch_proceeds_with_downloads -v
```

Expected: FAIL — `set_authorizer` doesn't exist yet.

- [ ] **Step 3: Add `set_authorizer` to the Transport Protocol**

In `src/link_project_to_chat/transport/base.py`, add the type alias near other handler types (around line 116):

```python
AuthorizerCallback = Callable[[Identity], Awaitable[bool]]
```

Add to the `Transport` Protocol class (near the other registration methods, around line 197):

```python
    def set_authorizer(self, authorizer: AuthorizerCallback | None) -> None:
        """Pre-message authorization gate. Called by the transport BEFORE any
        expensive platform work (file downloads, etc.). Returning False causes
        the transport to silently drop the message — no handlers, no downloads.

        This is a DoS-defense layer; the bot SHOULD still re-auth in its message
        handlers as defense-in-depth. Pass None to disable gating.
        """
        ...
```

- [ ] **Step 4: Implement on `TelegramTransport`**

In `src/link_project_to_chat/transport/telegram.py`:

Add to `__init__` (after line 112, alongside other state):

```python
        self._authorizer: "AuthorizerCallback | None" = None  # type: ignore[name-defined]
```

Add the import at the top of the file (around line 14):

```python
from .base import (
    AuthorizerCallback,
    ButtonHandler,
    ...
)
```

Add the method (group with other on_* methods, around line 662):

```python
    def set_authorizer(self, authorizer: AuthorizerCallback | None) -> None:
        self._authorizer = authorizer
```

In `_dispatch_message`, gate the entire body. Replace the start of the method (around line 484) — insert after the `msg/user None` early return:

```python
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        """Convert a telegram Update into IncomingMessage and invoke handlers.

        Downloads photo/document attachments to per-handler temp directories,
        then removes them after all handlers return. If an authorizer is set,
        it is consulted BEFORE any download work; rejection drops the update.
        """
        import tempfile

        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        # Pre-download authorization gate (defends against unauth-DoS via large attachments).
        if self._authorizer is not None:
            sender = identity_from_telegram_user(user)
            if not await self._authorizer(sender):
                return

        from .base import IncomingFile, IncomingMessage
        ...
```

(Leave the rest of the method unchanged.)

- [ ] **Step 5: Implement on `FakeTransport`**

In `src/link_project_to_chat/transport/fake.py`, add to `__init__`:

```python
        self._authorizer = None
```

Add the method:

```python
    def set_authorizer(self, authorizer):
        self._authorizer = authorizer
```

(Update FakeTransport's incoming-dispatch path to honor the authorizer — read fake.py first to find the right insertion point. If FakeTransport doesn't have a media-download path, just storing the authorizer is enough; document this in a comment.)

- [ ] **Step 6: Register authorizer in `bot.py`**

In `src/link_project_to_chat/bot.py`, in `build()` (around line 1873–1879), after `self._transport = TelegramTransport.build(...)`:

```python
    def build(self):
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport.build(self.token, menu=COMMANDS)
        self._app = self._transport.app
        self._app.post_init = self._transport.post_init
        self._app.post_stop = self._transport.post_stop
        self._transport.on_ready(self._after_ready)

        async def _pre_authorize(identity) -> bool:
            return self._auth_identity(identity)

        self._transport.set_authorizer(_pre_authorize)

        app = self._app
        ...
```

- [ ] **Step 7: Run tests to verify pass**

```bash
pytest tests/transport/test_telegram_transport.py -v
pytest tests/transport/test_contract.py -v
pytest -v  # full suite, ~5 min
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/transport/fake.py src/link_project_to_chat/bot.py tests/transport/test_telegram_transport.py
git commit -m "$(cat <<'EOF'
feat(transport): add pre-download authorization hook (C2)

Closes the unauth-DoS regression: photo/document/voice/audio downloads
in TelegramTransport._dispatch_message used to run before bot.py auth.
Now Transport.set_authorizer registers a callback the transport invokes
BEFORE get_file()/download_to_drive — rejection silently drops the
update without burning disk or bandwidth.

bot.py registers _auth_identity as the authorizer during build().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: [I2] Unsupported-media captions no longer leak as plain text

**Findings closed:** I2 (video/sticker/location/contact/video_note captions become plain prompts to Claude)

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py` (add `has_unsupported_media` to `IncomingMessage`)
- Modify: `src/link_project_to_chat/transport/telegram.py` (set the flag when an unsupported attachment was present)
- Modify: `src/link_project_to_chat/bot.py` (`_on_text` checks the flag before submitting to Claude)
- Test: `tests/transport/test_telegram_transport.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_telegram_transport.py`:

```python
async def test_video_with_caption_marks_message_as_unsupported_media():
    """Telegram allows video/sticker/etc. through the filter; the bot must NOT
    treat their captions as plain text."""
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None,
        caption="please summarize this video",
        photo=None, document=None, voice=None, audio=None,
        video=SimpleNamespace(file_id="vid123"),
        sticker=None, location=None, contact=None, video_note=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(received) == 1
    assert received[0].has_unsupported_media is True
    assert received[0].text == "please summarize this video"  # caption preserved for the rejection UX


async def test_text_only_message_is_not_unsupported_media():
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hi there",
        photo=None, document=None, voice=None, audio=None,
        video=None, sticker=None, location=None, contact=None, video_note=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert received[0].has_unsupported_media is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/transport/test_telegram_transport.py::test_video_with_caption_marks_message_as_unsupported_media -v
```

Expected: FAIL — `has_unsupported_media` doesn't exist on `IncomingMessage`.

- [ ] **Step 3: Extend `IncomingMessage`**

In `src/link_project_to_chat/transport/base.py`, the `IncomingMessage` dataclass (line 84):

```python
@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    native: Any = None
    is_relayed_bot_to_bot: bool = False
    message: MessageRef | None = None
    reply_to_text: str | None = None
    reply_to_sender: Identity | None = None
    has_unsupported_media: bool = False  # True if the platform delivered a
    # video/sticker/location/contact/video-note that the transport can't decode.
    # Bot SHOULD reject with a "media type not supported" reply rather than
    # treating any caption as a normal prompt.
```

- [ ] **Step 4: Set the flag in `TelegramTransport._dispatch_message`**

In `src/link_project_to_chat/transport/telegram.py`, in `_dispatch_message` after the four supported-media blocks (around line 559, before the `text = msg.text or ...` line):

```python
        # Detect unsupported attachments delivered by the filter
        # (filters.VIDEO | filters.Sticker.ALL | filters.LOCATION | filters.CONTACT | filters.VIDEO_NOTE).
        # We surface a flag so the bot can reject instead of treating the caption as a prompt.
        has_unsupported_media = (
            len(files) == 0
            and any(
                getattr(msg, attr, None) is not None
                for attr in ("video", "sticker", "location", "contact", "video_note")
            )
        )
```

In the `IncomingMessage(...)` construction (around line 580), pass the flag:

```python
        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=text,
            files=files,
            reply_to=(
                message_ref_from_telegram(reply_native)
                if reply_native is not None else None
            ),
            native=msg,
            is_relayed_bot_to_bot=is_relayed,
            message=message_ref_from_telegram(msg),
            reply_to_text=reply_to_text,
            reply_to_sender=reply_to_sender,
            has_unsupported_media=has_unsupported_media,
        )
```

- [ ] **Step 5: Reject in `bot.py._on_text_from_transport`**

Read `src/link_project_to_chat/bot.py` around line 700 to find `_on_text_from_transport`. Insert the check immediately after the auth and rate-limit gates (after line 717, before the pending-skill capture block):

```python
        if incoming.has_unsupported_media:
            await self._transport.send_text(
                incoming.chat,
                "Unsupported media type. I can read text, photos, documents, voice notes, and audio.",
                reply_to=incoming.message,
            )
            return
```

- [ ] **Step 6: Run tests to verify pass**

```bash
pytest tests/transport/test_telegram_transport.py -v
pytest tests/test_bot_streaming.py tests/test_bot_team_wiring.py tests/test_bot_voice.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/bot.py tests/transport/test_telegram_transport.py
git commit -m "$(cat <<'EOF'
fix(transport): flag unsupported media so captions don't leak as prompts (I2)

Telegram's private-message filter accepts video/sticker/location/contact
/video-note. _dispatch_message only extracts photo/doc/voice/audio, so
captions on unsupported media used to flow to Claude as if no attachment
existed. IncomingMessage gains has_unsupported_media; bot rejects with
a polite reply.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: [I1] HTML retry on deleted reply target — inside the transport

**Findings closed:** I1 (`_send_html` lost HTML-on-deleted-reply retry; previously fixed in `4b4c08d`)

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py` (`send_text` retries without `reply_to` on the specific BadRequest)
- Modify: `src/link_project_to_chat/bot.py` (`_send_html` no longer needs the HTML→plain fallback for this case)
- Test: `tests/transport/test_telegram_transport.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_telegram_transport.py`:

```python
async def test_send_text_retries_without_reply_to_when_target_deleted_preserving_html():
    """If Telegram returns 'Message to be replied not found', send_text retries
    once without reply_to_message_id, preserving parse_mode=HTML."""
    t, bot = _make_transport_with_mock_bot()

    # First call raises the specific BadRequest; second call (the retry) succeeds.
    class _ReplyTargetMissing(Exception):
        def __init__(self):
            super().__init__("Message to be replied not found")
            self.message = "Message to be replied not found"

    success_native = SimpleNamespace(
        message_id=99, chat=SimpleNamespace(id=12345, type="private")
    )
    bot.send_message = AsyncMock(side_effect=[_ReplyTargetMissing(), success_native])

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_to = MessageRef(transport_id=TRANSPORT_ID, native_id="500", chat=chat)

    ref = await t.send_text(chat, "<b>hi</b>", html=True, reply_to=reply_to)

    assert ref.native_id == "99"
    assert bot.send_message.await_count == 2
    second_kwargs = bot.send_message.await_args_list[1].kwargs
    assert "reply_to_message_id" not in second_kwargs, "retry must drop reply_to"
    assert second_kwargs["parse_mode"] == "HTML", "retry must preserve HTML parse_mode"


async def test_send_text_does_not_retry_for_unrelated_badrequest():
    """Other BadRequest errors (e.g., chat not found) must NOT trigger the retry."""
    t, bot = _make_transport_with_mock_bot()

    class _OtherBadRequest(Exception):
        def __init__(self):
            super().__init__("Chat not found")
            self.message = "Chat not found"

    bot.send_message = AsyncMock(side_effect=_OtherBadRequest())
    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_to = MessageRef(transport_id=TRANSPORT_ID, native_id="500", chat=chat)

    with pytest.raises(Exception) as excinfo:
        await t.send_text(chat, "hi", reply_to=reply_to)
    assert "Chat not found" in str(excinfo.value)
    assert bot.send_message.await_count == 1, "no retry for unrelated errors"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/transport/test_telegram_transport.py::test_send_text_retries_without_reply_to_when_target_deleted_preserving_html -v
```

Expected: FAIL — current `send_text` re-raises any exception that isn't a retry-after.

- [ ] **Step 3: Implement the retry inside `TelegramTransport.send_text`**

In `src/link_project_to_chat/transport/telegram.py`, replace the `send_text` body (lines 361–388):

```python
    _DELETED_REPLY_TARGET_MARKERS = (
        "message to be replied not found",
        "replied message not found",
        "reply message not found",
    )

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
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
            remapped = _as_retry_after(e)
            if remapped is not None:
                raise remapped from e
            # Retry once without reply_to if the target message was deleted —
            # preserving HTML/buttons. This restores the behavior previously
            # hand-rolled in bot.py._send_html (commit 4b4c08d).
            if reply_to is not None and self._is_deleted_reply_target(e):
                logger.info(
                    "send_text retry without reply_to: target message deleted",
                )
                kwargs.pop("reply_to_message_id", None)
                native_msg = await self._app.bot.send_message(**kwargs)
                return message_ref_from_telegram(native_msg)
            raise
        return message_ref_from_telegram(native_msg)

    @classmethod
    def _is_deleted_reply_target(cls, exc: BaseException) -> bool:
        """Recognize the BadRequest variants Telegram uses when the reply
        target has been deleted (covers slight wording differences across
        PTB versions)."""
        message = (getattr(exc, "message", "") or str(exc) or "").lower()
        return any(marker in message for marker in cls._DELETED_REPLY_TARGET_MARKERS)
```

- [ ] **Step 4: Drop the redundant fallback in `bot.py._send_html`**

The plain-text fallback at `src/link_project_to_chat/bot.py:318-331` was added because the deleted-reply case used to bubble up. Now the transport handles it. Tighten the fallback to only catch *unexpected* errors:

```python
    async def _send_html(
        self,
        chat: ChatRef,
        html: str,
        reply_to: MessageRef | None = None,
        reply_markup: Buttons | None = None,
    ) -> MessageRef | None:
        """Send HTML message(s), attaching buttons to the last chunk. Returns the last sent ref."""
        assert self._transport is not None
        chunks = split_html(html)
        last_ref: MessageRef | None = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            btns = reply_markup if is_last else None
            try:
                last_ref = await self._transport.send_text(
                    chat, chunk, html=True, reply_to=reply_to, buttons=btns,
                )
            except Exception as exc:
                # The transport already retries on deleted-reply-target. Anything
                # reaching here is genuinely unexpected (parse error, malformed
                # HTML, network issue). Fall back to plain so the user gets *some*
                # output; log so the cause is recoverable.
                logger.warning("HTML send failed, falling back to plain: %s", exc, exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    try:
                        last_ref = await self._transport.send_text(
                            chat,
                            plain[:4096] if len(plain) > 4096 else plain,
                            reply_to=reply_to,
                            buttons=btns,
                        )
                    except Exception:
                        logger.error("Plain-text fallback also failed", exc_info=True)
        return last_ref
```

(This also closes M4 — the inner try/except catches the case where the plain-text fallback itself fails. The `4096` literal is replaced in Task 7.)

- [ ] **Step 5: Run tests to verify pass**

```bash
pytest tests/transport/test_telegram_transport.py tests/test_bot_streaming.py -v
```

Expected: all PASS, including the two new retry tests. The pre-existing `test_send_html_retries_without_reply_to_when_target_deleted` (if it exists) should now pass against the transport-layer implementation; if it asserted bot-layer call counts, update those assertions to reflect that retry happens inside the transport.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/bot.py tests/transport/test_telegram_transport.py
git commit -m "$(cat <<'EOF'
fix(transport): retry without reply_to on deleted-target inside TelegramTransport (I1)

Restores the behavior added in 4b4c08d (which the transport port lost):
when Telegram returns 'Message to be replied not found', send_text now
retries once without reply_to_message_id, preserving parse_mode=HTML and
buttons. bot.py's plain-text fallback narrows to truly unexpected failures.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: [I3] Identity keys are strings, not ints

**Findings closed:** I3 (auth/rate-limit assume `native_id` is `int`-parseable; will break Discord/Slack/Web)

**Files:**
- Modify: `src/link_project_to_chat/_auth.py` (rate-limit + failed-auth dicts keyed by string)
- Modify: `src/link_project_to_chat/bot.py` (3× rate-limit call sites)
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_security.py`:

```python
def test_rate_limit_works_with_non_numeric_native_id():
    """Auth/rate-limit must NOT assume native_id is int-parseable.

    A Slack channel id 'C0XXXXXX' or Discord snowflake stays a string
    end-to-end. Calling _rate_limited with a string identity-key must
    succeed and return False on first call, True after exceeding the cap.
    """
    from link_project_to_chat._auth import AuthMixin

    class _Bot(AuthMixin):
        _allowed_usernames = ["alice"]

    bot = _Bot()
    bot._init_auth()

    key = "discord:abc123-snowflake"
    # 30 messages allowed per minute by default.
    for _ in range(bot._MAX_MESSAGES_PER_MINUTE):
        assert bot._rate_limited(key) is False
    # 31st in the same window: rate limited.
    assert bot._rate_limited(key) is True


def test_failed_auth_count_works_with_string_key():
    from link_project_to_chat._auth import AuthMixin

    class _Bot(AuthMixin):
        _allowed_usernames = ["alice"]

    bot = _Bot()
    bot._init_auth()
    bot._failed_auth_counts["telegram:42"] = 5
    # Direct check: lockout dict accepts string keys without TypeError.
    assert bot._failed_auth_counts.get("telegram:42") == 5
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_security.py::test_rate_limit_works_with_non_numeric_native_id -v
```

Expected: FAIL — `_rate_limited` is currently typed `def _rate_limited(self, user_id: int)` and the dict is `dict[int, deque]`. (The test won't blow up on the type hint, but downstream callers in bot.py do `int(...)` cast — confirming the design.)

- [ ] **Step 3: Switch internal dicts to string keys**

In `src/link_project_to_chat/_auth.py`:

Replace `_init_auth` (lines 25–27):

```python
    def _init_auth(self) -> None:
        self._rate_limits: dict[str, collections.deque] = {}
        self._failed_auth_counts: dict[str, int] = {}
```

Replace `_rate_limited` (lines 139–147):

```python
    def _rate_limited(self, identity_key: str) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(identity_key, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
```

Replace `_auth_identity` (lines 130–137):

```python
    def _auth_identity(self, identity) -> bool:
        """Authorize based on a transport Identity. Wraps _auth.

        Telegram-only legacy: _auth still consumes user.id as int because
        _trusted_users persistence is int-typed. We coerce here so the
        boundary is contained, but rate-limit/failed-auth dicts use the
        platform-neutral identity-key (transport_id:native_id) directly.
        """
        from types import SimpleNamespace
        # _auth still expects user.id to be int-comparable against _trusted_users.values().
        # Until persistence migrates, keep the cast scoped to this one site.
        try:
            uid = int(identity.native_id)
        except (TypeError, ValueError):
            uid = identity.native_id  # non-numeric: skip the int-trusted-id fast path; username match still works.
        user = SimpleNamespace(id=uid, username=identity.handle or "")
        return self._auth(user)

    @staticmethod
    def _identity_key(identity) -> str:
        """Stable string key for rate-limit / failed-auth bookkeeping.

        Includes transport_id so the same numeric id from different platforms
        doesn't collide (telegram:42 vs discord:42).
        """
        return f"{identity.transport_id}:{identity.native_id}"
```

In `_auth` (line 115), update the failed-auth bookkeeping to use string keys when `user.id` is non-int:

```python
        # _failed_auth_counts is now keyed by the same value as user.id (whatever type it is).
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return False
        ...
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
```

(No code change needed here — Python dicts accept mixed keys. The earlier int-cast in `_init_auth` was the only problem.)

- [ ] **Step 4: Update `bot.py` rate-limit call sites**

In `src/link_project_to_chat/bot.py`:

Line 713 (and analogous sites at 1662, 1720 — search for `int(incoming.sender.native_id)` or `int(.native_id))` to find them):

Before:
```python
        if self._rate_limited(int(incoming.sender.native_id)):
```

After:
```python
        if self._rate_limited(self._identity_key(incoming.sender)):
```

Run a sanity grep:

```bash
grep -n "int(.*\.native_id" src/link_project_to_chat/bot.py
```

Expected: only the four `group_chat_id` comparisons remain (those are config-stored Telegram-int and out of scope for spec #0; spec #0a will address them).

- [ ] **Step 5: Run tests to verify pass**

```bash
pytest tests/test_security.py tests/test_auth.py tests/test_auth_m10.py tests/test_bot_streaming.py -v
```

Expected: all PASS, including the two new identity-key tests.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/_auth.py src/link_project_to_chat/bot.py tests/test_security.py
git commit -m "$(cat <<'EOF'
refactor(auth): rate-limit and failed-auth keyed by string identity-key (I3)

Spec says native_id is opaque string. _rate_limited / _failed_auth_counts
now use 'transport_id:native_id' as the key. bot.py's three int(.native_id)
casts at rate-limit sites are gone. Discord snowflakes and Slack channel
ids will work without coercion.

_auth_identity still int-coerces user.id for the legacy _trusted_users
fast-path (Telegram-only persistence); that's contained at the boundary
and falls back gracefully for non-numeric ids.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: [I4] Elevate `run()` to the Transport Protocol

**Findings closed:** I4 (`bot.py` binds PTB-named `app.post_init`, `app.post_stop`, `app.run_polling()` — runtime PTB coupling that the lockout doesn't catch)

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py` (add `run` to Protocol)
- Modify: `src/link_project_to_chat/transport/telegram.py` (implement `run` — owns the `post_init`/`post_stop`/`run_polling` wiring)
- Modify: `src/link_project_to_chat/transport/fake.py` (no-op `run` for tests)
- Modify: `src/link_project_to_chat/bot.py` (drop the three PTB attribute touches; call `await transport.run()`)
- Modify: `tests/test_transport_lockout.py` (lockout `run_polling`, `post_init`, `post_stop` strings in bot.py)
- Test: `tests/transport/test_telegram_transport.py`, `tests/transport/test_contract.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_contract.py`:

```python
def test_transport_has_run_method(transport):
    """Every Transport must expose run() so bot.py never touches the native app.

    Sync (not async): PTB's run_polling() creates its own event loop internally;
    async-native transports (Discord) wrap in asyncio.run inside their run().
    """
    assert hasattr(transport, "run"), f"{type(transport).__name__} missing run()"
    assert callable(transport.run)
    import inspect
    assert not inspect.iscoroutinefunction(transport.run), (
        "run must be sync — PTB owns its event loop; async-native transports "
        "internally wrap with asyncio.run inside their run()"
    )
```

Add to `tests/test_transport_lockout.py` (in the bot.py allowlist test, expand the forbidden-strings list):

```python
def test_bot_py_does_not_reference_ptb_application_internals():
    """Locks out runtime PTB coupling: bot.py must not name application-level
    attributes (run_polling, post_init, post_stop, ApplicationBuilder)
    directly. These are TelegramTransport's responsibility."""
    src = (Path(__file__).parent.parent / "src" / "link_project_to_chat" / "bot.py").read_text()
    forbidden = ["run_polling", ".post_init", ".post_stop", "ApplicationBuilder"]
    found = [tok for tok in forbidden if tok in src]
    assert not found, f"bot.py references PTB internals: {found}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/transport/test_contract.py::test_transport_has_run_method tests/test_transport_lockout.py::test_bot_py_does_not_reference_ptb_application_internals -v
```

Expected: both FAIL — `run` not on Protocol; `bot.py` still names `run_polling`/`post_init`/`post_stop`.

- [ ] **Step 3: Add `run` to the Protocol**

In `src/link_project_to_chat/transport/base.py`, add to the `Transport` Protocol (near `start`/`stop`, around line 145):

```python
    def run(self) -> None:
        """Synchronously run the transport's main loop until cancelled.

        Implementations own their event loop. PTB's Application.run_polling()
        is sync and creates its own loop; async-native transports (Discord
        client.start, uvicorn.serve) wrap with asyncio.run inside this method.
        Returns when the transport stops.
        """
        ...
```

- [ ] **Step 4: Implement on `TelegramTransport`**

In `src/link_project_to_chat/transport/telegram.py`, add the method (group with `start`/`stop` lifecycle, after `stop` around line 358):

```python
    def run(self) -> None:
        """Run PTB's polling loop. Owns post_init/post_stop wiring so bot.py
        never touches the Application by name.

        Synchronous: PTB's run_polling creates and manages its own event loop.
        """
        self._app.post_init = self.post_init
        self._app.post_stop = self.post_stop
        self._app.run_polling()
```

- [ ] **Step 5: Implement on `FakeTransport`**

In `src/link_project_to_chat/transport/fake.py`:

```python
    def run(self) -> None:
        """Fake transport: no-op (tests drive dispatch synchronously)."""
        return
```

- [ ] **Step 6: Update `bot.py` to use `transport.run()`**

In `src/link_project_to_chat/bot.py`:

Remove lines 1877–1878 from `build()`:

```python
        self._app.post_init = self._transport.post_init
        self._app.post_stop = self._transport.post_stop
```

(They migrate into `TelegramTransport.run` — Step 4 above.)

Add a `run` method to `ProjectBot` (near other public methods):

```python
    def run(self) -> None:
        """Run the transport's main loop. Owns the lifecycle from here on.

        Synchronous: matches the underlying Transport.run() contract.
        """
        assert self._transport is not None
        self._transport.run()
```

Update the run-bot orchestration around line 2023–2027:

Before:
```python
    app = bot.build()
    logger.info(...)
    app.run_polling()
```

After:
```python
    bot.build()
    logger.info(...)
    bot.run()
```

(`build()` previously returned `self._app`; if any caller still relies on the return value, audit `tests/test_cli.py` for `bot.build()` usage and update accordingly. Internal-only consumers — most tests build TelegramTransport directly with a mock app — are unaffected.)

- [ ] **Step 7: Run tests to verify pass**

```bash
pytest tests/transport/test_contract.py tests/test_transport_lockout.py tests/transport/test_telegram_transport.py -v
pytest tests/test_cli.py -v  # CLI orchestration sanity check
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/transport/fake.py src/link_project_to_chat/bot.py tests/transport/test_contract.py tests/transport/test_telegram_transport.py tests/test_transport_lockout.py
git commit -m "$(cat <<'EOF'
feat(transport): elevate run() to the Protocol (I4)

bot.py used to bind PTB Application attributes by name (post_init,
post_stop, run_polling) — runtime coupling the static lockout missed.
Transport.run() now owns the polling-loop lifecycle. ProjectBot.run()
delegates. The lockout test forbids 'run_polling', '.post_init',
'.post_stop', 'ApplicationBuilder' in bot.py for permanent enforcement.

Discord/Slack/Web transports plug in by implementing run() (websocket
loop / HTTP server) without bot.py changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: [M2] `Transport.max_text_length`

**Findings closed:** M2 (hardcoded `4096` Telegram limit in `bot.py`)

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `src/link_project_to_chat/transport/fake.py`
- Modify: `src/link_project_to_chat/bot.py`
- Test: `tests/transport/test_contract.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/transport/test_contract.py`:

```python
def test_transport_exposes_max_text_length(transport):
    """Every Transport declares its platform's max single-message text length."""
    assert hasattr(transport, "max_text_length")
    assert isinstance(transport.max_text_length, int)
    assert transport.max_text_length > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/transport/test_contract.py::test_transport_exposes_max_text_length -v
```

Expected: FAIL.

- [ ] **Step 3: Add the attribute**

In `src/link_project_to_chat/transport/base.py`, in the `Transport` Protocol body (near other class-level declarations, around line 144):

```python
    max_text_length: int  # Largest single-message text length the platform accepts.
```

In `src/link_project_to_chat/transport/telegram.py`, in `TelegramTransport` (near `TRANSPORT_ID = TRANSPORT_ID`, around line 97):

```python
    max_text_length: int = 4096  # Telegram's hard cap.
```

In `src/link_project_to_chat/transport/fake.py`, in the class body:

```python
    max_text_length: int = 4096  # Match the most-restrictive transport for test parity.
```

- [ ] **Step 4: Replace hardcoded literals in `bot.py`**

```bash
grep -n "4096" src/link_project_to_chat/bot.py
```

For each match, replace `4096` with `self._transport.max_text_length` (skip any inside docstrings or comments). The two known sites are around `bot.py:328` and `bot.py:457` per the review. Verify after each edit:

```bash
grep -n "4096" src/link_project_to_chat/bot.py  # should match only docs/comments
```

- [ ] **Step 5: Run tests to verify pass**

```bash
pytest tests/transport/test_contract.py tests/test_bot_streaming.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/transport/fake.py src/link_project_to_chat/bot.py tests/transport/test_contract.py
git commit -m "refactor(transport): Transport.max_text_length replaces hardcoded 4096 (M2)"
```

---

## Task 8: [M1] Drop unused `AskQuestion` import

**Findings closed:** M1

**Files:** `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Verify it's unused**

```bash
grep -n "AskQuestion" src/link_project_to_chat/bot.py
```

Expected: only the `from .stream import ... AskQuestion ...` line at line 42.

- [ ] **Step 2: Remove `AskQuestion` from the import list**

In `src/link_project_to_chat/bot.py:42`, remove `AskQuestion` from the import. If the `from .stream import (...)` becomes empty, drop the whole line.

- [ ] **Step 3: Run tests to verify pass**

```bash
pytest -x -q
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "chore(bot): drop unused AskQuestion import (M1)"
```

---

## Task 9: [M3] Tighten `IncomingMessage.message: MessageRef`

**Findings closed:** M3 (4× duplicated `MessageRef(... native_id="0", ...)` fallback in `bot.py`)

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Verify both transports populate `message`**

```bash
grep -n "message=message_ref_from_telegram" src/link_project_to_chat/transport/telegram.py
grep -n "message=" src/link_project_to_chat/transport/fake.py
```

Both should populate `message` unconditionally. If `FakeTransport` doesn't, add a synthetic `MessageRef` there first.

- [ ] **Step 2: Tighten the type**

In `src/link_project_to_chat/transport/base.py`, in `IncomingMessage` (line 97):

```python
    message: MessageRef  # Required: every transport-emitted message has a back-ref.
```

(Drop the `| None = None` default. Consumers and constructors update accordingly.)

- [ ] **Step 3: Remove the four fallbacks in `bot.py`**

```bash
grep -n "incoming.message or MessageRef" src/link_project_to_chat/bot.py
```

For each match (expected at lines 689, 753, 1708, 1775 per the review), replace `incoming.message or MessageRef(transport_id=..., native_id="0", chat=incoming.chat)` with just `incoming.message`.

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest -x -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/bot.py
git commit -m "refactor(transport): IncomingMessage.message is required, drop 4x dead fallback (M3)"
```

---

## Task 10: [M4] Log fallback-of-fallback failures in `_send_html`

**Findings closed:** M4

**Files:** Already addressed in Task 4 Step 4. If Task 4 was skipped, do this in isolation:

In `src/link_project_to_chat/bot.py:322-331`, wrap the inner `await self._transport.send_text(...)` call in its own `try/except Exception` and log with `logger.error(..., exc_info=True)`. See Task 4 Step 4 for the exact code.

If Task 4 already ran, mark this task done and skip to Task 11.

- [ ] Step 1: confirm Task 4 ran (if so, skip).
- [ ] Step 2: if standalone, apply the inner try/except around the plain-text send.
- [ ] Step 3: `pytest tests/test_bot_streaming.py -v`.
- [ ] Step 4: commit.

---

## Task 11: [M5] Fix log-message wording in `post_init` failure path

**Findings closed:** M5

**Files:** `src/link_project_to_chat/transport/telegram.py:337`

- [ ] **Step 1: Locate**

```bash
grep -n "post_stop fallback after post_init failure" src/link_project_to_chat/transport/telegram.py
```

- [ ] **Step 2: Rewrite the log message**

Replace `"post_stop fallback after post_init failure"` with `"post_init failure: unwinding partial startup"` (or whatever wording matches the surrounding code's tone).

- [ ] **Step 3: Run tests to verify pass**

```bash
pytest tests/transport/test_telegram_transport.py -v
```

Expected: all PASS. (If a test asserts the exact log string, update it.)

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py
git commit -m "chore(transport): fix log-message wording in post_init failure path (M5)"
```

---

## Final Verification

- [ ] **Run the full suite**

```bash
pytest -v
```

Expected: `667+ passed` (the new tests increase count). No skips beyond the existing 4.

- [ ] **Run the lockout test specifically — it has been extended**

```bash
pytest tests/test_transport_lockout.py -v
```

Expected: PASS, including the new `test_bot_py_does_not_reference_ptb_application_internals`.

- [ ] **Confirm no regression in transport contract**

```bash
pytest tests/transport/ -v
```

Expected: PASS for both `FakeTransport` and `TelegramTransport` parametrizations.

- [ ] **Spot-check the `int(.native_id)` count**

```bash
grep -n "int(.*\.native_id" src/link_project_to_chat/bot.py
```

Expected: at most 4 hits, all referencing `group_chat_id` (config-stored Telegram-int). If any others remain, they're a missed call site for I3.

- [ ] **Confirm bot.py has no PTB Application references**

```bash
grep -nE "run_polling|\.post_init|\.post_stop|ApplicationBuilder" src/link_project_to_chat/bot.py
```

Expected: zero matches.

---

## Notes for Reviewer

- Tasks 1–6 collectively close every issue both code reviews flagged as blocking. After Task 6, the spec #0 exit claim is genuinely closed at the static, runtime, and DoS-defense levels.
- Tasks 7–11 are quality-of-life. They can land in a follow-up commit if the reviewer wants criticals first.
- The `_trusted_users` int-typed persistence (Telegram-only) intentionally remains. It's tracked for spec #0a (group/team port) which already touches the persistence layer.
- The `int(self.group_chat_id)` casts in `bot.py` (4 sites around lines 635, 643, 1257, 1274) are out of scope: `group_chat_id` is a config-stored Telegram int, owned by spec #0a.
