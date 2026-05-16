# `respond_in_groups` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the GitLab fork's standard-bot-in-Telegram-group behavior — when `ProjectConfig.respond_in_groups=True`, the bot responds to `@bot_username` mentions and replies to its own prior messages in groups; ignores everything else.

**Architecture:** One new per-project config field; 3-way switch on `TelegramTransport`'s PTB chat-type filter; a small routing gate in `ProjectBot._on_text_from_transport` that reuses `group_filters.is_directed_at_me` and a new `_strip_self_mention` helper. Sibling `elif` branch alongside the existing team-mode path — no changes to team-mode behavior.

**Tech Stack:** Python 3.14, `python-telegram-bot >= 22` (Telegram transport), `click >= 8`, `pytest` with `asyncio_mode=auto`. No new dependencies.

**Reference design:** [`docs/superpowers/specs/2026-05-16-respond-in-groups-design.md`](../specs/2026-05-16-respond-in-groups-design.md)

**Branch:** Create and work on `feat/respond-in-groups` off `dev` (`7f04adb` or later — the spec commit). Final destination: merge into `dev` for v1.1.0.

---

## Task 0: Setup branch + record baseline

**Files:**
- N/A (git + venv only)

- [ ] **Step 1: Create the feature branch off `dev`**

```bash
git checkout dev
git pull --ff-only
git checkout -b feat/respond-in-groups
git status
```
Expected: `On branch feat/respond-in-groups` with a clean working tree.

- [ ] **Step 2: Verify baseline test suite passes**

```bash
.venv/bin/pip install -e ".[all]"
.venv/bin/pytest -q
```

Record the actual passing count in this task's commit message (e.g., `chore: pin test baseline at 1143 passed, 5 skipped`). The number recorded here is the regression gate for the rest of the plan. As of the spec date the baseline on `dev` was `1143 passed, 5 skipped` (HEAD `14362b3` on `feat/plugin-system` before merge into dev); your number on `dev` HEAD may drift slightly — record what YOU see after `pip install -e ".[all]" && pytest -q`.

If anything fails after a clean install, **STOP** and ask before proceeding.

- [ ] **Step 3: Commit an empty baseline-pin commit**

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore: pin test baseline at N passed, M skipped for respond_in_groups branch

Baseline recorded for feat/respond-in-groups implementation.
Subsequent tasks must keep pytest -q at or above this count plus
the new tests added in each task.

Environment:
- Branch: feat/respond-in-groups off dev <head-sha>
- Python 3.14 in .venv
- pip install -e ".[all]" clean run

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Replace `N`, `M`, and `<head-sha>` with your actual numbers.

---

## Task 1: Add `ProjectConfig.respond_in_groups` field

**Files:**
- Modify: `src/link_project_to_chat/config.py` (add field on `ProjectConfig` dataclass; load + save logic)
- Create: `tests/test_config_respond_in_groups.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_respond_in_groups.py`:

```python
"""Per-project respond_in_groups field — load/save/default behavior.

The flag is default-off and only emitted on disk when True. Loader tolerates
missing keys (→ False) and non-bool values (→ False with WARNING).
"""
from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    load_config,
    save_config,
)


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2))


def test_respond_in_groups_defaults_false(tmp_path: Path):
    """A project with no respond_in_groups key loads as False."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is False


def test_respond_in_groups_round_trip_true(tmp_path: Path):
    """Setting True survives load/save/load."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is True
    save_config(loaded, cfg_file)
    reloaded = load_config(cfg_file)
    assert reloaded.projects["p"].respond_in_groups is True


def test_respond_in_groups_omitted_on_disk_when_false(tmp_path: Path):
    """save_config does not write the key when the field is False —
    keeps configs tidy and avoids spurious diffs for the default case."""
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        respond_in_groups=False,
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "respond_in_groups" not in raw["projects"]["p"]


def test_respond_in_groups_emitted_on_disk_when_true(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        respond_in_groups=True,
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["respond_in_groups"] is True


def test_respond_in_groups_non_bool_input_coerces_to_false(tmp_path: Path, caplog):
    """A string "yes" or a list value silently coerces to False with a WARNING.
    Real bools (True/False) and Python truthy ints (0/1) pass through bool().
    """
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": "yes",  # not a bool
            }
        }
    })
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is False
    assert any(
        "respond_in_groups" in r.message.lower()
        for r in caplog.records
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_config_respond_in_groups.py -v
```
Expected: All 5 tests FAIL — `ProjectConfig.respond_in_groups` doesn't exist yet (`AttributeError` or `TypeError: unexpected keyword argument`).

- [ ] **Step 3: Add the field to `ProjectConfig`**

In [`src/link_project_to_chat/config.py`](src/link_project_to_chat/config.py), find `class ProjectConfig:`. After the existing fields (after `plugins: list[dict] = field(default_factory=list)` or equivalent — keep alphabetical-ish ordering if the codebase already does):

```python
    respond_in_groups: bool = False
    # When True, the project bot responds in Telegram groups to messages that
    # @mention `@<bot_username>` OR reply to a prior bot message. All other
    # group messages are silently ignored. Default False (pre-v1.1.0 behavior:
    # DM-only). Independent of team mode: a team bot's group routing is
    # governed by team_name + role, not this flag. The PTB filter is set
    # once at startup, so toggling this field requires a bot restart.
```

- [ ] **Step 4: Wire the load path**

Find `_load_config_unlocked` in `config.py` (it iterates `raw.get("projects", {})` and constructs `ProjectConfig(...)` per entry). Add a tolerant parse for the new key. Locate where other ProjectConfig kwargs are populated (e.g., `plugins=_parse_plugins(...)`) and add:

```python
raw_rig = proj.get("respond_in_groups", False)
if isinstance(raw_rig, bool):
    respond_in_groups = raw_rig
elif isinstance(raw_rig, int) and not isinstance(raw_rig, bool):
    # Python: bool is subclass of int. Treat int 0/1 as bool via bool().
    respond_in_groups = bool(raw_rig)
else:
    logger.warning(
        "project %r: respond_in_groups must be a bool; got %r (treating as False)",
        name_iter, raw_rig,
    )
    respond_in_groups = False
```

Then pass `respond_in_groups=respond_in_groups` into the `ProjectConfig(...)` constructor call in the same block.

- [ ] **Step 5: Wire the save path**

Find `_save_config_unlocked` in `config.py`. Locate the per-project save block (next to `proj["plugins"] = p.plugins if p.plugins else …`). Add:

```python
if p.respond_in_groups:
    proj["respond_in_groups"] = True
else:
    proj.pop("respond_in_groups", None)
```

