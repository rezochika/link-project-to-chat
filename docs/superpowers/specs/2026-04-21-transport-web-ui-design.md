# Transport - Web UI + Conversation Primitive - Design Spec

**Status:** Shipped 2026-04-25 (commits `6c12b39`..`d24ef52`); review-fix landed same day (commits `77abcff`..`7b73b8d`). See [docs/TODO.md §1.3](../../TODO.md#13-new-transport-platforms-designed-not-implemented) for current status and follow-ups.
**Date:** 2026-04-21
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a), [2026-04-21-transport-manager-port-design.md](2026-04-21-transport-manager-port-design.md) (spec #0c)
**Part of:** First additive non-Telegram transport. This is spec #1 of 3 in the Web UI / Discord / Slack track and introduces the shared Conversation/Wizard primitive the later specs depend on.

---

## 1. Overview

Specs #0, #0b, #0a, and #0c port the project bot and manager bot behind `Transport`, but the codebase is still Telegram-shaped in two important places:

1. The manager's multi-step flows still depend on `ConversationHandler`, `Update`, and PTB-owned `ctx.user_data`.
2. Team/group routing still assumes Telegram-style `@handle` mentions and Telegram-only peer identity config (`bot_username`, `group_chat_id`).

Web UI is the first real non-Telegram transport, so this is the right point to fix both. A browser UI is the most demanding cross-platform case for wizard design: it wants explicit prompts, forms, validation, and live updates, not a hidden message-state machine. At the same time, Discord and Slack will need stable native IDs and structured mentions rather than Telegram usernames.

**The deliverable:** a local-first `WebTransport`, a shared prompt/conversation primitive, and the shared identity/mention/config changes needed so Web, Discord, and Slack all fit the same model without copying Telegram's PTB bridge patterns.

## 2. Goals & non-goals

**Goals**
- Ship the first production non-Telegram `Transport` implementation: `WebTransport`.
- Introduce a portable prompt/conversation primitive that can express the manager's current wizards without `ConversationHandler`.
- Add structured mentions to inbound transport events so group/team routing no longer depends on regex-parsing raw `@handle` text.
- Replace Telegram-only team peer bindings (`bot_username`, `group_chat_id`) with transport-agnostic config types built around stable native IDs.
- Validate the native bot-to-bot path from spec #0a: `incoming.is_relayed_bot_to_bot == False` and `incoming.sender.is_bot == True`.
- Keep the current message/button/file/voice primitives intact; this spec adds to the transport model rather than replacing it.

**Non-goals (this spec)**
- A composite "one bot process serves Telegram + Web + Discord + Slack at once" runtime. This track is additive at the codebase/product level, not a multi-homed single-process transport fan-out.
- Rewriting Telegram manager flows in the same implementation slice. The new primitive is introduced here; a small Telegram follow-up can consume it afterward and delete the PTB shim from spec #0c.
- A SPA or frontend framework-heavy rewrite. Server-rendered HTML plus incremental updates is enough.
- Public internet exposure, OAuth, SSO, or multi-tenant hosted deployment. Local/private operation is the default shape.
- Re-abstracting every credential field in config. This spec only generalizes the fields that block non-Telegram team and room routing.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-21:

| # | Question | Decision |
|---|---|---|
| 1 | How do we replace `ConversationHandler` without over-designing? | Add a small prompt/session primitive to `Transport`, not a generic workflow DSL and not a framework-native escape hatch |
| 2 | What shape should the prompt primitive have? | Single logical input per step (`text`, `secret`, `choice`, `confirm`, or no-input display), which maps cleanly to Web forms now and Telegram/Discord/Slack later |
| 3 | How do we stop future transports depending on Telegram `@username` parsing? | Add `IncomingMessage.mentions: list[Identity]` and move team-peer matching to stable native IDs with handle/display-name as hints only |
| 4 | How is Web UI hosted? | Shared local ASGI service (`FastAPI` + `Jinja2` + `HTMX` + SSE) backed by a lightweight SQLite store/event queue |
| 5 | Where does conversation state live? | In app-owned `ConversationSession` state keyed by `(chat, sender, flow)`, not in transport-native `ctx.user_data` / `native` |

## 4. Architecture

### 4.1 Shared transport additions

This spec adds two cross-platform concepts to the transport layer: structured mentions and prompts.

```python
# src/link_project_to_chat/transport/base.py

@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    native: Any = None
    is_relayed_bot_to_bot: bool = False
    mentions: list[Identity] = field(default_factory=list)   # NEW


class PromptKind(Enum):
    DISPLAY = "display"   # buttons only, no text input
    TEXT = "text"
    SECRET = "secret"
    CHOICE = "choice"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class PromptOption:
    value: str
    label: str
    description: str | None = None
    style: ButtonStyle = ButtonStyle.DEFAULT


@dataclass(frozen=True)
class PromptSpec:
    key: str
    title: str
    body: str
    kind: PromptKind
    placeholder: str = ""
    initial_text: str = ""
    submit_label: str = "Continue"
    allow_cancel: bool = True
    options: list[PromptOption] = field(default_factory=list)


@dataclass(frozen=True)
class PromptRef:
    transport_id: str
    native_id: str
    chat: ChatRef
    key: str


@dataclass(frozen=True)
class PromptSubmission:
    chat: ChatRef
    sender: Identity
    prompt: PromptRef
    text: str | None = None
    option: str | None = None
    native: Any = None


class Transport(Protocol):
    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef: ...

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None: ...
    async def close_prompt(
        self,
        prompt: PromptRef,
        *,
        final_text: str | None = None,
    ) -> None: ...

    def on_prompt_submit(
        self,
        handler: Callable[[PromptSubmission], Awaitable[None]],
    ) -> None: ...
```

Rationale:
- `mentions` fixes the biggest remaining Telegram leak from spec #0a. Discord and Slack mentions are structured, not plain `@handle` text. Web UI can expose explicit user/bot mentions too.
- Prompt steps are intentionally narrow: one logical answer per step. That is enough for the current manager flows and stays portable to Telegram follow-up work.
- `PromptSpec` is not a form-builder DSL. It describes what the transport must render next, not the whole wizard graph.
- Prompt lifecycle (`open/update/close`) keeps transient UI state on the transport side while app state lives above it.

### 4.2 Conversation runtime above transport

The prompt primitive replaces PTB `ConversationHandler` for new transports, but we still keep flow state above the transport:

```python
# src/link_project_to_chat/manager/conversation.py

@dataclass
class ConversationSession:
    flow: str
    chat: ChatRef
    sender: Identity
    prompt: PromptRef | None
    state: dict[str, Any]
```

Rules:
- Key sessions by `(flow, chat.transport_id, chat.native_id, sender.native_id)`.
- Store wizard data in `ConversationSession.state`, not `native`, not transport-owned session bags.
- Manager and future project-side flows express transitions as pure Python functions that consume `PromptSubmission` and emit the next `PromptSpec` or completion.
- Telegram follow-up can later map these same prompt steps onto normal messages/buttons instead of `ConversationHandler`.

This is the architectural line we want after spec #1:
- `Transport` owns UI/event translation.
- App code owns business state and flow transitions.

### 4.3 Structured mentions and transport-agnostic peer identity

Spec #0a's Telegram-shaped team routing used raw mention parsing and `bot_username`. That does not generalize to Discord's `<@123>`, Slack's `<@U123>`, or Web's structured people-picker UI.

This spec introduces two shared config concepts:

```python
@dataclass(frozen=True)
class BotPeerRef:
    transport_id: str
    native_id: str
    handle: str | None = None
    display_name: str = ""


@dataclass(frozen=True)
class RoomBinding:
    transport_id: str
    native_id: str
    kind: ChatKind = ChatKind.ROOM
```

Config migration:
- `TeamConfig.group_chat_id: int` -> `room: RoomBinding | None`
- `TeamBotConfig.bot_username: str` -> `bot_peer: BotPeerRef | None`
- Telegram loaders/writers keep backward-compatible read support for one version window, then write only the new structure.

Bot/group logic changes after this spec:
- Team-peer matching is by `(transport_id, native_id)` first.
- `handle` remains useful for human-readable logs and best-effort fallback, but no longer drives correctness.
- `group_filters` prefers `IncomingMessage.mentions` over regex-parsed text and falls back to text parsing only for old Telegram-shaped paths until Telegram is updated.

### 4.4 Web transport architecture

`WebTransport` is backed by a shared local web service rather than each bot opening its own random port:

- **Backend:** `FastAPI` ASGI app
- **Rendering:** server-rendered `Jinja2` templates plus `HTMX` for incremental actions
- **Live updates:** SSE for message streams and prompt refreshes
- **Persistence / queue:** SQLite store for chats, messages, prompts, uploads, and inbound user events

Suggested package shape:

```text
src/link_project_to_chat/web/
  app.py          # FastAPI app + route wiring
  store.py        # SQLite read/write helpers
  transport.py    # WebTransport implementation
  templates/
  static/
```

Why a shared service:
- Manager, project bots, and team bots are separate processes today.
- A single web service avoids one-port-per-bot sprawl and gives team rooms a natural shared backing store.
- Native bot-to-bot delivery becomes straightforward: bot messages are just rows/events in the same room stream with `sender.is_bot=True`.

### 4.5 Web UX mapping

Mapping from transport primitives to Web UI:

- `send_text` -> timeline message card
- `edit_text` -> card replacement in-place over SSE
- `send_file` -> attachment card with download link / image preview
- `send_voice` -> audio player attachment
- `send_typing` -> transient "bot is typing" indicator row
- `Buttons` -> action row of HTML buttons/posts
- `PromptSpec(TEXT/SECRET)` -> form card with one input
- `PromptSpec(CHOICE/CONFIRM)` -> button group or select
- `PromptSpec(DISPLAY)` -> informational card with actions only
- `reply_to` -> quoted-message block above the new message
- `html=True` -> sanitized HTML subset rendered to safe markup

DM vs room model:
- `ChatKind.DM`: one user + one bot timeline
- `ChatKind.ROOM`: shared room timeline with humans and bots

Commands:
- Browser composer accepts slash-style entries (for example `/help`, `/projects`, `/lp2c projects`).
- The service parses these into `CommandInvocation` before handing them to `WebTransport` listeners.
- Non-command text becomes `IncomingMessage`.

### 4.6 Why Web should set the shared abstraction, not Telegram

Telegram's current manager implementation proves the transport port is workable, but its PTB bridge is explicitly temporary:
- it tunnels framework state through `native=(update, ctx)`
- it reaches through private `_dispatch_*` helpers
- it keeps wizard state in `ctx.user_data`

Web is the better forcing function because:
- forms and validation must be explicit
- buttons and prompt state must survive page refresh and reconnect
- native bot-to-bot delivery is direct, so no relay-shaped compromise is needed

If the abstraction feels clean in Web first, Discord and Slack will fit. If it is shaped around PTB internals, they will not.

## 5. Migration - implementation sequence

Eight steps. Each independently landable.

### Step 1 - Add prompt and mention primitives to `transport/base.py`

Add `IncomingMessage.mentions`, `PromptKind`, `PromptSpec`, `PromptOption`, `PromptRef`, `PromptSubmission`, and the `open_prompt/update_prompt/close_prompt/on_prompt_submit` methods. Extend `FakeTransport` to support them.

### Step 2 - Introduce transport-agnostic room and peer bindings

Add `RoomBinding` and `BotPeerRef` config types. Load old Telegram fields with backward compatibility; write new fields. Update group/team code to match peers by native ID first.

### Step 3 - Update group filters to prefer structured mentions

Port `group_filters.py` so "message is directed at bot X" prefers `incoming.mentions` and only falls back to raw text parsing when mention metadata is absent.

### Step 4 - Build the shared Web service/store skeleton

Add the ASGI app, SQLite store, template scaffolding, and SSE/event plumbing. No bot logic yet; just enough to render timelines and post inbound events.

### Step 5 - Implement `WebTransport`

Map outbound primitives into store writes and inbound user actions into normalized `IncomingMessage`, `CommandInvocation`, `ButtonClick`, and `PromptSubmission`.

### Step 6 - Run a project bot on Web

Validate text, files, voice, streaming, buttons, and room delivery on `WebTransport`. Team-bot rooms must use native `sender.is_bot=True` with no relay path.

### Step 7 - Run manager flows on Web using prompt sessions

Implement the manager's wizards against the new conversation runtime and `PromptSpec`. This is the proof that the new primitive is sufficient.

### Step 8 - Follow-up: consume the same primitive from Telegram

Out of scope for the first implementation slice, but expected immediately after Web proves the shape. This step removes the spec #0c PTB shim and shrinks the manager lockout allowlist toward zero.

## 6. Testing approach

- Extend `tests/transport/test_contract.py` to cover prompt lifecycle and structured mentions.
- Extend `FakeTransport` so manager/project logic can be tested without a browser.
- Add Web transport contract tests for:
  - text / file / voice / typing / edit operations
  - button clicks
  - prompt open -> submit -> close flow
  - SSE update fan-out
- Add team-room tests with two web-backed bot processes in one room verifying native bot-to-bot delivery (`sender.is_bot=True`, `is_relayed_bot_to_bot=False`).
- Add manager wizard tests that drive `PromptSubmission` directly instead of PTB `Update` objects.
- Add a small HTTP-level smoke suite for the ASGI app using `httpx.AsyncClient` or `FastAPI` test utilities. Full browser automation is optional.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Follow-up after #1 | Replacing Telegram manager `ConversationHandler` with the new prompt runtime |
| Future | Composite/multi-home transport runtime |
| Future | Public deployment, OAuth, SSO, hosted multi-user productization |
| Future | Rich multi-field forms in one prompt step; current prompt model is intentionally one logical answer per step |
| Future | Transport-agnostic credential abstraction for every bot token / app secret field |

## 8. Risks

- **Prompt primitive too small:** if real Web flows immediately need multi-field atomic submit, the one-step model may feel cramped. Mitigation: keep `PromptSpec` narrow for v1 and expand only if a concrete flow truly needs it.
- **Prompt primitive too large:** a form-builder DSL would recreate framework complexity inside `Transport`. Mitigation: keep rendering concerns declarative and business transitions in app code.
- **Shared SQLite store contention:** manager plus many project/team bots may stress write contention. Mitigation: append-mostly event tables, short transactions, and a clean path to swap the store later if needed.
- **Mention migration churn:** old Telegram-shaped team config and regex-based group filters will coexist briefly. Mitigation: write a clear one-version migration path and prefer ID-based matching as soon as possible.
- **Web service becomes a hidden platform layer:** if too much app logic moves into FastAPI routes, `WebTransport` stops being a transport and becomes a parallel product. Mitigation: routes only translate HTTP <-> normalized transport events.

## 9. Next steps after this spec ships

1. **Follow-up to spec #0c:** port Telegram manager flows from `ConversationHandler` to the new prompt runtime and empty the residual lockout allowlist.
2. **Spec #2 - Discord transport:** reuse `PromptSpec`, `IncomingMessage.mentions`, `BotPeerRef`, and `RoomBinding`; do not invent new conversation or peer-routing shapes.
3. **Spec #3 - Slack transport:** same rule as Discord; consume the shared abstractions rather than adding Slack-specific flow state.
