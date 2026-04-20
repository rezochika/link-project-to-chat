# Transport Group/Team Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port bot-side group/team handling through the Transport abstraction, internalize the Telethon relay inside `TelegramTransport`, add `IncomingMessage.is_relayed_bot_to_bot` so relayed messages correctly increment the round counter, and apply three cleanups flagged during the #0b final review (M4, M5, M6).

**Architecture:** Eight-step strangler. Steps 1–4 build substrate (field + ported primitives + relay detection). Steps 5–6 rewire bot.py's group logic onto `IncomingMessage`. Step 7 moves `team_relay.py` into `transport/` and makes the manager bot drive relay activation via `TelegramTransport.enable_team_relay`. Step 8 bundles the three cleanups. Each step is independently shippable; bot works end-to-end at every step.

**Tech Stack:** Python 3.11+, `python-telegram-bot>=22.0`, `telethon>=1.36` (optional, for team relay), `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), existing Transport abstraction from specs #0 and #0b.

**Reference spec:** [docs/superpowers/specs/2026-04-21-transport-group-team-port-design.md](docs/superpowers/specs/2026-04-21-transport-group-team-port-design.md)

---

## File Structure

**Modify:**
- `src/link_project_to_chat/transport/base.py` — add `is_relayed_bot_to_bot: bool = False` field to `IncomingMessage`.
- `src/link_project_to_chat/transport/telegram.py` — relay-prefix detection in `_dispatch_message`; new `enable_team_relay()` method; `start()`/`stop()` lifecycle integration for the relay.
- `src/link_project_to_chat/group_state.py` — re-key `GroupStateRegistry` by `ChatRef`.
- `src/link_project_to_chat/group_filters.py` — rewrite to consume `IncomingMessage`; add `extract_mentions()`.
- `src/link_project_to_chat/bot.py` — new `_handle_group_text()` + `_submit_group_message_to_claude()` + `_render_question_html()` helpers; wire group logic into `_on_text_from_transport`; delete group block from `_on_text`; three cleanups.
- `src/link_project_to_chat/manager/bot.py` — update relay activation call to use `TelegramTransport.enable_team_relay`.
- `tests/test_group_state.py` — migrate to `ChatRef` keys.
- `tests/test_group_filters.py` — migrate to `IncomingMessage`; add `extract_mentions` tests.
- `tests/test_bot_team_wiring.py` — refactor scenarios to drive messages through `FakeTransport.inject_message`.
- `tests/test_group_halt_integration.py` — same refactor; add round-counter tests for relayed + native bot-to-bot paths.
- `tests/test_team_relay.py` — update import path (file moved).
- `tests/transport/test_telegram_transport.py` — add relay-prefix detection tests + `enable_team_relay` lifecycle test.
- `tests/test_bot_streaming.py` — add M5 regression test for ask-question annotation.
- `where-are-we.md` — spec #0a summary + prune pending lines.
- `pyproject.toml` — version bump 0.14.0 → 0.15.0.

**Move:**
- `src/link_project_to_chat/manager/team_relay.py` → `src/link_project_to_chat/transport/_telegram_relay.py` (underscore-prefixed; TelegramTransport-private helper).

**Not touched by this plan:**
- `src/link_project_to_chat/manager/telegram_group.py` — Telethon group-creation ops (spec #0c).
- `src/link_project_to_chat/manager/bot.py` except the one relay-activation call site (spec #0c for full port).
- Tests outside the files above — they don't touch group/team code.

---

## Task 1: Add `is_relayed_bot_to_bot` field to `IncomingMessage`

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `tests/transport/test_base_types.py`

- [ ] **Step 1.1: Write the failing shape test**

Append to `tests/transport/test_base_types.py`:

```python
def test_incoming_message_has_is_relayed_bot_to_bot_field():
    m = IncomingMessage(
        chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None, native=None,
    )
    # default is False
    assert m.is_relayed_bot_to_bot is False


def test_incoming_message_accepts_is_relayed_bot_to_bot_true():
    m = IncomingMessage(
        chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None, native=None,
        is_relayed_bot_to_bot=True,
    )
    assert m.is_relayed_bot_to_bot is True
```

- [ ] **Step 1.2: Run the test and confirm failure**

Run: `source venv/Scripts/activate && python -m pytest tests/transport/test_base_types.py::test_incoming_message_has_is_relayed_bot_to_bot_field -v`
Expected: FAIL — `TypeError: IncomingMessage.__init__() got an unexpected keyword argument 'is_relayed_bot_to_bot'` (on the second test) OR passes vacuously on the first (since `hasattr` is False — depends on interpretation; in either case the second test fails).

Actually expected: FAIL on the second test with `TypeError`.

- [ ] **Step 1.3: Add the field to IncomingMessage**

In `src/link_project_to_chat/transport/base.py`, find the `IncomingMessage` dataclass and add the field:

```python
@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    native: Any = None
    is_relayed_bot_to_bot: bool = False
```

- [ ] **Step 1.4: Run tests and confirm pass**

Run: `python -m pytest tests/transport/ -v 2>&1 | tail -6`
Expected: all transport tests PASS including the two new ones.

- [ ] **Step 1.5: Commit**

```bash
git add src/link_project_to_chat/transport/base.py tests/transport/test_base_types.py
git commit -m "feat(transport): add IncomingMessage.is_relayed_bot_to_bot field"
```

---

## Task 2: Port `group_state.py` to ChatRef keying

**Files:**
- Modify: `src/link_project_to_chat/group_state.py`
- Modify: `tests/test_group_state.py`
- Modify: `src/link_project_to_chat/bot.py` (callers)

- [ ] **Step 2.1: Rewrite tests/test_group_state.py for ChatRef**

Replace the content of `tests/test_group_state.py` with:

```python
from __future__ import annotations

from link_project_to_chat.group_state import GroupState, GroupStateRegistry
from link_project_to_chat.transport import ChatKind, ChatRef


def _chat(native_id: str = "-100123") -> ChatRef:
    return ChatRef(transport_id="telegram", native_id=native_id, kind=ChatKind.ROOM)


def test_new_group_defaults():
    reg = GroupStateRegistry(max_bot_rounds=20)
    s = reg.get(_chat())
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_user_message_resets_round_counter():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    s = reg.get(chat)
    s.bot_to_bot_rounds = 5
    reg.note_user_message(chat)
    assert reg.get(chat).bot_to_bot_rounds == 0


def test_bot_to_bot_increment():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    reg.note_bot_to_bot(chat)
    reg.note_bot_to_bot(chat)
    assert reg.get(chat).bot_to_bot_rounds == 2


def test_cap_halts_at_max_rounds():
    reg = GroupStateRegistry(max_bot_rounds=3)
    chat = _chat()
    for _ in range(3):
        reg.note_bot_to_bot(chat)
    s = reg.get(chat)
    assert s.halted is True
    assert s.bot_to_bot_rounds == 3


