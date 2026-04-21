# Transport - Slack - Design Spec

**Status:** Designed (2026-04-21). Not yet implemented.
**Date:** 2026-04-21
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a), [2026-04-21-transport-manager-port-design.md](2026-04-21-transport-manager-port-design.md) (spec #0c), [2026-04-21-transport-web-ui-design.md](2026-04-21-transport-web-ui-design.md) (spec #1)
**Part of:** Third additive non-Telegram transport. This is spec #3 of 3 in the Web UI / Discord / Slack track.

---

## 1. Overview

Slack is the last transport in the additive track and the one most likely to punish Telegram-shaped assumptions:
- commands are not plain chat messages by default
- buttons, selects, and modals are strongly interaction-driven
- mentions and identities are stable IDs, not text `@handle` conventions
- app surfaces include DMs, channels, shortcuts, and App Home

That makes Slack a good final validation of the abstractions introduced in spec #1. If `PromptSpec`, structured mentions, and transport-agnostic peer bindings are sufficient for Slack, the transport model is genuinely cross-platform rather than "Telegram with adapters".

**The deliverable:** `SlackTransport`, implemented on top of the shared prompt/session and identity model from spec #1, with native room delivery and no relay concept.

## 2. Goals & non-goals

**Goals**
- Implement `SlackTransport` using Slack-native interactivity, modals, and message events.
- Support DM and room semantics for manager and project/team bot flows.
- Map the shared prompt/session primitive to Slack modals and Block Kit interactions.
- Use structured mentions and stable Slack IDs for team routing and peer identity.
- Preserve the existing transport contract: commands, text, edits, buttons, files, voice-as-file, typing/no-op typing, and prompts.
- Keep bot-to-bot behavior native: `sender.is_bot=True`, `is_relayed_bot_to_bot=False`.

**Non-goals (this spec)**
- A Slack-specific conversation engine separate from `PromptSpec`.
- RTM-era transport behavior or legacy Slack app shapes.
- Audio-call / huddle participation. `send_voice` only needs attachment semantics.
- Hosted/public webhook deployment requirements. Local/private operation remains the baseline.
- Multi-workspace federation in the first implementation slice.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-21:

| # | Question | Decision |
|---|---|---|
| 1 | How should local development/connectivity work? | `slack_bolt` with Socket Mode, so the app can run locally without public ingress |
| 2 | How do commands map into the existing `Transport.on_command` contract? | One root slash command, `/lp2c`, with free-form text parsed into internal command name + args; App Home and shortcut actions may also synthesize `CommandInvocation` |
| 3 | How do prompts map to Slack UI? | `PromptSpec(TEXT/SECRET)` -> modal views; `CHOICE/CONFIRM` -> Block Kit buttons/selects; `DISPLAY` -> informational blocks |
| 4 | How are DM vs room surfaces modeled? | DMs use IM channels; rooms use channels / private channels. App Home is a discovery/dashboard surface, not a separate `ChatRef` kind |
| 5 | What is the identity source of truth? | Stable Slack IDs (`U...`, `B...`, `C...`, etc.), not display names |

## 4. Architecture

### 4.1 Slack transport mapping

```python
ChatRef.transport_id == "slack"
Identity.transport_id == "slack"
MessageRef.transport_id == "slack"
PromptRef.transport_id == "slack"
```

Field mapping:
- `ChatRef.native_id`: IM channel ID for DMs, channel/private-channel ID for rooms
- `ChatRef.kind`: `DM` for IMs, `ROOM` for channels/private channels
- `Identity.native_id`: Slack user ID or bot user ID as a string
- `Identity.handle`: best-effort Slack handle/name if available
- `Identity.display_name`: human-readable profile/display name
- `Identity.is_bot`: true for bot-authored events/messages

Outbound primitives:
- `send_text` / `edit_text`: chat post/update
- `send_file`: file upload or external upload flow
- `send_voice`: audio attachment upload
- `send_typing`: no-op unless a clean Slack-native equivalent is available; the transport contract already allows that

Inbound primitives:
- `/lp2c ...` slash command -> `CommandInvocation`
- App Home actions / message shortcuts / Block Kit actions -> `CommandInvocation` or `ButtonClick`
- modal submissions -> `PromptSubmission`
- DM/channel message events -> `IncomingMessage`

### 4.2 Commands: `/lp2c` as the stable bridge into `CommandInvocation`

Slack does not naturally map every internal command to a separate slash-command registration. The compact shape is:

```text
/lp2c help
/lp2c projects
/lp2c create-project
/lp2c model set gpt-5.2
```

Transport behavior:
- register one slash command, `/lp2c`
- parse the first token of the command text as `CommandInvocation.name`
- parse the rest into `args`
- normalize `raw_text` as `/lp2c <name> ...`

Why this is the right bridge:
- it avoids a Slack-manifest explosion of one slash command per internal action
- it preserves the existing bot-facing command model
- it keeps command discovery simple enough to mirror in App Home and shortcut surfaces

App Home, buttons, or shortcuts may also synthesize the same `CommandInvocation` shape for common manager entry points.

### 4.3 Prompt mapping

Prompt rules:
- `PromptKind.TEXT` / `SECRET`: Slack modal with one input block
- `PromptKind.CHOICE` / `CONFIRM`: Block Kit buttons or select menus
- `PromptKind.DISPLAY`: informational blocks plus actions

State rules:
- the transport only renders and reports the prompt lifecycle
- app-owned `ConversationSession` remains the source of truth for wizard state
- modal callback payloads map into `PromptSubmission`

This follows the same line as Web and Discord: app logic never depends on Slack modal payload internals directly.

### 4.4 Structured mentions and peer identity

Slack mentions are structured and ID-based. The transport must populate:

```python
IncomingMessage.mentions: list[Identity]
```

Team routing therefore becomes:
- compare `incoming.mentions[*].native_id` to configured `BotPeerRef.native_id`
- compare `incoming.reply_to` sender identity as a secondary signal
- never rely on display names for correctness

Config expectations:
- `RoomBinding.transport_id == "slack"`
- `RoomBinding.native_id` is the channel/private-channel ID
- `BotPeerRef.transport_id == "slack"`
- `BotPeerRef.native_id` is the peer bot user ID

This is the same shared model introduced in spec #1, which is exactly why Slack no longer needs a custom routing layer.

### 4.5 App Home role

App Home is useful, but it is not a new transport kind.

Use App Home for:
- discoverability
- manager dashboard entry points
- pinned shortcuts into common commands and flows

Do **not** treat App Home as a separate `ChatRef`:
- the actual conversational DM surface remains the IM channel
- room behavior remains channels/private channels

This keeps the shared transport model simple while still giving Slack a first-class home surface.

### 4.6 Files, rich text, and edits

Files:
- inbound attachments download to local disk and become `IncomingFile`
- outbound files and voice responses upload through Slack's file APIs

Rich text:
- `html=True` is converted to Slack-friendly formatting where possible
- unsupported constructs degrade to plain text instead of throwing

Edits:
- `edit_text` maps to message update where the surface supports it
- if a given Slack surface limits edits, the transport should degrade predictably rather than leak Slack-only failure modes into bot code

## 5. Migration - implementation sequence

Seven steps. Each independently landable.

### Step 1 - Add Slack package/config surface

Add `slack_bolt` / Socket Mode dependencies, Slack transport config, and `SlackTransport` skeleton.

### Step 2 - Implement outbound primitives

Text, edits, files, voice-as-file, and basic message/thread references.

### Step 3 - Implement `/lp2c` command bridge

Parse slash-command text into `CommandInvocation`, and add optional App Home / shortcut entry points for common actions.

### Step 4 - Implement prompt mapping

Map `PromptSpec` to modals and Block Kit interactions, with submissions normalized into `PromptSubmission`.

### Step 5 - Implement inbound messages and structured mentions

Normalize DM/channel message events into `IncomingMessage`, including `mentions`, `reply_to`, and bot identities.

### Step 6 - Validate team rooms and native bot-to-bot delivery

Run multiple Slack bot identities in the same channel/private-channel and verify routing through `sender.is_bot=True` with no relay.

### Step 7 - Run manager/project flows on Slack

Exercise command, prompt, file, and room flows end-to-end using only the shared abstractions from spec #1.

## 6. Testing approach

- Extend transport contract tests so `SlackTransport` satisfies the same suite as Telegram/Fake/Web/Discord.
- Add prompt tests for modal open -> submit and choice/confirm button flows.
- Add command parsing tests for `/lp2c <name> ...`.
- Add structured-mention tests that prove routing works by Slack IDs, not display names.
- Mock Slack API boundaries in unit tests; no live workspace integration suite required in the first slice.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Future | Hosted/public ingress deployment patterns instead of Socket Mode |
| Future | Workspace install/distribution polish |
| Future | Huddles / live call participation |
| Future | Cross-workspace multi-home transport fan-out |

## 8. Risks

- **Slack surface fragmentation:** slash commands, App Home, DMs, channels, buttons, and modals all have different payload shapes. Mitigation: normalize everything aggressively at the transport boundary.
- **Overusing slash commands:** forcing every action through `/lp2c` would feel clumsy. Mitigation: keep `/lp2c` as the stable command bridge, but use App Home/buttons for discoverability.
- **Edit limitations / API quirks:** some Slack surfaces may not behave like Telegram/Discord edits. Mitigation: degrade cleanly inside `SlackTransport` rather than leaking platform details upward.
- **Identity confusion:** display names are mutable and non-unique. Mitigation: all peer routing and trust decisions use stable Slack IDs.

## 9. Next steps after this spec ships

1. At that point the additive transport track is complete: Telegram, Web, Discord, and Slack all fit the same shared transport model.
2. Any cleanup needed for Telegram should happen by consuming the shared prompt/session and structured-mention primitives, not by adding more Telegram-specific bypasses.
