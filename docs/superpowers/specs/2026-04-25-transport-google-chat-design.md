# Transport - Google Chat - Design Spec

**Status:** Designed (2026-04-25). Not yet implemented.
**Date:** 2026-04-25
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a), [2026-04-21-transport-manager-port-design.md](2026-04-21-transport-manager-port-design.md) (spec #0c), [2026-04-21-transport-web-ui-design.md](2026-04-21-transport-web-ui-design.md) (spec #1)
**Part of:** Fourth additive non-Telegram transport. This follows Web, Discord, and Slack and validates HTTPS event delivery plus card/dialog UI.

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

Implementation starts only after these prerequisites are landed in the target branch:

- spec #0a group/team transport port, so team room routing no longer depends on Telegram-only group IDs
- spec #0c manager transport port, so manager flows can run through transport primitives
- at least one HTTP-receiver sibling, specifically Web transport, so Google Chat can reuse the repo's proven ASGI lifecycle, dispatch queue, and test patterns

Google Chat should consume these primitives rather than reintroducing platform-native state into project or manager bot code.

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
- `edit_text`: `spaces.messages.update` where supported; degrade by posting a replacement message if editing is unavailable for the message/surface
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

The transport owns a small ASGI service, likely following the Web transport's FastAPI/uvicorn shape:

```text
src/link_project_to_chat/google_chat/
  __init__.py
  app.py          # FastAPI app and POST route for Chat events
  client.py       # Google Chat REST client/auth wrapper
  cards.py        # Cards v2 builders for Buttons and PromptSpec
  transport.py    # GoogleChatTransport implementation
```

HTTP flow:
1. Google Chat POSTs an interaction event to the configured endpoint.
2. The route validates the request as far as Google Chat auth/config allows.
3. The route normalizes the event into an internal queue item.
4. The route returns quickly with either an empty/small acknowledgement or a synchronous response message.
5. The dispatch task invokes registered transport handlers.
6. Long-running handler output posts/edits asynchronously through the Chat API.

This is deliberately similar to `WebTransport`: HTTP routes translate platform events, while `GoogleChatTransport` owns the protocol contract and dispatch behavior.

### 4.3 Commands: `/lp2c` as the stable bridge

Google Chat supports slash commands registered in Google Cloud Console. The transport should register one root command:

```text
/lp2c help
/lp2c projects
/lp2c create-project
/lp2c model set gpt-5.2
```

Transport behavior:
- parse the first token after `/lp2c` as `CommandInvocation.name`
- parse remaining tokens into `args`
- normalize `raw_text` as `/lp2c <name> ...`
- preserve native command metadata in `CommandInvocation.native`

Why one root command:
- avoids a long command registration list in Google Cloud Console
- mirrors Discord and Slack specs
- keeps command handling portable while allowing cards/dialogs to provide nicer entry points later

If Google Chat delivers an app command payload instead of plain text, the transport should still synthesize the same `CommandInvocation` shape.

### 4.4 Cards v2 and buttons

Cards v2 is the primary Google Chat UI surface for transport buttons and compact menus.

Mapping rules:
- `Buttons` -> card widgets with button actions
- `Button.value` -> action parameter stored in the card payload
- `ButtonStyle.PRIMARY` -> visually prominent button where Cards v2 supports it, otherwise default button
- `ButtonStyle.DANGER` -> text/icon treatment where supported, otherwise default button with a destructive label
- button click event -> `ButtonClick(chat, message, sender, value, native=event)`

The card builder should be isolated in `google_chat/cards.py` so tests can verify pure JSON output without starting the transport.

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
- `PromptRef.native_id` should encode the message/dialog/action token needed to correlate future callbacks
- prompt submissions must verify the submitting user matches the session sender before dispatching upward

Dialog caveat:
- Google Chat dialogs are tied to interaction callbacks. If a text/secret prompt is opened outside a valid interaction context, the transport falls back to a message-then-reply flow:
  - post the prompt body as a normal message in the target space and store a `PromptRef` whose `native_id` records the originating space + sender + a server-generated correlation token
  - mark that `(space, sender)` pair as expecting a reply for that `PromptRef`
  - treat the next plain text message from that exact sender in that space as the `PromptSubmission`
  - ignore messages from other senders for the purpose of this prompt; they still flow through `on_message` normally
  - expire the pending reply on the same timeout the dialog path uses (configurable, with a sane default), and emit a cancel/timeout submission so app state does not leak
- This same pattern will likely apply to Discord's design-only fallback. Whichever transport ships first owns settling the concrete timeout value and any helper extracted to shared code; the second transport must adopt that decision rather than re-litigating it.

### 4.6 Async responses and streaming

Google Chat interaction handlers must return quickly. Codex responses can take far longer, so streaming needs a split path:

- initial HTTP response: fast acknowledgement, optional "Working..." message/card
- `send_text`: asynchronous message create through Chat API
- `edit_text`: asynchronous message update where supported
- `StreamingMessage`: edit-based updates with a conservative v1 throttle. The shared helper at [`transport/streaming.py`](../../../src/link_project_to_chat/transport/streaming.py) already owns time-based throttling, overflow rotation, and `TransportRetryAfter` back-off; Google Chat composes by constructing `StreamingMessage(transport, chat, throttle=2.0, ...)` and layers the additional Google-specific caps below (delta-size minimum, live-edit count, failure trigger) inside `GoogleChatTransport.edit_text`. Do not duplicate the throttle logic inside `StreamingMessage`.
- rate limits: translate Google API retry/backoff signals into `TransportRetryAfter` when the caller can use it

V1 streaming budget:
- no more than one edit every 2 seconds per message
- skip edits unless the rendered text changed by at least 120 characters or the stream has completed
- cap live edits at 30 per streamed response before switching to final-only updates
- after two consecutive Google API update failures or any explicit retry/backoff response over 10 seconds, stop live edits for that response and send or edit only the final text

If message edits prove too constrained, the v1 transport may degrade streaming to periodic appended messages or final-only updates. The degradation must stay inside `GoogleChatTransport`.

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
    "auth_audience_type": "endpoint_url",
    "allowed_audiences": ["https://example.ngrok.app/google-chat/events"]
  }
}
```

The expected issuer is derived from `auth_audience_type` and pinned at startup, not configured by the user, because mismatching the pair is a verification bug rather than a deployment choice:

| `auth_audience_type` | Expected issuer | Audience claim source |
|---|---|---|
| `endpoint_url` | `https://accounts.google.com` (Google-signed OIDC ID token) | configured HTTPS endpoint URL |
| `project_number` | `chat@system.gserviceaccount.com` (self-signed JWT) | configured Google Cloud project number |