def test_halt_and_resume():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    reg.halt(chat)
    assert reg.get(chat).halted is True
    reg.resume(chat)
    s = reg.get(chat)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_independent_groups_do_not_interfere():
    reg = GroupStateRegistry(max_bot_rounds=20)
    a = _chat("-1")
    b = _chat("-2")
    reg.halt(a)
    assert reg.get(a).halted is True
    assert reg.get(b).halted is False


def test_different_transport_ids_do_not_interfere():
    """A ChatRef with the same native_id but a different transport_id is a different group."""
    reg = GroupStateRegistry(max_bot_rounds=20)
    tg = ChatRef(transport_id="telegram", native_id="-100123", kind=ChatKind.ROOM)
    dc = ChatRef(transport_id="discord", native_id="-100123", kind=ChatKind.ROOM)
    reg.halt(tg)
    assert reg.get(tg).halted is True
    assert reg.get(dc).halted is False
```

- [ ] **Step 2.2: Run tests and confirm failure**

Run: `python -m pytest tests/test_group_state.py -v`
Expected: FAIL — `TypeError: unhashable type: 'ChatRef'` or similar; the current impl keys by `int` and will choke on ChatRef.

- [ ] **Step 2.3: Rewrite group_state.py**

Replace the content of `src/link_project_to_chat/group_state.py` with:

```python
"""Per-room state for dual-agent teams, keyed by ChatRef.

Lives for the process lifetime. Halts and round counters do not persist across
restarts — a process restart is itself a reset.
"""

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
        """Increment the bot-to-bot round counter. Halts the group if cap reached."""
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

- [ ] **Step 2.4: Update bot.py callers to pass ChatRef**

In `src/link_project_to_chat/bot.py`, find every call to `self._group_state.<method>` and change the argument from an int chat_id to a `ChatRef`. Use grep to enumerate:

```bash
grep -n "_group_state\." src/link_project_to_chat/bot.py
```

For each call site, replace the int argument with a ChatRef derived from context. The context varies by call site; use these patterns:

**A. Inside `_on_stream_event` or task-handling methods (context: have a `task: Task`):**

