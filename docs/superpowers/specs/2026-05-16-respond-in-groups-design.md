# Standard project bot: respond in Telegram groups (`respond_in_groups`)

**Date:** 2026-05-16
**Status:** Approved (brainstorming complete; awaiting plan)
**Author:** Revaz Chikashua (drafted with Claude)
**Target release:** v1.1.0 (additive on top of `feat/plugin-system` v1.0.0)

## Summary

Restore the GitLab fork's group-handling behavior for **standard (non-team) project bots** on Telegram. When `ProjectConfig.respond_in_groups: bool` is set, a project bot added to a Telegram group responds only when explicitly addressed (`@bot_username` mention OR a reply to a prior bot message). All other group messages are silently ignored. The flag is off by default — no behavior change for existing deployments.

The change is small and surgical: one new config field, a 3-way switch on `TelegramTransport`'s PTB filter, and a ~30-line routing gate in `ProjectBot._on_text_from_transport` that reuses the existing `group_filters.is_directed_at_me` predicate.

## Background

`bot.py` has zero direct telegram imports; all platform calls go through the `Transport` Protocol. The GitHub version of this codebase couples "group participation" to team mode via [`self.group_mode = team_name is not None`](../../src/link_project_to_chat/bot.py:262). Two side effects fall out of that coupling:

1. **PTB filter is mode-partitioned.** [`attach_telegram_routing(group_mode=...)`](../../src/link_project_to_chat/transport/telegram.py:263) hard-selects between `ChatType.PRIVATE` and `ChatType.GROUPS`. A standard project bot's filter is `PRIVATE` only — **group messages are dropped by python-telegram-bot before the dispatcher is even called**.
2. **`_handle_group_text` is team-only.** The mention / reply-to-bot gate, the auto-capture of `RoomBinding`, the bot-to-bot peer detection, the halt / round-counter all live behind `if self.group_mode:` in [`_on_text_from_transport`](../../src/link_project_to_chat/bot.py:954).

The GitLab fork (`/gl/link-project-to-chat`, the merge source for the v1.0.0 plugin work) registers its `MessageHandler` with `TEXT & ~COMMAND` — no `ChatType` filter — and runs an inline mention / reply-to-bot check inside `_on_text`. The result: a single project bot can be added to a group, gets `@MyBot do X` and "reply to bot" semantics, and ignores everything else.

