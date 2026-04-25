# Transport — Voice Port — Design Spec

**Status:** Shipped as v0.14.0. See [docs/TODO.md §1.1](../../TODO.md#11-shipped-specs) for current status and follow-ups.
**Date:** 2026-04-20
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0)
**Part of:** The transport-abstraction follow-up track. This is spec #0b — voice inbound + outbound TTS port.

---

## 1. Overview

Spec #0 deferred voice handling to this follow-up spec. After #0, [bot.py](src/link_project_to_chat/bot.py) still imports `Update`, `ContextTypes`, `MessageHandler`, and `filters` from telegram — entirely because of the `_on_voice` and `_on_unsupported` legacy handlers and their `MessageHandler` registrations. This spec ports both handlers through the Transport abstraction and removes the last telegram imports from bot.py.

**The deliverable is a complete telegram lockout in bot.py (zero `from telegram` imports) plus a new `Transport.send_voice` primitive.** Voice behavior is preserved identically to the current product.

## 2. Goals & non-goals

**Goals**
- Port `_on_voice` to consume `IncomingMessage` via `transport.on_message`.
- Port `_send_voice_response` to use a new `Transport.send_voice` primitive.
- Port `_on_unsupported` into a generic fallback inside the unified inbound dispatch in bot.py.
- Move `_on_error` and `_post_init` into `TelegramTransport` so bot.py never touches telegram types.
- Remove all `from telegram` / `import telegram` statements from bot.py.
- Update the lockout test ([tests/test_transport_lockout.py](tests/test_transport_lockout.py)) to require an empty telegram-import allowlist.
- Preserve existing voice UX: status message "🎤 Transcribing..." followed by edit with the transcript, routing to waiting-input tasks, persona application, TTS output when `_synthesizer` is set.