Use `self._chat_ref_for_task(task)` (helper from spec #0).

Example: `self._group_state.halt(task.chat_id)` → `self._group_state.halt(self._chat_ref_for_task(task))`

**B. Inside `_on_text` group block (context: have a `msg: telegram.Message`):**

Add at the top of the group block:
```python
from .transport.telegram import chat_ref_from_telegram
chat_ref = chat_ref_from_telegram(msg.chat)
```

Then every `self._group_state.*(chat_id)` in that block becomes `self._group_state.*(chat_ref)`. The local `chat_id = msg.chat_id` variable can be deleted if no longer used outside the group_state calls. (Task 8 later replaces this entire block with `_handle_group_text`, so this intermediate state is short-lived.)

**C. Inside `_on_halt` / `_on_resume` (context: have `update: telegram.Update`):**

Replace the current body patterns. The `_on_halt` method should become:

```python
    async def _on_halt(self, update, ctx) -> None:
        if not self.group_mode:
            return await update.effective_message.reply_text("/halt is only available in group mode.")
        if self.group_chat_id is not None and update.effective_chat.id != self.group_chat_id:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        from .transport.telegram import chat_ref_from_telegram
        self._group_state.halt(chat_ref_from_telegram(update.effective_chat))
        await update.effective_message.reply_text("Halted. Use /resume to continue.")
```

And `_on_resume` should become:

```python
    async def _on_resume(self, update, ctx) -> None:
        if not self.group_mode:
            return await update.effective_message.reply_text("/resume is only available in group mode.")
        if self.group_chat_id is not None and update.effective_chat.id != self.group_chat_id:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        from .transport.telegram import chat_ref_from_telegram
        self._group_state.resume(chat_ref_from_telegram(update.effective_chat))
        await update.effective_message.reply_text("Resumed.")
```

**D. Inside the `is_usage_cap_error` handler and the halted-wait loop (around bot.py lines 361 and 385):**

For the usage-cap handler (around line 361):

```python
if is_usage_cap_error(task.error) and self.group_mode:
    self._group_state.halt(self._chat_ref_for_task(task))
```

(Keep whatever other lines follow in the original branch — this step only changes the call-site argument, not the surrounding logic.)

For the halted-wait loop (around line 385), replace `chat_id = task.chat_id` with `chat_ref = self._chat_ref_for_task(task)` at the top, and use `chat_ref` in every `_group_state.get(...).halted` / `_group_state.resume(...)` call inside the loop.

**After completing A–D**, re-grep to confirm zero remaining int arguments to `_group_state.*`:

```bash
grep -n "_group_state\." src/link_project_to_chat/bot.py | grep -v "chat_ref\|_chat_ref_for_task\|chat_ref_from_telegram"
```

Expected: zero lines (all remaining `_group_state.*` calls should have ChatRef-style arguments; if any show up with `task.chat_id` or `chat_id`, they weren't ported).

- [ ] **Step 2.5: Run the tests**

Run: `python -m pytest tests/test_group_state.py tests/test_bot_team_wiring.py tests/test_group_halt_integration.py -v 2>&1 | tail -10`
Expected: `test_group_state.py` all PASS. `test_bot_team_wiring.py` and `test_group_halt_integration.py` may have failures if they assert on `_group_state.get(int_chat_id)` directly — we'll fix those in Tasks 9 + 10.

If team_wiring / halt_integration tests fail because they pre-construct group_state with int keys, leave them failing for now — they get rewritten in Tasks 9 and 10. The tests we need green here are `test_group_state.py`.

- [ ] **Step 2.6: Commit**

```bash
git add src/link_project_to_chat/group_state.py tests/test_group_state.py src/link_project_to_chat/bot.py
git commit -m "refactor(group): GroupStateRegistry keyed by ChatRef"
```

---

## Task 3: Port `group_filters.py` to IncomingMessage

**Files:**
- Modify: `src/link_project_to_chat/group_filters.py`
- Modify: `tests/test_group_filters.py`

- [ ] **Step 3.1: Rewrite tests/test_group_filters.py**

Replace the content of `tests/test_group_filters.py` with:

```python
from __future__ import annotations

from types import SimpleNamespace

from link_project_to_chat.group_filters import (
    extract_mentions,
    is_directed_at_me,
    is_from_other_bot,
    is_from_self,
    is_reply_to_bot,
    mentions_bot,
)
from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="telegram", native_id="-100123", kind=ChatKind.ROOM)


def _sender(handle: str | None = None, is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="telegram",
        native_id="1",
        display_name="X",
        handle=handle,
        is_bot=is_bot,
    )


def _msg(
    text: str = "",
    sender_handle: str | None = None,
    sender_is_bot: bool = False,
    reply_to_bot_username: str | None = None,
) -> IncomingMessage:
    native = None
    reply_to: MessageRef | None = None
    if reply_to_bot_username:
        # Construct a minimal native object carrying reply_to_message.from_user.username.
        reply_from_user = SimpleNamespace(username=reply_to_bot_username)
        reply_native = SimpleNamespace(from_user=reply_from_user)
        native = SimpleNamespace(reply_to_message=reply_native)
        reply_to = MessageRef(transport_id="telegram", native_id="0", chat=_chat())
    return IncomingMessage(
        chat=_chat(),
        sender=_sender(handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=native,
    )


# extract_mentions


def test_extract_mentions_empty_text():
    assert extract_mentions("") == []


def test_extract_mentions_single():
    assert extract_mentions("@acme_dev_bot do X") == ["acme_dev_bot"]


def test_extract_mentions_multiple():
    out = extract_mentions("@bot_a and @bot_b please")
    assert out == ["bot_a", "bot_b"]


def test_extract_mentions_case_folding():
    assert extract_mentions("@Acme_Dev_Bot hi") == ["acme_dev_bot"]


def test_extract_mentions_ignores_non_mention_text():
    assert extract_mentions("no mentions here") == []


def test_extract_mentions_strips_punctuation_boundaries():
    assert extract_mentions("hey @bot_a, can you") == ["bot_a"]


# is_directed_at_me


def test_directed_at_me_via_mention():
    msg = _msg(text="@acme_dev_bot implement task 1")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_via_reply_to_bot():
    msg = _msg(text="please redo this", reply_to_bot_username="acme_dev_bot")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_not_directed_when_mention_is_other_bot():
    msg = _msg(text="@acme_manager_bot review")
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_not_directed_when_no_mention_no_reply():
    msg = _msg(text="just chatting")
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_reply_to_me_is_suppressed_when_user_mentions_other_bot():
    """Regression: if the user replies to bot A's message but only @mentions
    bot B, bot A must not respond (previously both woke up)."""
    msg = _msg(
        text="@acme_manager_bot",
        reply_to_bot_username="acme_dev_bot",
    )
    assert is_directed_at_me(msg, "acme_dev_bot") is False
    assert is_directed_at_me(msg, "acme_manager_bot") is True


def test_reply_to_me_still_fires_without_any_mention():
    msg = _msg(text="yes please redo it", reply_to_bot_username="acme_dev_bot")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_mention_match_is_case_insensitive():
    msg = _msg(text="@Acme_Dev_Bot hi")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_when_human_mentions_bot():
    msg = _msg(text="@acme_dev_bot help me out", sender_handle="alice", sender_is_bot=False)
    assert is_directed_at_me(msg, "acme_dev_bot") is True


# is_from_self / is_from_other_bot


def test_is_from_self_true_when_usernames_match():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is True


def test_is_from_self_false_when_different_username():
    msg = _msg(sender_handle="acme_manager_bot", sender_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_self_false_when_not_bot():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=False)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_other_bot_true():
    msg = _msg(sender_handle="acme_manager_bot", sender_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is True


def test_is_from_other_bot_false_when_human():
    msg = _msg(sender_handle="revaz", sender_is_bot=False)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False


def test_is_from_other_bot_false_when_self():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False
```

- [ ] **Step 3.2: Run tests and confirm failure**

Run: `python -m pytest tests/test_group_filters.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_mentions' from 'link_project_to_chat.group_filters'` (or similar signature mismatches).

- [ ] **Step 3.3: Rewrite group_filters.py**

Replace the content of `src/link_project_to_chat/group_filters.py` with:

```python
"""Pure functions for deciding whether a group-chat message is directed at this bot.

No transport-specific dependencies — takes an IncomingMessage and returns bools.

One exception: `is_reply_to_bot` uses the `msg.native` escape hatch to read
reply_to_message.from_user.username, because MessageRef doesn't carry sender
info. Documented scope limit for spec #0a.
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
    """True when the message was sent by this bot itself (prevents self-reply loops)."""
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return sender == my_username.lower()


def is_from_other_bot(msg: IncomingMessage, my_username: str) -> bool:
    """True when the message was sent by a different bot account.

    Note: a relayed bot-to-bot message (msg.is_relayed_bot_to_bot=True) has
    sender=trusted user, so this check returns False for relays. Call sites
    that care about bot-to-bot semantics should also check
    `msg.is_relayed_bot_to_bot`.
    """
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return bool(sender) and sender != my_username.lower()


def mentions_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message text mentions this bot via `@handle`."""
    target = bot_username.lower()
    return target in extract_mentions(msg.text)


def is_reply_to_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message is a reply to an earlier message from this bot.

    Uses the `native` escape hatch — MessageRef doesn't carry sender info.
    Future work (not #0a): MessageRef.sender: Identity | None.
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
    """Top-level decision: treat the message as addressed to this bot.

    An explicit @mention always wins. A reply to this bot's prior message only
    counts when the user did NOT @mention anyone else — otherwise replying to
    bot A while pinging bot B would wake both A and B.
    """
    if mentions_bot(msg, my_username):
        return True
    if extract_mentions(msg.text):
        return False
    return is_reply_to_bot(msg, my_username)
```

- [ ] **Step 3.4: Run tests and confirm pass**

Run: `python -m pytest tests/test_group_filters.py -v`
Expected: all PASS (6 extract_mentions tests + 14 filter tests).

- [ ] **Step 3.5: Commit**

```bash
git add src/link_project_to_chat/group_filters.py tests/test_group_filters.py
git commit -m "refactor(group): port group_filters to IncomingMessage; add extract_mentions"
```

---

## Task 4: Update bot.py `_on_text` caller of group_filters

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 4.1: Construct transient IncomingMessage inside _on_text group block**

In `src/link_project_to_chat/bot.py`, find the group_filters call site inside `_on_text` (around line 576-607). The current code passes `msg` (telegram Message) to `is_from_self` / `is_directed_at_me` / `is_from_other_bot`. Replace with transient IncomingMessage construction:

Find the block:

```python
if self.group_mode:
    # Auto-capture: if chat_id not yet bound (sentinel 0 or None), and sender is the trusted user,
    # write this group's chat_id into the team config and update in-memory state.
    if self.group_chat_id in (0, None):
        if self._auth(update.effective_user) and self.team_name:
            new_chat_id = msg.chat_id
            patch_team(self.team_name, {"group_chat_id": new_chat_id})
            self.group_chat_id = new_chat_id
            # Fall through so this same message still gets processed normally.
    elif msg.chat_id != self.group_chat_id:
        return  # wrong group — silent ignore
    from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot
    if is_from_self(msg, self.bot_username):
        return  # self-silence
    if not is_directed_at_me(msg, self.bot_username):
        return  # not addressed to this bot
    chat_id = msg.chat_id
    if is_from_other_bot(msg, self.bot_username):
        # Bot-to-bot message: check halt before acting.
        # (full body: check halt, note_bot_to_bot, check if cap tripped, send auto-pause message)
```

Replace the filter-calling lines with transient IncomingMessage construction:

```python
if self.group_mode:
    # Auto-capture: if chat_id not yet bound (sentinel 0 or None), and sender is the trusted user,
    # write this group's chat_id into the team config and update in-memory state.
    if self.group_chat_id in (0, None):
        if self._auth(update.effective_user) and self.team_name:
            new_chat_id = msg.chat_id
            patch_team(self.team_name, {"group_chat_id": new_chat_id})
            self.group_chat_id = new_chat_id
    elif msg.chat_id != self.group_chat_id:
        return

    # Transient IncomingMessage for group_filters — fully replaced by
    # _on_text_from_transport dispatch in Task 8.
    from .transport import IncomingMessage
    from .transport.telegram import chat_ref_from_telegram, identity_from_telegram_user
    _transient_incoming = IncomingMessage(
        chat=chat_ref_from_telegram(msg.chat),
        sender=identity_from_telegram_user(update.effective_user),
        text=msg.text or "",
        files=[],
        reply_to=None,  # not needed for group_filters in this transient path; is_reply_to_bot uses native
        native=msg,
    )
    from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot
    if is_from_self(_transient_incoming, self.bot_username):
        return
    if not is_directed_at_me(_transient_incoming, self.bot_username):
        return
    chat_ref = chat_ref_from_telegram(msg.chat)
    if is_from_other_bot(_transient_incoming, self.bot_username):
        if self._group_state.get(chat_ref).halted:
            return
        self._group_state.note_bot_to_bot(chat_ref)
        if self._group_state.get(chat_ref).halted:
            await msg.reply_text(
                f"Auto-paused after {self._group_state.max_bot_rounds} bot-to-bot rounds. "
                "Send any message to resume."
            )
            return
    else:
        self._group_state.resume(chat_ref)
```

- [ ] **Step 4.2: Run bot-related tests**

Run: `python -m pytest tests/test_bot_streaming.py tests/test_bot_voice.py tests/test_bot_team_wiring.py tests/test_group_halt_integration.py tests/test_group_state.py tests/test_group_filters.py tests/transport/ -v 2>&1 | tail -10`
Expected: group_state, group_filters, transport all PASS. team_wiring and halt_integration may have failures; we'll fix those in Tasks 9 + 10. Key tests needing green here: group_state, group_filters, transport.

- [ ] **Step 4.3: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): use transient IncomingMessage for group_filters call in _on_text"
```

---

## Task 5: Relay-prefix detection in TelegramTransport

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix():
    """Inbound text starting with '[auto-relay from <handle>]' is marked as relayed
    and the prefix is stripped from the dispatched IncomingMessage.text."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=-100123, type="supergroup")
    tg_user = SimpleNamespace(id=42, full_name="Rezo", username="rezo", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="[auto-relay from bot_a]\n\n@bot_b go do X",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert captured[0].is_relayed_bot_to_bot is True
    assert captured[0].text == "@bot_b go do X"


async def test_dispatch_non_relay_text_unchanged():
    """Messages without the relay prefix have is_relayed_bot_to_bot=False and text unchanged."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hello world",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert captured[0].is_relayed_bot_to_bot is False
    assert captured[0].text == "hello world"
```

- [ ] **Step 5.2: Run tests and confirm failure**

Run: `python -m pytest tests/transport/test_telegram_transport.py::test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix -v`
Expected: FAIL — `assert False is True` because `is_relayed_bot_to_bot` stays False by default (not yet detected by the transport).

- [ ] **Step 5.3: Add prefix detection in _dispatch_message**

In `src/link_project_to_chat/transport/telegram.py`, find the `_dispatch_message` method. Near the bottom of the method, right before `for h in self._message_handlers: await h(incoming)`, add the detection logic:

Locate:

```python
        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=msg.text or getattr(msg, "caption", None) or "",
            files=files,
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
        )
        for h in self._message_handlers:
            await h(incoming)
```

Replace with:

```python
        import re
        text = msg.text or getattr(msg, "caption", None) or ""
        is_relayed = False
        # The Telethon relay posts messages with this prefix (no '@' on the handle —
        # intentional, see transport/_telegram_relay.py comment). Detect and strip.
        relay_match = re.match(r"^\[auto-relay from [A-Za-z][A-Za-z0-9_]*\]\n\n", text)
        if relay_match:
            is_relayed = True
            text = text[relay_match.end():]

        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=text,
            files=files,
            reply_to=(
                message_ref_from_telegram(msg.reply_to_message)
                if msg.reply_to_message is not None
                else None
            ),
            native=msg,
            is_relayed_bot_to_bot=is_relayed,
        )
        for h in self._message_handlers:
            await h(incoming)
```

(`import re` may already be in the file. Confirm via `grep -n "^import re" src/link_project_to_chat/transport/telegram.py`; if missing, add to the imports at the top of the file rather than inside the function.)

- [ ] **Step 5.4: Run tests and confirm pass**

Run: `python -m pytest tests/transport/test_telegram_transport.py -v 2>&1 | tail -12`
Expected: all PASS (previous tests + 2 new).

- [ ] **Step 5.5: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): detect relay prefix and set IncomingMessage.is_relayed_bot_to_bot"
```

---

## Task 6: Add `_handle_group_text` helper to ProjectBot

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 6.1: Add the method**

In `src/link_project_to_chat/bot.py`, add a new method on `ProjectBot`. Place it near `_on_file_from_transport` (co-locate with transport-native handlers):

```python
    async def _handle_group_text(self, incoming) -> bool:
        """Route a group-mode text message via the Transport-native path.

        Returns True if handled (caller should return immediately).
        Returns False if the message should proceed to further processing
        (normal user flow OR bot-to-bot direct-to-Claude).
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
            # Caller submits to Claude on False return via _submit_group_message_to_claude.
            return False

        # Human (trusted user) message — reset the round counter and clear any halt.
        self._group_state.resume(incoming.chat)
        return False
```

- [ ] **Step 6.2: Verify no new imports needed**

`patch_team` should already be imported at the top of bot.py (from spec #0 work). Verify via `grep -n "from .config import" src/link_project_to_chat/bot.py` — `patch_team` is in the list.

- [ ] **Step 6.3: No new tests yet — method is exercised in Task 8**

This method is only called from `_on_text_from_transport` (wired in Task 8) and from refactored tests (Tasks 9–10). We don't add a dedicated unit test for it — the integration tests in Tasks 9 + 10 cover all its branches.

- [ ] **Step 6.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat(bot): _handle_group_text method for transport-native group dispatch"
```

---

## Task 7: Add `_submit_group_message_to_claude` helper to ProjectBot

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 7.1: Add the method**

In `src/link_project_to_chat/bot.py`, add near `_handle_group_text`:

```python
    async def _submit_group_message_to_claude(self, incoming) -> None:
        """Bypass auth + rate-limit and submit a bot-to-bot message to Claude.

        Called when _handle_group_text returned False AND the sender is a bot/relay
        (so the message has already been validated as peer-bot-to-this-bot and
        the round counter has been incremented).

        Human messages in groups go through the full auth/rate-limit path via
        the legacy _on_text shim — not through this method.
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