This keeps configs tidy: the key is only written when True. Pre-v1.1.0 configs without the key remain untouched on save.

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_config_respond_in_groups.py -v
```
Expected: All 5 tests PASS.

- [ ] **Step 7: Run the full suite for regressions**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 5 new tests pass. No regressions.

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config_respond_in_groups.py
git commit -m "$(cat <<'EOF'
feat(config): add ProjectConfig.respond_in_groups (default False)

Adds a per-project flag controlling whether the standard (non-team)
project bot responds in Telegram groups. Default False — pre-v1.1.0
behavior preserved.

Loader tolerates missing keys (→ False), non-bool values (→ False
with WARNING), and Python truthy ints (bool()-coerced).
Saver omits the key when False to keep configs tidy.

No new module; no schema migration. Field is consumed by:
- bot.py (Task 4: routing gate)
- transport/telegram.py (Task 2: PTB filter widening)
- cli.py (Task 6: --respond-in-groups flag)
- manager/bot.py (Task 7: _EDITABLE_FIELDS)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Widen `TelegramTransport` PTB filter when `respond_in_groups`

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py` (3-way filter switch in `attach_telegram_routing`; matching post-routing `on_command` filter)
- Modify: `tests/transport/test_dynamic_command_dispatch.py` (extend with `respond_in_groups` cases)

- [ ] **Step 1: Write the failing tests**

Append to [`tests/transport/test_dynamic_command_dispatch.py`](tests/transport/test_dynamic_command_dispatch.py):