The solo-bot group capability was scoped out of the GitHub fork during the transport-abstraction track (specs #0 / #0a / #0c) when `group_mode` became coupled to team membership. The v1.0.0 plugin merge folded in the GitLab fork's plugin framework and `AllowedUser` role model but didn't restore the solo-group routing — that's what this spec addresses.

See [docs/TODO.md §1.4](../../docs/TODO.md) for the v1.0.0 plugin-system port (which is what this spec builds on).

## Goals

1. **Per-project opt-in flag.** `ProjectConfig.respond_in_groups: bool = False`. Operators set it via `config.json`, the `configure` CLI flag, the `projects edit` CLI command, or the manager bot's project-edit UI.
2. **GitLab-equivalent routing for solo bots:**
   - Text in groups: respond only when `@bot_username` is in the text OR the message is a reply to the bot's prior message.
   - Captioned files / voice / photos in groups: same `@mention OR reply-to-bot` gate against the caption.
   - Mention-stripping: `@bot_username` removed from the prompt before the agent sees it.
   - Reply-to-context: existing `_build_user_prompt` handles `[Replying to: …]` prefixing via `IncomingMessage.reply_to_text` / `reply_to_sender`.
3. **`/commands` work in groups when the flag is on.** Telegram routes `/cmd@MyBot` to the right bot in multi-bot rooms. Existing role gating (executor / viewer) applies in groups exactly as in DM.
4. **Defense-in-depth against bot-to-bot loops.** A solo bot in a group **ignores all other bots' messages**, including `@MyBot` from a peer bot. Peer-bot workflows remain team-mode-only.
5. **Existing behavior preserved when the flag is off** — pre-v1.1.0 configs load with no change.

## Non-goals

- Auto-capture of `RoomBinding`, halt / round-counter, peer-bot dispatch. Those are team-mode concepts; solo bots have no canonical bound room and shouldn't talk to other bots.
- Web / Discord / Slack ports of the flag. The bot-side routing gate reads `incoming.chat.kind == ChatKind.ROOM` so it's transport-portable, but no non-Telegram transport currently produces `ROOM` chats. Widening the flag to Discord / future Slack is a separate spec.
- A runtime `--respond-in-groups` CLI flag at `start` time. Per-project config is the only activation surface.
- Allow-listing specific group IDs (e.g. "respond only in supergroup X"). YAGNI; can land in a follow-up if operators demand it.
- Plugin opt-in for observing non-addressed group messages. Plugins continue to see addressed-at-me messages only (consistent with the existing "plugins see authorized, addressed messages" pattern). A `Plugin.observe_unfiltered_group_messages: bool` opt-in is a future enhancement.

## Architecture

**Approach: surgical pre-filter + bot-side routing gate.** Reuses `group_filters.is_directed_at_me`, `is_from_self`; reuses `_on_text`; no new helper module.

```
Telegram group message
  ↓
PTB MessageHandler                                  ← filter widened by
  filter = ChatType.PRIVATE | ChatType.GROUPS         attach_telegram_routing
  ↓                                                   when respond_in_groups=True
TelegramTransport._dispatch_message
  → IncomingMessage (chat.kind = ChatKind.ROOM)
  ↓
ProjectBot._on_text_from_transport
  if self.group_mode:                                team-mode path (unchanged)
      handled = await self._handle_group_text(...)
      …
  elif self._respond_in_groups and chat.kind==ROOM:   ← NEW solo-group gate
      if is_from_self(...): return                    ← silent
      if sender.is_bot: return                        ← silent (peer-bot defense)
      if not is_directed_at_me(...): return           ← silent (drive-by)
      incoming = self._strip_self_mention(incoming)
  await self._on_text(incoming)                       existing flow:
                                                        auth, rate-limit,
                                                        plugin on_message,
                                                        _guard_executor,
                                                        pending skill/persona,
                                                        waiting input,
                                                        _build_user_prompt
                                                        (prepends [Replying to: …]
                                                         via reply_to_text),
                                                        backend submit
```

The team-mode branch (`if self.group_mode: …`) is untouched. Solo-group support is a sibling `elif` branch — clean separation, no shared mutable state with the team path.

## Components

### Files modified

1. **`src/link_project_to_chat/config.py`**
   - Add `respond_in_groups: bool = False` to `ProjectConfig`.
   - `_load_config_unlocked` reads `proj.get("respond_in_groups", False)`. Non-bool values (string `"yes"`, list, dict) coerce to False with a `logger.warning`; int 0/1 coerce via `bool()` (Python's default truthiness).
   - `_save_config_unlocked` writes the field only when `True` — keeps `config.json` compact.

2. **`src/link_project_to_chat/bot.py`**
   - `ProjectBot.__init__` accepts `respond_in_groups: bool = False` → `self._respond_in_groups`.
   - New private helper `_strip_self_mention(incoming: IncomingMessage) -> IncomingMessage`. Pure; uses `dataclasses.replace(incoming, text=cleaned)`. Removes case-insensitive `@<self.bot_username>` from text (and from caption text — captions ride on `incoming.text` per `TelegramTransport._dispatch_message`). Safe when `self.bot_username == ""` (returns unchanged).
   - New routing gate in `_on_text_from_transport` as the `elif` branch shown in §Architecture.
   - `run_bot` / `run_bots` plumb `respond_in_groups` through to `ProjectBot`. Mirrors the existing `plugins` / `allowed_users` / `auth_source` plumbing pattern from v1.0.0.

3. **`src/link_project_to_chat/transport/telegram.py`**
   - `attach_telegram_routing(*, group_mode, command_names, respond_in_groups=False)` — new kwarg.
   - Filter selection becomes a 3-way switch:
     ```python
     if group_mode:           chat_filter = filters.ChatType.GROUPS
     elif respond_in_groups:  chat_filter = filters.ChatType.PRIVATE | filters.ChatType.GROUPS
     else:                    chat_filter = filters.ChatType.PRIVATE
     ```
   - New instance flag `self._respond_in_groups_attached: bool` set alongside `_routing_attached` / `_group_mode_attached`.
   - `on_command`'s post-routing dynamic registration picks the matching filter (mirror the 3-way switch).

4. **`src/link_project_to_chat/cli.py`**
   - `projects add`: add `--respond-in-groups / --no-respond-in-groups` flag pair; default off. Writes `entry["respond_in_groups"] = True` only when explicitly on.
   - `projects_edit`: add `"respond_in_groups"` to `_EDITABLE` and a branch that parses `value.lower()` as bool — accepts `true|1|yes|on` → True; `false|0|no|off` → False; anything else → `SystemExit("Invalid bool for respond_in_groups: …")`.

5. **`src/link_project_to_chat/manager/bot.py`**
   - Add `"respond_in_groups"` to `_EDITABLE_FIELDS` so the keyboard auto-generates the edit button.
   - `_apply_edit` gains a branch that parses bool with the same accepted-values list as the CLI and writes through `_save_projects`.

### No new modules, no schema migration

The flag defaults to False. Pre-v1.1.0 configs load with `respond_in_groups=False` and the bot behaves exactly as it does today (DM-only). No migration step needed.

### What stays untouched

`_handle_group_text`, `GroupStateRegistry`, `RoomBinding`, the team-relay path, the plugin lifecycle, the role-gating helpers (`_guard_executor`, `_wrap_with_persist`, `_require_executor`), auth, rate-limit, `_persist_auth_if_dirty`, conversation log, the backend layer, the Web / Fake transports.

## Data flow

End-to-end trace for a few representative paths once `respond_in_groups=True` is set on a project. The bot is `@MyBot`; `alice` is `executor`, `viewer-bob` is `viewer`, `mallory` is not in the allow-list.

### Path A — Authorized executor `@MyBot do X`

1. PTB filter `PRIVATE | GROUPS` matches the group text.
2. `TelegramTransport._dispatch_message` → `IncomingMessage(chat.kind=ROOM, mentions=[…MyBot…], reply_to_*=None)`.
3. `ProjectBot._on_text_from_transport`:
   - `group_mode == False`, `_respond_in_groups == True`, `chat.kind == ROOM`.
   - `is_from_self` False; `sender.is_bot` False; `is_directed_at_me` True.
   - `_strip_self_mention`: `"Hi @MyBot do X"` → `"Hi  do X"`.
4. `_on_text(incoming)`:
   - `_auth_identity(alice_identity)` True (alice in `_allowed_users`).
   - `_rate_limited` False.
   - `_dispatch_plugin_on_message` (fires for active plugins).
   - `_guard_executor` True (alice has role `executor`).
   - `_build_user_prompt` — prepends conversation history; persona block if active; `[Replying to: …]` if `reply_to_text` was set.
   - `task_manager.submit_agent` → backend stream → reply threaded under alice's group message.

### Path B — Authorized viewer @-mentions for a state-changing intent

Same as Path A until `_guard_executor` returns False. Bot replies `"Read-only access — your role is viewer."` threaded under the viewer's group message. No backend turn.

### Path C — Unauthorized user @-mentions

Same as Path A until `_auth_identity` returns False. Bot replies `"Unauthorized."` threaded under mallory's message. Brute-force counter on `_identity_key("telegram:<mallory_id>")` increments; the 6th failed attempt is silently dropped (process-lifetime lockout from Task 5).

### Path D — Authorized user sends a plain group message (no mention, no reply-to-bot)

`_respond_in_groups AND chat.kind==ROOM` matches; `is_directed_at_me` returns False → **silent return**. No auth check runs (intentional — preserves quiet bot UX). No plugin dispatch. No `_persist_auth_if_dirty`.

### Path E — Another bot in the same group `@MyBot`s

`_respond_in_groups AND chat.kind==ROOM` matches; `is_from_self` False; `sender.is_bot` True → **silent return**. Defense against bot-to-bot loops. Solo mode never accepts peer-bot input; team workflows remain team-mode-only.

### Path F — `/tasks@MyBot` command in group

1. PTB CommandHandler `/tasks` filter `PRIVATE | GROUPS` matches; `/tasks@MyBot` syntax routes only to MyBot.
2. `TelegramTransport._dispatch_command` → `CommandInvocation`.
3. `ProjectBot._on_tasks_from_transport` (wrapped by `_wrap_with_persist`):
   - `_auth_identity`, `_guard_executor` (gated per-handler; `/tasks` is viewer-allowed).
   - Replies with task list in the group.
4. `_persist_auth_if_dirty` fires in the wrap's `finally` (first-contact lock from `_auth_identity` survives restart).

### Path G — Captioned document with `@MyBot`

Same as Path A except `incoming.files` is non-empty and `incoming.text` carries the caption (Telegram convention, surfaced by `TelegramTransport._dispatch_message`). `_strip_self_mention` cleans the caption; `_on_text` dispatches files via its existing branches (image / document / voice / unsupported).

### Backward direction (replies, streams, button clicks)

- `Transport.send_text(chat=incoming.chat, ...)` already targets the originating chat. Replies, streams, edit-text updates all land in the group when the incoming was from a group.
- Button clicks: a keyboard rendered in a group produces `ButtonClick(chat=group_chat_ref)`. The click flows through `_on_button_from_transport` with the existing auth + role + persist wrap. No routing changes.

## Edge cases & error handling

| Scenario | Behavior | Why |
|---|---|---|
| `@MyBot` in different case (`@MYBOT`, `@mybot`) | Match | `mentions_bot` lowercases both sides |
| `@MyBotIsCool` (longer handle starting with `MyBot`) | No match | Regex captures the full word; equality is exact |
| `@MyBot` + `@SomeoneElse` in the same message | Processed | An explicit `@MyBot` mention always wins in `is_directed_at_me`, regardless of other mentions. `_strip_self_mention` removes only `@MyBot`; `@SomeoneElse` survives in the prompt verbatim so the agent sees who else was tagged |
| Reply to a now-deleted bot message | Silent unless mention also present | `reply_to_sender` is None; falls through to mention check |
| Reply to bot + simultaneously @-mentions another user | Silent | Reply-to-bot wakes the bot only when no other handles are mentioned in the same message — prevents waking bot A while pinging bot B. Explicit `@MyBot` co-mention overrides this and processes (row above) |
| Edited message that newly includes `@MyBot` | Processed | PTB `EDITED_MESSAGE` filter delivers it; gate runs; same risk shape as DM today |
| `self.bot_username == ""` (before `_after_ready`) | All group messages silent | `is_directed_at_me("")` returns False; fail-closed |
| Caption-only file with `@MyBot` | Processed | `incoming.text` carries the caption |
| File / voice with NO caption | Silent | No text → not addressed |
| Multiple bots in group, multiple with `respond_in_groups=True` | Each runs the gate against its own handle; only the addressed one responds | Independent state per bot subprocess |
| `_strip_self_mention` produces empty text | `_on_text` early-returns at `if not incoming.text.strip() and not incoming.has_unsupported_media: return` | Matches GitLab |
| `is_directed_at_me` raises | Bubbles to `_dispatch_message`'s top-level catch → logged + dropped | Same failure mode as any unexpected exception in dispatch |

### PTB filter is set once at startup

`attach_telegram_routing` runs once during bot startup. Toggling `respond_in_groups` in `config.json` while the bot is running has no effect until restart. Same pattern as every other startup-time field in this codebase. Documented inline on the config field's docstring.

### Brute-force counter is shared between DM and group

Failed auth from the same identity in a group increments the same `_failed_auth_counts[identity_key]` bucket as DM. After 5 fails the identity is process-lifetime-locked. Correct: `_identity_key` doesn't depend on chat type.

### Plugin behavior

Plugins are dispatched inside `_on_text` via `_dispatch_plugin_on_message`. Because the new gate runs *before* `_on_text`, plugins **only see addressed-at-me group messages** — not drive-by messages. Consistent with the existing GitHub pattern that "plugins see authorized + rate-limit-passing messages." Differs from GitLab where plugins saw all group messages. If a plugin author later needs to observe non-addressed group chatter, that's a separate feature (`Plugin.observe_unfiltered_group_messages`); not in v1.1.0.

## Testing strategy

| Test file | New / extend | Cases |
|---|---|---|
| `tests/test_bot_respond_in_groups.py` | New | 10 — bot-level routing gate (mention, reply-to-bot, drive-by, peer-bot, mention-strip, captioned files, viewer denied, unauthorized) |
| `tests/transport/test_telegram_transport.py` | Extend | 3 — `attach_telegram_routing` 3-way filter switch |
| `tests/transport/test_dynamic_command_dispatch.py` | Extend | 1 — late `on_command` post-routing picks `PRIVATE \| GROUPS` filter when `respond_in_groups=True` |
| `tests/test_config_respond_in_groups.py` | New | 5 — load / save / default / non-bool tolerance / omit-when-False |
| `tests/test_cli.py` | Extend | 4 — `projects add --respond-in-groups`, `projects edit NAME respond_in_groups true|false|invalid` |
| `tests/manager/test_bot_commands.py` | Extend | 2 — `_apply_edit` parses bool; `_EDITABLE_FIELDS` includes the field |
| (no change) `tests/transport/test_contract.py` | — | Contract is transport-agnostic; group routing lives in bot.py |

Total: ~25 new test functions, ~270 lines of new test code. Baseline at start of work: 1143 passed (current `feat/plugin-system` HEAD `14362b3`). Expected at end of v1.1.0: ~1168 passed.

### Bot-routing tests use FakeTransport

A helper `_make_group_incoming(text, *, sender, mentions=None, reply_to_sender=None, files=None)` builds an `IncomingMessage` with `chat.kind=ChatKind.ROOM`. Tests construct `ProjectBot` via `__new__` with `_respond_in_groups=True`, stub `_on_text` (assert called-with) or assert via `FakeTransport.sent_messages`. Mirrors the test patterns already in `tests/test_bot_plugin_hooks.py` and `tests/manager/test_bot_plugins.py`.

### Transport-filter tests use real PTB

Same pattern as `tests/transport/test_dynamic_command_dispatch.py::test_telegram_transport_late_on_command_registers_ptb_handler`. Builds `TelegramTransport.build("123:fake-token", menu=[])`, calls `attach_telegram_routing(group_mode=False, respond_in_groups=True, command_names=["help"])`, inspects `transport.app.handlers` for the MessageHandler's filter expression. Gated by `pytest.importorskip("telegram")`.

### End-to-end test

One case via `run_bot` + `FakeTransport`: build a `ProjectBot` with `respond_in_groups=True`, allow-list `[alice executor]`, inject a group `@MyBot do X` incoming, assert `task_manager.tasks` has a `submitted` entry with the right prompt. Verifies the full pipeline (auth → role → plugin → backend submit) runs end-to-end with the new gate in place.

## Migration / rollout

- **Zero on-disk schema change.** Field is optional, defaults to False. Pre-v1.1.0 configs load with no behavior change.
- **No CLI deprecation.** Existing flags / commands work unchanged. The new flag is purely additive.
- **Manager / CLI surfaces:** users enable per project via `projects edit NAME respond_in_groups true` (CLI) or the manager bot's project-edit keyboard (auto-generated from `_EDITABLE_FIELDS`).
- **First-time enablement:** operator adds the bot to a Telegram group → flips the flag → restarts the bot. The bot now answers in that group when `@MyBot`-mentioned. No further setup.
- **Disabling:** flip the flag back to False and restart. Bot returns to DM-only.
- **Documentation:** README gains a short "Group support" subsection under the existing "Multi-user support" / role-based access section. Two paragraphs:
  - How to enable (`projects edit NAME respond_in_groups true` or manager UI).
  - What it does (responds to `@MyBot` or replies to bot; ignores everything else; same role / auth gates as DM).

## Resolved questions

1. **Per-project flag vs. always-on vs. CLI start-time flag.** Per-project flag (chosen). Operators may run multi-bot setups where they want only some bots responsive in groups. Per-project is the natural granularity.
2. **Mention semantic.** GitLab-equivalent: `@bot_username OR reply-to-bot`. Permissive "any authorized user" mode rejected — risk of spamming Claude in larger rooms.
3. **`/commands` in groups.** Yes, with the existing role gate. Telegram's `/cmd@MyBot` routing makes multi-bot rooms safe.
4. **Captioned files / voice / photos.** Yes, same `@mention OR reply-to-bot` gate against the caption (which Telegram surfaces as `IncomingMessage.text`).
5. **Bot-to-bot in solo mode.** Always ignored. Peer-bot workflows remain team-mode-only.

## Open questions

None for this rev. Implementation plan can proceed.

## Risks

- **Configuration sprawl.** Each new project flag adds a knob operators have to remember. Mitigated by: default-off, clearly-named, documented in the README and in the `_EDITABLE_FIELDS` keyboard.
- **`/commands` in groups may surprise team operators.** A user accustomed to `/run` being DM-only might be surprised when it works in a group. Mitigated by: the role gate is unchanged; only executors can run state-changing commands; same security boundary as DM.
- **Cross-bot @-mention false positives.** Two bots whose usernames differ only by case — Telegram doesn't allow this (handles are case-insensitive and unique). N/A.
- **Plugin authors expecting GitLab-style "see all group activity"** will find that plugins only see addressed messages. Documented in the spec's "Plugin behavior" subsection; can be lifted later via a `Plugin.observe_unfiltered_group_messages` opt-in if real demand surfaces.
- **PTB filter is set once at startup.** Toggle-without-restart doesn't take effect. Same pattern as the rest of this codebase; documented in the field's docstring.

---

## Source documents

- This spec: `docs/superpowers/specs/2026-05-16-respond-in-groups-design.md`
- Reference: GitHub fork's `_handle_group_text` (team-mode path) at `src/link_project_to_chat/bot.py:976`
- Reference: GitLab fork's `_on_text` group-routing at `/Users/rezochikashua/PycharmProjects/gl/link-project-to-chat/src/link_project_to_chat/bot.py:318-339`
- Reference: shared filter predicates at `src/link_project_to_chat/group_filters.py`
- Plugin system port background: `docs/TODO.md §1.4`