- [ ] **Step 7.2: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat(bot): _submit_group_message_to_claude bypasses auth for bot-to-bot"
```

---

## Task 8: Wire group logic into `_on_text_from_transport`; delete group block from `_on_text`

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 8.1: Update _on_text_from_transport to dispatch group messages**

In `src/link_project_to_chat/bot.py`, find `_on_text_from_transport`. Its current text branch:

```python
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
```

Replace with:

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
                # Human message in group — fall through to legacy _on_text shim
                # for the full auth/rate-limit/pending-skill/pending-persona flow.
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

- [ ] **Step 8.2: Delete the group block from _on_text**

In `_on_text` (the legacy shim path), find and delete the entire `if self.group_mode:` block (the one introduced by Task 4's transient IncomingMessage construction; now dead code because the group logic lives in `_handle_group_text`).

Approximately, find and delete the block that starts with `if self.group_mode:` near the top of `_on_text` body and ends right before `if not self._auth(update.effective_user):`.

After the deletion, `_on_text` begins its body with:

```python
    async def _on_text(self, update, ctx) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        # (remaining body: rate-limit check, pending_skill/pending_persona handling,
        # Claude task submission — all unchanged from the pre-Task-8 state)
```

(The group logic is fully handled in `_on_text_from_transport` now; `_on_text` handles DM and the "human-in-group" fall-through case where auth already ran via `_handle_group_text`'s auto-capture.)

- [ ] **Step 8.3: Run tests**

Run: `python -m pytest tests/test_bot_streaming.py tests/test_bot_voice.py tests/transport/ -v 2>&1 | tail -10`
Expected: all PASS (group-specific tests still fail pending Tasks 9 + 10, but these transport+streaming+voice tests must stay green).

- [ ] **Step 8.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "refactor(bot): route group-mode text via _handle_group_text in transport dispatch"
```