**Non-goals (this spec)**
- No new voice features (no multi-file voice messages, no voice-note format detection beyond what's already present, no partial-transcription streaming).
- No changes to [transcriber.py](src/link_project_to_chat/transcriber.py) or the Synthesizer protocols — they're already backend-agnostic and don't import telegram.
- No port of [manager/bot.py](src/link_project_to_chat/manager/bot.py) — spec #0c.
- No port of group/team features — spec #0a.
- No Web UI, Discord, or Slack transports — specs #1, #2, #3.
- No platform-specific UX polish — e.g., the current "Stickers aren't supported. Please type your message" tailored reply becomes a generic "This message type isn't supported. Please send text, a voice message, or a file."

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-20:

| # | Question | Decision |
|---|---|---|
| 1 | Scope: voice-only, or voice + unsupported? | Voice + unsupported — full lockout of bot.py |
| 2 | Transport outbound voice surface? | Add `Transport.send_voice` primitive (new method on the Protocol) |
| 3 | How are unsupported types recognized after lockout? | Generic fallback — bot replies "not supported" when `IncomingMessage` has no text and no files |

Trade-off explicitly accepted under Q3: the current tailored messages per unsupported subtype (sticker vs video vs video_note) collapse to one generic message. Users still get the signal.

## 4. Architecture

### 4.1 Transport interface extension

```python
# src/link_project_to_chat/transport/base.py

class Transport(Protocol):
    # ... existing methods ...

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef: ...
```

One new method. Required-floor per spec #0 Q4 — every Transport implements it.

**Platform renderings:**
- `TelegramTransport` → `self._app.bot.send_voice(chat_id, file, reply_to_message_id=...)` produces the telegram voice-note UI (waveform, inline playback).
- `FakeTransport` → captures to `sent_voices: list[SentVoice]`.
- Future transports (Discord, Slack, Web) render platform-appropriately when those specs ship.

Rationale:
- Matches `send_text` / `send_file` pattern. Explicit intent at call sites.
- Rejected alternative: `send_file(as_voice=True)` — signature clutter on a hot method.
- Rejected alternative: extension-sniff in `send_file` (implicit `.opus` → voice) — surprise behavior, makes "send opus as document" impossible.

### 4.2 Inbound voice + unsupported

**`TelegramTransport._dispatch_message`** gains two new download branches (voice, audio):

```python
voice = getattr(msg, "voice", None)
if voice is not None:
    tmpdir = tempfile.TemporaryDirectory()
    path = Path(tmpdir.name) / f"voice_{voice.file_id}.ogg"
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

Unsupported types (sticker, video_note, video, location, contact) are NOT downloaded. They arrive as `IncomingMessage` with empty `files` and empty `text` — the bot falls into the generic unsupported branch.

**`TelegramTransport.attach_telegram_routing`** broadens its main MessageHandler filter from:

```python
filters.TEXT | filters.Document.ALL | filters.PHOTO
```

to:

```python
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
```

Every non-command message flows through `_dispatch_message` → single `on_message` handler in bot.py. No separate `MessageHandler` registrations for voice or unsupported.

### 4.3 Bot inbound dispatch — unified entry point

```python
# src/link_project_to_chat/bot.py

async def _on_text_from_transport(self, incoming) -> None:
    # 1. Voice (audio mime).
    if incoming.files and any((f.mime_type or "").startswith("audio/") for f in incoming.files):
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

Legacy methods removed: `_on_voice`, `_on_unsupported`.

### 4.4 Bot inbound voice handler

```python
# src/link_project_to_chat/bot.py

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
            reply_text = getattr(incoming.native.reply_to_message, "text", None)
            if reply_text:
                prompt = f"[Replying to: {reply_text}]\n\n{prompt}"

        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)

        message_id_int = int(incoming.native.message_id) if incoming.native is not None else 0
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

Notes:
- Temp-file cleanup for the downloaded audio is managed by the transport (per spec #0 — `msg._transport_tmpdirs` auto-cleans when the message handler returns). No `finally: ogg_path.unlink()` in this handler.
- `incoming.native.reply_to_message.text` access is the `native` escape hatch per spec #0. Acceptable for a rare enrichment path that only makes sense on platforms with reply semantics. Non-Telegram transports whose `native` doesn't carry `reply_to_message.text` skip the branch.
- `submit_claude` currently takes `chat_id: int` and `message_id: int` — we cast from `MessageRef.native_id` for now. Future: port `submit_claude` to take `ChatRef`/`MessageRef` (out of scope for #0b).

### 4.5 Bot outbound TTS

```python
# src/link_project_to_chat/bot.py

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

Direct `self._app.bot.send_voice(...)` call replaced with `self._transport.send_voice(...)`. `ChatRef` / `MessageRef` construction from int IDs mirrors the pattern used in `_send_image` (ported in spec #0 Task 23).

### 4.6 Moving `_on_error` and `_post_init` out of bot.py

These are the last two pieces holding the `Update` and `ContextTypes` imports in bot.py:

**`_on_error`** — telegram calls this on unhandled exceptions during update processing. Signature: `async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE)`. Logic: log the error, specially handle "Conflict" errors.

Move: into `TelegramTransport`. Register automatically during `attach_telegram_routing`:

```python
# src/link_project_to_chat/transport/telegram.py

async def _default_error_handler(self, update: Any, ctx: Any) -> None:
    import logging
    err = str(ctx.error)
    if "Conflict" in err:
        logging.getLogger(__name__).warning(
            "Conflict error (another instance?): %s | update=%s", err, update,
        )
    else:
        logging.getLogger(__name__).error(
            "Update error: %s | update=%s", ctx.error, update,
        )

def attach_telegram_routing(self, ...) -> None:
    # ... existing body ...
    self._app.add_error_handler(self._default_error_handler)
```

Bot no longer registers `app.add_error_handler(self._on_error)`. `_on_error` method deleted from bot.py.

**`_post_init`** — telegram calls this after `ApplicationBuilder().build()`. Logic:
1. `app.bot.delete_webhook(drop_pending_updates=True)`.
2. `app.bot.get_me()` to set `self.bot_username`.
3. Backfill `bot_username` into TeamConfig if missing.
4. `_refresh_team_system_note()`.
5. `app.bot.set_my_commands(COMMANDS)`.
6. For each trusted user ID, `app.bot.send_message(uid, "Bot started...")`.

Move: TelegramTransport handles steps 1 + 2 + 5 (platform-specific plumbing). The bot's remaining post-init work (3 + 4 + 6) fires via a new `Transport.on_ready(callback)` hook. `on_ready` callback signature:

```python
OnReadyCallback = Callable[[Identity], Awaitable[None]]
# Called once after Transport.start() has initialized but before polling begins.
# The Identity is the bot's own.
```

In `TelegramTransport`:

```python
def on_ready(self, callback: OnReadyCallback) -> None:
    self._on_ready_callbacks.append(callback)

async def start(self) -> None:
    await self._app.initialize()
    # Platform-specific post-init: drain pending updates + discover own identity +
    # register the /commands menu. Runs between initialize() and start() so the
    # application is fully configured before polling begins.
    await self._app.bot.delete_webhook(drop_pending_updates=True)
    try:
        me = await self._app.bot.get_me()
        self_identity = Identity(
            transport_id=TRANSPORT_ID,
            native_id=str(me.id),
            display_name=me.full_name or me.username or "bot",
            handle=(me.username or "").lower() or None,
            is_bot=True,
        )
    except Exception:
        self_identity = Identity(
            transport_id=TRANSPORT_ID, native_id="0",
            display_name="bot", handle=None, is_bot=True,
        )
    if self._menu:
        try:
            await self._app.bot.set_my_commands(self._menu)
        except Exception:
            pass
    # Caller-registered on_ready callbacks (backfill, system-note refresh,
    # startup pings) fire now — once the telegram app knows who it is.
    for cb in self._on_ready_callbacks:
        await cb(self_identity)
    await self._app.start()
    await self._app.updater.start_polling()

@classmethod
def build(cls, token: str, *, concurrent_updates: bool = True, menu: list[Any] | None = None) -> "TelegramTransport":
    from telegram.ext import ApplicationBuilder
    app = ApplicationBuilder().token(token).concurrent_updates(concurrent_updates).build()
    instance = cls(app)
    instance._menu = menu
    return instance
```

This removes `ApplicationBuilder.post_init()` entirely. Post-init logic runs inline within `start()` between `initialize()` and `start()` — equivalent timing, simpler ownership.

Bot uses:

```python
# bot.py
self._transport = TelegramTransport.build(self.token, menu=COMMANDS)
self._transport.on_ready(self._after_ready)

async def _after_ready(self, self_identity) -> None:
    self.bot_username = self_identity.handle or ""
    if self.team_name and self.role and self.bot_username:
        self._backfill_own_bot_username()
    self._refresh_team_system_note()
    # Startup ping to trusted users.
    assert self._transport is not None
    for uid in self._get_trusted_user_ids():
        chat = ChatRef(transport_id="telegram", native_id=str(uid), kind=ChatKind.DM)
        try:
            await self._transport.send_text(
                chat, f"Bot started.\nProject: {self.name}\nPath: {self.path}",
            )
        except Exception:
            logger.error("Failed to send startup message to %d", uid, exc_info=True)
```

`_post_init` method deleted from bot.py. Only `_after_ready` remains, and it's transport-typed.

### 4.7 Lockout

After #0b, bot.py's import block:

```python
# No `from telegram` or `import telegram` lines.
# All interaction with the telegram platform flows through self._transport.
```

[tests/test_transport_lockout.py](tests/test_transport_lockout.py) updated:

```python
ALLOWED_BOT_TELEGRAM_IMPORTS: set[str] = set()  # empty after spec #0b
```

The test fails if any `from telegram` or `import telegram` statement appears in bot.py.

## 5. Migration — strangler step sequence

Four steps. Each independently shippable; the bot works end-to-end at every step.

### Step 1 — Transport interface + `send_voice`

- Add `send_voice` to `Transport` Protocol.
- Implement `FakeTransport.send_voice` with `sent_voices: list[SentVoice]` capture.
- Implement `TelegramTransport.send_voice`.
- Extend contract test with one `send_voice` test, parametrized across both transports.
- Extend telegram-transport tests with one `send_voice` unit test using a mocked `bot.send_voice`.

**Exit:** new tests pass; existing tests unchanged.

### Step 2 — Inbound voice + unsupported through transport

- Extend `TelegramTransport._dispatch_message` with voice + audio download branches (populate `IncomingFile` with `audio/ogg` or `audio/mpeg` mime).
- Broaden `TelegramTransport.attach_telegram_routing` filter to include voice/audio/video_note/sticker/video/location/contact.
- Add `_on_voice_from_transport` method to `ProjectBot`.
- Extend `_on_text_from_transport` dispatch with voice branch (1) and unsupported fallback (4).
- Port `_send_voice_response` to use `self._transport.send_voice`.
- Delete `_on_voice` and `_on_unsupported` methods from bot.py.
- Delete the `voice_filter` and `unsupported_filter` MessageHandler registrations from `build()`.

**Exit:** voice messages work identically; unsupported types get the generic reply; all tests pass.

### Step 3 — Move `_on_error` + `_post_init` into Transport

- Add `TelegramTransport._default_error_handler`; register it during `attach_telegram_routing`.
- Add `TelegramTransport.on_ready(callback)` + `_on_ready_callbacks` list.
- Extend `TelegramTransport.build(token, *, menu=None)` to accept the commands menu.
- Fold the delete_webhook / get_me / set_my_commands / on_ready-firing logic into `TelegramTransport.start()` (between `initialize()` and `start()`). Drop `ApplicationBuilder.post_init()` entirely.
- In bot.py: replace `_post_init` method with `_after_ready(self_identity)`. Remove `app.add_error_handler(self._on_error)` registration.
- In bot.py: register via `self._transport.on_ready(self._after_ready)`.
- Delete `_on_error` and `_post_init` methods from bot.py.

**Exit:** bot startup behaves identically (delete_webhook, get_me, set_my_commands, backfill, system-note refresh, startup pings — in the same order); all tests pass.

### Step 4 — Lockout

- Remove `from telegram import Update` from bot.py.
- Remove `from telegram.ext import ContextTypes, MessageHandler, filters` from bot.py.
- Remove any residual `Update` / `ContextTypes` / `MessageHandler` / `filters` references (grep; all should be gone after Steps 2 and 3).
- Update [tests/test_transport_lockout.py](tests/test_transport_lockout.py) to require an empty allowlist.

**Exit:** `grep "from telegram\|import telegram" src/link_project_to_chat/bot.py` returns zero matches. Lockout test passes.

## 6. Testing approach

1. **Contract test** — one new test `test_send_voice_returns_usable_message_ref` added to [tests/transport/test_contract.py](tests/transport/test_contract.py), parametrized across all transports. Uses `tmp_path` to write a dummy file.

2. **TelegramTransport unit tests** — two new tests in [tests/transport/test_telegram_transport.py](tests/transport/test_telegram_transport.py):
   - `test_send_voice_calls_bot_send_voice`: mocks `bot.send_voice`, asserts it's called with the right chat_id and reply_to_message_id.
   - `test_incoming_message_populates_files_from_voice`: injects a telegram Update with a `.voice` attribute, asserts `IncomingMessage.files` contains one `IncomingFile` with `mime_type="audio/ogg"`.

3. **FakeTransport extension** — `sent_voices` list + `SentVoice(chat, path, reply_to, message)` dataclass. Smoke test in [tests/transport/test_fake.py](tests/transport/test_fake.py).

4. **Bot-level voice flow test** — `tests/test_bot_voice.py` (new). Uses `FakeTransport`, mocks `_transcriber.transcribe` with a stub. Tests:
   - Feeds an `inject_message` with an audio-mime `IncomingFile`.
   - Asserts status message `"🎤 Transcribing..."` appears in `sent_messages`.
   - Asserts the status gets edited with the transcript.
   - Asserts `task_manager.submit_claude` receives the transcript as prompt.
   - Asserts `_voice_tasks` contains the task id when `_synthesizer` is set.

5. **Unsupported fallback test** — in the same file, feed an `inject_message` with empty text and empty files. Assert the generic "not supported" reply is sent.

6. **Lockout test** — updated allowlist to empty. Guarantees no future direct telegram coupling in bot.py.

7. **Existing tests** unchanged:
   - [tests/test_voice_integration.py](tests/test_voice_integration.py) (currently fails in CI due to optional `openai` dep; continues to fail identically — not this spec's problem).
   - [tests/test_transcriber.py](tests/test_transcriber.py) (same — optional-dep failure).
   - Bot-level tests stay green because the port is behavior-preserving.

**No integration tests against real Telegram.** Manual smoke test at the end of Step 4: send a voice message, get a transcribed reply; trigger TTS by enabling synthesizer, verify a voice note arrives back; send a sticker, verify the generic "not supported" reply.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Spec #0a (future) | Group/team-specific voice handling (if any) — currently voice is disabled in group mode, so there's no group-voice work here |
| Spec #0c (future) | Manager bot port |
| Spec #1/#2/#3 (future) | Non-Telegram transports — they will implement `send_voice` when they ship |
| `submit_claude(chat_ref, msg_ref)` | A future refactor of `task_manager.submit_claude` to take `ChatRef`/`MessageRef` instead of ints. Not blocking for #0b — the int cast is localized. |

## 8. Risks

- **The broadened MessageHandler filter catches message types the transport doesn't know how to handle** (e.g., polls, dice). These arrive as `IncomingMessage` with empty text/files and fall into the generic unsupported reply. Correct by construction — no risk.
- **`incoming.native.reply_to_message.text` relies on `native`.** If the native payload shape drifts between python-telegram-bot versions, this enrichment silently breaks. Mitigation: wrap in `getattr(...)` chain; failure degrades to "no reply context in prompt", not a crash.
- **`_post_init` relocation changes startup timing slightly.** The original order: `delete_webhook → get_me → backfill → refresh_system_note → set_my_commands → startup ping`. New order: `delete_webhook → get_me → set_my_commands → on_ready (backfill → refresh_system_note → startup ping)`. `set_my_commands` moves before backfill. No observable impact — backfill reads persisted state, not bot commands. Acceptable.
- **`_voice_tasks` set interaction with `_synthesizer`** depends on `self._synthesizer` being set at voice-message time AND at `_finalize_claude_task` time. Unchanged by this spec.

## 9. Next steps after this spec ships

1. **Spec #0a** — group/team features port.
2. **Spec #0c** — manager bot port.
3. **Spec #1** — Web UI transport (highest user-visible payoff).
4. **Spec #2** — Discord transport.
5. **Spec #3** — Slack transport.

Each non-Telegram transport gets a `send_voice` implementation that uses its platform-specific voice/audio UX (Discord audio attachment, Slack files_upload with audio mime, Web HTML5 audio element).
