# Transport — Group/Team Port — Design Spec

**Status:** Shipped as v0.15.0. See [docs/TODO.md §1.1](../../TODO.md#11-shipped-specs) for current status and follow-ups.
**Date:** 2026-04-21
**Depends on:** [2026-04-20-transport-abstraction-design.md](2026-04-20-transport-abstraction-design.md) (spec #0), [2026-04-20-transport-voice-port-design.md](2026-04-20-transport-voice-port-design.md) (spec #0b)
**Part of:** The transport-abstraction follow-up track. This is spec #0a — group/team feature port.

---

## 1. Overview

Spec #0 and #0b ported the project bot's DM surface (text, commands, streaming, buttons, files, voice) through the Transport abstraction. This spec ports the team/group-chat surface: bot-to-bot mention routing, round counter with auto-halt, `/halt` / `/resume` commands, and the Telethon user-session relay that works around Telegram Bot API's bot-to-bot delivery blockade.

**The core insight:** the relay is a Telegram-specific workaround. On Discord, Slack, and a future Web UI, bots see each other natively and no relay exists. This spec internalizes the relay inside `TelegramTransport` so bot.py never knows it exists — when a non-Telegram transport ships, the bot-to-bot path will "just work" through `on_message` with `sender.is_bot == True`.

**Additional scope:** three minor cleanups flagged during the spec #0b final review:
- **M4:** replace hardcoded `transport_id="telegram"` with `self._transport.TRANSPORT_ID` at 6+ sites in bot.py.
- **M5:** fix the ask-question annotation regression introduced by spec #0 (lost original question text when user picks an option).
- **M6:** delete the dead `from .livestream import LiveMessage` import from bot.py.

## 2. Goals & non-goals

**Goals**
- Port [group_state.py](src/link_project_to_chat/group_state.py) from int `chat_id` keys to `ChatRef` keys.
- Port [group_filters.py](src/link_project_to_chat/group_filters.py) from `telegram.Message` signatures to `IncomingMessage` signatures.
- Port bot.py's `_on_text` group-mode branching to consume `IncomingMessage` via `_on_text_from_transport`.
- Internalize `TeamRelay` inside `TelegramTransport` behind an opt-in `enable_team_relay()` method; relay lifecycle tied to transport start()/stop().
- Add `IncomingMessage.is_relayed_bot_to_bot: bool` field so the bot can correctly count relayed bot-to-bot rounds (fixes the v1 tradeoff documented in [team_relay.py](src/link_project_to_chat/manager/team_relay.py)).
- Apply the three cleanups (M4, M5, M6).
- Preserve all existing team behavior: group_chat_id auto-capture, mention-based routing, auto-halt at 20 bot-to-bot rounds, `/halt`/`/resume`, peer-bot system-note pinning.

**Non-goals (this spec)**
- No port of `manager/telegram_group.py` (Telethon group-creation ops used by manager's `/create_team` — spec #0c territory).
- No port of `manager/bot.py` — spec #0c.
- No web UI, Discord, or Slack transports (specs #1, #2, #3).
- No persistence of round counter / halt state across restarts (current behavior: process restart is itself a reset — unchanged).
- No richer `MessageRef` (adding `sender: Identity | None` is tempting but scoped out — `is_reply_to_bot` retains a documented `native` escape hatch).

## 3. Decisions driving this design

Outcomes of brainstorming on 2026-04-21:

| # | Question | Decision |
|---|---|---|
| 1 | Scope of the port | Bot-side group + internalize team_relay into TelegramTransport + 3 cleanups |
| 2 | How does team_relay integrate into TelegramTransport? | Opt-in `TelegramTransport.enable_team_relay(telethon_client, team_bot_usernames, group_chat_id, team_name)` called post-build |
| 3 | How does the bot detect `@bot_b` mentions after the port? | Pure-Python regex `extract_mentions(text)` helper in ported `group_filters` |
| 4 | The v1 round-counter tradeoff (relayed messages reset the counter) | Fix: add `IncomingMessage.is_relayed_bot_to_bot: bool` flag; bot increments counter and bypasses auth on relayed paths |

## 4. Architecture

### 4.1 Transport interface — `IncomingMessage.is_relayed_bot_to_bot`

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
    is_relayed_bot_to_bot: bool = False   # NEW
```

- Default `False`. Existing transport implementations + tests keep compiling without modification.
- Only `TelegramTransport` sets it today. When its `_dispatch_message` sees a message whose text starts with the relay prefix `[auto-relay from <handle>]` (note: the prefix deliberately omits the `@` sign to avoid the peer bot re-processing the prefix as a self-mention — see [team_relay.py](src/link_project_to_chat/manager/team_relay.py) line 124 comment), it:
  - Sets `is_relayed_bot_to_bot=True`.
  - Strips the prefix from `text` so downstream consumers see only the peer's original content.
- Other transports (Web, Discord, Slack) never need to set it. Their bot-to-bot messages arrive natively with `sender.is_bot == True`.

### 4.2 TelegramTransport — opt-in team relay

```python
# src/link_project_to_chat/transport/telegram.py

class TelegramTransport:
    def enable_team_relay(
        self,
        telethon_client: Any,
        team_bot_usernames: set[str],
        group_chat_id: int,
        team_name: str,
    ) -> None:
        """Activate the Telethon-user-session relay for a team group chat.

        Required because Telegram Bot API never delivers bot-to-bot messages.
        Other transports (Discord, Slack, Web) don't need this and won't
        implement it — this method is not on the Transport Protocol.

        Call once after build(), before start(). Relay lifecycle is tied to
        start()/stop() thereafter.
        """
```

- Stashes a `TeamRelay` instance (constructed from the provided client + config).
- `start()` calls `self._team_relay.start()` if one was configured — fires after the existing post-init sequence (delete_webhook / get_me / set_my_commands / on_ready callbacks) so the relay activates before polling begins.
- `stop()` calls `self._team_relay.stop()` before tearing down the Application.
- `team_relay.py` moves from `manager/` to `transport/_telegram_relay.py` (underscore-prefixed — TelegramTransport-private helper). Manager bot updates its import path.

**Not on `Transport` Protocol.** `enable_team_relay` is `TelegramTransport`-specific. Forcing Web/Discord/Slack to implement a no-op version is interface noise. Team-capable non-Telegram transports can grow their own team-enablement methods if they need different shape (e.g., Discord needs a guild ID + role list, not a Telethon client).

### 4.3 Portable `group_state.py`

```python
# src/link_project_to_chat/group_state.py

from __future__ import annotations

from dataclasses import dataclass, field
from time import time

from .transport import ChatRef


@dataclass
class GroupState:
    halted: bool = False
    bot_to_bot_rounds: int = 0
    last_user_activity_ts: float = field(default_factory=time)


class GroupStateRegistry:
    def __init__(self, max_bot_rounds: int = 20) -> None:
        self._states: dict[tuple[str, str], GroupState] = {}
        self._max = max_bot_rounds

    @property
    def max_bot_rounds(self) -> int:
        return self._max

    @staticmethod
    def _key(chat: ChatRef) -> tuple[str, str]:
        return (chat.transport_id, chat.native_id)

    def get(self, chat: ChatRef) -> GroupState:
        return self._states.setdefault(self._key(chat), GroupState())

    def note_user_message(self, chat: ChatRef) -> None:
        s = self.get(chat)
        s.bot_to_bot_rounds = 0
        s.last_user_activity_ts = time()

    def note_bot_to_bot(self, chat: ChatRef) -> None:
        s = self.get(chat)
        s.bot_to_bot_rounds += 1
        if s.bot_to_bot_rounds >= self._max:
            s.halted = True

    def halt(self, chat: ChatRef) -> None:
        self.get(chat).halted = True

    def resume(self, chat: ChatRef) -> None:
        s = self.get(chat)
        s.halted = False
        s.bot_to_bot_rounds = 0
```

Key changes:
- Method signatures: `int chat_id` → `chat: ChatRef`.
- Dict key: `int` → `(transport_id, native_id)` tuple (explicit about identity composition; `ChatRef` itself is hashable but the tuple form reads clearer).
- Import `ChatRef` from `transport`. No telegram dependency.

### 4.4 Portable `group_filters.py`

```python
# src/link_project_to_chat/group_filters.py

"""Pure functions for deciding whether a group-chat message is directed at this bot.

No transport-specific dependencies — takes an IncomingMessage and returns bools.
"""
from __future__ import annotations

import re

from .transport import IncomingMessage

_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")


def extract_mentions(text: str) -> list[str]:
    """Return lowercased `@handle` mentions from free text, without the leading '@'."""
    if not text:
        return []
    return [m.lower() for m in _MENTION_RE.findall(text)]


def is_from_self(msg: IncomingMessage, my_username: str) -> bool:
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return sender == my_username.lower()


def is_from_other_bot(msg: IncomingMessage, my_username: str) -> bool:
    """True when the message was sent by a different bot account.

    Note: a relayed bot-to-bot message (msg.is_relayed_bot_to_bot) has sender =
    trusted user, so this check returns False for relays. Call sites that care
    about bot-to-bot semantics should also check `msg.is_relayed_bot_to_bot`.
    """
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return bool(sender) and sender != my_username.lower()


def mentions_bot(msg: IncomingMessage, bot_username: str) -> bool:
    target = bot_username.lower()
    return target in extract_mentions(msg.text)


def is_reply_to_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message is a reply to an earlier message from this bot.

    Uses the `native` escape hatch to read reply_to_message.from_user.username —
    MessageRef doesn't carry sender info. Future work: add MessageRef.sender
    and drop the escape. Deliberate scope-limit for #0a.
    """
    reply = msg.reply_to
    if reply is None or msg.native is None:
        return False
    native_reply = getattr(msg.native, "reply_to_message", None)
    if native_reply is None:
        return False
    from_user = getattr(native_reply, "from_user", None)
    if from_user is None:
        return False
    sender = (getattr(from_user, "username", "") or "").lower()
    return sender == bot_username.lower()


def is_directed_at_me(msg: IncomingMessage, my_username: str) -> bool:
    """An explicit @mention always wins. A reply to this bot's prior message only
    counts when the user did NOT @mention anyone else — otherwise replying to
    bot A while pinging bot B would wake both A and B.
    """
    if mentions_bot(msg, my_username):
        return True
    if extract_mentions(msg.text):
        return False  # mentions someone, but not us
    return is_reply_to_bot(msg, my_username)
```

Key choices:
- **`extract_mentions` regex** matches telegram-valid handles (start with letter, then alphanumeric/underscore). Works for Discord-style `@username` too. Slack `<@U12345>` requires transport-side normalization into `@handle` text form.
- **`is_reply_to_bot` retains `native` escape.** The only function that reaches into `msg.native`. Documented as conscious scope limit — `MessageRef` doesn't carry sender info, and porting reply-sender identification properly would require a `MessageRef.sender: Identity | None` field with transport-level implementation across all transports. Future work.

### 4.5 Bot-side group handling

The group-mode branching moves from `_on_text` (legacy, consumes Update) into `_on_text_from_transport` (transport-native, consumes IncomingMessage).

```python
# bot.py — new helper method

async def _handle_group_text(self, incoming) -> bool:
    """Route a group-mode text message. Returns True if handled (caller returns),
    False if it should fall through to the normal user-message flow.
    """
    from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot

    # Auto-capture: if chat_id not yet bound and sender is trusted, write it.
    if self.group_chat_id in (0, None):
        if self._auth_identity(incoming.sender) and self.team_name:
            new_chat_id = int(incoming.chat.native_id)
            patch_team(self.team_name, {"group_chat_id": new_chat_id})
            self.group_chat_id = new_chat_id
            # Fall through so this message still gets processed.
    elif int(incoming.chat.native_id) != self.group_chat_id:
        return True  # wrong group — silent ignore

    if is_from_self(incoming, self.bot_username):
        return True  # self-silence

    if not is_directed_at_me(incoming, self.bot_username):
        return True  # not addressed to this bot

    if incoming.is_relayed_bot_to_bot or is_from_other_bot(incoming, self.bot_username):
        # Bot-to-bot path — via relay (is_relayed_bot_to_bot) or native (non-Telegram transports).
        if self._group_state.get(incoming.chat).halted:
            return True
        self._group_state.note_bot_to_bot(incoming.chat)
        if self._group_state.get(incoming.chat).halted:
            assert self._transport is not None
            await self._transport.send_text(
                incoming.chat,
                f"Auto-paused after {self._group_state.max_bot_rounds} bot-to-bot rounds. "
                "Send any message to resume.",
            )
            return True
        # Auth was implicitly validated (relay only triggers for trusted users;
        # native bot-to-bot on other transports skips auth by design).
        # Caller submits to Claude on the False return.
        return False

    # Human (trusted user) message — reset the round counter and clear any halt.
    self._group_state.resume(incoming.chat)
    return False


async def _submit_group_message_to_claude(self, incoming) -> None:
    """Bypass auth + rate-limit and submit a bot-to-bot message to Claude.
    Called when _handle_group_text returned False AND the sender is a bot/relay.
    """
    assert self._transport is not None
    prompt = incoming.text
    if self._active_persona:
        from .skills import load_persona, format_persona_prompt
        persona = load_persona(self._active_persona, self.path)
        if persona:
            prompt = format_persona_prompt(persona, prompt)
    message_id_int = (
        int(getattr(incoming.native, "message_id", 0))
        if incoming.native is not None else 0
    )
    self.task_manager.submit_claude(
        chat_id=int(incoming.chat.native_id),
        message_id=message_id_int,
        prompt=prompt,
    )
```

In `_on_text_from_transport`, the text branch:

```python
# 3. Text.
if incoming.text.strip():
    if self.group_mode:
        handled = await self._handle_group_text(incoming)
        if handled:
            return
        # Bot-to-bot path bypasses auth; submit directly.
        if incoming.is_relayed_bot_to_bot or incoming.sender.is_bot:
            await self._submit_group_message_to_claude(incoming)
            return
        # Human message in group — fall through to legacy _on_text shim for full flow
        # (auth + rate limit + pending_skill/pending_persona + Claude submission).
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
```

Legacy `_on_text` keeps its DM auth+rate-limit checks and the pending_skill/pending_persona logic. It's called from `_on_text_from_transport` for DMs and for the "human in group" fall-through case.

The group-mode block currently in `_on_text` ([bot.py:576-607](src/link_project_to_chat/bot.py:576)) is deleted — its logic moved into `_handle_group_text`.

### 4.6 Three cleanups

**M4 — hardcoded `transport_id="telegram"`:**

Every `ChatRef(transport_id="telegram", ...)` and `MessageRef(transport_id="telegram", ...)` in bot.py replaced with `self._transport.TRANSPORT_ID`. Six call sites identified in the #0b final review (exact line numbers shift per-commit; grep from `grep -n 'transport_id="telegram"' src/link_project_to_chat/bot.py`). Mechanical replacement.

**M5 — ask-question annotation regression:**

`_on_button`'s ask-answer branch currently has dead code:

```python
native = getattr(msg_ref, "native", None)   # MessageRef has no `native` — always None
if native is not None:
    original = getattr(native, "text_html", None) or getattr(native, "text", "") or ""
```

`MessageRef` has no `native` field (verified in `transport/base.py`), so this code silently loses the question context. The edit becomes just `"\n\n<i>Selected:</i> {label}"` — no question preserved.

Fix: extract a `_render_question_html(question: Question) -> str` helper from `_on_waiting_input`'s existing HTML construction block. Reuse it in `_on_button`:

```python
if self.task_manager.submit_answer(task_id, label):
    try:
        question = task.pending_questions[q_idx]
        original_html = self._render_question_html(question)
        escaped = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._transport.edit_text(
            msg_ref,
            f"{original_html}\n\n<i>Selected:</i> {escaped}",
            html=True,
        )
    except Exception:
        logger.debug("could not annotate selected option", exc_info=True)
```

The bot already owns the source data (`task.pending_questions[q_idx]` is a `Question` dataclass). No need to query transport for message text. No interface change.

**M6 — dead `LiveMessage` import:**

```python
from .livestream import LiveMessage  # legacy — kept for test_livestream.py; bot.py uses StreamingMessage
```

Delete. `tests/test_livestream.py` imports from `.livestream` directly; no dependency on the bot-side re-export.

## 5. Migration — strangler step sequence

Eight steps. Each independently landable; the bot works end-to-end at every step.

### Step 1 — Add `is_relayed_bot_to_bot` field to `IncomingMessage`

Extend the dataclass in `transport/base.py`. Default `False`. Add a shape assertion in `tests/transport/test_base_types.py`.

**Exit:** field exists, default False; all existing tests pass unchanged.

### Step 2 — Port `group_state.py` to ChatRef keying

Migrate `GroupStateRegistry` method signatures to take `ChatRef`, change dict key to `(transport_id, native_id)` tuple. Update `tests/test_group_state.py` to construct `ChatRef` instances. Update bot.py callers in the same commit so nothing breaks (`self._group_state.get(task.chat_id)` → `self._group_state.get(self._chat_ref_for_task(task))` etc.).

**Exit:** `group_state.py` is ChatRef-based; all tests pass.

### Step 3 — Port `group_filters.py` to IncomingMessage

Replace telegram-Message signatures with IncomingMessage. Add `extract_mentions(text)` regex helper. `is_reply_to_bot` retains the documented `msg.native` escape hatch.

Update `tests/test_group_filters.py`: add a small `_build_incoming(...)` helper that constructs IncomingMessage from stub parts; existing test scenarios get their message construction swapped. Add ~6 tests for `extract_mentions`.

Update bot.py's single group_filters call site in `_on_text` to construct a transient IncomingMessage from the telegram msg via existing `chat_ref_from_telegram` / `identity_from_telegram_user` helpers. This transient construction is removed in Step 5 once the group logic moves out of `_on_text`.

**Exit:** group_filters is telegram-free; bot works via transient IncomingMessage in `_on_text`.

### Step 4 — Add relay-prefix detection to `TelegramTransport._dispatch_message`

In `_dispatch_message`, after building the base `IncomingMessage`, check for the `[auto-relay from <handle>]\n\n` prefix (no `@` sign — this is load-bearing; adding `@` would cause peer bots to re-process the prefix as a self-mention). When matched:
- Use `dataclasses.replace` to construct a new IncomingMessage with `is_relayed_bot_to_bot=True` and `text=<prefix_stripped>`.
- Dispatch that instead.

Add two tests in `tests/transport/test_telegram_transport.py`:
- `test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix`
- `test_dispatch_non_relay_text_unchanged`

**Exit:** relay detection works at transport layer.

### Step 5 — Wire group logic into `_on_text_from_transport`

Add `_handle_group_text(incoming)` and `_submit_group_message_to_claude(incoming)` methods (per section 4.5). Update `_on_text_from_transport`'s text branch to invoke them when `self.group_mode`. Delete the group-mode block from `_on_text` (the transient IncomingMessage construction from Step 3 becomes dead code here).

Refactor `tests/test_bot_team_wiring.py` and `tests/test_group_halt_integration.py` scenarios to drive messages via `FakeTransport.inject_message` / `_on_text_from_transport` rather than constructing telegram Updates for `_on_text`. Preserve all existing scenario assertions.

**Exit:** group-mode logic flows through the transport; round counter works on both relayed (`is_relayed_bot_to_bot`) and native (`sender.is_bot`) bot-to-bot paths. Auto-halt at 20 rounds, `/halt`, `/resume` all work.

### Step 6 — Add `TelegramTransport.enable_team_relay`; move team_relay.py into transport/

Move `src/link_project_to_chat/manager/team_relay.py` → `src/link_project_to_chat/transport/_telegram_relay.py` (underscore-prefixed private helper). Update `tests/test_team_relay.py` import path.

Add `enable_team_relay(telethon_client, team_bot_usernames, group_chat_id, team_name)` method to `TelegramTransport`. Stashes a `TeamRelay` instance. `start()` activates the relay after the existing post-init; `stop()` deactivates before teardown.

Update manager bot: its current code creates a `TeamRelay` and calls `.start()` on it when a team-mode project bot starts. Replace that with a `TelegramTransport.enable_team_relay(...)` call. The manager still owns the Telethon client (from `/setup`) — ownership boundary unchanged — but relay instantiation moves into the transport.

Add `test_enable_team_relay_lifecycle` in `tests/transport/test_telegram_transport.py` — mock Telethon client, assert add_event_handler on start(), remove_event_handler on stop().

**Exit:** `TelegramTransport.enable_team_relay` works; manager drives it via its Telethon client; old `manager/team_relay.py` path gone.

### Step 7 — Three cleanups (M4 + M5 + M6)

In one commit:

- **M4:** Replace every hardcoded `transport_id="telegram"` in bot.py with `self._transport.TRANSPORT_ID`. Grep-verify zero hits after.
- **M5:** Extract `_render_question_html(question)` helper from `_on_waiting_input`. Use it in `_on_button`'s ask-answer branch; delete the dead `getattr(msg_ref, "native", None)` code path. Add one regression test in `tests/test_bot_streaming.py` or a new `tests/test_bot_ask_annotations.py` that injects a button click on an ask-question message and asserts the edit contains the original question HTML + the `Selected: X` suffix.
- **M6:** Delete `from .livestream import LiveMessage` from bot.py. Verify `tests/test_livestream.py` still passes.

**Exit:** cleanups applied; all tests pass.

### Step 8 — Final sweep + docs

- Grep for any remaining `update: Update` or `ctx: ContextTypes.DEFAULT_TYPE` patterns in group-related code paths that should have been ported.
- Update `where-are-we.md` with a spec #0a summary entry under `## Done`.
- Remove the relevant entries from `## Pending` (the group/team-specific ones).
- Bump `pyproject.toml` version: `0.14.0` → `0.15.0`.

**Exit:** spec #0a complete.

## 6. Testing approach

### Ported tests (type migration, same assertions)

| Test file | Change |
|---|---|
| `tests/test_group_state.py` | int chat_ids → ChatRef instances. Same behavior assertions. |
| `tests/test_group_filters.py` | telegram.Message stubs → IncomingMessage via new `_build_incoming(...)` helper. Add ~6 tests for `extract_mentions`. |
| `tests/test_bot_team_wiring.py` | Refactor scenarios from `bot._on_text(update, ctx)` to `FakeTransport.inject_message` → `_on_text_from_transport`. |
| `tests/test_group_halt_integration.py` | Same refactor. Add a new variant: 20 relayed `is_relayed_bot_to_bot=True` messages also trigger auto-halt (verifies Q4-C). |
| `tests/test_team_relay.py` | Import path update only (file moved to `transport/_telegram_relay.py`); behavior tests unchanged. |

### New tests

**Transport layer:**
- `test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix` — the prefix `[auto-relay from bot_a]\n\n...` (no `@`) produces IncomingMessage with `is_relayed_bot_to_bot=True` and stripped text.
- `test_dispatch_non_relay_text_unchanged` — normal text has `is_relayed_bot_to_bot=False` and text unchanged.
- `test_enable_team_relay_lifecycle` — `enable_team_relay` + `start()` registers an event handler on the Telethon client; `stop()` removes it.

**Bot layer:**
- `test_relayed_bot_to_bot_increments_round_counter` — 20 relayed messages auto-halt the group (Q4-C fix).
- `test_native_bot_sender_increments_round_counter` — a `sender.is_bot=True` message (simulating a future non-Telegram transport's native bot-to-bot) also increments the counter and auto-halts at 20.
- One regression test for M5 (ask-question annotation preserves original question HTML).

### Untouched tests

- `tests/test_telegram_group.py` — Telethon group-creation ops; spec #0c territory.
- `tests/test_manager_create_team.py` — manager `/create_team` flow; spec #0c territory.

### Manual smoke test

At end of Step 5:
1. Start two team bots with `/create_team` already configured.
2. Post in the group as the trusted user: `@bot_a do X`.
3. Verify bot_a responds.
4. Verify bot_a's reply `@bot_b please review` reaches bot_b via the relay.
5. Verify round counter increments (check logs or force 20 rounds to see auto-halt).
6. `/halt` — verify further bot-to-bot messages are silently dropped.
7. `/resume` — verify bots resume.

### No integration tests against real Telegram in CI

Same policy as spec #0 and #0b. Secrets, flakiness, rate limits. Manual smoke at Step 5 is sufficient.

### Lockout

No changes to `tests/test_transport_lockout.py`. bot.py's telegram-import allowlist stays empty — this spec reintroduces no telegram import.

## 7. Explicit out-of-scope

| Belongs to | Item |
|---|---|
| Spec #0c (future) | Manager bot port — includes `/create_team`, `/delete_team`, `manager/telegram_group.py`, `manager/bot.py` full port |
| Spec #1/#2/#3 (future) | Non-Telegram transports get team-enablement methods (or equivalent) per their own platform shape |
| Future | `MessageRef.sender: Identity | None` — would let `is_reply_to_bot` drop the `native` escape hatch. Not blocking #0a — the escape is documented and narrow. |
| Future | Persistence of round counter / halt state across restarts. Current: restart is itself a reset. Unchanged. |

## 8. Risks

- **Relay-prefix detection false positives.** If a user literally types `[auto-relay from <anything>]` as a message, the transport would mark it as `is_relayed_bot_to_bot=True` and possibly trigger the round counter. Mitigation: the prefix is awkward enough that accidental matches are rare; the Telethon relay uses this exact prefix intentionally; and the user has `/halt` as an escape. Accept the risk.
- **Manager bot needs update.** Step 6 changes how the manager bot instantiates the relay. The manager is still Telegram-coupled (pre-#0c), but its relay-instantiation call site changes. Straightforward migration (`TeamRelay(client, ...).start()` → `transport.enable_team_relay(client, ...)`).
- **Bot-to-bot auth bypass.** The bot-to-bot code path in `_handle_group_text` / `_submit_group_message_to_claude` skips `_auth_identity` + `_rate_limited` because peer bots aren't on the allowed-usernames list. This is correct: the relay only triggers on messages from trusted users in a trusted group, and native bot-to-bot (on non-Telegram transports) arrives via the same permission model as the group membership. No new attack surface. Documented in `_submit_group_message_to_claude`'s docstring.
- **Test migration churn.** `tests/test_bot_team_wiring.py` + `tests/test_group_halt_integration.py` have scenarios built around `_on_text(update, ctx)`. Rewriting them to drive via `inject_message` is mechanical but touches many tests. Care needed to preserve original assertions — if a scenario's behavior legitimately changed (e.g., the round counter now fires on relayed messages where it didn't before), the assertion should be updated to match the new, correct behavior.

## 9. Next steps after this spec ships

1. **Spec #0c** — manager bot port. Ports `manager/bot.py` + `manager/telegram_group.py` + `/create_team`, `/delete_team`, `/setup` commands.
2. **Spec #1** — Web UI transport. First non-Telegram transport. Will exercise `is_relayed_bot_to_bot=False` + native `sender.is_bot=True` path — validates the Q4-C design.
3. **Spec #2** — Discord transport.
4. **Spec #3** — Slack transport.

Each non-Telegram transport will need its own mention-extraction adaptation if the platform's mention syntax differs from `@handle`. Slack `<@U12345>` is the notable case — the SlackTransport should normalize into `@handle` text before dispatching.