---

## Task 9: Refactor `tests/test_bot_team_wiring.py` to use transport dispatch

**Files:**
- Modify: `tests/test_bot_team_wiring.py`

- [ ] **Step 9.1: Inspect the current test file**

```bash
head -50 tests/test_bot_team_wiring.py
```

The file constructs telegram Update stubs and calls `bot._on_text(update, ctx)` directly. Refactor to use `FakeTransport.inject_message` where group scenarios are exercised.

- [ ] **Step 9.2: Introduce shared helpers at the top of the file**

Add near the top of `tests/test_bot_team_wiring.py`, after the existing imports:

```python
from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage
from link_project_to_chat.transport.fake import FakeTransport


def _team_bot_with_fake_transport(bot):
    """Replace a team ProjectBot's _transport with a FakeTransport for assertion."""
    bot._transport = FakeTransport()
    return bot


def _group_chat(chat_id: int) -> ChatRef:
    return ChatRef(transport_id="fake", native_id=str(chat_id), kind=ChatKind.ROOM)


def _sender_identity(uid: int, handle: str, is_bot: bool) -> Identity:
    return Identity(
        transport_id="fake", native_id=str(uid),
        display_name=handle, handle=handle, is_bot=is_bot,
    )


def _group_incoming(
    chat: ChatRef,
    text: str,
    *,
    sender_handle: str = "rezo",
    sender_is_bot: bool = False,
    is_relayed: bool = False,
    reply_to_bot_username: str | None = None,
) -> IncomingMessage:
    from types import SimpleNamespace
    native = None
    reply_to = None
    if reply_to_bot_username:
        reply_from_user = SimpleNamespace(username=reply_to_bot_username)
        reply_native = SimpleNamespace(from_user=reply_from_user)
        native = SimpleNamespace(reply_to_message=reply_native, message_id=1)
        from link_project_to_chat.transport import MessageRef
        reply_to = MessageRef(transport_id="fake", native_id="0", chat=chat)
    return IncomingMessage(
        chat=chat,
        sender=_sender_identity(uid=1, handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=native,
        is_relayed_bot_to_bot=is_relayed,
    )
```

- [ ] **Step 9.3: Rewrite scenario-driving tests**

For each test in the file that currently calls `bot._on_text(update, ctx)` for a group-mode scenario, rewrite to:

1. Install a `FakeTransport` on the bot via `_team_bot_with_fake_transport(bot)`.
2. Build a `ChatRef` + `IncomingMessage` via the helpers.
3. Call `await bot._on_text_from_transport(incoming)`.
4. Assert against `bot._transport.sent_messages` / `bot._transport.edited_messages` / `bot.task_manager.submit_claude.call_args` / `bot._group_state.get(chat).halted` etc.

Example conversion: a test that previously did:

```python
# BEFORE
update = _build_group_text_update(
    chat_id=-100123, user_id=42, username="rezo",
    text="@acme_dev_bot do X",
)
await bot._on_text(update, ctx)
assert bot.task_manager.submit_claude.called
```

Becomes:

```python
# AFTER
_team_bot_with_fake_transport(bot)
chat = _group_chat(-100123)
incoming = _group_incoming(chat, "@acme_dev_bot do X")
await bot._on_text_from_transport(incoming)
assert bot.task_manager.submit_claude.called
```

Go through each test one at a time. For `test_permissions_callback_works_in_group_chat` (which was already ported in spec #0), leave it alone — it uses `bot._on_button(click)` and doesn't touch `_on_text`.

For auth tests that check the "unauthorized user silently dropped" invariant, add `assert bot.task_manager.submit_claude.assert_not_called()` and `assert bot._transport.sent_messages == []` assertions — the FakeTransport provides this observability.

- [ ] **Step 9.4: Run the test file**

Run: `python -m pytest tests/test_bot_team_wiring.py -v 2>&1 | tail -30`
Expected: all tests PASS.

If some tests fail because the `bot` fixture doesn't set `_transport` before the test body runs (e.g., the legacy flow didn't need it), adjust each test to call `_team_bot_with_fake_transport(bot)` before the scenario action. Do NOT modify the production bot code to work around test setup issues.

- [ ] **Step 9.5: Commit**

```bash
git add tests/test_bot_team_wiring.py
git commit -m "test(bot-team): refactor scenarios to drive via FakeTransport.inject_message"
```

---

## Task 10: Refactor `tests/test_group_halt_integration.py` + add Q4-C fix tests

**Files:**
- Modify: `tests/test_group_halt_integration.py`

- [ ] **Step 10.1: Refactor existing scenarios to transport dispatch**

Same pattern as Task 9. At the top of the file, add the same helpers (`_team_bot_with_fake_transport`, `_group_chat`, `_sender_identity`, `_group_incoming`). DO NOT duplicate them if they're already importable from `tests/test_bot_team_wiring.py` — pytest allows cross-test-file imports, but test files shouldn't depend on each other. Inline the helpers in both files (duplication is acceptable in test helpers; avoids test-file coupling).

For each test that currently calls `bot._on_text(update, ctx)` in a halt-integration scenario, rewrite to build IncomingMessage + call `bot._on_text_from_transport(incoming)`.

- [ ] **Step 10.2: Add the Q4-C fix tests**

Append to `tests/test_group_halt_integration.py`:

```python
@pytest.mark.asyncio
async def test_relayed_bot_to_bot_increments_round_counter():
    """Q4-C fix: relayed bot-to-bot messages (is_relayed_bot_to_bot=True)
    increment the round counter (previously reset it, per the v1 tradeoff)."""
    bot = _make_team_bot_stub(role="manager")  # Use existing test helper; verify name via grep if different
    _team_bot_with_fake_transport(bot)
    chat = _group_chat(int(bot.group_chat_id))

    for _ in range(20):
        incoming = _group_incoming(
            chat,
            text="@acme_manager_bot please continue",
            sender_handle="rezo",  # trusted user (that's who the relay posts as)
            sender_is_bot=False,
            is_relayed=True,
        )
        await bot._on_text_from_transport(incoming)

    # Counter should have auto-halted by round 20.
    assert bot._group_state.get(chat).halted is True


@pytest.mark.asyncio
async def test_native_bot_sender_increments_round_counter():
    """For non-Telegram transports: a sender with is_bot=True also increments."""
    bot = _make_team_bot_stub(role="manager")
    _team_bot_with_fake_transport(bot)
    chat = _group_chat(int(bot.group_chat_id))

    for _ in range(20):
        incoming = _group_incoming(
            chat,
            text="@acme_manager_bot please continue",
            sender_handle="acme_dev_bot",  # peer bot
            sender_is_bot=True,
            is_relayed=False,
        )
        await bot._on_text_from_transport(incoming)

    assert bot._group_state.get(chat).halted is True
```

The `_make_team_bot_stub` helper: confirm its exact name by grepping `tests/test_group_halt_integration.py` for `def _make_team_bot_stub` or similar. Rename if the actual helper has a different name. If the file has a `_team_bot(...)` or `_bot_in_group(...)` factory, use that.

- [ ] **Step 10.3: Run the test file**

Run: `python -m pytest tests/test_group_halt_integration.py -v 2>&1 | tail -20`
Expected: all tests PASS including the 2 new ones.

- [ ] **Step 10.4: Commit**

```bash
git add tests/test_group_halt_integration.py
git commit -m "test(group-halt): refactor to transport dispatch + Q4-C relayed-counter fix tests"
```

---

## Task 11: Move `team_relay.py` into `transport/`

**Files:**
- Move: `src/link_project_to_chat/manager/team_relay.py` → `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py` (import path only)
- Modify: `src/link_project_to_chat/manager/bot.py` (import path only — temporarily; full integration in Task 12)

- [ ] **Step 11.1: Perform the git move**

```bash
git mv src/link_project_to_chat/manager/team_relay.py src/link_project_to_chat/transport/_telegram_relay.py
```

- [ ] **Step 11.2: Update the import path in tests**

In `tests/test_team_relay.py`, change:

```python
from link_project_to_chat.manager.team_relay import (
    TeamRelay,
    find_peer_mention,
    is_relayed_text,
)
```

to:

```python
from link_project_to_chat.transport._telegram_relay import (
    TeamRelay,
    find_peer_mention,
    is_relayed_text,
)
```

- [ ] **Step 11.3: Update imports in manager/bot.py**

In `src/link_project_to_chat/manager/bot.py`, grep for `team_relay`:

```bash
grep -n "team_relay" src/link_project_to_chat/manager/bot.py
```

For each match, update the import path from `from .team_relay import ...` (or `from ..manager.team_relay import ...`) to `from ..transport._telegram_relay import ...`.

- [ ] **Step 11.4: Run the tests and the bot to confirm no breakage**

Run: `python -m pytest tests/test_team_relay.py tests/test_manager_create_team.py tests/test_process_manager_teams.py -v 2>&1 | tail -10`
Expected: `test_team_relay.py` PASSES. Manager-side tests may or may not touch the relay import — check output. Any test that fails because the import path changed needs its import updated, same pattern.

- [ ] **Step 11.5: Commit**

```bash
git add -A  # captures both the move and the import updates
git commit -m "refactor(transport): move team_relay.py into transport/_telegram_relay.py"
```

---

## Task 12: Add `TelegramTransport.enable_team_relay` + lifecycle integration

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `tests/transport/test_telegram_transport.py`
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 12.1: Write the failing test**

Append to `tests/transport/test_telegram_transport.py`:

```python
async def test_enable_team_relay_lifecycle():
    """enable_team_relay stashes config; start() starts the relay; stop() stops it."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    mock_client = MagicMock()
    mock_client.add_event_handler = MagicMock(return_value=object())  # handle object
    mock_client.remove_event_handler = MagicMock()

    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    await t.start()
    mock_client.add_event_handler.assert_called_once()

    await t.stop()
    mock_client.remove_event_handler.assert_called_once()


async def test_build_without_enable_team_relay_starts_and_stops_cleanly():
    """TelegramTransport without a team relay starts/stops without touching relay code."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    await t.start()
    await t.stop()
    # No assertion beyond "didn't raise" — the implicit contract is that start/stop
    # don't invoke a non-existent relay.
```

- [ ] **Step 12.2: Run tests and confirm failure**

Run: `python -m pytest tests/transport/test_telegram_transport.py::test_enable_team_relay_lifecycle -v`
Expected: FAIL — `AttributeError: 'TelegramTransport' object has no attribute 'enable_team_relay'`.

- [ ] **Step 12.3: Add enable_team_relay and lifecycle integration**

In `src/link_project_to_chat/transport/telegram.py`, add to `TelegramTransport.__init__` (after existing attribute inits):

```python
        self._team_relay = None  # Set by enable_team_relay; lifecycle-tied to start/stop.
```

Add the method (place it near `build()` / `attach_telegram_routing()`):

```python
    def enable_team_relay(
        self,
        telethon_client: Any,
        team_bot_usernames: set[str],
        group_chat_id: int,
        team_name: str,
    ) -> None:
        """Activate the Telethon-user-session relay for a team group chat.

        Required because Telegram Bot API never delivers bot-to-bot messages.
        Other transports (Discord, Slack, Web) don't need this and don't
        implement it — this method is TelegramTransport-specific, not on the
        Transport Protocol.

        Call once after build(), before start(). Relay lifecycle is tied to
        start()/stop() thereafter.
        """
        from ._telegram_relay import TeamRelay
        self._team_relay = TeamRelay(
            client=telethon_client,
            team_name=team_name,
            group_chat_id=group_chat_id,
            bot_usernames=team_bot_usernames,
        )
```

In the existing `start()` method, add to the tail (AFTER `on_ready` callbacks fire, BEFORE `app.start()` + `start_polling()`):

Find the existing structure:

```python
    async def start(self) -> None:
        await self._app.initialize()
        # ... existing post-init: delete_webhook, get_me, set_my_commands, on_ready callbacks ...
        await self._app.start()
        await self._app.updater.start_polling()
```

Insert the relay start between on_ready callbacks and `app.start()`:

```python
    async def start(self) -> None:
        await self._app.initialize()
        # ... existing post-init ...

        # Fire on_ready callbacks (existing code)
        for cb in self._on_ready_callbacks:
            await cb(self_identity)

        # Start the team relay (if one was configured via enable_team_relay).
        if self._team_relay is not None:
            await self._team_relay.start()

        await self._app.start()
        await self._app.updater.start_polling()
```

In `stop()`, add the relay stop at the beginning:

```python
    async def stop(self) -> None:
        if self._team_relay is not None:
            await self._team_relay.stop()
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
```