```python
def test_telegram_transport_filter_widened_when_respond_in_groups_true():
    """When respond_in_groups=True, the MessageHandler filter accepts both
    private DMs AND groups. Matches the GitLab fork's behavior for solo
    bots; isolated from team mode (group_mode=True still narrows to GROUPS).
    """
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=True,
    )
    # Inspect the filter expression on the registered MessageHandler.
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    # ChatType.PRIVATE | ChatType.GROUPS — verify both present.
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()


def test_telegram_transport_filter_private_only_when_respond_in_groups_false():
    """Default behavior unchanged: solo bots see only PRIVATE messages."""
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=False,
    )
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    # GROUPS should NOT be in the filter for solo+respond_in_groups=False.
    assert "ChatType.GROUPS" not in filter_repr


def test_telegram_transport_filter_groups_only_when_team_mode():
    """Team-mode behavior unchanged: group_mode=True narrows to GROUPS,
    regardless of respond_in_groups (which is a solo-only concern)."""
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=True,
        command_names=["help"],
        respond_in_groups=True,  # ignored when group_mode=True
    )
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()
    # PRIVATE should NOT be in the team-mode filter.
    assert "ChatType.PRIVATE" not in filter_repr


def test_telegram_transport_late_on_command_picks_widened_filter():
    """When routing was attached with respond_in_groups=True, late on_command
    registrations also pick up the wider filter (so plugin commands work in
    both DMs and groups, not just one)."""
    pytest.importorskip("telegram")

    from telegram.ext import CommandHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=True,
    )

    async def late_handler(ci):
        return None

    transport.on_command("late_cmd", late_handler)
    late_ptb_handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler) and "late_cmd" in h.commands
    )
    filter_repr = repr(late_ptb_handler.filters)
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/transport/test_dynamic_command_dispatch.py -v
```
Expected: 4 new tests FAIL (`attach_telegram_routing` doesn't accept `respond_in_groups` kwarg yet). Existing tests still pass.

- [ ] **Step 3: Add `respond_in_groups` kwarg + 3-way filter switch in `attach_telegram_routing`**

In [`src/link_project_to_chat/transport/telegram.py`](src/link_project_to_chat/transport/telegram.py), locate `def attach_telegram_routing` (around line 263). Update the signature:

```python
def attach_telegram_routing(
    self,
    *,
    group_mode: bool,
    command_names: list[str],
    respond_in_groups: bool = False,
) -> None:
    """Wire telegram's MessageHandler/CommandHandler/CallbackQueryHandler
    so all incoming updates route through our _dispatch_* methods.

    ``group_mode``: team-mode bot — restrict to GROUPS only.
    ``respond_in_groups``: solo-mode bot that wants to also answer in groups.
    The two flags are mutually exclusive at the filter level: when both are
    True, group_mode wins (team workflow takes precedence).
    """
```

Replace the existing filter selection block:

```python
if group_mode:
    chat_filter = filters.ChatType.GROUPS
    incoming_filter = (
        chat_filter
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & filters.TEXT
        & ~filters.COMMAND
    )
else:
    chat_filter = filters.ChatType.PRIVATE
    incoming_filter = (
        chat_filter
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & (
            filters.TEXT
            | filters.Document.ALL
            | filters.PHOTO
            | filters.VOICE
            | filters.AUDIO
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
        )
        & ~filters.COMMAND
    )
```

with:

```python
if group_mode:
    chat_filter = filters.ChatType.GROUPS
    incoming_filter = (
        chat_filter
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & filters.TEXT
        & ~filters.COMMAND
    )
elif respond_in_groups:
    chat_filter = filters.ChatType.PRIVATE | filters.ChatType.GROUPS
    incoming_filter = (
        chat_filter
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & (
            filters.TEXT
            | filters.Document.ALL
            | filters.PHOTO
            | filters.VOICE
            | filters.AUDIO
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
        )
        & ~filters.COMMAND
    )
else:
    chat_filter = filters.ChatType.PRIVATE
    incoming_filter = (
        chat_filter
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & (
            filters.TEXT
            | filters.Document.ALL
            | filters.PHOTO
            | filters.VOICE
            | filters.AUDIO
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
        )
        & ~filters.COMMAND
    )
```

At the END of `attach_telegram_routing` (after `add_error_handler(...)`, alongside `self._routing_attached = True` and `self._group_mode_attached = group_mode`), add:

```python
self._respond_in_groups_attached: bool = respond_in_groups
```

In `__init__`, initialize the flag to False (alongside `_routing_attached` and `_group_mode_attached`):

```python
self._respond_in_groups_attached: bool = False
```

- [ ] **Step 4: Update post-routing `on_command` to pick the matching filter**

Find `def on_command(self, name: str, handler) -> None:` in the same file (around line 822). Update the dynamic PTB-handler registration block (the part that runs when `self._app is not None and self._routing_attached`). The current code picks the filter based on `_group_mode_attached`:

```python
chat_filter = (
    _filters.ChatType.GROUPS if self._group_mode_attached
    else _filters.ChatType.PRIVATE
)
```

Replace with:

```python
if self._group_mode_attached:
    chat_filter = _filters.ChatType.GROUPS
elif self._respond_in_groups_attached:
    chat_filter = _filters.ChatType.PRIVATE | _filters.ChatType.GROUPS
else:
    chat_filter = _filters.ChatType.PRIVATE
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/transport/test_dynamic_command_dispatch.py -v
```
Expected: all 4 new tests + existing tests PASS.

- [ ] **Step 6: Run the full suite for regressions**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 9 (5 from Task 1 + 4 from Task 2) new tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/transport/telegram.py \
        tests/transport/test_dynamic_command_dispatch.py
git commit -m "$(cat <<'EOF'
feat(transport): 3-way filter switch for respond_in_groups

attach_telegram_routing gains a respond_in_groups: bool = False kwarg.
The chat-type filter becomes a 3-way:

- group_mode=True  → ChatType.GROUPS (team-mode, unchanged)
- respond_in_groups=True (and group_mode=False)
                  → ChatType.PRIVATE | ChatType.GROUPS (NEW)
- both False (default) → ChatType.PRIVATE (unchanged)

The post-routing on_command dynamic registration picks the matching
filter so plugin commands work in both DMs and groups when the flag
is on.

When both flags are True, group_mode wins (team workflow takes
precedence). Documented in the method's docstring.

No call site is updated yet — bot.py uses the default False until
Task 5 plumbs the flag through run_bot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `_strip_self_mention` helper on `ProjectBot`

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (new private helper)
- Create: `tests/test_bot_strip_self_mention.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot_strip_self_mention.py`:

```python
"""Unit tests for ProjectBot._strip_self_mention.

The helper removes @<bot_username> (case-insensitive, word-bounded) from
the IncomingMessage's text — used by the respond_in_groups routing gate
to clean the prompt before it reaches the agent.
"""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)


def _make_bot(bot_username: str = "MyBot") -> ProjectBot:
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = bot_username
    return bot


def _make_incoming(text: str) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
    )


def test_strip_removes_simple_mention():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("@MyBot do X"))
    assert out.text == " do X"


def test_strip_is_case_insensitive():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("hello @MYBOT please"))
    assert out.text == "hello  please"


def test_strip_word_bounded_does_not_clobber_longer_handle():
    """@MyBotIsCool should NOT be stripped — it's a different handle."""
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("ping @MyBotIsCool here"))
    assert out.text == "ping @MyBotIsCool here"


def test_strip_word_bounded_does_not_clobber_email_at():
    """user@MyBot.example.com (an email-like string) must not be stripped."""
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("contact user@MyBot.example"))
    # The @MyBot inside the email has no leading non-word char before it,
    # but the negative lookbehind on @ catches it. The . after MyBot is the
    # word-boundary that allows the negative lookahead to match. Concretely,
    # this would strip user@MyBot if we weren't careful. The implementation
    # MUST use a negative lookbehind on @ ([^A-Za-z0-9_@] OR start-of-string).
    assert out.text == "contact user@MyBot.example"


def test_strip_leaves_other_mentions_intact():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("@MyBot and @SomeoneElse"))
    assert out.text == " and @SomeoneElse"


def test_strip_with_empty_bot_username_returns_unchanged():
    """Defensive: before _after_ready fires, bot_username may be empty."""
    bot = _make_bot("")
    incoming = _make_incoming("hi @MyBot")
    out = bot._strip_self_mention(incoming)
    assert out is incoming or out.text == "hi @MyBot"


def test_strip_returns_immutable_replacement():
    """_strip_self_mention returns a NEW IncomingMessage (dataclasses.replace).
    Original is untouched (IncomingMessage is frozen)."""
    bot = _make_bot("MyBot")
    incoming = _make_incoming("@MyBot ping")
    out = bot._strip_self_mention(incoming)
    assert incoming.text == "@MyBot ping"
    assert out.text == " ping"
    assert out is not incoming
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_bot_strip_self_mention.py -v
```
Expected: All 7 tests FAIL — `_strip_self_mention` doesn't exist yet (`AttributeError`).

- [ ] **Step 3: Implement `_strip_self_mention`**

In [`src/link_project_to_chat/bot.py`](src/link_project_to_chat/bot.py), find an appropriate spot in the `ProjectBot` class — alongside other small message helpers (e.g., just above `_on_text_from_transport`). Add:

```python
def _strip_self_mention(self, incoming: "IncomingMessage") -> "IncomingMessage":
    """Remove case-insensitive ``@<bot_username>`` from incoming.text.

    Word-bounded: only strips when the mention is bounded by non-word
    characters (or start/end of string). Handles like ``@MyBotIsCool`` or
    embedded sequences like ``user@MyBot.example`` are left intact.

    Returns a new ``IncomingMessage`` via ``dataclasses.replace`` since
    ``IncomingMessage`` is frozen. When ``self.bot_username`` is empty
    (typical before ``_after_ready`` fires), the helper is a no-op and
    returns the original incoming unchanged.

    Used by the ``respond_in_groups`` routing gate in
    ``_on_text_from_transport``; captions ride on ``incoming.text`` per
    ``TelegramTransport._dispatch_message`` so this helper also cleans
    captioned files / voice / photo messages.
    """
    if not self.bot_username:
        return incoming
    import dataclasses
    import re
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_@])@{re.escape(self.bot_username)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    cleaned = pattern.sub("", incoming.text)
    if cleaned == incoming.text:
        return incoming
    return dataclasses.replace(incoming, text=cleaned)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_bot_strip_self_mention.py -v
```
Expected: all 7 tests PASS.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 16 new tests (5 + 4 + 7) pass.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_strip_self_mention.py
git commit -m "$(cat <<'EOF'
feat(bot): _strip_self_mention helper for respond_in_groups routing

Pure helper that removes case-insensitive @<bot_username> from
IncomingMessage.text via a word-bounded regex. Used by the upcoming
respond_in_groups routing gate (Task 4) to clean the prompt before
the agent sees it.

The regex uses negative lookbehind on [A-Za-z0-9_@] so:
- @MyBotIsCool is NOT stripped (longer handle).
- user@MyBot.example is NOT stripped (email-like).
- "@MyBot do X" → " do X" (stripped, leading space remains).
- "@MyBot and @SomeoneElse" → " and @SomeoneElse" (other mention intact).

Returns a new IncomingMessage via dataclasses.replace; the input is
frozen. No-op when bot_username == "" (before _after_ready fires).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add the routing gate in `_on_text_from_transport`

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (new `elif` branch in `_on_text_from_transport`; new instance field `_respond_in_groups`)
- Create: `tests/test_bot_respond_in_groups.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot_respond_in_groups.py`:

```python
"""Solo project bot in Telegram group — routing gate tests.

Verifies the `respond_in_groups=True` elif branch in
ProjectBot._on_text_from_transport:
  - mention → process
  - reply-to-bot → process
  - drive-by → silent
  - self → silent
  - peer bot → silent
  - mention-strip happens before _on_text
  - DMs still work (filter is PRIVATE | GROUPS, not GROUPS only)
  - captioned file with @mention → process
  - DM behavior unchanged when flag is False
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot(*, respond_in_groups: bool, bot_username: str = "MyBot"):
    """Build a minimal ProjectBot suitable for routing-gate tests.

    Bypasses __init__ via __new__ and sets only the fields the gate touches.
    Stubs _on_text so tests can assert called-with cleanly.
    """
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = bot_username
    bot._respond_in_groups = respond_in_groups
    bot.group_mode = False
    bot.team_name = None
    bot.role = None
    bot._allowed_users = [AllowedUser(username="alice", role="executor",
                                       locked_identities=["fake:42"])]
    bot._auth_dirty = False
    bot._on_text = AsyncMock()
    bot._transport = FakeTransport()
    return bot


def _make_group_incoming(
    text: str,
    *,
    sender_handle: str = "alice",
    sender_id: str = "42",
    sender_is_bot: bool = False,
    mentions: list[Identity] | None = None,
    reply_to_sender: Identity | None = None,
    files: list[IncomingFile] | None = None,
) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="100", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id=sender_id,
        display_name=sender_handle, handle=sender_handle, is_bot=sender_is_bot,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=files or [],
        reply_to=None, message=msg,
        reply_to_sender=reply_to_sender,
        mentions=mentions or [],
    )


def _make_dm_incoming(text: str) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
    )


def _bot_mention(handle: str = "MyBot") -> Identity:
    return Identity(
        transport_id="fake", native_id="bot-self",
        display_name=handle, handle=handle, is_bot=True,
    )


@pytest.mark.asyncio
async def test_group_mention_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # Mention stripped before reaching _on_text.
    assert forwarded.text == " do X"


@pytest.mark.asyncio
async def test_group_reply_to_bot_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "follow-up question",
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # No mention to strip; text unchanged.
    assert forwarded.text == "follow-up question"


@pytest.mark.asyncio
async def test_group_drive_by_message_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming("chatter between humans")
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_self_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "any text",
        sender_handle="MyBot",  # same as bot_username
        sender_is_bot=True,
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_peer_bot_with_mention_is_silent():
    """Peer-bot defense: solo bot in group must NEVER respond to another bot,
    even when @mentioned. Avoids bot-to-bot loops; team workflows are
    explicitly opt-in via team mode."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot ping",
        sender_handle="OtherBot",
        sender_is_bot=True,
        mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_captioned_file_with_mention_reaches_on_text():
    """Captioned files: caption rides on incoming.text per
    TelegramTransport._dispatch_message. Same @mention gate applies."""
    bot = _make_bot(respond_in_groups=True)
    files = [IncomingFile(
        path=Path("/tmp/x.png"),
        original_name="x.png",
        mime_type="image/png",
        size_bytes=1024,
    )]
    incoming = _make_group_incoming(
        "@MyBot analyze this",
        mentions=[_bot_mention("MyBot")],
        files=files,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.files == files
    assert forwarded.text == " analyze this"


@pytest.mark.asyncio
async def test_group_text_when_flag_is_false_is_silent_via_filter():
    """With respond_in_groups=False, group messages don't reach _on_text
    even if they would have been addressed-at-me. (In production this is
    enforced by the PTB filter — here we verify the bot-side gate also
    refuses to process them defensively.)"""
    bot = _make_bot(respond_in_groups=False)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_message_unaffected_by_flag():
    """DM messages reach _on_text regardless of respond_in_groups setting."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_dm_incoming("just a DM")
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # DM text isn't mention-stripped (no @mention typically).
    assert forwarded.text == "just a DM"


@pytest.mark.asyncio
async def test_group_reply_to_bot_with_other_mention_is_silent():
    """Reply to bot + simultaneously @-mentions someone else → silent.
    Matches team-mode semantics in is_directed_at_me."""
    bot = _make_bot(respond_in_groups=True)
    other = Identity(
        transport_id="fake", native_id="999",
        display_name="OtherUser", handle="OtherUser", is_bot=False,
    )
    incoming = _make_group_incoming(
        "@OtherUser the bot said X",
        mentions=[other],  # mentions OtherUser, NOT MyBot
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_empty_text_after_strip_falls_through_silently():
    """User types just '@MyBot' with nothing else. After stripping the bot
    is responsible for whatever _on_text does with empty text — typically
    early-returns. Verify _on_text receives the empty-text incoming and
    handles it (early-return semantics are owned by _on_text itself)."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    # _on_text IS called — it's _on_text's responsibility to early-return
    # on empty text. The gate's job is only to filter not-addressed-at-me.
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.text == ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_bot_respond_in_groups.py -v
```
Expected: 10 tests FAIL — `_on_text_from_transport` doesn't recognize the new flag; messages from groups either reach `_on_text` unconditionally (text + mention case) or AttributeError on `bot._respond_in_groups` (not yet initialized in `__init__`).

- [ ] **Step 3: Add the `_respond_in_groups` field to `ProjectBot.__init__`**

Find `ProjectBot.__init__` in [`src/link_project_to_chat/bot.py`](src/link_project_to_chat/bot.py). The signature already has kwargs like `plugins`, `allowed_users`, `auth_source`. Add another:

```python
respond_in_groups: bool = False,
```

In the body, alongside `self._plugins = …` and `self._allowed_users = …` (around line 280-290), add:

```python
self._respond_in_groups: bool = bool(respond_in_groups)
```

- [ ] **Step 4: Add the routing gate in `_on_text_from_transport`**

Find `_on_text_from_transport` in `bot.py` (around line 928). Locate the existing team-mode branch:

```python
if self.group_mode:
    handled = await self._handle_group_text(incoming)
    if handled:
        return
    # Bot-to-bot path bypasses auth; submit directly.
    if incoming.is_relayed_bot_to_bot or incoming.sender.is_bot:
        await self._submit_group_message_to_claude(incoming)
        return
    # Human message in group — fall through to full auth/rate-limit/
    # pending-skill/pending-persona flow below.
```

Insert a sibling `elif` branch immediately after this block, before `await self._on_text(incoming)`:

```python
elif (
    self._respond_in_groups
    and incoming.chat.kind == ChatKind.ROOM
):
    # Solo project bot in a Telegram group. Restore the GitLab-fork
    # behavior: respond ONLY when explicitly addressed by @mention or
    # reply-to-bot. Drive-by messages, self-echoes, and peer bots are
    # silently dropped — no auth check, no plugin dispatch, no reply.
    from .group_filters import is_from_self, is_directed_at_me
    if is_from_self(incoming, self.bot_username):
        return
    if incoming.sender.is_bot:
        return  # peer-bot defense: solo mode never accepts other bots
    if not is_directed_at_me(incoming, self.bot_username):
        return
    incoming = self._strip_self_mention(incoming)
```

Make sure `ChatKind` is imported at the top of the file (it should already be, since `_handle_group_text` uses `ChatRef` and related primitives). Verify with:

```bash
grep -n "from .transport.base import" src/link_project_to_chat/bot.py | head -3
```

If `ChatKind` isn't in the import list, add it.

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_bot_respond_in_groups.py -v
```
Expected: all 10 tests PASS.

- [ ] **Step 6: Run the full suite for regressions**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 26 new tests (5 + 4 + 7 + 10) pass. The previously-existing 1143 baseline must be preserved — if any pre-existing test breaks, it means the `elif` branch is intercepting cases it shouldn't (e.g., team-mode messages). Inspect the failures and adjust.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_respond_in_groups.py
git commit -m "$(cat <<'EOF'
feat(bot): respond_in_groups routing gate in _on_text_from_transport

Adds a sibling elif branch to the existing team-mode (group_mode)
path: when self._respond_in_groups is True and incoming.chat.kind is
ROOM, runs is_from_self / is_directed_at_me / peer-bot defense
before falling through to _on_text.

The gate:
  - silently drops self-echoes and peer-bot messages,
  - silently drops drive-by messages (no auth check runs),
  - strips @<bot_username> from incoming.text via _strip_self_mention
    before passing to _on_text.

ProjectBot.__init__ gains respond_in_groups: bool = False kwarg
storing into self._respond_in_groups. Default off — pre-v1.1.0
behavior preserved.

No changes to team-mode path or _handle_group_text. The elif branch
is mutually exclusive with the if-team-mode branch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Plumb `respond_in_groups` through `run_bot` and `run_bots`

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (`run_bot` + `run_bots` signatures and call sites)
- Create: `tests/test_run_bot_respond_in_groups.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_bot_respond_in_groups.py`:

```python
"""Verify run_bot / run_bots propagate respond_in_groups into ProjectBot
AND into TelegramTransport.attach_telegram_routing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from link_project_to_chat.bot import ProjectBot, run_bot


def test_run_bot_passes_respond_in_groups_to_project_bot(tmp_path: Path):
    """run_bot(..., respond_in_groups=True) constructs a ProjectBot
    with self._respond_in_groups=True.

    run_bot's signature uses positional `name, path, token` (NOT
    `project_path` — see src/link_project_to_chat/bot.py:3289). We need
    a non-empty `username` OR a non-empty `allowed_users` to pass the
    fail-closed check inside run_bot.
    """
    from link_project_to_chat.config import AllowedUser
    captured: dict = {}

    def _fake_build(self):
        captured["respond_in_groups"] = self._respond_in_groups

        class _App:
            def run_polling(_self):
                return None
        return _App()

    with patch.object(ProjectBot, "build", _fake_build):
        run_bot(
            "p", tmp_path, "t",
            allowed_users=[AllowedUser(username="alice", role="executor")],
            auth_source="project",
            respond_in_groups=True,
        )
    assert captured["respond_in_groups"] is True


def test_run_bot_defaults_to_false(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    captured: dict = {}

    def _fake_build(self):
        captured["respond_in_groups"] = self._respond_in_groups

        class _App:
            def run_polling(_self):
                return None
        return _App()

    with patch.object(ProjectBot, "build", _fake_build):
        run_bot(
            "p", tmp_path, "t",
            allowed_users=[AllowedUser(username="alice", role="executor")],
            auth_source="project",
        )
    assert captured["respond_in_groups"] is False


def test_run_bots_pulls_respond_in_groups_from_project_config(tmp_path: Path):
    """run_bots iterates Config.projects and constructs a ProjectBot per
    entry; the per-project respond_in_groups field must flow through."""
    from link_project_to_chat.bot import run_bots
    from link_project_to_chat.config import (
        AllowedUser,
        Config,
        ProjectConfig,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    cfg.projects["p1"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t1",
        respond_in_groups=True,
    )
    cfg.projects["p2"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t2",
        respond_in_groups=False,
    )
    save_config(cfg, cfg_path)

    captured: list[dict] = []

    def _record_run_bot(**kwargs):
        captured.append({
            "name": kwargs.get("name"),
            "respond_in_groups": kwargs.get("respond_in_groups", False),
        })

    with patch("link_project_to_chat.bot.run_bot", _record_run_bot):
        run_bots(cfg, config_path=cfg_path)

    by_name = {c["name"]: c for c in captured}
    assert by_name["p1"]["respond_in_groups"] is True
    assert by_name["p2"]["respond_in_groups"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_run_bot_respond_in_groups.py -v
```
Expected: 3 tests FAIL — `run_bot` doesn't accept `respond_in_groups` kwarg yet (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Add `respond_in_groups` to `run_bot` signature**

In [`src/link_project_to_chat/bot.py`](src/link_project_to_chat/bot.py), find `def run_bot(`. Add `respond_in_groups: bool = False` to the signature alongside other forwarded kwargs (e.g., `plugins`, `allowed_users`, `auth_source`).

In the body, find the `bot = ProjectBot(...)` call and add the new kwarg:

```python
bot = ProjectBot(
    ...,
    plugins=plugins,
    allowed_users=allowed_users,
    auth_source=auth_source,
    respond_in_groups=respond_in_groups,
)
```

Find the `self._transport.attach_telegram_routing(...)` call site in `build()` (around line 3207). Add the new kwarg:

```python
self._transport.attach_telegram_routing(
    group_mode=self.group_mode,
    command_names=[n for n, _ in ported_commands],
    respond_in_groups=self._respond_in_groups,
)
```

- [ ] **Step 4: Add `respond_in_groups` to `run_bots`**

Find `def run_bots(` in `bot.py`. Locate the loop over `config.projects.items()` and the inner `run_bot(...)` call. Add the field from the project entry:

```python
for name, proj in config.projects.items():
    effective_allowed, auth_source = resolve_project_allowed_users(proj, config)
    run_bot(
        name=name,
        project_path=Path(proj.path),
        token=proj.telegram_bot_token,
        allowed_users=effective_allowed,
        auth_source=auth_source,
        plugins=proj.plugins or None,
        respond_in_groups=proj.respond_in_groups,
        # ... existing kwargs
    )
```

(Adjust to match the current `run_bots` body — it has more kwargs; only the new line needs adding.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_run_bot_respond_in_groups.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 29 new tests (5 + 4 + 7 + 10 + 3) pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_run_bot_respond_in_groups.py
git commit -m "$(cat <<'EOF'
feat(bot): plumb respond_in_groups through run_bot / run_bots

run_bot signature gains respond_in_groups: bool = False kwarg.
ProjectBot construction and TelegramTransport.attach_telegram_routing
both receive the flag.

run_bots reads ProjectConfig.respond_in_groups from each project
entry and forwards into the per-project run_bot call.

Mirrors how plugins / allowed_users / auth_source flow today.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: CLI surfaces

**Files:**
- Modify: `src/link_project_to_chat/cli.py` (`projects add --respond-in-groups`, `projects edit NAME respond_in_groups true|false`)
- Modify: `tests/test_cli.py` (append cases)

- [ ] **Step 1: Write the failing tests**

Append to [`tests/test_cli.py`](tests/test_cli.py):

```python
def test_projects_add_with_respond_in_groups_writes_field(tmp_path):
    """`projects add --respond-in-groups` writes respond_in_groups=True
    into the project entry on disk."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    runner = CliRunner()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "add",
        "--name", "myproj",
        "--path", str(proj_dir),
        "--token", "t",
        "--respond-in-groups",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert proj["respond_in_groups"] is True


