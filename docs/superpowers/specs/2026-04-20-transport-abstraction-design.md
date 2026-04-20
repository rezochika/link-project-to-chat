# Transport Abstraction — Design Spec

**Status:** Designed (2026-04-20). Not yet implemented.
**Date:** 2026-04-20
**Part of:** Tier 1 additive-transports track (Web UI / Discord / Slack). This spec is #0 of 4 — the prerequisite refactor that the three new-transport specs depend on.

---

## 1. Overview

`link-project-to-chat` is currently welded to `python-telegram-bot`. 271 telegram-API calls span 5 files, concentrated in [bot.py](src/link_project_to_chat/bot.py) (1,797 lines, 101 calls) and [manager/bot.py](src/link_project_to_chat/manager/bot.py) (1,921 lines, 150 calls). Adding Discord, Slack, or a custom web UI as *additional* transports — without replacing Telegram — requires extracting a `Transport` interface that both the existing Telegram code and new backends can implement.

This spec defines the interface shape, the migration strategy, and the exact sequence of strangler-fig steps that port the project bot behind the interface with no behavior change.

**The deliverable is the interface plus a ported `TelegramTransport`.** No new transports are implemented here; those are separate specs (#1 web, #2 discord, #3 slack).

## 2. Goals & non-goals

**Goals**
- Define a `Transport` Protocol that a future Web UI, Discord, and Slack implementation can all fit.
- Port the project bot's DM feature set (text, commands, streaming edits, inline buttons, file uploads) behind the interface without behavior change.
- Ship each strangler step as an independently-landable commit; the bot works end-to-end at every step.
- Shape the interface (not the Telegram port) to accommodate group/room semantics and the manager bot, so future specs don't require reshaping.

**Non-goals (this spec)**
- Porting [manager/bot.py](src/link_project_to_chat/manager/bot.py) — shape only; full port is a separate future spec.
- Implementing any non-Telegram transport.
- Porting voice handling (deferred to spec #0b).
- Porting the group/team feature — the `team_relay.py` hack, group routing, round counter, `/halt`, `/resume` (deferred to spec #0a).
- Feature additions of any kind. Pure refactor.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-20:

| # | Question | Decision |
|---|---|---|
| 1 | Which surfaces does the abstraction cover? | Project bot now; interface shaped for manager later |
| 2 | Does the interface model DM + group from day 1? | Yes — shape includes both; group port is deferred to a follow-up spec |
| 3 | Migration strategy? | Strangler fig — feature-by-feature port, each step independently shippable |
| 4 | Capability model? | Required floor — every Transport implements every method; no `capabilities()` negotiation |
| 5 | Scope of "done" for this spec? | MVP DM slices: text, commands, streaming edits, inline buttons, file uploads. Voice and group are follow-up specs |

## 4. Architecture

### 4.1 Interface shape

```python
# src/link_project_to_chat/transport/base.py

class Transport(Protocol):
    """A concrete chat platform. Implementations: TelegramTransport (this spec),
    WebTransport (spec #1), DiscordTransport (spec #2), SlackTransport (spec #3).
    """

    # Lifecycle
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # Outbound — called by the bot
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

    # Inbound — the bot registers handlers
    def on_message(self, handler: Callable[[IncomingMessage], Awaitable[None]]) -> None: ...
    def on_command(self, name: str, handler: Callable[[CommandInvocation], Awaitable[None]]) -> None: ...
    def on_button(self, handler: Callable[[ButtonClick], Awaitable[None]]) -> None: ...
```

Supporting primitives (full definitions in sections 4.2–4.5; the two references below are defined here for completeness):

```python
@dataclass(frozen=True)
class MessageRef:
    """Opaque reference to a sent message. Platform-native ID inside; bot code
    never inspects the fields directly — only passes the object back to
    edit_text or stores it for later button-click correlation."""
    transport_id: str
    native_id: str              # platform-native message ID as string
    chat: ChatRef               # the chat this message lives in

@dataclass(frozen=True)
class CommandInvocation:
    """A dispatched command, e.g. /help or /run ls -la."""
    chat: ChatRef
    sender: Identity
    name: str                   # without the leading slash, lowercased
    args: list[str]             # whitespace-split arguments
    raw_text: str               # the full original message (for commands that take free-form input, e.g. /run)
    message: MessageRef         # the message this command came in on, for replies
```

Rationale:
- **`Protocol` not `ABC`.** Structural typing — matches the duck-typed style already used in [stream.py](src/link_project_to_chat/stream.py). Transports don't inherit; they just match the shape.
- **Async throughout.** Matches existing handlers and every relevant transport lib (python-telegram-bot, discord.py, slack_bolt).
- **Handlers registered via callbacks, not subclass overrides.** Lets the bot remain a plain object. Consistent with how [bot.py](src/link_project_to_chat/bot.py) currently wires handlers to a `telegram.ext.Application`.
- **Inbound events are normalized types** (`IncomingMessage`, `CommandInvocation`, `ButtonClick`) — transports parse platform-native events into these before calling handlers.
- **`ChatRef` / `MessageRef` / `CommandInvocation` are opaque or semi-opaque.** Bot code reads documented fields only; platform-specific internals never leak.

Rejected alternative: event-bus style (transport emits generic events, bot subscribes with pattern-matching). More flexible, but adds indirection when the current code is already shaped around handlers.

### 4.2 Chat context model (DM + room)

```python
@dataclass(frozen=True)
class ChatRef:
    """Opaque reference to a conversation target."""
    transport_id: str           # "telegram", "discord", "web", "slack"
    native_id: str              # platform-native chat/channel ID, as string
    kind: ChatKind              # DM or ROOM

class ChatKind(Enum):
    DM = "dm"        # 1:1 user ↔ bot
    ROOM = "room"    # multi-participant; may contain peer bots

@dataclass(frozen=True)
class Identity:
    """Who sent a message. Transport-agnostic."""
    transport_id: str
    native_id: str              # platform-native user ID as string
    display_name: str           # human-readable, not stable
    handle: str | None          # @username-ish, not stable
    is_bot: bool                # true for peer bots in rooms
```

Rationale:
- **Binary `ChatKind` (DM vs ROOM).** Not a hierarchy of thread/channel/group/supergroup. Platforms differ on those subcategories; the bot only cares about "can there be peer bots here?" and "is this multi-participant?".
- **`native_id` always `str`.** Telegram uses ints, Discord uses snowflake strings, Slack uses C-prefixed strings, Web will use UUIDs. Stringifying is the common denominator.
- **`is_bot` on Identity.** The future team feature's "am I talking to a peer bot?" check is `msg.sender.is_bot and msg.sender.handle in team.bot_handles`.
- **No `Room` roster queries.** `list_members()` is a Telegram Bot API rabbit hole (incomplete lists, relay hacks). The bot doesn't need the member list; it needs mention-routing, which is per-message.

What this enables for future specs:
- Group/team port (spec #0a): `IncomingMessage` with `chat.kind == ROOM` and `sender.is_bot == true` + mention → peer handoff. Works identically across platforms.
- Web/Discord/Slack transports never have to invent their own room model.

### 4.3 Inline buttons

```python
@dataclass(frozen=True)
class Button:
    label: str                  # visible text, <= 80 chars (Discord cap)
    value: str                  # opaque callback payload, <= 64 bytes (Telegram cap)
    style: ButtonStyle = ButtonStyle.DEFAULT

class ButtonStyle(Enum):
    DEFAULT = "default"
    PRIMARY = "primary"
    DANGER = "danger"

@dataclass(frozen=True)
class Buttons:
    rows: list[list[Button]]    # max 5 rows, max 5 per row (Discord cap)

@dataclass(frozen=True)
class ButtonClick:
    chat: ChatRef
    message: MessageRef
    sender: Identity
    value: str                  # matches Button.value
```

Rationale:
- **Lowest-common-denominator caps.** 5×5 grid, 80-char labels, 64-byte values. Telegram's 64-byte callback_data is binding for `value`; Discord's 80-char label cap binds `label`.
- **`value` opaque to the transport.** Bot encodes task IDs, action names, etc. Telegram uses `callback_data`; Discord `custom_id`; Slack `action_id`+`value`.
- **Soft exemption on `style`.** Telegram has no styled buttons; `TelegramTransport` ignores `style`. This is the only cosmetic exception to the required-floor rule — no functional impact.
- **No URL buttons, no deep-link buttons.** Current [bot.py](src/link_project_to_chat/bot.py) only uses callback-style buttons. YAGNI.

Rejected: platform-native passthrough (`buttons_telegram=...`, `buttons_discord=...`) — leaks the abstraction.

### 4.4 Streaming edits

Streaming is modeled as a *caller-side* concern, not a Transport concern. Transports are dumb pipes (`send_text` / `edit_text`).

```python
# src/link_project_to_chat/transport/streaming.py

class StreamingMessage:
    """Owns one editable message and throttles updates.

    Portable across transports. The bot calls `update(new_text)` freely;
    this class handles rate-limiting (flush every N seconds), terminal
    flush on close, and max-length chunking (split into multiple messages
    when content exceeds the transport's limit).
    """
    def __init__(
        self,
        transport: Transport,
        chat: ChatRef,
        *,
        min_interval_s: float = 2.0,
        max_chars: int = 4000,
    ): ...
    async def open(self, initial_text: str) -> None: ...
    async def update(self, text: str) -> None: ...
    async def close(self, final_text: str | None = None) -> None: ...
```

Rationale:
- **Throttling lives here, not in every Transport.** One implementation; every transport behaves identically.
- **`max_chars` configurable, default 4000.** Telegram's ~4096 floor. Web/Discord/Slack can raise it; the default works everywhere.
- **Chunking on overflow.** Last message gets edited with the tail; preceding chunks sent as new messages. Replaces [formatting.py](src/link_project_to_chat/formatting.py) chunking logic at the call sites (the formatting module itself can stay as a helper).
- **Server-side rate-limit (e.g., Telegram 429) handled internally** with backoff. Bot never sees it.

Rejected:
- `Transport.open_stream()` returning a handle — forces every transport to reimplement throttling.
- Protocol-level streaming (SSE-style) — only web-native; not portable.

### 4.5 File uploads

```python
@dataclass(frozen=True)
class IncomingFile:
    """An attachment on an incoming message. Already downloaded to local disk."""
    path: Path                  # local path, cleaned up when the IncomingMessage handler returns
    original_name: str
    mime_type: str | None
    size_bytes: int

@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]   # empty list if none
    reply_to: MessageRef | None
    native: Any                 # escape hatch — platform raw object, debugging only

class Transport(Protocol):
    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef: ...
```

Rationale:
- **Transports eagerly download incoming files to local disk**, hand the bot a `Path`. Normalizes across Telegram's file-download API, Discord CDN URLs, Slack private URLs, Web multipart uploads.
- **Unified `IncomingFile`, mime-type discriminator** instead of separate photo/document/voice types. Bot branches on `mime_type` if it needs to.
- **Voice is `IncomingFile` with `mime_type` starting `audio/`.** This is the bridge that cleanly defers voice to spec #0b: file plumbing lands in this spec, transcription-call wiring lands later.
- **Temp-file lifecycle is the Transport's problem.** `tempfile.TemporaryDirectory` scoped to the handler invocation; auto-cleanup on exit. Fixes the open issue in [where-are-we.md](where-are-we.md) about temp files stored permanently.
- **Outbound `send_file` takes `Path`, not a file handle.** Avoids the "handle not closed" bug in current `_send_image`.
- **`native: Any` escape hatch.** For logging/debugging only; usage in production code is a smell.

Rejected:
- Lazy download (hand the bot a URL) — URL expiry race on Slack/Discord.
- Split incoming types (`IncomingPhoto`/`IncomingDocument`/`IncomingVoice`) — forces every transport to decide the category; forces bot code to handle three shapes.

## 5. Migration — strangler step sequence

Nine steps. Each is independently landable; the bot works end-to-end at every step. No feature flags, no dual code paths.

### Step 1 — Types and Protocol (no implementation)

**File:** `src/link_project_to_chat/transport/base.py`
**Content:** `Transport` Protocol, `ChatRef`, `ChatKind`, `Identity`, `MessageRef`, `Buttons`, `Button`, `ButtonStyle`, `ButtonClick`, `IncomingMessage`, `IncomingFile`, `CommandInvocation` — dataclasses and Protocol only, no bodies.
**Wired:** nothing consumes it yet.
**Exit criteria:** `mypy` / `pyright` passes on the new module in isolation.

### Step 2 — `TelegramTransport` skeleton

**File:** `src/link_project_to_chat/transport/telegram.py`
**Content:** Class implementing the `Transport` Protocol. All methods raise `NotImplementedError`. Constructor accepts the telegram `Application` object (or equivalent) and stores it.
**Proves:** Telegram's native types (chat ID int, user ID int, Message, Update) can be mapped onto `ChatRef`/`Identity`/`MessageRef` via helper functions (also in this file).
**Wired:** still nothing consumes it.

### Step 3 — Port `send_text` + inbound message

This is the one step with two sub-parts. Both have to land together so the bot stays functional.

- Implement `TelegramTransport.send_text`, `TelegramTransport.start`, `TelegramTransport.stop`.
- Implement the inbound-message path: telegram's `MessageHandler` calls a helper that builds `IncomingMessage` and dispatches to the registered `on_message` handler.
- In [bot.py](src/link_project_to_chat/bot.py):
  - **Outbound:** route the unsupported-message-type reply (voice/sticker/video/location/contact/audio — the handler that currently says "I don't support that") through `transport.send_text`. This is a low-stakes path to prove the outbound adapter.
  - **Inbound:** register `on_message` for the main text handler; the handler body is unchanged, it just reads fields off `IncomingMessage` instead of off `telegram.Update`. All existing message logic continues to work through the new inbound path.
- Leave all other outbound send paths (streaming edits, button menus, file sends, Claude replies) using raw telegram calls for now — later steps port them.

**Exit criteria:** bot works end-to-end with *no behavior change*. Existing `tests/` pass unchanged. Manual smoke: send a sticker → get the unsupported-type reply via `transport.send_text`. Send a regular text message → the main handler fires via `on_message` and replies normally.

### Step 4 — Port three simple commands

Port `/help`, `/version`, `/status` through `transport.on_command`. These three have no state, no side effects beyond replies — they prove the command path without tangling in complex features.

**Exit criteria:** all three commands return identical output to their pre-port versions.

### Step 5 — Port remaining commands

Mechanical port: `/model`, `/effort`, `/permissions`, `/reset`, `/tasks`, `/persona`, `/skills`, `/compact`, `/run`, `/thinking`, `/voice`, `/use`, `/stop_skill`, `/create_skill`, `/delete_skill`, `/stop_persona`, `/create_persona`, `/delete_persona`. No design unknowns at this point.

**Exit criteria:** all commands work identically to pre-port behavior.

### Step 6 — `StreamingMessage` + port Claude streaming

- Create `src/link_project_to_chat/transport/streaming.py` with `StreamingMessage` class.
- Extract streaming logic from [bot.py](src/link_project_to_chat/bot.py) into `StreamingMessage` in a pure-refactor commit first (still using telegram objects under the hood).
- Then swap `StreamingMessage` to consume `Transport` instead of a telegram `Application`.
- Bot's Claude-chat path constructs `StreamingMessage(transport, chat_ref, ...)`.

**Exit criteria:** chatting with Claude shows the same 2s-throttled edits; long responses chunk identically.

### Step 7 — Port inline buttons

- `TelegramTransport.send_text` / `edit_text` accept `buttons: Buttons | None`; convert to `InlineKeyboardMarkup`.
- `TelegramTransport` fires `on_button` with a `ButtonClick`.
- Bot: `/tasks`, `/reset` confirmation, persona picker, skill picker converted to `Buttons(...)`.
- The existing `callback_data` → action mapping is preserved; values still encode task IDs, action names, etc.

**Exit criteria:** every button in the current UI clicks correctly.

### Step 8 — Port file uploads

- `TelegramTransport` populates `IncomingMessage.files`, manages per-handler temp-dir lifecycle.
- `TelegramTransport.send_file` implemented.
- Bot: the photo and document handlers collapse into one `IncomingMessage.files` loop. `_send_image` (outbound Claude-screenshot path) becomes `transport.send_file`.

**Exit criteria:** upload a photo, upload a document, receive a Claude tool-use screenshot. All behave identically to pre-port.

### Step 9 — Lockout

Grep for `from telegram` and `import telegram` in [bot.py](src/link_project_to_chat/bot.py) — must be zero matches. The only module importing the telegram library is `src/link_project_to_chat/transport/telegram.py`. Delete any now-dead helper code in `bot.py`.

**Exit criteria:** the grep test passes. Spec #0 is done.

## 6. Testing approach

`FakeTransport` is the primary test vehicle. The new `transport/` package owns its own tests; existing tests for [formatting.py](src/link_project_to_chat/formatting.py), [stream.py](src/link_project_to_chat/stream.py), [task_manager.py](src/link_project_to_chat/task_manager.py), [transcriber.py](src/link_project_to_chat/transcriber.py), and [_auth.py](src/link_project_to_chat/_auth.py) stay untouched — they never imported telegram, and they test seams below the transport line.

```python
# src/link_project_to_chat/transport/fake.py

class FakeTransport:
    """In-memory Transport for tests. Implements the full Protocol.

    Public test API:
      sent_messages: list[SentMessage]        # captured send_text calls
      edited_messages: list[EditedMessage]    # captured edit_text calls
      sent_files: list[SentFile]              # captured send_file calls

      inject_message(chat, sender, text, files=[])   # simulate inbound text
      inject_command(name, args, sender)             # simulate inbound command
      inject_button_click(msg_ref, sender, value)    # simulate button press

    Handlers registered via on_message/on_command/on_button are invoked
    synchronously when inject_* is called. Tests can assert state after a
    single await without timer-settling hacks.
    """
```

**Contract test — single parametrized module.** One test module, `tests/transport/test_contract.py`, verifies the `Transport` Protocol's observable behavior. Parametrized over `[FakeTransport, TelegramTransport]` initially; new transports (Web/Discord/Slack) get added to the parameter list as their specs ship. For `TelegramTransport`, the parametrization fixture wires in `python-telegram-bot`'s own test utilities (specific tool — `Application.test_mode`, `pytest-asyncio` fixtures, or a local hand-rolled stub — chosen at implementation time based on library version). No separate "adapter test" file; one test body runs against all transports. This is how "required floor" (decision Q4) is enforced mechanically.

**What the contract test covers:** `send_text` → `MessageRef` is returned and usable in a subsequent `edit_text`; `edit_text` on a fresh `MessageRef` succeeds; `on_message` fires for injected inbound text; `on_command` fires for a `/cmd`; `on_button` fires for a button click with matching `value`; `send_file` accepts a Path and returns a `MessageRef`; buttons with `style != DEFAULT` do not error on Telegram (they're silently downgraded).

**No mocking of python-telegram-bot.** Mocks of `Update` / `Message` / `Bot` produce confidence-free tests (noted in `superpowers:test-driven-development`). `FakeTransport` is a real object implementing the real interface — that's the test double.

**No integration tests against real Telegram in CI.** Secrets, flakiness, rate limits. Manual smoke test at the end of each strangler step is sufficient.

Example post-port bot test:

```python
async def test_help_command_replies_with_command_list():
    transport = FakeTransport()
    bot = ProjectBot(transport, config=...)
    await bot.start()

    transport.inject_command("help", args=[], sender=alice)

    assert len(transport.sent_messages) == 1
    assert "/help" in transport.sent_messages[0].text
```

No telegram imports. No sleeps. No mocks.

## 7. Explicit out-of-scope

The following belong to separate specs and must NOT land as part of spec #0:

| Belongs to | Item |
|---|---|
| Spec #0a (future) | Port group/team features — [team_relay.py](src/link_project_to_chat/manager/team_relay.py), [telegram_group.py](src/link_project_to_chat/manager/telegram_group.py), [group_filters.py](src/link_project_to_chat/group_filters.py), [group_state.py](src/link_project_to_chat/group_state.py), round-counter, `/halt`, `/resume`, peer-mention routing |
| Spec #0b (future) | Port voice handling — [transcriber.py](src/link_project_to_chat/transcriber.py) wiring, voice-message routing |
| Spec #0c (future) | Port [manager/bot.py](src/link_project_to_chat/manager/bot.py) |
| Spec #1 (future) | Web UI transport |
| Spec #2 (future) | Discord transport |
| Spec #3 (future) | Slack transport |

The interface defined in this spec *accommodates* all of the above (see section 4.2 on `ChatKind.ROOM` and `Identity.is_bot`) without implementing them.

## 8. Risks

- **Streaming step (step 6) is the validation moment.** If the `Transport` interface is wrong, it shows up here. Steps 1–5 are easy enough to finish before discovering a flaw. Mitigation: plan a rollback checkpoint after step 6; if streaming doesn't fit cleanly, revisit the interface before continuing.
- **python-telegram-bot version coupling.** Current code uses a specific version. If the port accidentally relies on a deprecated API path, the Telegram adapter's blast radius is isolated to `transport/telegram.py` — no worse than today's situation, and easier to fix in one file than across a 1,797-line bot.
- **FakeTransport drift.** If `FakeTransport` silently diverges from the Protocol, bot-level tests pass but real Telegram breaks. Mitigation: the parametrized contract test in section 6 runs the same scenarios against both — drift fails a test.

## 9. Next steps after this spec ships

1. Spec #0a — Port group/team features to the Transport interface.
2. Spec #0b — Port voice handling.
3. Spec #1 — Web UI transport (the one that actually solves the original bot-to-bot pain).
4. Spec #2 — Discord transport.
5. Spec #3 — Slack transport.
6. Spec #0c — Port `manager/bot.py`.

Order of #0a/#0b/#1 is negotiable; #1 is the highest user-visible payoff.