- [ ] **Step 12.4: Update manager bot to use the new API**

In `src/link_project_to_chat/manager/bot.py`, grep for TeamRelay usage:

```bash
grep -n "TeamRelay" src/link_project_to_chat/manager/bot.py
```

For each site where the manager directly instantiates `TeamRelay` and calls `.start()`, replace with a call to the project bot's transport:

Find the pattern:

```python
from ..transport._telegram_relay import TeamRelay  # updated in Task 11
relay = TeamRelay(client=telethon_client, team_name=..., group_chat_id=..., bot_usernames=...)
await relay.start()
```

Replace with:

```python
# The project bot's TelegramTransport handles relay lifecycle via enable_team_relay.
# Manager passes its Telethon client + team peers to the bot at startup; bot's
# build() wires them into the transport.
```

If the manager previously passed a TeamRelay instance to project bots via some API, refactor so it passes the Telethon client + peer usernames instead, and the project bot's build() calls `self._transport.enable_team_relay(...)`. Check how the project bot currently obtains its Telethon client for relay — if via a constructor argument, keep that argument and move the `enable_team_relay` call into `ProjectBot.build()`.

Grep `ProjectBot.__init__` for any Telethon-related argument:

```bash
grep -n "telethon" src/link_project_to_chat/bot.py | head -20
```

If none, the manager's relay management is entirely manager-side today and the project bots don't have their own relay — in which case, Task 12 just adds the method but no manager bot changes, and the manager continues to run `TeamRelay` directly until spec #0c ports it fully.

**Decision:** If the manager bot's TeamRelay usage is entirely separate from project bots' TelegramTransport instances (which is the current architecture), leave the manager bot's direct TeamRelay usage in place. The `enable_team_relay` method ships for future use; manager bot port is spec #0c's job. Document this in the commit message.

- [ ] **Step 12.5: Run tests and confirm pass**