Startup should refuse to launch if `auth_audience_type` is missing or unknown, and request handling must reject any token whose `iss` does not match the issuer paired with the configured `auth_audience_type`.

Exact field names can be refined during implementation, but the spec requires audience/issuer-driven verification rather than shared-secret checking. Secrets and credential paths must follow the existing config rule: write files with `0o600`, avoid logging raw tokens, and keep backward-compatible migrations explicit.

### 4.8 Spaces, DMs, rooms, and team routing

Google Chat spaces are the room primitive.

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
- `send_text(..., reply_to=msg)` must post into `msg`'s Google Chat thread when `msg.native` contains thread metadata
- `edit_text(msg, ...)` must update the referenced message only; it must not create a sibling thread reply as an edit fallback
- if `reply_to` lacks thread metadata, send in the same space without pretending to quote
- do not add a new `ChatKind` for threads unless multiple transports need it

### 4.9 Files, attachments, and voice

Google Chat attachment support has more constraints than Telegram/Web. V1 behavior should be conservative:

- inbound attachments become `IncomingFile` only if the transport can download them with configured credentials
- unsupported attachments set `has_unsupported_media=True`
- outbound `send_file` uses Chat API attachment support where practical
- if direct file upload is not available in the chosen auth/surface, send a text fallback with the display name and a clear limitation
- `send_voice` delegates to `send_file`

The transport contract matters more than perfect media parity in the first slice.

## 5. Migration - implementation sequence

Eight steps. Each independently landable.

### Step 1 - Add package/config surface

Add optional Google dependencies, config fields, and a `GoogleChatTransport` skeleton wired into the CLI transport choice.

### Step 2 - Build HTTP receiver

Add FastAPI route(s), event validation hook, queueing, lifecycle management, and fast acknowledgement behavior.

### Step 3 - Implement outbound REST client

Support text create, best-effort edit, rate-limit translation, and app authentication for asynchronous messages.

### Step 4 - Implement command and message normalization

Map Google Chat `MESSAGE` and slash command events into `IncomingMessage` and `CommandInvocation`, including stable identities and `ChatRef`.

### Step 5 - Implement Cards v2 buttons

Build card JSON for `Buttons`, correlate callbacks, and dispatch `ButtonClick`.

### Step 6 - Implement prompts/dialogs

Map `PromptSpec` to Cards v2/dialog flows and dispatch `PromptSubmission` for submit/cancel/action events.

### Step 7 - Validate project/team room behavior

Run project bot and two team bots in Google Chat spaces, verifying stable IDs, mentions, thread behavior, and no Telegram relay assumptions. Requires spec #0a per [§3.1](#31-sequencing-prerequisite); do not start until that port is merged.

### Step 8 - Run manager flows

Exercise manager commands and prompt-backed wizards end-to-end through Google Chat, with no Google payloads leaking into manager business logic. Requires spec #0c per [§3.1](#31-sequencing-prerequisite); do not start until that port is merged.

## 6. Testing approach

- Extend transport contract tests so `GoogleChatTransport` satisfies the same behavioral suite as Telegram/Fake/Web/Discord/Slack where practical.
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
- Add identity tests proving auth and team routing use stable Google native IDs, not display names.
- Add async response tests proving the HTTP handler returns before the long handler completes.
- Mock Google REST boundaries; no live Workspace integration suite required for v1.

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
- Google Chat authentication and authorization: https://developers.google.com/workspace/chat/authenticate-authorize
- Google Chat request verification: https://developers.google.com/workspace/chat/verify-requests-from-chat
- Cards v2 reference: https://developers.google.com/workspace/chat/api/reference/rest/v1/cards

## 10. Next steps after this spec ships

1. Write an implementation plan for `GoogleChatTransport` after Discord/Slack priority is confirmed.
2. Revisit whether Pub/Sub delivery should be promoted from future option to v1 if HTTP+tunnel proves too brittle.
3. If Google Chat exposes prompt needs that `PromptSpec` cannot express, update the shared transport primitive rather than adding a Google-only workflow layer.
