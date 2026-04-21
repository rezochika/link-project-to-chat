# Transport - Discord - Design Spec

**Status:** Designed (2026-04-21). Not yet implemented.
**Date:** 2026-04-21
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b), [2026-04-21-transport-group-team-port-design.md](2026-04-21-transport-group-team-port-design.md) (spec #0a), [2026-04-21-transport-manager-port-design.md](2026-04-21-transport-manager-port-design.md) (spec #0c), [2026-04-21-transport-web-ui-design.md](2026-04-21-transport-web-ui-design.md) (spec #1)
**Part of:** Second additive non-Telegram transport. This is spec #2 of 3 in the Web UI / Discord / Slack track.

---

## 1. Overview

After spec #1, the shared transport model has the missing pieces future transports need:
- prompt/session primitives for manager-style wizards
- structured mentions on inbound messages
- transport-agnostic room and peer identity config

Discord is the first external chat platform to consume those abstractions. Unlike Telegram, it has native bot-to-bot delivery in channels. Unlike Web, it has strong interaction primitives (`/` commands, buttons, selects, modals) and stable snowflake IDs for users, bots, channels, and threads.

**The deliverable:** `DiscordTransport`, wired to the existing project bot and manager bot surfaces without adding new Telegram-shaped compromises. Team rooms must use native Discord bot traffic (`sender.is_bot=True`) rather than any relay mechanism.

## 2. Goals & non-goals

**Goals**
- Implement `DiscordTransport` using Discord-native interactions and message events.
- Map the shared prompt/session primitive from spec #1 onto Discord components and modals.
- Support DM and room semantics (`ChatKind.DM` and `ChatKind.ROOM`) for both manager and project/team bot flows.
- Use structured mentions and stable snowflake IDs for room routing and team-peer identity.
- Validate native bot-to-bot room traffic with `sender.is_bot=True` and no `is_relayed_bot_to_bot` path.
- Preserve the existing transport contract: text, edits, files, voice, typing, buttons, commands, and prompts.

**Non-goals (this spec)**
- A Discord-specific workflow abstraction separate from `PromptSpec`.
- Voice-channel participation, stage channels, or live audio input. `send_voice` only needs attachment-level behavior.
- Full moderation/admin automation beyond what the current manager already does.
- Simultaneous Telegram+Discord fan-out from one process.
- Importing Discord-only concepts into the shared `Transport` Protocol unless another transport needs them too.

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-21:

| # | Question | Decision |
|---|---|---|
| 1 | Which Discord surface should drive command handling? | Interactions-first: a root `/lp2c` application command with subcommands, synthesized into `CommandInvocation` |
| 2 | How do prompts map to Discord UI? | `PromptSpec(TEXT/SECRET)` uses modals when interaction context exists; `CHOICE/CONFIRM` uses buttons/selects; message fallback only when modal launch is impossible |
| 3 | What is the identity source of truth? | Stable snowflake IDs (`native_id`), not usernames or global display names |
| 4 | How do team rooms work? | Native channels/threads only; no relay layer and no text-regex mention parsing as the primary routing path |
| 5 | Library choice | `discord.py` 2.x (application commands, components, modals, attachments in one library) |

## 4. Architecture

### 4.1 Discord transport mapping

```python
ChatRef.transport_id == "discord"
Identity.transport_id == "discord"
MessageRef.transport_id == "discord"
PromptRef.transport_id == "discord"
```

Field mapping:
- `ChatRef.native_id`: DM channel ID, guild channel ID, or thread ID as a string
- `ChatRef.kind`: `DM` for private bot chats, `ROOM` for guild text channels / private threads / public threads
- `Identity.native_id`: Discord user or bot user snowflake as a string
- `Identity.handle`: username-ish display value, best effort only
- `Identity.display_name`: guild nickname or global display name
- `Identity.is_bot`: `author.bot`

Outbound primitives:
- `send_text` / `edit_text`: Discord message create/edit
- `send_file`: file attachment upload
- `send_voice`: audio attachment upload (no voice-channel semantics required)
- `send_typing`: channel typing indicator

Inbound primitives:
- application command interactions -> `CommandInvocation`
- message component interactions -> `ButtonClick`
- modal submits / select submits -> `PromptSubmission`
- normal message events -> `IncomingMessage`

### 4.2 Commands: root `/lp2c` command, not raw message parsing

Discord application commands are the primary command surface:

```text
/lp2c help
/lp2c projects
/lp2c model set gpt-5.2
/lp2c create-project
```

Transport behavior:
- Register a root `lp2c` command with subcommands that mirror the existing internal command names.
- Synthesize `CommandInvocation.name` from the subcommand and `args` from the option payload.
- Preserve `raw_text` in a normalized form such as `/lp2c projects`.

Why not raw slash-like message parsing:
- interaction-first avoids Discord message-content dependence for commands
- command discoverability and permissions are better
- the transport can still hand the bot the same `CommandInvocation` shape it already expects

Free-text DM and room messages still flow through `on_message`; only explicit commands become `CommandInvocation`.

### 4.3 Prompt mapping

Prompt rules:
- `PromptKind.TEXT` / `SECRET`: launch a Discord modal when the user is already in an interaction flow (command, button, or select)
- `PromptKind.CHOICE` / `CONFIRM`: use buttons or a select menu
- `PromptKind.DISPLAY`: render informational response plus actions

Fallback behavior:
- if a text prompt must be shown without an interaction context, send a normal message prompt and treat the user's next text message in that session as the submission
- the app-level `ConversationSession` remains the source of truth either way

This preserves the shared abstraction from spec #1: the bot emits `PromptSpec`, while `DiscordTransport` chooses the best Discord-native rendering for that step.

### 4.4 Structured mentions and team-peer routing

Discord mentions are structured and ID-based. The transport must populate:

```python
IncomingMessage.mentions: list[Identity]
```

Team/group routing therefore works like this:
- check `incoming.mentions` against configured `BotPeerRef.native_id`
- use `reply_to` plus bot sender identity as a secondary signal
- use `handle` only for display/logging, never as the primary routing key

Config expectations:
- `RoomBinding.transport_id == "discord"`
- `RoomBinding.native_id` is the target channel/thread snowflake
- `BotPeerRef.transport_id == "discord"`
- `BotPeerRef.native_id` is the peer bot's user ID

This is the key improvement over Telegram-shaped logic: no regex over raw text is required for correctness.

### 4.5 Manager and project UX on Discord

Manager bot:
- admin commands via `/lp2c ...`
- wizard flows via prompt-backed modals/buttons
- project list / detail navigation via message components

Project bot:
- DMs for one-to-one use
- room channels/threads for group/team use
- streaming edits remain message edits
- buttons remain message components

Group/team behavior:
- peer bots are just normal Discord bot authors in the same channel/thread
- `incoming.sender.is_bot=True` is the native bot-to-bot signal
- `incoming.is_relayed_bot_to_bot` stays `False`

### 4.6 Rich text and files

Rich text:
- shared `html=True` hint is converted to Discord-supported formatting where possible
- unsupported HTML constructs degrade to plain text rather than erroring

Files:
- `IncomingFile` is populated from Discord attachments after download
- voice/audio inputs continue through the same `IncomingFile` path used by spec #0b

## 5. Migration - implementation sequence

Seven steps. Each independently landable.

### Step 1 - Add Discord package and config surface

Add `discord.py` dependency, transport config fields, and `DiscordTransport` skeleton.

### Step 2 - Implement outbound transport methods

Text, edits, files, voice-as-attachment, typing, and message reference conversion.

### Step 3 - Implement interaction-driven commands and buttons

Register `/lp2c` subcommands and map component callbacks into `CommandInvocation` / `ButtonClick`.

### Step 4 - Implement prompt mapping

Map `PromptSpec` to modals/buttons/selects plus message fallback where needed.

### Step 5 - Implement inbound messages and structured mentions

Populate `IncomingMessage`, `mentions`, `reply_to`, and bot identities from Discord events.

### Step 6 - Validate project/team native room behavior

Run multiple Discord bot identities in a shared room and verify native bot-to-bot delivery with stable peer IDs.

### Step 7 - Run manager flows on Discord

Exercise the manager's prompt-backed flows end-to-end without Telegram-specific state or PTB-style shims.

## 6. Testing approach

- Extend transport contract tests so `DiscordTransport` passes the same suite as Telegram/Fake/Web.
- Add prompt-specific tests: modal submit, choice buttons, confirm buttons, message fallback.
- Add mention-routing tests using real Discord-style mention payload normalization into `IncomingMessage.mentions`.
- Add room tests with two bot identities in one channel/thread verifying native `sender.is_bot=True` delivery.
- Mock Discord client/network boundaries in unit tests; no live Discord integration suite required for the first slice.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Future | Voice-channel / stage-channel participation |
| Future | Discord-specific moderation/admin APIs outside the current manager feature set |
| Future | Context-menu commands and other discovery polish |
| Future | Cross-posting the same bot state to Telegram and Discord at once |

## 8. Risks

- **Interaction context limits:** Discord modals open from interaction flows, not arbitrary background code. Mitigation: keep the message fallback path for text prompts.
- **Guild vs DM permissions:** command registration and channel permissions may differ across servers. Mitigation: keep the root command surface compact and room bindings explicit.
- **Overfitting to slash commands:** some flows still need free text after a modal/button. Mitigation: command entry is interaction-first, but `on_message` remains first-class.
- **Identity mismatch bugs:** usernames and display names are not stable enough for team routing. Mitigation: all peer matching is by snowflake ID.

## 9. Next steps after this spec ships

1. Reuse the same prompt/session runtime and peer-identity model for Slack; do not fork the abstraction.
2. If Discord implementation reveals prompt gaps, fix them in the shared spec #1 primitives rather than adding Discord-only escape hatches.