Run: `python -m pytest tests/transport/test_telegram_transport.py tests/test_team_relay.py tests/test_manager_create_team.py tests/test_process_manager_teams.py -v 2>&1 | tail -15`
Expected: all PASS (if any manager tests fail, verify they're unrelated optional-dep failures and not regressions).

- [ ] **Step 12.6: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py tests/transport/test_telegram_transport.py
git commit -m "feat(transport): TelegramTransport.enable_team_relay + lifecycle integration"
```

(If manager bot actually required changes in Step 12.4, add `src/link_project_to_chat/manager/bot.py` to the commit and note "update manager bot to pass relay config via transport" in the message.)

---

## Task 13: Cleanups M4 + M6

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 13.1: M4 — replace hardcoded transport_id**

```bash
grep -n 'transport_id="telegram"' src/link_project_to_chat/bot.py
```

For each match, replace `transport_id="telegram"` with `transport_id=self._transport.TRANSPORT_ID`. Keep the surrounding context.

Example change: `ChatRef(transport_id="telegram", native_id=str(chat_id), kind=...)` → `ChatRef(transport_id=self._transport.TRANSPORT_ID, native_id=str(chat_id), kind=...)`.

Watch for call sites that don't have `self._transport` in scope (e.g., static methods or helpers called before the transport is instantiated). Grep for those edge cases and leave them with the hardcoded string if there's no alternative — in that case, add a `# TODO(#0c/#1): unhardcode when moving to non-Telegram transport` comment. Ideally there are zero such sites.

After replacement, re-grep:

```bash
grep -n 'transport_id="telegram"' src/link_project_to_chat/bot.py
```

Expected: zero matches.

- [ ] **Step 13.2: M6 — delete dead LiveMessage import**

In `src/link_project_to_chat/bot.py`, delete the line:

```python
from .livestream import LiveMessage  # legacy — kept for test_livestream.py; bot.py uses StreamingMessage
```

Verify no remaining LiveMessage references in bot.py:

```bash
grep -n "LiveMessage" src/link_project_to_chat/bot.py
```

Expected: zero matches.

- [ ] **Step 13.3: Run tests**

Run: `python -m pytest tests/ 2>&1 | tail -5`
Expected: same pass/fail count as before this task (no regressions from the cleanups).

- [ ] **Step 13.4: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "chore(bot): unhardcode transport_id; drop dead LiveMessage import"
```

---

## Task 14: Cleanup M5 — fix ask-question annotation

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `tests/test_bot_streaming.py`

- [ ] **Step 14.1: Find the existing question-rendering code**

The question HTML is built inside `_on_waiting_input` (around bot.py line 446). Locate it:

```bash
grep -n "def _on_waiting_input\|parse_entities\|_render_question" src/link_project_to_chat/bot.py
```

Read the method and identify the block that builds the question HTML — it should construct lines like:

```python
lines = []
if header:
    lines.append(f"<b>{_esc(header)}</b>")
lines.append(_esc(body))
# (multi_select prompt text here — see existing code)
for opt in question.options:
    if opt.description:
        lines.append(f"• <b>{_esc(opt.label)}</b> — {_esc(opt.description)}")
```

- [ ] **Step 14.2: Extract _render_question_html helper**

In `src/link_project_to_chat/bot.py`, add a new method on `ProjectBot` near `_on_waiting_input`:

```python
    @staticmethod
    def _render_question_html(question) -> str:
        """Render an AskUserQuestion Question as Telegram-compatible HTML.

        Used by _on_waiting_input (initial send) and _on_button (ask-answer
        annotation after user picks an option).
        """
        from html import escape as _esc

        header = question.header
        body = question.question
        lines: list[str] = []
        if header:
            lines.append(f"<b>{_esc(header)}</b>")
        lines.append(_esc(body))
        if question.multi_select:
            lines.append("<i>(Multi-select: tap an option or reply with comma-separated values.)</i>")
        else:
            lines.append("<i>(Tap an option or reply with free text.)</i>")
        for opt in question.options:
            if opt.description:
                lines.append(f"• <b>{_esc(opt.label)}</b> — {_esc(opt.description)}")
        return "\n".join(lines)
```

Then update `_on_waiting_input` to use it:

Find the block that does inline HTML construction. Replace that construction with:

```python
        for q_idx, question in enumerate(task.pending_questions):
            html = self._render_question_html(question)
            await self._send_html(
                task.chat_id,
                html,
                reply_to=task.message_id,
                reply_markup=self._question_buttons(task.id, q_idx, question),
            )
```

(Exact line numbers vary — use grep to confirm. The existing code already calls `self._send_html(task.chat_id, "\n".join(lines), ...)`. Just replace `"\n".join(lines)` with `self._render_question_html(question)` and delete the now-unused local `lines` construction.)

- [ ] **Step 14.3: Fix the ask-answer branch in _on_button**

In `_on_button`, find the `ask_` branch that annotates after `submit_answer` succeeds. The current broken code:

```python
            if self.task_manager.submit_answer(task_id, label):
                # Annotate the selection into the existing message body.
                try:
                    original = ""
                    native = getattr(msg_ref, "native", None)
                    if native is not None:
                        original = getattr(native, "text_html", None) or getattr(native, "text", "") or ""
                    escaped = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    await self._transport.edit_text(
                        msg_ref,
                        f"{original}\n\n<i>Selected:</i> {escaped}",
                        html=True,
                    )
                except Exception:
                    logger.debug("could not annotate selected option", exc_info=True)
```

Replace with:

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

- [ ] **Step 14.4: Add the regression test**

Append to `tests/test_bot_streaming.py`:

```python
@pytest.mark.asyncio
async def test_ask_answer_annotation_preserves_question_html():
    """M5 regression: after user picks an option, the edit contains the original
    question HTML + 'Selected: X', not just the selection suffix."""
    from link_project_to_chat.stream import Question, QuestionOption
    from link_project_to_chat.transport import (
        ButtonClick, ChatKind, ChatRef, Identity, MessageRef,
    )
    from unittest.mock import MagicMock
    bot = await _stub_bot()
    bot._auth_identity = lambda _sender: True

    # Prepare a fake task with one pending question.
    task = _fake_task(task_id=77)
    task.pending_questions = [Question(
        question="Which option?",
        header="Pick one",
        options=[
            QuestionOption(label="Option A", description="desc A"),
            QuestionOption(label="Option B", description="desc B"),
        ],
    )]
    bot.task_manager = MagicMock()
    bot.task_manager.get = MagicMock(return_value=task)
    bot.task_manager.submit_answer = MagicMock(return_value=True)
    # Ensure task status gates work (status==WAITING_INPUT).
    from link_project_to_chat.task_manager import TaskStatus
    task.status = TaskStatus.WAITING_INPUT

    chat = ChatRef(transport_id="telegram", native_id="12345", kind=ChatKind.DM)
    msg_ref = MessageRef(transport_id="telegram", native_id="200", chat=chat)
    sender = Identity(
        transport_id="telegram", native_id="42",
        display_name="Alice", handle="alice", is_bot=False,
    )
    click = ButtonClick(chat=chat, message=msg_ref, sender=sender, value="ask_77_0_0")

    await bot._on_button(click)

    # Assert the edit contains both the question header AND "Selected:" annotation.
    edits = bot._app.bot.edits
    assert edits, "expected at least one edit after option click"
    edit_text = edits[-1]["text"]
    assert "Pick one" in edit_text
    assert "Which option?" in edit_text
    assert "Option A" in edit_text
    assert "<i>Selected:</i> Option A" in edit_text
```

- [ ] **Step 14.5: Run tests and confirm pass**

Run: `python -m pytest tests/test_bot_streaming.py::test_ask_answer_annotation_preserves_question_html -v`
Expected: PASS.

Also run the full bot test suite to catch regressions:

Run: `python -m pytest tests/test_bot_streaming.py tests/test_bot_voice.py tests/test_bot_team_wiring.py tests/test_group_halt_integration.py -v 2>&1 | tail -15`
Expected: all PASS.

- [ ] **Step 14.6: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "fix(bot): preserve question HTML in ask-answer annotation (M5)"
```

---

## Task 15: Final sweep + docs + version bump

**Files:**
- Modify: `where-are-we.md`
- Modify: `pyproject.toml`

- [ ] **Step 15.1: Final grep sweep**

Run these verification greps:

```bash
grep -n "from telegram\|import telegram" src/link_project_to_chat/bot.py
# Expected: zero matches (lockout holds)

grep -n 'transport_id="telegram"' src/link_project_to_chat/bot.py
# Expected: zero matches (M4 applied)

grep -n "LiveMessage" src/link_project_to_chat/bot.py
# Expected: zero matches (M6 applied)

grep -n "from .group_filters\|from .group_state" src/link_project_to_chat/bot.py
# Expected: imports consume the new IncomingMessage/ChatRef signatures
```

- [ ] **Step 15.2: Run the full test suite**

Run: `python -m pytest -v 2>&1 | tail -5`
Expected: same failure profile as baseline (28 pre-existing failures from optional deps) + all new/refactored tests PASS. No new failures.

- [ ] **Step 15.3: Update where-are-we.md**

In `where-are-we.md`, append to the `## Done` section after the voice-port entry:

```markdown
- **Group/team port — Transport-native** (spec #0a, v0.15.0):
  - `IncomingMessage.is_relayed_bot_to_bot` field — TelegramTransport detects the `[auto-relay from <handle>]` prefix and strips it; bot increments round counter on relayed paths (fixes the v1 tradeoff)
  - `group_state.py` re-keyed by `ChatRef` (was int chat_id)
  - `group_filters.py` ported to consume `IncomingMessage`; new `extract_mentions` regex helper
  - Bot-side group logic moved into `_handle_group_text` + `_submit_group_message_to_claude` on the transport-native `_on_text_from_transport` dispatch
  - `TelegramTransport.enable_team_relay(telethon_client, team_bot_usernames, group_chat_id, team_name)` — relay lifecycle tied to start()/stop(); `team_relay.py` moved into `transport/_telegram_relay.py`
  - Three cleanups: M4 hardcoded `transport_id="telegram"` → `self._transport.TRANSPORT_ID`; M5 ask-question annotation regression fixed via `_render_question_html` helper; M6 dead `LiveMessage` import removed
```

Remove the following stale lines from `## Pending`:

```markdown
- Group/team features (team_relay, group_filters, group_state) still telegram-specific (pending spec #0a)
```

(If `livestream.LiveMessage` cleanup line is also there, leave the remaining wording about "remove once confident no other code paths use it" — M6 only drops the bot.py import, not the livestream module.)

- [ ] **Step 15.4: Bump version**

In `pyproject.toml`, change:

```toml
version = "0.14.0"
```

to:

```toml
version = "0.15.0"
```

- [ ] **Step 15.5: Final commit**

```bash
git add where-are-we.md pyproject.toml
git commit -m "docs: note group/team port complete; bump to 0.15.0"
```

---

## Completion checklist

- [ ] All 15 tasks committed in order.
- [ ] `grep 'transport_id="telegram"' src/link_project_to_chat/bot.py` returns zero matches (M4).
- [ ] `grep "LiveMessage" src/link_project_to_chat/bot.py` returns zero matches (M6).
- [ ] `grep "from telegram\|import telegram" src/link_project_to_chat/bot.py` returns zero matches (lockout preserved).
- [ ] `pytest tests/test_group_state.py tests/test_group_filters.py -v` passes clean.
- [ ] `pytest tests/test_bot_team_wiring.py tests/test_group_halt_integration.py -v` passes, includes the two Q4-C fix tests.
- [ ] `pytest tests/transport/test_telegram_transport.py -v` passes, includes relay-prefix detection tests + `enable_team_relay` lifecycle test.
- [ ] `pytest tests/test_bot_streaming.py::test_ask_answer_annotation_preserves_question_html` passes (M5 regression fix).
- [ ] `where-are-we.md` mentions spec #0a under `## Done`; stale group/team pending line removed.
- [ ] `pyproject.toml` version == `0.15.0`.
- [ ] Spec #0a is closed.
- [ ] Specs #0c (manager port), #1 (Web UI), #2 (Discord), #3 (Slack) are unblocked.
