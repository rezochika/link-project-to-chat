# Transport - Google Chat - Design Spec

**Status:** Ready for implementation planning. Designed 2026-04-25; refreshed 2026-05-16 against current repo state and current Google Chat docs; refined 2026-05-17 (sequencing prereqs resolved; callback-token, fast-ack, and threading decisions pinned).
**Date:** 2026-04-25; refreshed 2026-05-16; refined 2026-05-17
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a), [2026-04-21-transport-manager-port-design.md](2026-04-21-transport-manager-port-design.md) (spec #0c), [2026-04-21-transport-web-ui-design.md](2026-04-21-transport-web-ui-design.md) (spec #1)
**Part of:** Second non-Telegram transport to actually ship code, after Web (spec #1). Discord (spec #2) and Slack (spec #3) are designed but unimplemented as of 2026-05-17; this spec does not block on them. Validates HTTPS event delivery plus card/dialog UI for the first time in the codebase.

**Current repo assumptions (2026-05-16):**
- `Transport`, `ChatRef`, `MessageRef`, `Identity`, `IncomingMessage`, `CommandInvocation`, `ButtonClick`, `PromptSpec`, and `PromptSubmission` are the only bot-facing integration surface.
- `AllowedUser` is the sole app-auth source; transports provide stable `Identity.native_id` values and call `set_authorizer` before expensive dispatch work.
- `RoomBinding` and `BotPeerRef` are transport-agnostic and already support `transport_id="google_chat"` / `native_id="spaces/..."`.
- `ProjectBot` and `ManagerBot` are transport-portable; no Google payload type may leak above `GoogleChatTransport`.
- Safety prompt, recent discussion, `meta_dir`, and hot-reload are existing bot/backend features. `GoogleChatTransport` must preserve them by producing correct transport primitives; it must not reimplement them.

---

## 1. Overview

Google Chat is a strong next transport because it differs from Telegram, Web, Discord, and Slack in one important way: interactive Chat apps are normally invoked through Google Workspace event delivery. For an HTTP implementation, Google sends interaction events to a configured HTTPS endpoint, and long-running work must continue asynchronously through the Google Chat API.

That means the Google Chat transport should not be a separate plugin API or a `TelegramBot` parallel. It should be a first-class `Transport` implementation:

- incoming Chat interaction events normalize into `IncomingMessage`, `CommandInvocation`, `ButtonClick`, and `PromptSubmission`
- outgoing messages, edits, files, and cards stay behind `Transport`
- project bot and manager bot logic stay unchanged
- Cards v2 and dialogs become the platform rendering for existing `Buttons` and `PromptSpec`

**The deliverable:** `GoogleChatTransport`, backed by a small ASGI HTTP receiver and Google Chat REST client, with async posting for Codex streams and card/dialog support for buttons and prompts.

## 2. Goals & non-goals

**Goals**
- Implement `GoogleChatTransport` using Google Chat app interaction events delivered to an HTTPS endpoint.
- Support DM and room semantics for project, manager, and team bot flows.
- Map existing transport primitives onto Google Chat messages, Cards v2, dialogs, and action callbacks.
- Use Google Chat stable resource names/IDs for users, spaces, messages, threads, and app identity.
- Preserve the transport contract: commands, text, edits, buttons, files where supported, voice-as-file where supported, typing/no-op typing, and prompts.
- Acknowledge HTTP events quickly and continue long Codex work through asynchronous Chat API messages/updates.
- Keep bot logic platform-neutral; Google payloads stay inside the transport boundary.

**Non-goals (this spec)**
- A new platform-agnostic plugin API. The existing `Transport` Protocol is the integration point.
- Incoming webhooks as the primary integration. Incoming webhooks can post messages, but they are not the interactive Chat app surface.
- A marketplace-grade installation flow, OAuth consent polish, or multi-domain distribution.
- Google Workspace add-on conversion. This spec targets an interactive Google Chat app.
- Rich multi-field app workflows beyond the existing one-step `PromptSpec` model.
- Live audio/meeting participation. `send_voice` only needs attachment-style degradation.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-25:

| # | Question | Decision |
|---|---|---|
| 1 | Should this be an adapter/plugin or a transport? | Implement `GoogleChatTransport` under the existing `Transport` Protocol. |
| 2 | How should local development receive events? | HTTP endpoint plus a tunnel for v1; leave Pub/Sub as a future delivery option. |
| 3 | How do commands map into `Transport.on_command`? | Register one root slash command, `/lp2c`, and parse its text into internal command name + args. |
| 4 | How do prompts map to Google Chat UI? | `PromptSpec(TEXT/SECRET)` -> dialog text input; `CHOICE/CONFIRM` -> Cards v2 buttons or selection input; `DISPLAY` -> informational card. |
| 5 | How do long Codex runs respond to the user? | Return a fast synchronous acknowledgement, then post/edit asynchronously through Google Chat REST API. |
| 6 | What is the identity source of truth? | Stable Google Chat resource names/native IDs, not display names or email text. |

### 3.1 Sequencing prerequisite

All sequencing prerequisites are satisfied as of 2026-05-17 (`main`):

- ✅ spec #0a group/team transport port — shipped in v0.15.0, so team room routing uses `RoomBinding`/`BotPeerRef` rather than Telegram-only group IDs
- ✅ spec #0c manager transport port — shipped in v0.16.0, so manager flows run through transport primitives
- ✅ at least one HTTP-receiver sibling — Web transport (spec #1) shipped, so Google Chat can reuse the repo's proven ASGI lifecycle, dispatch queue, and test patterns

Implementation may proceed. Google Chat should consume these primitives rather than reintroducing platform-native state into project or manager bot code. Steps 9 and 10 in §5 reference these prerequisites for context only; no remaining gate.

Discord (spec #2) and Slack (spec #3) are **not** prerequisites. Where this spec discusses shared decisions with Discord/Slack (e.g., the reply-fallback timeout in §4.5), Google Chat owns the decision if it ships first; later transports adopt it.

## 4. Architecture

### 4.1 Google Chat transport mapping

```python
ChatRef.transport_id == "google_chat"
Identity.transport_id == "google_chat"
MessageRef.transport_id == "google_chat"
PromptRef.transport_id == "google_chat"
```

Field mapping:
- `ChatRef.native_id`: Google Chat space resource name, for example `spaces/...`
- `ChatRef.kind`: `DM` for direct-message spaces, `ROOM` for named spaces/group conversations
- `Identity.native_id`: Google Chat user resource name, service account/app resource name, or bot identity resource name
- `Identity.handle`: best-effort email/name hint when available
- `Identity.display_name`: human-readable display name from the event payload
- `Identity.is_bot`: true for app/bot-originated events where Google exposes bot identity
- `MessageRef.native_id`: Google Chat message resource name, for example `spaces/.../messages/...`

Outbound primitives:
- `send_text`: `spaces.messages.create`
- `edit_text`: `spaces.messages.update` where supported; degrade to final-only updates or an explicitly marked replacement message when editing is unavailable for the message/surface
- `send_file`: attach or link file where Chat API support and auth scopes allow; otherwise send a clear fallback text with the file name
- `send_voice`: delegate to `send_file` or text fallback
- `send_typing`: no-op unless Google Chat exposes a clean equivalent
- `render_markdown`: convert the shared HTML/markdown subset to Google Chat-supported text/card markup where possible

Inbound primitives:
- `MESSAGE` events -> `IncomingMessage` or `CommandInvocation`
- slash command events -> `CommandInvocation`
- card action callbacks -> `ButtonClick` or `PromptSubmission`
- dialog submit/cancel events -> `PromptSubmission` or prompt close handling
- added/removed space events -> setup/cleanup hooks inside the transport

### 4.2 HTTP receiver and dispatch loop

The transport owns a small ASGI service following the Web transport's FastAPI/uvicorn shape:

```text
src/link_project_to_chat/google_chat/
  __init__.py
  app.py          # FastAPI app and POST route for Chat events
  auth.py         # Google Chat request verifier
  client.py       # Google Chat REST client/auth wrapper
  cards.py        # Cards v2 builders for Buttons and PromptSpec
  transport.py    # GoogleChatTransport implementation
```

Package/config surface:
- Add optional extra `google-chat` with the dependencies needed by this transport only: `fastapi[standard]`, `httpx`, and `google-auth`.
- Add `GoogleChatConfig` to `config.py` and expose it as `Config.google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)`.
- Exact config fields for v1:

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
```

Loader behavior:
- An absent/default-empty `google_chat` block means the Google Chat transport is not configured.
- Unknown `auth_audience_type` values fail at config load or transport startup.
- `allowed_audiences` must be a list of strings; empty is valid only until the transport is started.
- Saving config should omit the `google_chat` block when every field is still default, matching the existing config-minimality style.
- Only the credential path is stored in config. Never copy service-account JSON contents into config, logs, cards, command output, or exceptions.

CLI wiring:
- Add `--transport google_chat` to the existing transport choice once the skeleton exists.
- Add narrow startup overrides only where they materially help local tunnels: `--google-chat-host`, `--google-chat-port`, and `--google-chat-public-url`.
- Do not add credential-generation/setup commands in the first implementation slice. Document manual Google Cloud setup instead.

HTTP flow:
1. Google Chat POSTs an interaction event to the configured endpoint.
2. The route verifies the `Authorization: Bearer ...` token before queueing or normalizing the event.
3. The route normalizes the event into an internal queue item only after platform auth succeeds.
4. The route returns quickly. **Default v1 acknowledgement is an HTTP 200 with an empty JSON body (`{}`)** for `MESSAGE` and `CARD_CLICKED` events, which Google Chat treats as "no synchronous response, dispatch will post asynchronously." Dialog events (`DIALOG`) return a dialog-action body inline because Google requires it; see §4.5. A user-visible "Working…" text response is opt-in per command, not the default, to keep streams from posting a transient message that later edits never replace.
5. The dispatch task invokes registered transport handlers.
6. Long-running handler output posts/edits asynchronously through the Chat API.

This is deliberately similar to `WebTransport`: HTTP routes translate platform events, while `GoogleChatTransport` owns the protocol contract and dispatch behavior.

### 4.3 Commands: `/lp2c` as the stable bridge

Google Chat supports slash commands registered in Google Cloud Console. The v1 transport registers one root command:

```text
/lp2c help
/lp2c projects
/lp2c create-project
/lp2c model set gpt-5.2
```

Transport behavior:
- prefer Google command metadata over text parsing when the event includes a command payload
- require `root_command_id` to match the configured command ID when `root_command_id` is configured
- ignore or reject command events whose command ID does not match the configured root command
- parse the first token after `/lp2c` as `CommandInvocation.name`
- parse remaining tokens into `args`
- normalize `raw_text` as `/lp2c <name> ...`
- preserve native command metadata in `CommandInvocation.native`

Why one root command:
- avoids a long command registration list in Google Cloud Console
- mirrors Discord and Slack specs
- keeps command handling portable while allowing cards/dialogs to provide nicer entry points later

If Google Chat delivers an app command payload instead of plain text, the transport still synthesizes the same `CommandInvocation` shape. Text parsing remains a fallback for local tests, sample payloads, and any payload variant that includes text but no usable command metadata.

Required command tests:
- command ID matches configured `root_command_id` -> dispatches `CommandInvocation`
- command ID mismatches configured `root_command_id` -> does not dispatch
- text-only `/lp2c help` fixture -> dispatches `CommandInvocation(name="help", args=[])`
- text-only `/lp2c model set gpt-5.2` fixture -> dispatches `CommandInvocation(name="model", args=["set", "gpt-5.2"])`

### 4.4 Cards v2 and buttons

Cards v2 is the primary Google Chat UI surface for transport buttons and compact menus.

Mapping rules:
- `Buttons` -> card widgets with button actions
- `Button.value` -> transport callback value after callback-token verification, not a raw trusted event field
- `ButtonStyle.PRIMARY` -> visually prominent button where Cards v2 supports it, otherwise default button
- `ButtonStyle.DANGER` -> text/icon treatment where supported, otherwise default button with a destructive label
- button click event -> `ButtonClick(chat, message, sender, value, native=event)`

The card builder should be isolated in `google_chat/cards.py` so tests can verify pure JSON output without starting the transport.

Callback integrity:
- Every card action includes `kind`, `callback_token`, and the minimal display-safe parameters needed by Google Chat.
- The transport must not trust a raw `Button.value` returned by Google Chat unless the callback token verifies.
- `callback_token` is an **HMAC-signed envelope using a per-process secret** (`secrets.token_bytes(32)` at transport startup; never persisted). Chosen over a server-side TTL map so callback validity survives in-process restarts of a single transport instance without coordinating shared state, and so memory usage stays bounded regardless of issuance rate. The envelope serializes as `base64url(payload).base64url(hmac_sha256(secret, payload))` where `payload` is a compact JSON object.
- The signed payload binds at least: transport ID, space native ID, originating message/dialog ID when available, sender native ID when user-specific, callback kind, callback value, and an `expires_at` unix timestamp.
- Callback tokens expire after `callback_token_ttl_seconds` (enforced by reading `expires_at` from the verified payload; no separate store).
- Rotating the process secret on restart invalidates any in-flight cards; this is the intended trade-off — users see the standard "This action expired" message and re-run the command.
- Expired or unknown callbacks send a short "This action expired. Run the command again." message when a response surface is available, and otherwise log at info/debug level without raising out of the dispatch loop.
- Prompt and wizard callbacks are sender-bound. Generic informational buttons may be app-auth-only if the upper-layer command is not user-specific.

### 4.5 Prompt mapping

Prompt rules:
- `PromptKind.TEXT`: Google Chat dialog with one text input
- `PromptKind.SECRET`: dialog with one text input; treat the value as secret in logs and app state even if Google Chat does not mask it perfectly
- `PromptKind.CHOICE`: card selection input or button row, depending on option count
- `PromptKind.CONFIRM`: two-button card/dialog action
- `PromptKind.DISPLAY`: informational card with optional actions

State rules:
- `GoogleChatTransport` renders prompts and maps callbacks to `PromptSubmission`
- app-owned `ConversationSession` remains the source of truth for wizard state
- `PromptRef.native_id` encodes the generated prompt ID, not raw Google payload JSON
- prompt submissions must verify the submitting user matches the session sender before dispatching upward
- the transport owns `_pending_prompts: dict[str, PendingPrompt]`, where each entry stores prompt ID, chat, expected sender, prompt kind, created/expires timestamps, and original message/thread metadata
- `PendingPrompt` is removed on submit, cancel, timeout, or transport shutdown

Dialog caveat:
- Google Chat dialogs are tied to interaction callbacks. If a text/secret prompt is opened outside a valid interaction context, the transport falls back to a message-then-reply flow:
  - post the prompt body as a normal message in the target space and store a `PromptRef` whose `native_id` records the originating space + sender + a server-generated correlation token
  - mark that `(space, sender)` pair as expecting a reply for that `PromptRef`
  - treat the next plain text message from that exact sender in that space as the `PromptSubmission`
  - ignore messages from other senders for the purpose of this prompt; they still flow through `on_message` normally
  - expire the pending reply on the same timeout the dialog path uses (configurable, with a sane default), and emit a cancel/timeout submission so app state does not leak
- **Default reply-fallback timeout is `pending_prompt_ttl_seconds` (900s).** Google Chat ships before Discord/Slack per current sequencing, so this spec owns the value. Discord/Slack adopt the same default unless their platform constraints demand otherwise; any extracted shared helper lives under `src/link_project_to_chat/transport/` and is introduced when the second transport actually needs it (do not pre-extract for a single caller).

Secret prompt handling:
- Do not log raw `PromptSubmission.value` for `PromptKind.SECRET`.
- Do not store secret values in `_pending_prompts`, callback tokens, `PromptRef.native_id`, card JSON test snapshots, or native event debug logs.
- Unit tests must sanitize native payloads before asserting logged/error output.

Prompt tests:
- text dialog submit by the expected sender -> `PromptSubmission(value=...)`
- text dialog submit by another sender -> rejected and not dispatched
- secret dialog submit -> dispatched without logging/storing the secret in transport state
- reply-fallback prompt submit -> accepted from exact same sender/space only
- timeout -> cleanup plus cancel/timeout submission
- dialog open attempted without interaction context -> reply fallback path, not an exception

### 4.6 Async responses and streaming

Google Chat interaction handlers must return quickly. Codex responses can take far longer, so streaming needs a split path:

- initial HTTP response: fast acknowledgement within 1 second in tests, optional "Working..." message/card
- route handlers must never await `ProjectBot`/`ManagerBot` command execution, backend execution, or REST posting beyond the immediate acknowledgement path
- `send_text`: asynchronous message create through Chat API
- `edit_text`: asynchronous message update where supported for app-created messages
- `StreamingMessage`: edit-based updates with a conservative v1 throttle. The shared helper at [`transport/streaming.py`](../../../src/link_project_to_chat/transport/streaming.py) already owns time-based throttling, overflow rotation, and `TransportRetryAfter` back-off; Google Chat composes by constructing `StreamingMessage(transport, chat, throttle=2.0, ...)` and layers the additional Google-specific caps below (delta-size minimum, live-edit count, failure trigger) inside `GoogleChatTransport.edit_text`. Do not duplicate the throttle logic inside `StreamingMessage`.
- rate limits: translate Google API retry/backoff signals into `TransportRetryAfter` when the caller can use it
- dispatch loop exceptions must be logged with `logger.exception`; where a response surface is available, send a short failure message to the user instead of silently swallowing the failure

V1 streaming budget:
- no more than one edit every 2 seconds per message
- skip edits unless the rendered text changed by at least 120 characters or the stream has completed
- cap live edits at 30 per streamed response before switching to final-only updates
- after two consecutive Google API update failures or any explicit retry/backoff response over 10 seconds, stop live edits for that response and send or edit only the final text

If message edits prove too constrained, the v1 transport may degrade streaming to final-only updates. The degradation must stay inside `GoogleChatTransport`.

Do not emulate edits by posting sibling thread replies. A user-facing "replacement" message is acceptable only when clearly marked as a new message or final response, because `edit_text(msg, ...)` must not mutate conversation structure unexpectedly.

REST retry mapping:
- HTTP 429 and retryable 5xx/503 responses with a retry hint -> `TransportRetryAfter`
- retryable response without a retry hint -> bounded exponential backoff in `GoogleChatClient`, then surface `TransportRetryAfter` or a transport error
- non-retryable 4xx -> structured transport error with token/credential material redacted

### 4.7 Authentication and authorization

There are two distinct auth concerns:

1. **Google platform auth**
   - HTTP event verification for the Chat app endpoint using Google Chat's bearer token.
   - Service account or app authentication for asynchronous Chat API calls.
   - Required scopes depend on message create/update, attachment, and space/member operations.

2. **Application user auth**
   - Existing `_auth.py` logic still gates trusted users.
   - The transport calls `set_authorizer` before expensive work, mirroring the DoS-defense contract.
   - Identity locking should bind trusted users to stable Google Chat native IDs, not display names.

Google Chat request verification should be explicit. Do not use an opaque shared `endpoint_secret` for v1. Google Chat sends an `Authorization: Bearer ...` token; the token shape depends on the Chat app's configured authentication audience:

- endpoint URL audience: verify a Google-signed OIDC ID token whose audience is the HTTPS endpoint URL
- project number audience: verify a self-signed JWT issued by `chat@system.gserviceaccount.com` whose audience is the configured Google Cloud project number

Config should keep Google credentials and request-verification settings platform-specific:

```json
{
  "google_chat": {
    "service_account_file": "...",
    "app_id": "...",
    "project_number": "...",
    "auth_audience_type": "endpoint_url",
    "allowed_audiences": ["https://example.ngrok.app/google-chat/events"],
    "endpoint_path": "/google-chat/events",
    "public_url": "https://example.ngrok.app"
  }
}
```

The expected issuer is derived from `auth_audience_type` and pinned at startup, not configured by the user, because mismatching the pair is a verification bug rather than a deployment choice:

| `auth_audience_type` | Expected issuer | Audience claim source |
|---|---|---|
| `endpoint_url` | `https://accounts.google.com` (Google-signed OIDC ID token) | configured HTTPS endpoint URL |
| `project_number` | `chat@system.gserviceaccount.com` (self-signed JWT) | configured Google Cloud project number |

Startup should refuse to launch if `auth_audience_type` is missing or unknown, and request handling must reject any token whose `iss` does not match the issuer paired with the configured `auth_audience_type`.

Startup validation:
- `auth_audience_type == "endpoint_url"` requires at least one configured audience. If `allowed_audiences` is empty but `public_url` and `endpoint_path` are set, implementation may derive exactly one audience as `public_url.rstrip("/") + endpoint_path`.
- `auth_audience_type == "project_number"` requires non-empty `project_number`, and the expected audience set contains that project number.
- `service_account_file` is required and must be readable before starting outbound Chat API support.
- `endpoint_path` must start with `/`.
- `callback_token_ttl_seconds` and `pending_prompt_ttl_seconds` must be positive.
- Startup errors must name the missing field, not expose credential contents.

Request verifier contract:
- Input: request headers plus raw body metadata needed by the selected verifier.
- Output: verified claims object with issuer, audience, subject, and expiry, or a rejected request.
- Missing/malformed `Authorization` -> HTTP 401 before queueing.
- Invalid issuer/audience/signature/expiry -> HTTP 401 before queueing.
- Valid platform token but unauthorized app user -> let existing `AllowedUser` auth reject after identity normalization; do not conflate Google platform auth with application auth.
- Never log the raw bearer token.

Duplicate/retry idempotency:
- Keep a bounded TTL cache of recently accepted Google event IDs.
- Prefer a stable event ID from the payload if available.
- If no event ID is available, derive a conservative key from `(eventTime, space.name, message.name, user.name, actionMethodName)` with a short TTL.
- Duplicate delivery must acknowledge successfully but not dispatch handlers twice.

Auth tests:
- valid endpoint-URL audience token -> accepted
- valid project-number audience token -> accepted
- issuer/audience mismatch -> 401
- missing token -> 401
- malformed token -> 401
- duplicate verified event -> no second dispatch
- tests monkeypatch the verifier; unit tests must not depend on live Google certs or network

### 4.8 Spaces, DMs, rooms, and team routing

Google Chat spaces are the room primitive.

Space type mapping:
- `DM` or `DIRECT_MESSAGE` -> `ChatKind.DM`
- `SPACE`, `GROUP_CHAT`, `ROOM`, or any non-DM conversation type Google returns for multi-user spaces -> `ChatKind.ROOM`
- unknown/missing type in a message event -> fail closed for room-only routing and log a redacted warning

Team routing should use the shared post-spec #1 model:
- `RoomBinding.transport_id == "google_chat"`
- `RoomBinding.native_id == "spaces/..."`
- `BotPeerRef.transport_id == "google_chat"`
- `BotPeerRef.native_id` is the peer app/user resource name

Mention routing:
- populate `IncomingMessage.mentions` when Google Chat event payloads expose structured annotations or app mentions
- compare `mentions[*].native_id` to configured peer IDs
- use display names/emails only for logs and UI hints

Mention fallback when annotations are absent:
- DM messages are directed to the bot by definition
- slash commands and card/dialog actions are directed to the app by definition
- room messages are directed only if the raw text contains a recognized app mention token/name for the bot or peer, and that fallback match must be treated as best-effort
- if a configured team room has exactly one active Google Chat bot, normal room text may be accepted as directed to that bot
- otherwise ignore unannotated room text for routing; do not guess from mutable display names alone

Threading:
- if an inbound space message has thread metadata, preserve it in `MessageRef.native`
- `MessageRef.native` stores at least the raw `space.name`, `message.name`, `thread.name` when present, event timestamp, and event idempotency key
- `send_text(..., reply_to=msg)` must post into `msg`'s Google Chat thread when `msg.native` contains thread metadata
- `edit_text(msg, ...)` must update the referenced message only; it must not create a sibling thread reply as an edit fallback
- if `reply_to` lacks thread metadata, send in the same space without pretending to quote
- **`StreamingMessage` overflow rotation must pin every rotated child message to the same thread as the originating message.** When `StreamingMessage` calls `send_text` to create an overflow child, `GoogleChatTransport.send_text` reads thread metadata from the parent `MessageRef.native` and forwards it via Chat API `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` with the parent's `thread.name`. Streams that begin in a thread stay in that thread; streams that begin outside a thread create top-level messages in the same space.
- do not add a new `ChatKind` for threads unless multiple transports need it

### 4.9 Files, attachments, and voice

Google Chat attachment support has more constraints than Telegram/Web. V1 behavior should be conservative:

- inbound attachments become `IncomingFile` only if the transport can download them with configured credentials and the attachment is inside the configured size cap
- use a 25 MiB default inbound size cap to match the Web upload hardening; **Step 8 must verify Google Chat's actual current limit against the [attachments reference](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages.attachments) and lower the default if Google imposes a smaller effective cap.** Document the verified ceiling in the PR description.
- attachment downloads must stream to a temp file and clean up after dispatch, including auth rejection and handler failure paths
- unsupported attachments set `has_unsupported_media=True`
- outbound `send_file` uses Chat API attachment support where practical
- if direct file upload is not available in the chosen auth/surface, send a text fallback with the display name and a clear limitation
- `send_voice` delegates to `send_file`

The transport contract matters more than perfect media parity in the first slice.

Attachment tests:
- supported downloadable attachment -> `IncomingFile` and cleanup after dispatch
- oversized attachment -> no temp-file leak and user-facing unsupported/too-large message where possible
- unsupported/non-downloadable attachment -> `has_unsupported_media=True`
- outbound file unsupported by credentials/scopes -> text fallback, not crash

### 4.10 Implementation-plan readiness checklist

The implementation plan can be written directly from this spec when it covers these files and acceptance criteria.

Files to create:
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

Files to modify:
- `pyproject.toml`
- `src/link_project_to_chat/config.py`
- `src/link_project_to_chat/cli.py`
- `tests/transport/test_contract.py`
- `README.md`
- `docs/CHANGELOG.md`
- `docs/TODO.md`

Acceptance criteria:
- `GoogleChatTransport` passes the shared transport contract for supported capabilities.
- HTTP route rejects invalid Google tokens before queueing or normalizing events.
- HTTP route acknowledges long-running interactions before handler completion.
- `/lp2c help` command events become `CommandInvocation(name="help")`.
- Google command metadata ID is honored when configured.
- Card button clicks become `ButtonClick` only after callback-token verification.
- Text/secret prompt dialog submissions become `PromptSubmission`; wrong-user submissions are rejected.
- DM/ROOM mapping uses Google space type, not display names.
- Stable `Identity.native_id` values feed existing `AllowedUser` authorization.
- No Google payload classes or raw event dict assumptions leak above the transport boundary.
- Unsupported attachments and outbound file limitations degrade through existing transport primitives.
- Duplicate Google event deliveries do not dispatch handlers twice.
- Full verification target: `pytest -q`, `git diff --check`, and `python3 -m compileall -q src/link_project_to_chat`.

## 5. Migration - implementation sequence

Ten steps. Each independently landable and testable.

### Step 1 - Add package/config surface

Add optional Google dependencies, `GoogleChatConfig`, config load/save behavior, startup validation, and a `GoogleChatTransport` skeleton wired into the CLI transport choice.

### Step 2 - Implement Google Chat request verification

Build `google_chat/auth.py`, verifier abstraction, startup auth-mode validation, and tests for valid/invalid issuer/audience/missing token paths.

### Step 3 - Build HTTP receiver and queue

Add FastAPI route(s), verified-event queueing, duplicate/retry suppression, lifecycle management, and fast acknowledgement behavior.

### Step 4 - Implement outbound REST client

Support text create, best-effort edit, rate-limit translation, app authentication for asynchronous messages, and redacted structured errors.

### Step 5 - Implement message and command normalization

Map Google Chat message and slash-command events into `IncomingMessage` and `CommandInvocation`, including stable identities, `ChatRef`, mention annotations, threads, and command metadata IDs.

### Step 6 - Implement Cards v2 buttons

Build card JSON for `Buttons`, correlate callbacks, and dispatch `ButtonClick`.

### Step 7 - Implement prompts/dialogs

Map `PromptSpec` to Cards v2/dialog flows and dispatch `PromptSubmission` for submit/cancel/action events.

### Step 8 - Implement streaming/edit degradation and file fallbacks

Compose with `StreamingMessage`, implement Google-specific edit/final-only degradation, and implement conservative inbound/outbound attachment behavior.

### Step 9 - Validate project/team room behavior

Run project bot and two team bots in Google Chat spaces, verifying stable IDs, mentions, thread behavior, and no Telegram relay assumptions. Builds on the shipped spec #0a primitives (`RoomBinding`/`BotPeerRef`).

### Step 10 - Run manager flows and update docs

Exercise manager commands and prompt-backed wizards end-to-end through Google Chat, with no Google payloads leaking into manager business logic. Builds on the shipped spec #0c manager transport port. Update README, changelog, TODO, and setup notes with the final supported/deferred behavior.

## 6. Testing approach

- Extend transport contract tests so `GoogleChatTransport` satisfies the same behavioral suite as Telegram/Fake/Web/Discord/Slack where practical.
- Add golden event fixtures under `tests/google_chat/fixtures/` for message, slash command, command metadata, card click, dialog submit, added-to-space, attachment, and retry/duplicate event payloads.
- Add auth verifier tests with monkeypatched verification. Do not call live Google cert endpoints in unit tests.
- Add pure unit tests for Cards v2 builders:
  - button rows
  - choice prompt
  - confirm prompt
  - text/secret dialog card
- Add HTTP route tests with sample Google Chat interaction events:
  - normal message
  - slash command
  - card click
  - dialog submit
  - added to space
- Add duplicate/retry tests proving accepted event IDs are dispatched once.
- Add unsupported-feature contract expectations for attachments/editing paths that intentionally degrade.
- Add identity tests proving auth and team routing use stable Google native IDs, not display names.
- Add async response tests proving the HTTP handler returns before the long handler completes.
- Add temp-file cleanup tests for attachment download rejection and handler failure.
- Mock Google REST boundaries; no live Workspace integration suite required for v1.
- Add a manual smoke checklist for a real Google Workspace app:
  - route rejects invalid/missing bearer token
  - `/lp2c help` works in a DM
  - app mention in a space reaches the intended project bot
  - card button click dispatches exactly once
  - prompt dialog submit works and wrong-user submit is rejected
  - long backend response returns immediate ack and later async response
  - service-account REST posting works with configured scopes

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Future | Pub/Sub event delivery as an alternative to HTTP+tunnel |
| Future | Google Workspace Marketplace publication and installation polish |
| Future | Domain-wide admin automation |
| Future | Full attachment parity with Telegram |
| Future | Google Workspace add-on conversion |
| Future | Multi-workspace or multi-domain federation |

## 8. Risks

- **Public ingress requirement for HTTP mode:** local users need a tunnel and a stable HTTPS URL. Mitigation: depend on the tunnel plugin for local development and keep Pub/Sub as a future option.
- **30-second interaction deadline:** Codex work is usually too slow for synchronous responses. Mitigation: acknowledge fast and post/edit asynchronously through the Chat API.
- **Auth/config complexity:** Google Workspace app visibility, service accounts, scopes, and admin allowlists can block usage before code runs. Mitigation: document setup clearly and make startup validation explicit.
- **Cards v2 constraints:** card widgets and dialogs are powerful but not identical to Slack/Discord components. Mitigation: keep mapping narrow and degrade unsupported styles/features.
- **Streaming edit behavior:** Chat API update limits may make high-frequency edits poor. Mitigation: throttle aggressively inside `GoogleChatTransport` and fall back to lower-frequency updates.
- **Identity confusion:** display names and emails can change or be hidden. Mitigation: trust and peer routing use stable native IDs.
- **Sequencing dependency:** this spec depends on #0a (group/team port), #0c (manager port), and at least one HTTP-receiver sibling (Web) landing first. If those slip, this spec slips. Mitigation: per [§3.1](#31-sequencing-prerequisite), do not start implementation until prerequisites are merged in the target branch.

## 9. Reference docs

- Google Chat interaction events: https://developers.google.com/workspace/chat/receive-respond-interactions
- Google Chat commands: https://developers.google.com/workspace/chat/commands
- Google Chat request verification: https://developers.google.com/workspace/chat/verify-requests-from-chat
- Google Chat authentication and authorization: https://developers.google.com/workspace/chat/authenticate-authorize
- Google Chat messages create/update: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages
- Google Chat attachments: https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages.attachments
- Cards v2 reference: https://developers.google.com/workspace/chat/api/reference/rest/v1/cards

## 10. Next steps after this spec ships

1. Write the implementation plan using [§4.10](#410-implementation-plan-readiness-checklist) and [§5](#5-migration---implementation-sequence) as the task skeleton.
2. Confirm product priority relative to Discord/Slack before execution begins.
3. Revisit whether Pub/Sub delivery should be promoted from future option to v1 if HTTP+tunnel proves too brittle.
4. If Google Chat exposes prompt needs that `PromptSpec` cannot express, update the shared transport primitive rather than adding a Google-only workflow layer.
