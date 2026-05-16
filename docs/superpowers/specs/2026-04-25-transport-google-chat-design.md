# Transport - Google Chat - Design Spec

**Status:** Ready for implementation planning after the contract/config cleanup in Step 0. Designed 2026-04-25; refreshed 2026-05-16 against current repo state and current Google Chat docs; refined 2026-05-17 to address contract-amendment blockers (`MessageRef.native`, `PromptSubmission.text`/`option`, `BotPeerRef` persistence, explicit transport selection), pin callback-token/fast-ack/threading decisions, and mark §3.1 prerequisites satisfied; refined again 2026-05-17 (v2) to add typed Google native metadata, non-Telegram team-room validity, UTF-8 byte-limit handling, outbound `requestId` idempotency, and `edit_text` no-create-on-missing semantics.

**Date:** 2026-04-25; refreshed 2026-05-16; refined 2026-05-17; refined v2 2026-05-17.

**Depends on:**
- `2026-04-20-transport-abstraction-design.md` (spec #0)
- `2026-04-20-transport-voice-port-design.md` (spec #0b)
- `2026-04-21-transport-group-team-port-design.md` (spec #0a)
- `2026-04-21-transport-manager-port-design.md` (spec #0c)
- `2026-04-21-transport-web-ui-design.md` (spec #1)

**Part of:** Second non-Telegram transport to actually ship code, after Web (spec #1). Discord (spec #2) and Slack (spec #3) are designed but unimplemented as of 2026-05-17; this spec does not block on them. Google Chat validates HTTPS event delivery, asynchronous Chat API posting, Cards v2, dialogs, and thread-aware message handling for the first time in the codebase.

---

## Refinement summary

This refinement keeps the original architecture but fixes implementation blockers identified during 2026-05-17 review against `main`:

1. `MessageRef` currently has no `native` field, but the Google Chat design needs native thread/message metadata. This spec now makes that an explicit Step 0 transport-contract amendment.
2. `PromptSubmission` uses `text` and `option`, not `value`. Prompt handling is updated accordingly.
3. Callback-token wording is corrected: per-process HMAC tokens do **not** survive process restarts; restart-invalidation is intentional.
4. Google Chat startup is made explicit in CLI and `ProjectBot.build()` so `--transport google_chat` cannot accidentally fall through to Telegram. Verified: `bot.py:3349-3351` currently has the silent `else: TelegramTransport` fall-through.
5. `GoogleChatConfig` load/save/validation helpers are specified because current config serialization is hand-written.
6. Team `BotPeerRef` persistence is called out explicitly. Verified: `config.py:_parse_team_bot` ignores `bot_peer`; `_serialize_team_bot` never writes it; only Telegram synthesis from `bot_username` works today.
7. Google Chat request verification now follows the current docs: endpoint-URL audience verifies a Google-signed OIDC token whose email is `chat@system.gserviceaccount.com`; project-number audience verifies a self-signed JWT issued by `chat@system.gserviceaccount.com`.
8. Threading and `StreamingMessage` overflow rotation are made implementable by carrying native metadata on `MessageRef`.
9. Attachment handling is pinned to `attachmentDataRef` / media API paths; `downloadUri` and `thumbnailUri` are not used by the app to fetch content.
10. Shared transport contract tests must include Google Chat through fixture/injection helpers.
11. Fast-acknowledgement default body is locked to HTTP 200 `{}` for non-dialog events; a user-visible "Working…" message is opt-in per command to avoid stranded transient messages that later stream edits cannot cleanly replace.
12. §3.1 prerequisites are marked satisfied (spec #0a shipped in v0.15.0, spec #0c in v0.16.0, spec #1 in main); Steps 10 and 11 in §6 no longer gate on those ports.

v2 (2026-05-17) additions:

13. **Typed Google Chat native metadata.** A `GoogleChatMessageNative` TypedDict pins the key set so `send_text`, `edit_text`, streaming overflow rotation, and tests do not drift on dict keys.
14. **Non-Telegram team-room validity.** Team config parsing currently requires both `path` AND legacy `group_chat_id` (verified at `config.py:712`), which silently drops `room`-only Google Chat team configs as malformed and cleans them up on disk. Step 0 widens the validity rule to `path` AND (`group_chat_id` OR structured `room`).
15. **UTF-8 byte-limit handling.** Google Chat enforces UTF-8 byte limits while shared helpers count characters; `GoogleChatConfig` gains `max_message_bytes` and the transport validates rendered UTF-8 byte length before each REST call.
16. **Outbound create idempotency.** `spaces.messages.create` accepts `requestId` to deduplicate retried creates. The client surfaces the parameter and the chosen ID is stored in `MessageRef.native`.
17. **`edit_text` no-create-on-missing semantics.** Update/patch calls require an explicit `updateMask` and must not silently create a new message; if the API cannot update the target, `GoogleChatTransport` degrades inside the transport to final-only or a clearly marked replacement.

`MessageRef.native` is additionally specified as `field(default=None, compare=False, hash=False, repr=False)` so that the field can carry an unhashable dict (`GoogleChatMessageNative`) without breaking equality or hashing for any future code that puts `MessageRef` into a set or dict. Current `src` has no such use; this is forward-looking defense.

---

## Current repo assumptions

- `Transport`, `ChatRef`, `MessageRef`, `Identity`, `IncomingMessage`, `CommandInvocation`, `ButtonClick`, `PromptSpec`, and `PromptSubmission` are the bot-facing integration surface.
- `AllowedUser` is the sole application-auth source. Transports provide stable `Identity.native_id` values and call `set_authorizer` before expensive dispatch work.
- `RoomBinding` is transport-agnostic and can represent `transport_id="google_chat"`, `native_id="spaces/..."`.
- `BotPeerRef` is transport-agnostic as a dataclass, but Google Chat implementation must add load/save support for persisted non-Telegram peer refs (Step 0).
- `ProjectBot` and `ManagerBot` are transport-portable. No Google payload type may leak above `GoogleChatTransport`.
- Safety prompt, recent discussion, `meta_dir`, plugin context, conversation history, hot-reload, and backend state are existing bot/backend features. `GoogleChatTransport` must preserve them by producing correct transport primitives; it must not reimplement them.
- The current `MessageRef` dataclass does **not** carry `native` metadata. This spec requires a small contract amendment in Step 0.

---

## 1. Overview

Google Chat is a strong next transport because it differs from Telegram and Web in a key way: interactive Chat apps are normally invoked through Google Workspace interaction events. For an HTTP implementation, Google sends interaction events to a configured HTTPS endpoint, and long-running work should continue asynchronously through the Google Chat API rather than blocking the request handler.

That means Google Chat should not be a separate plugin API, a `TelegramBot` parallel, or a Google-only business-logic layer. It should be a first-class `Transport` implementation:

- incoming Chat interaction events normalize into `IncomingMessage`, `CommandInvocation`, `ButtonClick`, and `PromptSubmission`
- outgoing messages, edits, files, Cards v2, and dialogs stay behind `Transport`
- project bot and manager bot logic stay unchanged
- Cards v2 and dialogs become the platform rendering of existing `Buttons` and `PromptSpec`

**Deliverable:** `GoogleChatTransport`, backed by a small ASGI HTTP receiver and Google Chat REST client, with async posting for backend streams and card/dialog support for buttons and prompts.

---

## 2. Goals and non-goals

### Goals

- Implement `GoogleChatTransport` using Google Chat app interaction events delivered to an HTTPS endpoint.
- Support DM and room semantics for project, manager, and team bot flows.
- Map existing transport primitives onto Google Chat messages, Cards v2, dialogs, and action callbacks.
- Use stable Google Chat resource names/IDs for users, spaces, messages, threads, and app identity.
- Preserve the transport contract: commands, text, edits where supported, buttons, files where supported, voice-as-file fallback, typing/no-op typing, and prompts.
- Acknowledge HTTP interaction events quickly and continue long backend work through asynchronous Chat API messages/updates.
- Keep bot logic platform-neutral; Google payloads stay inside the transport boundary.
- Add deterministic tests for request verification, fast acknowledgement, event normalization, callback integrity, prompt submission, idempotency, thread metadata, and file fallback behavior.

### Non-goals

- A new platform-agnostic plugin API. The existing `Transport` protocol remains the integration point.
- Incoming webhooks as the primary integration. Incoming webhooks can post messages but are not the interactive Chat app surface.
- Marketplace publication, polished installation UX, domain-wide admin automation, or multi-domain federation.
- Google Workspace add-on conversion.
- Rich multi-field app workflows beyond the existing one-step `PromptSpec` model.
- Live audio/meeting participation. `send_voice` only needs file/text degradation.
- Full attachment parity with Telegram.

---

## 3. Decisions driving this design

| # | Question | Decision |
|---|---|---|
| 1 | Adapter/plugin or transport? | Implement `GoogleChatTransport` under the existing `Transport` protocol. |
| 2 | Local event delivery? | HTTP endpoint plus tunnel for v1; leave Pub/Sub as a future option. |
| 3 | Command mapping? | Register one root slash command, `/lp2c`, and parse its text/argument payload into internal command name + args. |
| 4 | Prompt mapping? | `TEXT`/`SECRET` -> dialog text input where interaction context is available; otherwise reply fallback. `CHOICE`/`CONFIRM` -> Cards v2 buttons or selection input. `DISPLAY` -> informational card. |
| 5 | Long backend responses? | Return a fast acknowledgement, then post/edit asynchronously through Google Chat API. |
| 6 | Identity source of truth? | Stable Google Chat resource names/native IDs, not display names or emails. |
| 7 | Callback integrity? | HMAC-signed callback envelope with a per-process secret and TTL. Restart invalidates old cards (intentional trade-off). |
| 8 | Threading? | Carry thread metadata through `MessageRef.native`; do not model threads as a new `ChatKind` in v1. |
| 9 | Fast-ack default body? | HTTP 200 `{}` for `MESSAGE`/`APP_COMMAND`/normal `CARD_CLICKED`. Dialog events return required inline dialog actions. "Working…" message is opt-in per command. |
| 10 | Edit semantics? | `edit_text` edits only an app-created message via explicit `updateMask`. It must not silently create a new message; degrade inside the transport if update is unsupported. |
| 11 | Outbound create idempotency? | Use Chat API `requestId` where currently supported to avoid duplicate creates on retry; store the chosen ID in `MessageRef.native["request_id"]`. |
| 12 | Message-size handling? | Treat Google Chat limits as UTF-8 byte limits; keep `max_text_length` conservative and validate `len(rendered.encode("utf-8"))` before REST calls. |

### 3.1 Sequencing prerequisites

All sequencing prerequisites are satisfied as of 2026-05-17 (`main`):

- ✅ spec #0a group/team transport port — shipped in v0.15.0, so team room routing uses `RoomBinding`/`BotPeerRef` rather than Telegram-only group IDs
- ✅ spec #0c manager transport port — shipped in v0.16.0, so manager flows run through transport primitives
- ✅ at least one HTTP-receiver sibling — Web transport (spec #1) shipped, so Google Chat can reuse the repo's proven ASGI lifecycle, dispatch queue, and test patterns

Implementation may proceed once Step 0 lands. Steps 10 and 11 in §6 reference these prerequisites for context only; no remaining gate.

Discord (spec #2) and Slack (spec #3) are **not** prerequisites. Where this spec discusses shared decisions with Discord/Slack (e.g., the reply-fallback timeout in §4.7), Google Chat owns the decision if it ships first; later transports adopt or explicitly override them.

---

## 4. Architecture

### 4.0 Required Step 0: contract and config cleanup

Before implementing `GoogleChatTransport`, land a small prerequisite slice so the transport can be implemented without hidden side channels. Step 0 should ship as its own PR ahead of the main implementation, because it touches the cross-transport contract (`MessageRef`) and warrants independent review.

#### 4.0.1 Add native metadata to `MessageRef`

Current `MessageRef` shape (verified at `transport/base.py:37-43`):

```python
@dataclass(frozen=True)
class MessageRef:
    transport_id: str
    native_id: str
    chat: ChatRef
```

Required refined shape:

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class MessageRef:
    transport_id: str
    native_id: str
    chat: ChatRef
    native: Any = field(default=None, compare=False, hash=False, repr=False)
```

Rules:

- `native` is optional, opaque, and transport-owned.
- Existing transports may leave it as `None`.
- `native` must not participate in equality, hashing, or normal repr output. Reason: `native` may hold an unhashable dict (`GoogleChatMessageNative`) and may differ in richness for refs to the same platform message. Message identity remains `transport_id + native_id + chat`.
- Bot logic may read only platform-neutral fields. Google-specific data remains behind transport methods; bot code may pass `MessageRef` through unchanged.
- `GoogleChatTransport` stores thread/message/idempotency metadata in `native` using the `GoogleChatMessageNative` shape (§4.0.2).
- `send_text(..., reply_to=msg)` uses `reply_to.native` to preserve Google Chat thread placement.
- `edit_text(msg, ...)` uses `msg.native_id` / `msg.native` to update the referenced app-created message only.

Fallback if maintainers reject the contract amendment:

- Use a bounded internal side table keyed by `MessageRef.native_id`.
- This fallback is weaker because metadata can be lost across process restarts and when external code constructs `MessageRef` manually.
- If using the side table, document that thread preservation is best-effort and only guaranteed for messages seen/sent during the same process lifetime.

#### 4.0.2 Define Google Chat native metadata shape

Use one typed metadata shape so implementation and tests do not drift on key names:

```python
from typing import TypedDict

class GoogleChatMessageNative(TypedDict, total=False):
    space_name: str                  # e.g. "spaces/..."
    message_name: str                # e.g. "spaces/.../messages/..."
    thread_name: str                 # e.g. "spaces/.../threads/..."
    event_time: str                  # raw Google event timestamp where available
    event_idempotency_key: str       # chosen inbound duplicate key
    request_id: str                  # outbound create idempotency key when used
    message_reply_option: str        # reply option used for create calls
    is_app_created: bool             # True for messages created by this app/client
```

Rules:

- Store only JSON-serializable, display-safe values.
- Do not store bearer tokens, access tokens, service-account JSON, callback-token secrets, or secret prompt text.
- `MessageRef.native_id` remains the canonical message resource name; `native["message_name"]` is a redundant convenience only when useful.
- Tests should assert exact key names for thread propagation, outbound `request_id` idempotency, and reply-option propagation.

#### 4.0.3 Prompt submission semantics

`PromptSubmission` currently has `text` and `option`, not `value` (verified at `transport/base.py:156-163`).

Mapping:

```text
PromptKind.TEXT      -> PromptSubmission(text="...")
PromptKind.SECRET    -> PromptSubmission(text="...")  # never log raw text
PromptKind.CHOICE    -> PromptSubmission(option="...")
PromptKind.CONFIRM   -> PromptSubmission(option="yes" | "no")
PromptKind.DISPLAY   -> no submission unless an action button is clicked
```

Cancel/timeout representation for v1:

```python
PROMPT_CANCEL_OPTION = "__cancel__"
PROMPT_TIMEOUT_OPTION = "__timeout__"
```

- Cancel -> `PromptSubmission(text=None, option="__cancel__")`
- Timeout -> `PromptSubmission(text=None, option="__timeout__")`
- These reserved values are transport-level sentinels. If a future shared primitive gains an explicit status field, migrate away from sentinel options.
- User-provided choice values equal to these reserved strings must be rejected or escaped when rendering prompt options.

#### 4.0.4 Persist non-Telegram `BotPeerRef`

The `BotPeerRef` dataclass is already transport-agnostic, but Google team routing needs persisted peer app/user IDs. Verified gap: `config.py:_parse_team_bot` ignores `bot_peer`; `_serialize_team_bot` never writes it; only Telegram synthesis from `bot_username` works today.

Required config behavior:

- parse `bot_peer` from team bot config when present
- save `bot_peer` when non-`None`
- keep the existing Telegram legacy synthesis from `bot_username` for old configs
- do not overwrite a persisted Google `bot_peer` with a synthesized Telegram peer

Expected on-disk shape:

```json
{
  "teams": {
    "alpha": {
      "bots": {
        "manager": {
          "bot_peer": {
            "transport_id": "google_chat",
            "native_id": "users/app-manager-or-space-member-id",
            "handle": null,
            "display_name": "Manager Bot"
          }
        }
      }
    }
  }
}
```

#### 4.0.5 Accept non-Telegram room bindings in team config validation

Team config parsing currently requires both `path` AND `group_chat_id` (verified at `config.py:712` in `_split_team_entries`; `room`-only entries are dropped as malformed and cleaned up on disk by `_cleanup_malformed_teams`). This silently breaks Google Chat team configs.

Required validity rule:

```text
A team entry is valid when it has:
- path, and
- either group_chat_id or a structured room binding
```

Rules:

- `group_chat_id: 0` may remain as the legacy Telegram sentinel.
- For non-Telegram transports, a structured `room` binding is sufficient.
- Saving may keep `group_chat_id: 0` for downgrade compatibility, but loading must not reject `room`-only Google team configs.
- `_cleanup_malformed_teams` must not delete a valid non-Telegram `room`-only entry.

Expected on-disk shape for a Google Chat team:

```json
{
  "teams": {
    "alpha": {
      "path": "/path/to/project",
      "room": {"transport_id": "google_chat", "native_id": "spaces/..."},
      "bots": { "...": "..." }
    }
  }
}
```

#### 4.0.6 Make transport selection explicit

`ProjectBot.build()` must not use `else: TelegramTransport` after adding Google Chat. Verified gap: `bot.py:3349-3351` currently has the silent fall-through.

Required shape:

```python
if self.transport_kind == "web":
    ...
elif self.transport_kind == "google_chat":
    ...
elif self.transport_kind == "telegram":
    ...
else:
    raise ValueError(f"unknown transport: {self.transport_kind}")
```

One-shot `--path/--token` starts remain Telegram/Web-only in v1 unless the implementation explicitly supports starting Google Chat from a fully populated `google_chat` config. Google Chat does not use the Telegram bot token; passing a Telegram token must not be treated as Google credentials.

---

### 4.1 Transport mapping

```python
ChatRef.transport_id == "google_chat"
Identity.transport_id == "google_chat"
MessageRef.transport_id == "google_chat"
PromptRef.transport_id == "google_chat"
```

Field mapping:

- `ChatRef.native_id`: Google Chat space resource name, for example `spaces/...`
- `ChatRef.kind`: `DM` for direct-message spaces, `ROOM` for named spaces/group conversations
- `Identity.native_id`: Google Chat user/app resource name, service account identity, or bot resource name
- `Identity.handle`: best-effort email/name hint when available
- `Identity.display_name`: human-readable display name from event payload
- `Identity.is_bot`: true for app/bot-originated events where Google exposes bot identity
- `MessageRef.native_id`: Google Chat message resource name, for example `spaces/.../messages/...`
- `MessageRef.native`: `GoogleChatMessageNative` metadata required for thread/reply/idempotency preservation (§4.0.2)

Outbound primitives:

- `send_text`: `spaces.messages.create`
- `edit_text`: `spaces.messages.update` where supported for app-created messages; degrade to final-only updates or clearly marked replacement message when editing is unavailable
- `send_file`: upload/attach where supported; otherwise clear text fallback with display name
- `send_voice`: implement as attachment/text fallback while preserving `reply_to` where possible; do not blindly delegate to `send_file` if that drops thread context
- `send_typing`: no-op unless Google Chat exposes a clean equivalent
- `render_markdown`: convert the shared markdown/HTML subset to Google Chat-supported markup where possible

Inbound primitives:

- `MESSAGE` events -> `IncomingMessage` or `CommandInvocation`
- slash/quick command events -> `CommandInvocation`
- `CARD_CLICKED` callbacks -> `ButtonClick` or `PromptSubmission`
- dialog submit/cancel events -> `PromptSubmission` or prompt cleanup
- added/removed space events -> setup/cleanup hooks inside the transport

---

### 4.2 Package, config, and CLI surface

Create:

```text
src/link_project_to_chat/google_chat/
  __init__.py
  app.py          # FastAPI app and POST route for Chat events
  auth.py         # Google Chat request verifier
  client.py       # Google Chat REST client/auth wrapper
  cards.py        # Cards v2 builders for Buttons and PromptSpec
  transport.py    # GoogleChatTransport implementation
```

Optional dependency extra:

```toml
[project.optional-dependencies]
google-chat = [
  "fastapi[standard]",
  "httpx",
  "google-auth",
]
```

`GoogleChatConfig`:

```python
@dataclass
class GoogleChatConfig:
    service_account_file: str = ""
    app_id: str = ""
    project_number: str = ""
    auth_audience_type: Literal["endpoint_url", "project_number"] = "endpoint_url"
    allowed_audiences: list[str] = field(default_factory=list)
    endpoint_path: str = "/google-chat/events"
    public_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8090
    root_command_name: str = "lp2c"
    root_command_id: int | None = None
    callback_token_ttl_seconds: int = 900
    pending_prompt_ttl_seconds: int = 900
    max_message_bytes: int = 32_000
```

Expose as:

```python
@dataclass
class Config:
    ...
    google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)
```

Config helpers to implement:

```python
def _parse_google_chat(raw: object) -> GoogleChatConfig: ...
def _serialize_google_chat(cfg: GoogleChatConfig) -> dict: ...
def _google_chat_is_default(cfg: GoogleChatConfig) -> bool: ...
def validate_google_chat_for_start(cfg: GoogleChatConfig) -> None: ...
```

Load behavior:

- Missing `google_chat` block -> default `GoogleChatConfig()`.
- Non-dict `google_chat` -> `ConfigError` or warning + default; choose one and test it. Startup must fail if Google Chat is selected and required fields are absent.
- `allowed_audiences` must be a list of strings.
- Unknown `auth_audience_type` fails at config load or transport startup; startup failure is mandatory.
- `port`, `callback_token_ttl_seconds`, `pending_prompt_ttl_seconds`, and `max_message_bytes` must be positive integers.
- `endpoint_path` must start with `/`.

Save behavior:

- Omit `google_chat` entirely when all fields are default.
- Persist only the service-account **path**, never the JSON contents.
- Do not print credential JSON, bearer tokens, access tokens, or callback secrets in logs, cards, command output, or exceptions.

Startup validation:

- `auth_audience_type == "endpoint_url"` requires at least one audience. If `allowed_audiences` is empty but `public_url` and `endpoint_path` are set, derive exactly one audience as `public_url.rstrip("/") + endpoint_path`.
- `auth_audience_type == "project_number"` requires non-empty `project_number`.
- `service_account_file` is required and must be readable before enabling outbound Chat API support.
- `callback_token_ttl_seconds` and `pending_prompt_ttl_seconds` must be positive.
- `max_message_bytes` must be positive and no greater than the currently documented Chat API message-size ceiling. Step 4 must verify the current ceiling against Google docs before implementation.
- Startup errors must name the missing field without exposing credential contents.

CLI wiring:

- Add `--transport google_chat` to the existing transport choice.
- Add local-tunnel-oriented overrides:
  - `--google-chat-host`
  - `--google-chat-port`
  - `--google-chat-public-url`
- Do not add credential-generation or Google Cloud setup commands in v1. Document manual setup instead.
- Starting `--transport google_chat` with an empty/default `google_chat` config fails clearly.

---

### 4.3 HTTP receiver and dispatch loop

The transport owns a small ASGI service following the Web transport pattern: HTTP routes translate platform events, while `GoogleChatTransport` owns protocol normalization and dispatch.

HTTP flow:

1. Google Chat POSTs an interaction event to `endpoint_path`.
2. The route reads headers and raw body.
3. The route verifies the `Authorization: Bearer ...` token before queueing or normalizing the event.
4. The route performs duplicate/idempotency suppression after platform auth succeeds.
5. The route returns a fast 2xx acknowledgement for non-dialog events without waiting for bot/backend work.
6. The dispatch task normalizes and invokes registered handlers.
7. Long-running handler output posts/edits asynchronously through the Chat API.

Acknowledgement policy:

- **Default v1 acknowledgement is HTTP 200 with an empty JSON body (`{}`)** for `MESSAGE`, `APP_COMMAND`, and normal `CARD_CLICKED` events. Google Chat treats this as "no synchronous response, dispatch will post asynchronously."
- Tests should assert behavior (fast return, no synchronous user-visible message by default, asynchronous dispatch continues) rather than asserting an overly brittle exact body byte-for-byte. The `{}` body is the chosen default; an empty 200 with no body is also acceptable if a future change requires it.
- Dialog events that request/open/submit/close a dialog return the required dialog action response inline when needed.
- A user-visible "Working…" message is opt-in per command or response path, not the default, to avoid transient messages that later stream edits cannot cleanly replace.

Dispatch queue rules:

- Route handlers must never await `ProjectBot` / `ManagerBot` command execution, backend execution, file downloads beyond safe pre-auth metadata, or REST posting beyond required synchronous dialog actions.
- Queue items should carry raw event payload, verified claims summary, idempotency key, receive timestamp, and redacted request metadata.
- Dispatch loop exceptions must be logged with `logger.exception`. Where a response surface exists, send a short failure message rather than silently swallowing user-visible failures.

---

### 4.4 Authentication and authorization

There are two distinct auth concerns:

1. **Google platform auth**
   - Verify HTTP interaction events from Google Chat.
   - Authenticate outbound Chat API calls using service account/app credentials.
   - Use scopes appropriate for message create/update, attachment upload/download, and space/member operations.

2. **Application user auth**
   - Existing `_auth.py` / `AllowedUser` logic gates trusted users.
   - `GoogleChatTransport` calls `set_authorizer` before expensive dispatch work.
   - Identity locking binds trusted users to stable Google Chat native IDs, not display names.

#### Request verification modes

Google Chat includes a bearer token in the `Authorization` header of HTTPS requests to the app endpoint. The token shape depends on the Chat app Authentication Audience setting.

Endpoint URL audience:

- Verify a Google-signed OIDC ID token.
- Audience must match one of the configured endpoint URLs.
- The token email must be `chat@system.gserviceaccount.com` and verified.
- This mode is preferred when the HTTP endpoint URL is the trust boundary.

Project Number audience:

- Verify a self-signed JWT using Google Chat service-account certificates.
- Issuer must be `chat@system.gserviceaccount.com`.
- Audience must match the configured Google Cloud project number.
- This mode is useful when endpoint URLs change but the project number is stable.

Config table:

| `auth_audience_type` | Verifier | Expected Chat identity | Audience source |
|---|---|---|---|
| `endpoint_url` | Google-signed OIDC ID token | verified email `chat@system.gserviceaccount.com` | configured HTTPS endpoint URL(s) |
| `project_number` | self-signed JWT with Chat certs | issuer `chat@system.gserviceaccount.com` | configured Google Cloud project number |

Request verifier contract:

```python
@dataclass(frozen=True)
class VerifiedGoogleChatRequest:
    issuer: str | None
    audience: str
    subject: str | None
    email: str | None
    expires_at: int | None
    auth_mode: Literal["endpoint_url", "project_number"]
```

Rules:

- Missing `Authorization` -> HTTP 401 before queueing.
- Malformed bearer token -> HTTP 401 before queueing.
- Invalid signature/audience/issuer/email/expiry -> HTTP 401 before queueing.
- Valid platform token but unauthorized app user -> let existing `AllowedUser` auth reject after identity normalization.
- Never log the raw bearer token.
- Unit tests monkeypatch the verifier; no unit test may depend on live Google certs or network.

Duplicate/retry idempotency:

- Keep a bounded TTL cache of recently accepted Google event IDs.
- Prefer a stable event ID from payload if available.
- If no event ID is available, derive a conservative key from `(eventTime, space.name, message.name, user.name, actionMethodName, dialogEventType)`.
- Duplicate delivery must return a successful acknowledgement but must not dispatch handlers twice.

Auth tests:

- valid endpoint-URL audience token -> accepted
- valid project-number audience token -> accepted
- issuer/email/audience mismatch -> 401
- missing token -> 401
- malformed token -> 401
- duplicate verified event -> no second dispatch
- raw token is absent from logs and exceptions

---

### 4.5 Commands: `/lp2c` as the stable bridge

Google Chat supports slash and quick commands registered in Google Cloud Console. The v1 transport registers one root command:

```text
/lp2c help
/lp2c projects
/lp2c create-project
/lp2c model set gpt-5.2
```

Transport behavior:

- Prefer Google command metadata over text parsing when event payload includes usable command data.
- Require `root_command_id` to match the configured command ID when `root_command_id` is configured.
- Ignore/reject command events whose command ID does not match the configured root command.
- Parse first token after `/lp2c` as `CommandInvocation.name`.
- Parse remaining tokens into `args`.
- Normalize `raw_text` as `/lp2c <name> ...`.
- Preserve native command metadata in `CommandInvocation.native`.

Why one root command:

- avoids a long command registration list in Google Cloud Console
- mirrors the intended Discord/Slack command bridge
- keeps command handling portable while allowing cards/dialogs to provide nicer entry points later

Required command tests:

- command ID matches configured `root_command_id` -> dispatches `CommandInvocation`
- command ID mismatches configured `root_command_id` -> no dispatch
- text-only `/lp2c help` fixture -> `CommandInvocation(name="help", args=[])`
- text-only `/lp2c model set gpt-5.2` fixture -> `CommandInvocation(name="model", args=["set", "gpt-5.2"])`
- unknown internal command name -> dispatch only if a handler is registered, otherwise no-op/log debug

---

### 4.6 Cards v2 and buttons

Cards v2 is the primary Google Chat UI surface for transport buttons and compact menus.

Mapping rules:

- `Buttons` -> card widgets with button actions
- `Button.value` -> trusted only after callback-token verification
- `ButtonStyle.PRIMARY` -> visually prominent button where Cards v2 supports it, otherwise default
- `ButtonStyle.DANGER` -> destructive label/icon treatment where supported, otherwise default with clear destructive label
- card click event -> `ButtonClick(chat, message, sender, value, native=event)`

Card builder isolation:

- Put pure builders in `google_chat/cards.py`.
- Unit tests verify JSON output without starting the transport.
- Builders receive sanitized display data only; secrets and bearer tokens never enter card JSON.

Callback integrity:

- Every action includes `kind`, `callback_token`, and only minimal display-safe parameters needed by Google Chat.
- The transport must not trust raw action parameters until the callback token verifies.
- `callback_token` is an HMAC-signed envelope using a per-process secret generated at transport startup: `secrets.token_bytes(32)`.
- The secret is never persisted. Restart intentionally invalidates in-flight cards — users see the standard "This action expired" message and re-run the command.
- Chosen over a server-side TTL map so memory use stays bounded and no shared callback store is needed for v1.
- Envelope format:

```text
base64url(payload_json).base64url(hmac_sha256(secret, payload_json))
```

Payload binds at least:

```json
{
  "transport_id": "google_chat",
  "space": "spaces/...",
  "message": "spaces/.../messages/...",
  "thread": "spaces/.../threads/...",
  "sender": "users/...",
  "kind": "button|prompt|wizard",
  "value": "...",
  "expires_at": 1770000000
}
```

Rules:

- Callback tokens expire after `callback_token_ttl_seconds` (enforced by reading `expires_at` from the verified payload; no separate store).
- Prompt and wizard callbacks are sender-bound.
- Generic informational buttons may be app-auth-only if upper-layer command is not user-specific.
- Expired/invalid callbacks send "This action expired. Run the command again." when a response surface is available.
- Invalid callbacks never raise out of the dispatch loop.
- Tests must cover tampered payload, tampered signature, expired token, wrong sender, wrong space, and restart-secret invalidation.

---

### 4.7 Prompt mapping

Prompt rules:

- `PromptKind.TEXT`: dialog with one text input when opened from a valid interaction context; reply fallback otherwise
- `PromptKind.SECRET`: dialog with one text input; value is treated as secret even if Google Chat does not mask it perfectly
- `PromptKind.CHOICE`: selection input or button row depending on option count
- `PromptKind.CONFIRM`: two-button action with `option="yes"` or `option="no"`
- `PromptKind.DISPLAY`: informational card with optional actions

State rules:

- `GoogleChatTransport` renders prompts and maps callbacks to `PromptSubmission`.
- App-owned `ConversationSession` remains the source of truth for wizard state.
- `PromptRef.native_id` encodes generated prompt ID and no raw Google payload JSON.
- `_pending_prompts: dict[str, PendingPrompt]` stores prompt ID, chat, expected sender, prompt kind, created/expires timestamps, and original message/thread metadata.
- `PendingPrompt` is removed on submit, cancel, timeout, or shutdown.
- Prompt submissions verify the submitting user matches the expected sender before dispatching upward.

Dialog caveat:

Google Chat dialogs are tied to interaction callbacks. If a text/secret prompt is opened outside a valid interaction context, use reply fallback:

1. Post the prompt body as a normal message in the target space.
2. Store a `PromptRef` whose `native_id` records a server-generated correlation token, not the secret value.
3. Mark `(space, sender)` as expecting a reply for that `PromptRef`.
4. Treat the next plain text message from that exact sender in that exact space as `PromptSubmission(text=...)`.
5. Ignore other senders for this prompt; their messages still flow through `on_message` normally.
6. Expire pending reply after `pending_prompt_ttl_seconds` and emit `PromptSubmission(option="__timeout__")`.

**Default reply-fallback timeout: `pending_prompt_ttl_seconds == 900`.** Google Chat ships before Discord/Slack per current sequencing, so this spec owns the value. Discord/Slack adopt the same default unless their platform constraints demand otherwise; any extracted shared helper lives under `src/link_project_to_chat/transport/` and is introduced when the second transport actually needs it (do not pre-extract for a single caller).

Secret handling:

- Do not log raw secret prompt text.
- Do not store secret values in `_pending_prompts`, callback tokens, `PromptRef.native_id`, card snapshots, event debug logs, or exception messages.
- Unit tests must sanitize native payloads before asserting logged/error output.

Prompt tests:

- text dialog submit by expected sender -> `PromptSubmission(text="...")`
- text dialog submit by another sender -> rejected and not dispatched
- secret dialog submit -> dispatched without logging/storing secret value
- choice submit -> `PromptSubmission(option="...")`
- confirm yes/no -> `PromptSubmission(option="yes" | "no")`
- cancel -> `PromptSubmission(option="__cancel__")`
- timeout -> cleanup plus `PromptSubmission(option="__timeout__")`
- reply fallback accepts exact same sender/space only
- dialog open attempted without interaction context -> reply fallback, not exception

---

### 4.8 Async responses, byte limits, and streaming

Google Chat interaction handlers must return quickly. Backend responses can take much longer, so streaming uses a split path:

- HTTP route returns fast acknowledgement (see §4.3 acknowledgement policy).
- Bot/backend handler runs through dispatch queue.
- `send_text` posts asynchronously through Chat API.
- `edit_text` updates app-created messages where permitted.
- `StreamingMessage` remains transport-agnostic and owns time-based throttling, overflow rotation, and `TransportRetryAfter` backoff (see [`transport/streaming.py`](../../../src/link_project_to_chat/transport/streaming.py)).
- Google Chat composes with `StreamingMessage(transport, chat, throttle=2.0, ...)` and layers Google-specific caps inside `GoogleChatTransport.edit_text`, not inside `StreamingMessage`.

Byte-limit handling:

- Google Chat message size is treated as a **UTF-8 byte limit**, not a Python character limit.
- `GoogleChatConfig.max_message_bytes` stores the verified current byte ceiling.
- `GoogleChatTransport.max_text_length` (the shared helper's character ceiling) must be set conservatively below the byte ceiling because the helper counts characters; one Unicode character can encode to up to 4 UTF-8 bytes.
- Before each `send_text`/`edit_text`, compute `len(rendered.encode("utf-8"))`. If the byte length exceeds `max_message_bytes`, split/rotate/final-only-degrade before issuing the REST call. Do not rely on the API to reject oversized payloads.
- Step 4 must verify the current Chat API message-size ceiling against Google docs and update the default `max_message_bytes` if needed.

V1 streaming budget:

- no more than one edit every 2 seconds per message
- skip live edits unless rendered text changed by at least 120 characters or stream completed
- cap live edits at 30 per streamed response before final-only updates
- after two consecutive Google API update failures, stop live edits for that response
- after any explicit retry/backoff hint over 10 seconds, stop live edits for that response and send/edit only final text

Threading for streamed responses:

- Streams started from an inbound threaded message stay in that thread.
- `StreamingMessage` overflow rotation calls `send_text(..., reply_to=original_reply_to)`.
- `GoogleChatTransport.send_text` reads `reply_to.native["thread_name"]` and creates the rotated child in the same Google Chat thread, using Chat API `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` with the parent's `thread.name`.
- Store the reply option actually used in the resulting `MessageRef.native["message_reply_option"]` so downstream code does not re-derive it.
- Streams that begin outside a thread create top-level messages in the same space.
- If `reply_to` lacks thread metadata, send in the same space without pretending to quote.

Edit fallback rules:

- Do not emulate edits by posting sibling thread replies.
- A replacement/final message is acceptable only when clearly marked as a new/final message.
- `edit_text(msg, ...)` must not mutate conversation structure unexpectedly.

REST retry mapping:

- HTTP 429 and retryable 5xx/503 responses with retry hint -> `TransportRetryAfter`.
- Retryable response without hint -> bounded exponential backoff in `GoogleChatClient`, then surface `TransportRetryAfter` or structured transport error.
- Non-retryable 4xx -> structured transport error with token/credential material redacted.

---

### 4.9 Spaces, DMs, rooms, mentions, and team routing

Google Chat spaces are the room primitive.

Space type mapping:

- `DM` or `DIRECT_MESSAGE` -> `ChatKind.DM`
- `SPACE`, `GROUP_CHAT`, `ROOM`, or other non-DM multi-user conversation type -> `ChatKind.ROOM`
- unknown/missing type in message event -> fail closed for room-only routing and log a redacted warning

Team routing uses shared model:

- `RoomBinding.transport_id == "google_chat"`
- `RoomBinding.native_id == "spaces/..."`
- `BotPeerRef.transport_id == "google_chat"`
- `BotPeerRef.native_id` is the peer app/user/member resource name

Mention routing:

- Populate `IncomingMessage.mentions` when event payload exposes annotations or app mentions.
- Compare `mentions[*].native_id` to configured peer IDs.
- Use display names/emails only for logs and UI hints.

Mention fallback when annotations are absent:

- DM messages are directed to the bot by definition.
- Slash commands, quick commands, card actions, and dialog actions are directed to the app by definition.
- Room messages are directed only if raw text contains a recognized app mention token/name for the bot or peer; fallback match is best-effort.
- If a configured team room has exactly one active Google Chat bot, normal room text may be accepted as directed to that bot.
- Otherwise ignore unannotated room text for routing; never guess from mutable display names alone.

Threading:

- Preserve inbound thread metadata in `MessageRef.native`.
- `send_text(..., reply_to=msg)` posts into `msg`'s Google Chat thread when `msg.native` contains `thread_name`.
- When creating a message with thread metadata, use Chat API thread fields and the appropriate `messageReplyOption` value supported by the API.
- `edit_text(msg, ...)` updates the referenced message only.
- Do not add `ChatKind.THREAD` unless multiple transports need it.

---

### 4.10 Files, attachments, and voice

Google Chat attachment support is constrained. V1 behavior is conservative.

Inbound attachments:

- Become `IncomingFile` only if the transport can download them with configured credentials and the attachment is inside the configured size cap.
- Prefer `attachmentDataRef.resourceName` with the media API for uploaded Chat content.
- Do not use `downloadUri` or `thumbnailUri` for app downloads; those are human-user URLs.
- Drive attachments require Drive API/scopes or become unsupported in v1.
- Use a 25 MiB default inbound size cap to match Web upload hardening unless Google Chat's current effective limit is smaller.
- **Step 9 must verify the current effective attachment ceiling against the [attachments reference](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages.attachments) and document it in the PR description; lower the default if Google imposes a smaller effective cap.**
- Downloads stream to a temp file and clean up after dispatch, including auth rejection and handler failure paths.
- Unsupported/non-downloadable attachments set `has_unsupported_media=True`.

Outbound files:

- Use Chat API upload/attachment support where practical and scoped.
- If direct upload is unavailable for the chosen auth/surface, send text fallback with display name and clear limitation.
- Never crash user flows because file upload is unavailable.

Voice:

- `send_voice` is implemented as file or text fallback.
- Preserve `reply_to`/thread context where possible.
- If voice fallback delegates to `send_file`, ensure `send_file` has enough internal support to preserve thread context; otherwise implement `send_voice` directly.

Attachment tests:

- supported uploaded-content attachment -> `IncomingFile` and cleanup after dispatch
- oversized attachment -> no temp-file leak and user-facing unsupported/too-large message where possible
- Google Drive attachment without configured Drive support -> `has_unsupported_media=True`
- non-downloadable attachment -> `has_unsupported_media=True`
- outbound file unsupported by credentials/scopes -> text fallback, not crash

---

### 4.11 Outbound REST client

`google_chat/client.py` owns Google Chat REST calls and auth wrapping.

Responsibilities:

- create message (with optional `requestId` idempotency)
- update/patch message with explicit `updateMask`
- upload/attach file where supported
- download uploaded-content attachment where supported
- redact secrets from structured errors
- translate retry/backoff signals into `TransportRetryAfter` where useful
- expose deterministic fake/mocked boundaries for tests

Client methods:

```python
class GoogleChatClient:
    async def create_message(
        self,
        space: str,
        body: dict,
        *,
        thread_name: str | None = None,
        request_id: str | None = None,
        message_reply_option: str | None = None,
    ) -> dict: ...

    async def update_message(
        self,
        message_name: str,
        body: dict,
        *,
        update_mask: str,
        allow_missing: bool = False,
    ) -> dict: ...

    async def upload_attachment(self, space: str, path: Path, *, mime_type: str | None) -> dict: ...
    async def download_attachment(self, resource_name: str, destination: Path) -> None: ...
```

Outbound create idempotency:

- Use `requestId` where supported by the current `spaces.messages.create` API.
- Generate deterministic-but-unique request IDs for retryable create calls (e.g., a UUID stamped once per `send_text` call, reused across retries).
- Store the chosen request ID in `MessageRef.native["request_id"]`.
- If a network timeout occurs after a successful server create, retrying with the same `request_id` must not duplicate the message.
- Step 4 must verify current `requestId` support and document any TTL or scoping constraints (e.g., per-space, per-app).

Update/edit semantics:

- Use update/patch semantics with an **explicit `updateMask`** for the fields being changed (e.g., `text`, `cards`, `accessoryWidgets` as applicable).
- `edit_text` must **never** silently create a new message. Do not use create-on-missing behavior for normal edits — call sites pass `allow_missing=False`.
- If the API cannot update the target message (e.g., the message was not created by this app, or the message was deleted), surface a structured transport error and let `GoogleChatTransport` degrade inside the transport to final-only or a clearly marked replacement/final message. Do not paper over the failure by posting a new message that looks like an edit.
- App authentication may only be able to update messages created by the same app; treat non-app-created `MessageRef`s (where `native.get("is_app_created") is False`) as unsupported for edit.

Rules:

- `GoogleChatTransport` constructs `MessageRef` from returned message resource names and `GoogleChatMessageNative` metadata, including `request_id` and `message_reply_option` when set.
- Client never logs bearer tokens, access tokens, credential JSON, callback tokens, or secret prompt values.
- Tests mock the client; no live Google Workspace integration suite is required for v1.

---

### 4.12 Test injection helpers

Shared transport contract tests expect or benefit from injection helpers. `GoogleChatTransport` should expose test-only helpers or a fixture wrapper:

```text
async def inject_message(...): ...
async def inject_command(...): ...
async def inject_button_click(...): ...
async def inject_prompt_submit(...): ...
```

These helpers must honor the same authorizer and normalization rules as real events where practical.

---

## 5. Implementation-plan readiness checklist

### Files to create

- `src/link_project_to_chat/google_chat/__init__.py`
- `src/link_project_to_chat/google_chat/app.py`
- `src/link_project_to_chat/google_chat/auth.py`
- `src/link_project_to_chat/google_chat/client.py`
- `src/link_project_to_chat/google_chat/cards.py`
- `src/link_project_to_chat/google_chat/transport.py`
- `tests/google_chat/fixtures/*.json`
- `tests/google_chat/test_app.py`
- `tests/google_chat/test_auth.py`
- `tests/google_chat/test_cards.py`
- `tests/google_chat/test_config.py`
- `tests/google_chat/test_transport.py`

### Files to modify

- `pyproject.toml`
- `src/link_project_to_chat/transport/base.py`
- `src/link_project_to_chat/config.py`
- `src/link_project_to_chat/cli.py`
- `src/link_project_to_chat/bot.py`
- `tests/transport/test_contract.py`
- `README.md`
- `docs/CHANGELOG.md`
- `docs/TODO.md`

### Acceptance criteria

- `MessageRef.native` contract amendment is in place with `field(default=None, compare=False, hash=False, repr=False)`, or the documented side-table fallback is implemented.
- `GoogleChatMessageNative` keys are defined and used consistently across send/edit/streaming/tests.
- `GoogleChatConfig` loads, saves, omits defaults, and validates startup fields correctly, including `max_message_bytes`.
- Team config validation accepts `path + room` for non-Telegram teams without requiring legacy `group_chat_id`; `_cleanup_malformed_teams` does not delete `room`-only entries.
- `BotPeerRef` parses from and saves to disk for non-Telegram peers; Telegram synthesis remains intact for legacy configs.
- `ProjectBot.build()` has explicit branches for `web`, `google_chat`, and `telegram`, and raises on unknown values.
- `GoogleChatTransport` passes shared transport contract tests for supported capabilities.
- HTTP route rejects invalid Google tokens before queueing or normalizing events.
- HTTP route acknowledges long-running interactions before handler completion, returning HTTP 200 `{}` for non-dialog events by default.
- Duplicate Google event deliveries do not dispatch handlers twice.
- Outbound create retries with the same `request_id` do not produce duplicate messages in the fake client.
- `edit_text` never silently creates a new message; unsupported-edit cases degrade to final-only or a clearly marked replacement.
- `send_text`/`edit_text` validate UTF-8 byte size against `max_message_bytes` before issuing the REST call.
- `/lp2c help` command events become `CommandInvocation(name="help")`.
- Google command metadata ID is honored when configured.
- Card button clicks become `ButtonClick` only after callback-token verification.
- Text/secret prompt dialog submissions become `PromptSubmission(text=...)`; wrong-user submissions are rejected.
- Choice/confirm prompt submissions become `PromptSubmission(option=...)`.
- Cancel/timeout use reserved sentinel options.
- DM/ROOM mapping uses Google space type, not display names.
- Stable `Identity.native_id` values feed existing `AllowedUser` authorization.
- `RoomBinding` and `BotPeerRef` can persist Google Chat space/peer IDs.
- Thread metadata is preserved through `MessageRef.native` and used for `reply_to` and streaming overflow rotation.
- Unsupported attachments and outbound file limitations degrade through existing transport primitives.
- No Google payload classes or raw event dict assumptions leak above the transport boundary.
- Verification target: `pytest -q`, `git diff --check`, and `python3 -m compileall -q src/link_project_to_chat`.

---

## 6. Migration / implementation sequence

Twelve independently landable steps. Step 0 should ship as its own PR ahead of the rest because it touches the cross-transport `MessageRef` contract.

### Step 0 - Contract and config prerequisites

Add `MessageRef.native` with `compare=False, hash=False, repr=False` semantics, define `GoogleChatMessageNative`, parse/save `BotPeerRef`, widen team config validity to accept `path + room` without `group_chat_id`, add `GoogleChatConfig` (with `max_message_bytes`) and its load/save/default-omission/startup-validation helpers, and make `ProjectBot.build()` transport selection explicit (raise on unknown values).

### Step 1 - Add package skeleton and optional dependencies

Create the `google_chat` package, optional dependency extra, and a `GoogleChatTransport` skeleton that satisfies lifecycle/registration methods but does not yet receive real events.

### Step 2 - Implement Google Chat request verification

Build `google_chat/auth.py`, verifier abstraction, startup auth-mode validation, redaction, and tests for endpoint-URL/project-number modes.

### Step 3 - Build HTTP receiver and queue

Add FastAPI route, verified-event queueing, duplicate/retry suppression, lifecycle management, and fast acknowledgement behavior (HTTP 200 `{}` default for non-dialog events).

### Step 4 - Implement outbound REST client

Support text create with `requestId` idempotency where supported, best-effort update/patch with explicit `updateMask` and `allow_missing=False`, UTF-8 byte-limit validation against `max_message_bytes`, retry mapping, service-account/app authentication, and redacted errors. Verify the current Chat API message-byte ceiling and `requestId` semantics against Google docs and document findings in the PR description.

### Step 5 - Implement message and command normalization

Map Google Chat message and command events into `IncomingMessage` and `CommandInvocation`, including stable identities, `ChatRef`, mentions, threads, and command metadata IDs.

### Step 6 - Implement Cards v2 buttons

Build card JSON for `Buttons`, sign/verify callback tokens, and dispatch `ButtonClick`.

### Step 7 - Implement prompts/dialogs

Map `PromptSpec` to dialog/card/reply-fallback flows and dispatch `PromptSubmission` with `text`/`option` semantics, including `__cancel__` / `__timeout__` sentinels.

### Step 8 - Implement streaming/edit degradation

Compose with `StreamingMessage`, implement Google-specific edit budget/final-only degradation, validate UTF-8 byte size before each `edit_text`/`send_text` call, and preserve thread metadata (`thread_name`, `message_reply_option`) across overflow rotation.

### Step 9 - Implement file/attachment/voice fallbacks

Handle uploaded-content attachment downloads via `attachmentDataRef`/media API where supported, unsupported Drive/non-downloadable cases, outbound file fallback, and voice fallback while preserving thread context where possible. Verify the actual current Google Chat attachment ceiling and document it in the PR description.

### Step 10 - Validate project/team room behavior

Run project bot and two team bots in Google Chat spaces, verifying stable IDs, mentions, room binding, peer refs, threads, and no Telegram relay assumptions. Builds on the shipped spec #0a primitives (`RoomBinding`/`BotPeerRef`).

### Step 11 - Run manager flows and update docs

Exercise manager commands and prompt-backed wizards end-to-end through Google Chat. Builds on the shipped spec #0c manager transport port. Update README, changelog, TODO, and setup notes with final supported/deferred behavior.

---

## 7. Testing approach

- Add Google Chat to shared transport contract tests.
- Add golden event fixtures under `tests/google_chat/fixtures/` for:
  - message
  - slash command
  - quick/app command
  - command metadata mismatch
  - card click
  - dialog request
  - dialog submit
  - dialog cancel
  - added to space
  - removed from space
  - attachment
  - duplicate/retry delivery
  - threaded message
  - oversized multibyte UTF-8 message
- Add auth verifier tests with monkeypatched verification; no live cert/network calls.
- Add pure unit tests for Cards v2 builders:
  - button rows
  - choice prompt
  - confirm prompt
  - text/secret dialog card
  - dialog-safe subset only
  - callback-token worst-case payload-size budget
- Add HTTP route tests:
  - missing token -> 401
  - bad token -> 401
  - valid token queues once
  - duplicate valid event queues once
  - slow handler does not delay HTTP response
  - default no-op acknowledgement produces no synchronous user-visible response
- Add client tests:
  - create uses `request_id` when provided
  - retry with the same `request_id` does not create a duplicate in the fake client
  - update requires explicit `update_mask`
  - update with `allow_missing=False` refuses create-on-missing behavior
  - byte-limit guard triggers before the REST call for oversized rendered text
- Add callback-token tests:
  - valid token
  - expired token
  - tampered payload
  - tampered signature
  - wrong sender
  - wrong space
  - restart-secret invalidation
- Add prompt tests:
  - expected sender submit
  - wrong sender rejected
  - secret value redacted
  - reply fallback exact sender/space
  - cancel/timeout sentinels
- Add identity tests proving auth/team routing uses native IDs, not display names.
- Add team config tests proving `path + room` (without `group_chat_id`) is valid, is not cleaned up as malformed, and `bot_peer` round-trips for non-Telegram peers.
- Add temp-file cleanup tests for attachment download rejection and handler failure.
- Mock Google REST boundaries; no live Workspace integration suite required for v1.

Manual smoke checklist:

- route rejects invalid/missing bearer token
- `/lp2c help` works in a DM
- app mention in a space reaches the intended project bot
- card button click dispatches exactly once
- prompt dialog submit works
- wrong-user prompt submit is rejected
- long backend response returns immediate acknowledgement and later async response
- threaded message receives threaded async reply
- service-account REST posting works with configured scopes
- unsupported attachment degrades safely
- outbound create retry with reused `requestId` does not duplicate the message
- long multibyte message does not exceed the Chat API byte limit

---

## 8. Explicit out of scope

| Belongs to | Item |
|---|---|
| Future | Pub/Sub event delivery as an alternative to HTTP+tunnel |
| Future | Google Workspace Marketplace publication and installation polish |
| Future | Domain-wide admin automation |
| Future | Full attachment parity with Telegram |
| Future | Google Workspace add-on conversion |
| Future | Multi-workspace or multi-domain federation |
| Future | Shared prompt-status primitive replacing cancel/timeout sentinel options |
| Future | Persisted/shared callback-token secret for multi-process deployments |
| Future | Drive attachment download support unless Drive scopes are explicitly added |

---

## 9. Risks

- **Public ingress requirement for HTTP mode:** local users need a tunnel and a stable HTTPS URL. Mitigation: document tunnel setup and keep Pub/Sub as a future option.
- **Interaction response deadline:** long backend work cannot run synchronously. Mitigation: fast acknowledgement and asynchronous Chat API posting.
- **Auth/config complexity:** Workspace visibility, service accounts, scopes, and admin allowlists can block usage before code runs. Mitigation: explicit startup validation and setup docs.
- **Request verifier mistakes:** endpoint-URL and project-number auth modes differ. Mitigation: verifier abstraction, strict tests, and startup mode validation.
- **Cards v2/dialog constraints:** cards and dialogs are not identical to Slack/Discord components. Mitigation: narrow mapping and dialog-safe card tests.
- **Streaming edit behavior:** Chat API update limits may make high-frequency edits poor. Mitigation: aggressive throttle and final-only degradation.
- **Identity confusion:** display names and emails can change or be hidden. Mitigation: trust and routing use stable native IDs.
- **Thread metadata dependency:** without `MessageRef.native`, thread preservation is fragile. Mitigation: Step 0 contract amendment.
- **Attachment limitations:** Drive and uploaded content have different download paths/scopes. Mitigation: uploaded-content support first, Drive fallback to unsupported unless scopes are configured.
- **Byte-limit mismatch:** shared streaming helpers count characters while the Chat API enforces UTF-8 bytes; multibyte content can silently exceed the platform cap. Mitigation: conservative `max_text_length` plus a UTF-8 byte guard immediately before each REST call.
- **Outbound retry duplication:** retried create calls without idempotency keys can post the same message twice. Mitigation: use `requestId` on `spaces.messages.create` where the current API supports it; verify TTL/scope in Step 4.
- **Step 0 cross-transport blast radius:** adding `MessageRef.native`, widening team-room validity, and `BotPeerRef` persistence touches the shared contract and config parsing. Mitigation: ship Step 0 as a standalone PR, audit existing transports for any code that constructs `MessageRef` manually, confirm `bot_peer` round-trips for both Telegram (legacy synthesis) and Google Chat paths, and confirm that legacy Telegram team entries still load after the validity widening.

---

## 10. Reference docs

- Google Chat interaction events: https://developers.google.com/workspace/chat/receive-respond-interactions
- Google Chat commands: https://developers.google.com/workspace/chat/commands
- Google Chat request verification: https://developers.google.com/workspace/chat/verify-requests-from-chat
- Google Chat authentication and authorization: https://developers.google.com/workspace/chat/authenticate-authorize
- Google Chat messages create/update: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages
- Google Chat message create: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create
- Google Chat message update: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/update
- Google Chat attachments: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages.attachments
- Cards v2 reference: https://developers.google.com/workspace/chat/api/reference/rest/v1/cards

---

## 11. Next steps

1. Land Step 0 as a small prerequisite PR.
2. Write the implementation plan using Sections 5 and 6 as the task skeleton.
3. Verify current Google Chat docs for message byte ceiling, create `requestId` semantics, update/patch behavior, and attachment ceiling before coding Steps 4 and 9; record findings in those step PRs.
4. Confirm product priority relative to Discord/Slack before execution begins.
5. Revisit whether Pub/Sub delivery should move from future option to v1 if HTTP+tunnel proves too brittle.
6. If Google Chat exposes prompt needs that `PromptSpec` cannot express, update the shared transport primitive rather than adding a Google-only workflow layer.