def test_projects_add_without_flag_omits_field(tmp_path):
    """Default off: the field is not written when the flag is absent."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    runner = CliRunner()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "add",
        "--name", "myproj",
        "--path", str(proj_dir),
        "--token", "t",
    ])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert "respond_in_groups" not in proj


def test_projects_edit_respond_in_groups_true(tmp_path):
    """`projects edit myproj respond_in_groups true` flips the flag on."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "true",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    assert on_disk["projects"]["myproj"]["respond_in_groups"] is True


def test_projects_edit_respond_in_groups_false_strips_field(tmp_path):
    """`projects edit myproj respond_in_groups false` flips the flag off,
    and the on-disk emit-only-when-True policy strips the key."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "false",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert "respond_in_groups" not in proj


def test_projects_edit_respond_in_groups_invalid_input_errors(tmp_path):
    """Garbage values produce a non-zero exit and don't mutate the file."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "maybe",
    ])
    assert result.exit_code != 0
    # File unchanged.
    on_disk = json.loads(cfg.read_text())
    assert "respond_in_groups" not in on_disk["projects"]["myproj"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_cli.py -k respond_in_groups -v
```
Expected: 5 tests FAIL — `--respond-in-groups` flag is unknown to Click; `respond_in_groups` field isn't in the editable list.

- [ ] **Step 3: Add `--respond-in-groups` to `projects add`**

In [`src/link_project_to_chat/cli.py`](src/link_project_to_chat/cli.py), find the `projects_add` command (around line 85-138). Add a Click flag below the existing options:

```python
@click.option(
    "--respond-in-groups/--no-respond-in-groups",
    "respond_in_groups",
    default=False,
    help="Respond in Telegram groups when @mentioned or replied to (default off)",
)
```

Add to the function signature:

```python
def projects_add(
    ctx,
    name: str,
    project_path: str,
    token: str,
    username: str | None,
    model: str | None,
    permission_mode: str | None,
    skip_permissions: bool,
    respond_in_groups: bool,
):
```

In the body, after the existing entry construction, add:

```python
if respond_in_groups:
    entry["respond_in_groups"] = True
```

(Don't emit when False — matches the save_config policy.)

- [ ] **Step 4: Add `respond_in_groups` to `projects edit`**

In the same file, find `projects_edit` (around line 157-228). Locate `_EDITABLE` and add `"respond_in_groups"` to the tuple. Add a new branch in the field-handling chain:

```python
elif field == "respond_in_groups":
    truthy = {"true", "1", "yes", "on"}
    falsy = {"false", "0", "no", "off"}
    lowered = value.strip().lower()
    if lowered in truthy:
        projects[name]["respond_in_groups"] = True
    elif lowered in falsy:
        projects[name].pop("respond_in_groups", None)
    else:
        raise SystemExit(
            f"Invalid bool for respond_in_groups: {value!r}. "
            f"Use one of: true, false, 1, 0, yes, no, on, off."
        )
    save_project_configs(projects, cfg_path)
    click.echo(f"Updated '{name}' respond_in_groups to {value}.")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_cli.py -k respond_in_groups -v
```
Expected: all 5 tests PASS.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 34 new tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): --respond-in-groups flag + editable field

projects add gains --respond-in-groups / --no-respond-in-groups
(default off). Only emits to disk when True (matches save_config
policy).

projects edit NAME respond_in_groups VALUE accepts:
- true / 1 / yes / on  → True
- false / 0 / no / off → False (strips key from on-disk entry)
- anything else        → SystemExit with usage hint

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Manager bot edit field

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py` (`_EDITABLE_FIELDS`, `_apply_edit` bool-parsing branch)
- Modify: `tests/manager/test_bot_commands.py` (append cases)

- [ ] **Step 1: Write the failing tests**

Append to [`tests/manager/test_bot_commands.py`](tests/manager/test_bot_commands.py):

```python
@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_true(bot_env, tmp_path: Path):
    """Manager wizard: setting respond_in_groups to a truthy string flips
    the per-project flag on, persists to disk."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "true")
    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["myproj"]["respond_in_groups"] is True
    text = fake.sent_messages[-1].text.lower()
    assert "respond_in_groups" in text or "updated" in text


@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_false_strips_key(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    }))
    _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "false")
    raw = json.loads(proj_cfg.read_text())
    assert "respond_in_groups" not in raw["projects"]["myproj"]


def test_editable_fields_include_respond_in_groups():
    """Manager has TWO related tuples (verified at manager/bot.py:53-54):
    - _EDITABLE_FIELDS: consumed by /edit_project's help text + unknown-field
      error path, plus the project-edit text wizard.
    - _BUTTON_EDIT_FIELDS: consumed by the project-detail keyboard generator
      that auto-creates the per-field edit button.

    Both must include the new field so respond_in_groups is reachable from
    BOTH the CommandHandler (/edit_project NAME respond_in_groups VALUE) AND
    the inline keyboard.
    """
    from link_project_to_chat.manager.bot import (  # type: ignore[attr-defined]
        _BUTTON_EDIT_FIELDS,
        _EDITABLE_FIELDS,
    )
    assert "respond_in_groups" in _EDITABLE_FIELDS
    assert "respond_in_groups" in _BUTTON_EDIT_FIELDS


@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_invalid_value_replies_error(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "maybe")
    raw = json.loads(proj_cfg.read_text())
    # File unchanged.
    assert "respond_in_groups" not in raw["projects"]["myproj"]
    text = fake.sent_messages[-1].text.lower()
    assert "invalid" in text or "true" in text  # error mentions accepted values
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/manager/test_bot_commands.py -k respond_in_groups -v
```
Expected: 4 tests FAIL.

- [ ] **Step 3: Add `respond_in_groups` to BOTH editable-fields constants**

In [`src/link_project_to_chat/manager/bot.py`](src/link_project_to_chat/manager/bot.py) (around line 53-54), find:

```python
_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions")
_BUTTON_EDIT_FIELDS = ("name", "path", "token", "username", "model", "permissions")
```

Add `"respond_in_groups"` to BOTH tuples:

```python
_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions", "respond_in_groups")
_BUTTON_EDIT_FIELDS = ("name", "path", "token", "username", "model", "permissions", "respond_in_groups")
```

- `_EDITABLE_FIELDS` is consumed by `/edit_project`'s help text and unknown-field error path.
- `_BUTTON_EDIT_FIELDS` drives the project-detail keyboard's auto-generated per-field edit buttons.

Both must include the field so it's reachable from both the CommandHandler and the inline keyboard.

- [ ] **Step 4: Add the parsing branch in `_apply_edit`**

Find `_apply_edit` in `manager/bot.py` (around line 990). Add a new `elif` branch alongside the existing field handlers:

```python
elif field == "respond_in_groups":
    truthy = {"true", "1", "yes", "on"}
    falsy = {"false", "0", "no", "off"}
    lowered = value.strip().lower()
    if lowered in truthy:
        projects[name]["respond_in_groups"] = True
        self._save_projects(projects)
        await self._transport.send_text(
            chat, f"Updated '{name}' respond_in_groups to True.",
        )
    elif lowered in falsy:
        projects[name].pop("respond_in_groups", None)
        self._save_projects(projects)
        await self._transport.send_text(
            chat, f"Updated '{name}' respond_in_groups to False.",
        )
    else:
        await self._transport.send_text(
            chat,
            f"Invalid bool for respond_in_groups: {value!r}. "
            f"Use one of: true, false, 1, 0, yes, no, on, off.",
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/manager/test_bot_commands.py -k respond_in_groups -v
```
Expected: all 4 tests PASS.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 38 new tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/manager/test_bot_commands.py
git commit -m "$(cat <<'EOF'
feat(manager): expose respond_in_groups in the project-edit keyboard

Adds "respond_in_groups" to _EDITABLE_FIELDS so the auto-generated
project-edit keyboard surfaces it. _apply_edit gains a branch that
parses bool with the same accepted-values list as the CLI
(true/1/yes/on, false/0/no/off; anything else replies an error).

Operators with manager-bot access can flip the flag from the
project-detail screen without touching config.json. Restart the
project bot for the change to take effect.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: End-to-end test + README docs

**Files:**
- Create: `tests/test_respond_in_groups_e2e.py`
- Modify: `README.md` (new subsection under role-based access)
- Modify: `docs/CHANGELOG.md` (v1.1.0 entry)

- [ ] **Step 1: Write the failing E2E test**

Create `tests/test_respond_in_groups_e2e.py`:

```python
"""End-to-end: respond_in_groups=True wires through run_bot → ProjectBot
→ FakeTransport → routing gate → _on_text → backend submit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_solo_bot_in_group_e2e_addressed_message_submits_to_backend(tmp_path: Path):
    """Full pipeline: a ProjectBot constructed with respond_in_groups=True,
    receiving an @-mentioned group message from an authorized executor,
    submits the (mention-stripped) prompt to the agent. Drive-by messages
    in the same group don't reach the backend at all.
    """
    bot = ProjectBot(
        "p", tmp_path, "t",
        backend_name="claude",
        backend_state={},
        allowed_users=[AllowedUser(username="alice", role="executor",
                                    locked_identities=["fake:42"])],
        auth_source="project",
        respond_in_groups=True,
    )
    # Force bot_username (normally set in _after_ready by the transport).
    bot.bot_username = "MyBot"
    fake = FakeTransport()
    bot._transport = fake
    # Stub backend submission so we don't spawn a real agent.
    # submit_agent is sync — use MagicMock, NOT AsyncMock.
    bot.task_manager.submit_agent = MagicMock()

    bot_identity = Identity(
        transport_id="fake", native_id="bot-self",
        display_name="MyBot", handle="MyBot", is_bot=True,
    )

    # Addressed message → submit.
    chat = ChatRef(transport_id="fake", native_id="100", kind=ChatKind.ROOM)
    alice = Identity(
        transport_id="fake", native_id="42",
        display_name="alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    addressed = IncomingMessage(
        chat=chat, sender=alice, text="@MyBot what's the status?",
        files=[], reply_to=None, message=msg,
        mentions=[bot_identity],
    )
    await bot._on_text_from_transport(addressed)
    assert bot.task_manager.submit_agent.call_count == 1
    submit_kwargs = bot.task_manager.submit_agent.call_args.kwargs
    # The mention was stripped from the prompt.
    assert "@MyBot" not in submit_kwargs.get("prompt", "")
    assert "what's the status" in submit_kwargs.get("prompt", "")

    # Drive-by message → silent (no second submit).
    bot.task_manager.submit_agent.reset_mock()
    drive_by_msg = MessageRef(transport_id="fake", native_id="201", chat=chat)
    drive_by = IncomingMessage(
        chat=chat, sender=alice, text="random chatter",
        files=[], reply_to=None, message=drive_by_msg,
    )
    await bot._on_text_from_transport(drive_by)
    bot.task_manager.submit_agent.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails OR passes**

```bash
.venv/bin/pytest tests/test_respond_in_groups_e2e.py -v
```

Expected behavior at this point depends on whether Tasks 1-7 are all in. If Tasks 1-7 are complete, this test should already PASS (the e2e wires through the existing surfaces). If it FAILS, that means an integration gap — inspect and adjust.

Note: depending on the actual `ProjectBot.__init__` signature, some kwargs in the test (e.g., `backend_name`, `backend_state`) may need adjustment. Read the current `__init__` body to confirm the right kwargs to pass. If `backend_name`/`backend_state` aren't required, omit them.

- [ ] **Step 3: Adjust if needed, then run**

If the test failed due to a kwarg mismatch, fix the test (not the source) — the source kwargs are stable from prior tasks. Re-run until PASS.

- [ ] **Step 4: Add README section**

In [`README.md`](README.md), find the "Multi-user support" or role-based access section (search for `allowed_users`). Add a new subsection after it:

````markdown
### Group support (Telegram)

By default a project bot only responds in private DMs. To let a project
bot respond in Telegram groups when explicitly addressed:

```bash
link-project-to-chat projects edit myproj respond_in_groups true
```

or, in the manager bot, open the project's detail keyboard and tap
"Respond in groups."

When enabled:

- The bot responds when `@<bot_username>` appears in a group message
  OR the message replies to one of the bot's prior messages.
- All other group messages are silently ignored — no auth check runs,
  nothing is logged.
- The `@<bot_username>` mention is stripped from the prompt before the
  agent sees it.
- `/commands` work in groups with the same role gate as DM. Telegram
  routes `/cmd@MyBot` only to the addressed bot in multi-bot rooms.
- The bot ignores all other bots' messages, including `@<bot_username>`
  from a peer bot (loop defense).

Restart the project bot for the flag change to take effect (the
`python-telegram-bot` filter is set once at startup).
````

- [ ] **Step 5: Add CHANGELOG entry**

In [`docs/CHANGELOG.md`](docs/CHANGELOG.md), prepend a new v1.1.0 section (or append under an Unreleased heading if that convention is used; check the existing top):

```markdown
## 1.1.0 — 2026-MM-DD

### Added
- **`ProjectConfig.respond_in_groups`** (default False). When True, a
  standard project bot responds in Telegram groups to `@bot_username`
  mentions and replies to its own prior messages; ignores everything
  else. CLI surface: `projects add --respond-in-groups`,
  `projects edit NAME respond_in_groups true|false`. Manager bot
  exposes the field via the project-edit keyboard. The PTB filter is
  set once at startup, so flipping the flag requires a bot restart.
  Restores the GitLab fork's solo-bot-in-group behavior that was
  scoped out during the transport-abstraction track.
- **`TelegramTransport.attach_telegram_routing(..., respond_in_groups=False)`**
  kwarg. 3-way chat-type filter: team-mode (GROUPS only),
  solo+respond_in_groups (PRIVATE | GROUPS), default (PRIVATE only).
- **`ProjectBot._strip_self_mention`** helper. Pure function;
  word-bounded case-insensitive `@bot_username` removal via regex.

### Notes
- Default off — pre-v1.1.0 deployments behave identically until the
  flag is flipped.
- Peer-bot loop defense: solo project bots in groups ignore all other
  bots' messages, including `@<bot_username>` from a peer bot.
- Plugins still only see addressed-at-me group messages (consistent
  with the existing "plugins see authorized + rate-limit-passing
  messages" pattern). Differs from the GitLab fork; future opt-in
  `Plugin.observe_unfiltered_group_messages` could change this.
```

Use today's date as `YYYY-MM-DD`.

- [ ] **Step 6: Run the full suite one last time**

```bash
.venv/bin/pytest -q
```
Expected: baseline + 39 new tests pass (38 from Tasks 1-7 + 1 from this task's E2E). No regressions.

- [ ] **Step 7: Commit**

```bash
git add tests/test_respond_in_groups_e2e.py README.md docs/CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(release): respond_in_groups README + CHANGELOG v1.1.0

README gains a "Group support (Telegram)" subsection under the
multi-user / role-based access section documenting:
  - How to enable (CLI + manager UI).
  - What it does (@mention / reply-to-bot only; silent otherwise;
    /commands work with role gate; peer bots ignored).
  - Restart requirement.

CHANGELOG entry for v1.1.0 calling out the feature, the new CLI/
manager surfaces, the transport kwarg, and the helper.

Plus an end-to-end test verifying run_bot(..., respond_in_groups=True)
wires through to task_manager.submit_agent with the prompt
mention-stripped on the addressed-message path AND silently drops
drive-by messages.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final verification

**Files:**
- N/A (verification only)

- [ ] **Step 1: Run the full suite from a clean install**

```bash
.venv/bin/pip install -e ".[all]"
.venv/bin/pytest -q
```
Expected: baseline + 39 new tests pass. No regressions.

- [ ] **Step 2: Inspect the branch diff**

```bash
git log dev..HEAD --oneline
git diff dev..HEAD --stat
```

Expected:
- 9 commits (Task 0 baseline-pin + Tasks 1-8).
- ~60 lines net new in source (config.py, bot.py, transport/telegram.py, cli.py, manager/bot.py).
- ~400 lines new in tests (8 new/extended test files).
- ~50 lines of doc changes (README + CHANGELOG).

- [ ] **Step 3: Manual smoke test (optional — by hand, not pytest)**

Recommended pre-merge smoke test against a real bot:

1. Pick a Telegram bot you control. Configure it as a project locally:
   ```bash
   link-project-to-chat projects add --name smoke --path . --token <BOT_TOKEN>
   link-project-to-chat configure --add-user <your-telegram-handle>:executor
   link-project-to-chat projects edit smoke respond_in_groups true
   link-project-to-chat start --project smoke
   ```
2. Add the bot to a Telegram group with at least one other member.
3. Send `@<bot_handle> what time is it?` — expect a normal Claude response, threaded under your message.
4. Send a plain message in the group (no `@`-mention) — expect silence.
5. Reply to the bot's prior response without `@`-mentioning — expect a normal Claude response.
6. Have an unauthorized account in the group `@<bot_handle> do X` — expect a public `"Unauthorized."` reply.
7. Run `/tasks@<bot_handle>` in the group — expect the task listing.
8. Toggle the flag back off:
   ```bash
   link-project-to-chat projects edit smoke respond_in_groups false
   ```
   Restart the bot. Repeat step 3 — expect the bot to no longer see the message (silent).

- [ ] **Step 4: Push the branch and open a PR**

```bash
git push -u origin feat/respond-in-groups
gh pr create --title "Solo bot in Telegram groups via respond_in_groups (v1.1.0)" --body "$(cat <<'EOF'
## Summary
- Restores the GitLab fork's standard-bot-in-group behavior on Telegram: when `ProjectConfig.respond_in_groups=True`, a project bot responds in groups to `@<bot_username>` mentions and replies to its own prior messages; ignores everything else.
- Default off — zero behavior change for existing deployments.
- Surgical: one new config field, a 3-way switch on the PTB chat-type filter, and a small `elif` branch in `ProjectBot._on_text_from_transport`. Team-mode path untouched.

Design doc: `docs/superpowers/specs/2026-05-16-respond-in-groups-design.md`
Implementation plan: `docs/superpowers/plans/2026-05-16-respond-in-groups.md`

## What's exposed
- CLI: `projects add --respond-in-groups`, `projects edit NAME respond_in_groups true|false`.
- Manager bot: field appears in the project-detail edit keyboard.
- Operator-facing docs: README "Group support (Telegram)" subsection, CHANGELOG v1.1.0 entry.

## Test plan
- [x] `pytest -q` green on every commit (baseline + 39 new tests).
- [ ] **Manual smoke**: addressed-at-me responses, drive-by silence, unauthorized public reply, `/tasks@MyBot`, flag toggle off (see plan Task 9 Step 3 for full script).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Verification gates

After every task:

1. `pytest -q` green. Baseline is whatever Task 0 recorded after `pip install -e ".[all]" && pytest -q`; tasks add new tests so the count grows monotonically (Task 1: +5, Task 2: +4, Task 3: +7, Task 4: +10, Task 5: +3, Task 6: +5, Task 7: +4, Task 8: +1, total +39).
2. New tests for the task pass.
3. No source files deleted. No telegram imports added to `bot.py` or `manager/bot.py` beyond the existing allowlists. (`tests/test_transport_lockout.py` and `tests/test_manager_lockout.py` enforce this.)
4. Each commit follows the heredoc-message format with the `Co-Authored-By` trailer.

---

## Out of scope (for v1.1.0)

- Web / Discord / Slack ports of `respond_in_groups`. The bot-side gate reads `chat.kind == ChatKind.ROOM` so it's transport-portable, but no non-Telegram transport currently produces `ROOM` chats. Future spec.
- Allow-listing specific group IDs (e.g. "respond only in supergroup X"). Future feature.
- Plugin opt-in for observing non-addressed group chatter (`Plugin.observe_unfiltered_group_messages: bool = False`). Future feature.
- Per-group rate-limit budgets distinct from per-user limits. The existing identity-keyed rate-limit (Task 5 of v1.0.0) already handles this correctly.
