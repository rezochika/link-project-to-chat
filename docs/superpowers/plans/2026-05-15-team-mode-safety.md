# Team-Mode Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative team-mode safety layer so bot-to-bot work cannot push, publish, create PRs, or run unbounded relay loops without fresh authenticated-user involvement.

**Architecture:** Keep the current `ProjectBot` prompt/session behavior intact. Add a small `team_safety.py` grant store, wire one `TeamAuthority` into each team bot/backend, and enforce autonomy limits inside `TeamRelay`, which is already the single Telegram bot-to-bot choke point. Claude gets scoped `--disallowedTools` blocks; Codex stays sandboxed in team mode because its CLI has no scoped tool allowlist.

**Tech Stack:** Python 3.11+, pytest with `asyncio_mode=auto`, existing backend and transport modules.

---

## Corrections From Plan Review

This file replaces the earlier draft of the same plan. The old version had five execution risks:

- It described relay counters but did not wire them into the real `_finalize_relay` path.
- It observed Telethon user messages in a way that could accidentally parse the relay account's own echoes.
- It let scoped Codex grants re-enable full sandbox bypass for a whole turn, which is broader than the grant.
- It moved prompt composition into backends even though the repo already skips team persona/history on resumed sessions and clears session ids on persona changes.
- It referenced stale test paths such as `tests/test_backend_claude.py`, `tests/test_backend_codex.py`, and `tests/test_bot_status.py`.

This corrected plan fixes those issues. Do not reintroduce Tasks 12-14 from the older draft unless a new failing test proves a real prompt-reinjection bug.

## File Map

| File | Action |
|---|---|
| `src/link_project_to_chat/team_safety.py` | Create authority grant data model and parser. |
| `src/link_project_to_chat/config.py` | Add `TeamConfig.max_autonomous_turns` and `TeamConfig.safety_mode`; preserve through load/save. |
| `src/link_project_to_chat/transport/_telegram_relay.py` | Observe authenticated user messages safely, enforce turn budget, suppress duplicate forwards, reset streak on peer response. |
| `src/link_project_to_chat/transport/telegram.py` | Pass relay safety options through Telegram relay constructors; expose relay counters for status. |
| `src/link_project_to_chat/backends/base.py` | Add `team_authority` to the backend Protocol. |
| `src/link_project_to_chat/backends/claude.py` | Add team-mode disallowed tool blocks and avoid dangerous skip permissions in team mode. |
| `src/link_project_to_chat/backends/codex.py` | Downgrade dangerous permissions to `--full-auto` in team mode; allow full bypass only for one consumed `--auth all` grant. |
| `src/link_project_to_chat/bot.py` | Create/wire `TeamAuthority`; pass safety options into relay; render `/status` team-safety block. |
| `src/link_project_to_chat/personas/software_manager.md` | Require explicit review-surface naming. |
| `src/link_project_to_chat/personas/software_dev.md` | Require handoff surface state: unstaged/staged/committed/pushed. |
| `tests/test_team_safety.py` | New unit tests for grants and directive parsing. |
| `tests/test_config.py` | TeamConfig default/load/save tests. |
| `tests/test_team_relay.py` | Real relay-path tests for dedupe, budget, peer-response reset, and user observation. |
| `tests/backends/test_claude_backend.py` | Claude team safety tests. |
| `tests/backends/test_codex_backend.py` | Codex team safety tests. |
| `tests/test_bot_team_wiring.py` | TeamAuthority and relay wiring tests. |
| `tests/test_backend_command.py` | `/status` team-safety output tests. |
| `tests/test_bundled_personas.py` | Persona invariant tests. |

## Task 1: Authority Grant Data Layer

**Files:**
- Create: `src/link_project_to_chat/team_safety.py`
- Create: `tests/test_team_safety.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_team_safety.py`:

```python
from link_project_to_chat.team_safety import (
    AuthorityGrant,
    TeamAuthority,
    VALID_SCOPES,
    parse_auth_directives,
)


def test_parse_auth_directives_empty_and_unknown():
    assert parse_auth_directives("") == frozenset()
    assert parse_auth_directives(None) == frozenset()
    assert parse_auth_directives("--auth pushh") == frozenset()
    assert parse_auth_directives("--authentication push") == frozenset()
    assert parse_auth_directives("--auth-mode push") == frozenset()


def test_parse_auth_directives_valid_repeated_scopes():
    assert parse_auth_directives("@bot push --auth push") == frozenset({"push"})
    assert parse_auth_directives("--auth push --auth pr_create") == frozenset({"push", "pr_create"})
    assert parse_auth_directives("--auth all") == frozenset({"all"})


def test_authority_grant_covers_and_expires():
    grant = AuthorityGrant(user_message_id=1, scopes=frozenset({"push"}), granted_at=100.0)
    assert grant.covers("push") is True
    assert grant.covers("release") is False
    assert grant.is_expired(now=700.0, ttl=600.0) is False
    assert grant.is_expired(now=701.0, ttl=600.0) is True


def test_authority_grant_all_covers_any_scope():
    grant = AuthorityGrant(user_message_id=1, scopes=frozenset({"all"}), granted_at=0.0)
    assert grant.covers("push") is True
    assert grant.covers("release") is True
    assert grant.covers("anything") is True


def test_team_authority_records_checks_and_consumes_grants():
    auth = TeamAuthority(team_name="lpct")
    assert auth.record_user_message(msg_id=10, text="hello") == frozenset()
    assert auth.is_authorized("push") is False

    assert auth.record_user_message(msg_id=11, text="@bot --auth push") == frozenset({"push"})
    assert auth.is_authorized("push") is True
    assert auth.is_authorized("release") is False

    consumed = auth.consume_grant("push")
    assert consumed is not None
    assert consumed.user_message_id == 11
    assert auth.is_authorized("push") is False


def test_team_authority_all_grant_consumed_once():
    auth = TeamAuthority(team_name="lpct")
    auth.record_user_message(msg_id=12, text="--auth all")
    assert auth.consume_grant("release") is not None
    assert auth.is_authorized("push") is False


def test_team_authority_retains_only_four_grants():
    auth = TeamAuthority(team_name="lpct")
    for i in range(6):
        auth.record_user_message(msg_id=i, text="--auth push")
    assert len(auth._grants) == 4


def test_team_authority_status_snapshot():
    auth = TeamAuthority(team_name="lpct")
    auth.record_user_message(msg_id=42, text="--auth push")
    snap = auth.status_snapshot
    assert snap["team_name"] == "lpct"
    assert snap["active_grants"][0]["scopes"] == ["push"]
    assert snap["active_grants"][0]["user_message_id"] == 42


def test_valid_scopes_are_closed():
    assert VALID_SCOPES == frozenset({"push", "pr_create", "release", "network", "all"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_team_safety.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'link_project_to_chat.team_safety'
```

- [ ] **Step 3: Implement the module**

Create `src/link_project_to_chat/team_safety.py`:

```python
"""Team-mode safety grants and directive parsing."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

VALID_SCOPES: Final[frozenset[str]] = frozenset({
    "push",
    "pr_create",
    "release",
    "network",
    "all",
})

GRANT_TTL_SECONDS: Final[float] = 600.0
_MAX_GRANTS_RETAINED: Final[int] = 4
_AUTH_DIRECTIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)--auth\s+([a-z_]+)(?=\s|$)"
)


@dataclass(frozen=True)
class AuthorityGrant:
    user_message_id: int
    scopes: frozenset[str]
    granted_at: float

    def covers(self, scope: str) -> bool:
        return "all" in self.scopes or scope in self.scopes

    def is_expired(self, now: float, ttl: float = GRANT_TTL_SECONDS) -> bool:
        return (now - self.granted_at) > ttl


def parse_auth_directives(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(
        scope for scope in _AUTH_DIRECTIVE_RE.findall(text)
        if scope in VALID_SCOPES
    )


class TeamAuthority:
    """Per-team grant store. One instance lives in each team bot process."""

    def __init__(self, team_name: str) -> None:
        self._team_name = team_name
        self._grants: deque[AuthorityGrant] = deque(maxlen=_MAX_GRANTS_RETAINED)

    def record_user_message(self, msg_id: int, text: str) -> frozenset[str]:
        scopes = parse_auth_directives(text)
        if scopes:
            self._grants.append(
                AuthorityGrant(
                    user_message_id=msg_id,
                    scopes=scopes,
                    granted_at=time.monotonic(),
                )
            )
        return scopes

    def is_authorized(self, scope: str) -> bool:
        if scope not in VALID_SCOPES:
            return False
        now = time.monotonic()
        return any(g.covers(scope) and not g.is_expired(now) for g in self._grants)

    def consume_grant(self, scope: str) -> AuthorityGrant | None:
        if scope not in VALID_SCOPES:
            return None
        now = time.monotonic()
        for i, grant in enumerate(self._grants):
            if grant.covers(scope) and not grant.is_expired(now):
                retained = [g for j, g in enumerate(self._grants) if j != i]
                self._grants.clear()
                self._grants.extend(retained)
                return grant
        return None

    @property
    def status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        active = [g for g in self._grants if not g.is_expired(now)]
        return {
            "team_name": self._team_name,
            "active_grants": [
                {
                    "user_message_id": g.user_message_id,
                    "scopes": sorted(g.scopes),
                    "age_seconds": int(now - g.granted_at),
                }
                for g in active
            ],
        }
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
pytest tests/test_team_safety.py -q
git add src/link_project_to_chat/team_safety.py tests/test_team_safety.py
git commit -m "feat(team-safety): add authority grants"
```

Expected:

```text
8 passed
```

## Task 2: TeamConfig Safety Defaults

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config.py`:

```python
def test_team_config_safety_defaults():
    from link_project_to_chat.config import TeamConfig

    team = TeamConfig(path="/tmp/project")
    assert team.max_autonomous_turns == 5
    assert team.safety_mode == "strict"


def test_team_config_load_save_preserves_safety_fields(tmp_path):
    import json
    from link_project_to_chat.config import load_config, save_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "teams": {
            "lpct": {
                "path": "/tmp/lpct",
                "group_chat_id": -1001,
                "max_autonomous_turns": 3,
                "safety_mode": "strict",
                "bots": {},
            }
        }
    }))

    cfg = load_config(cfg_path)
    assert cfg.teams["lpct"].max_autonomous_turns == 3
    assert cfg.teams["lpct"].safety_mode == "strict"

    save_config(cfg, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["teams"]["lpct"]["max_autonomous_turns"] == 3
    assert raw["teams"]["lpct"]["safety_mode"] == "strict"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_config.py -q -k "team_config_safety"
```

Expected:

```text
AttributeError: 'TeamConfig' object has no attribute 'max_autonomous_turns'
```

- [ ] **Step 3: Add dataclass fields and load/save wiring**

In `src/link_project_to_chat/config.py`, add to `TeamConfig`:

```python
    max_autonomous_turns: int = 5
    safety_mode: str = "strict"
```

In both team load paths, include:

```python
max_autonomous_turns=int(team.get("max_autonomous_turns", 5)),
safety_mode=team.get("safety_mode", "strict"),
```

In `_save_config_unlocked`, when writing each team entry, add:

```python
entry["max_autonomous_turns"] = team.max_autonomous_turns
entry["safety_mode"] = team.safety_mode
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
pytest tests/test_config.py -q -k "team_config_safety"
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add team safety defaults"
```

Expected:

```text
2 passed
```

## Task 3: Relay Safety Enforcement

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py`

- [ ] **Step 1: Write failing relay-path tests**

Append to `tests/test_team_relay.py`:

```python
@pytest.mark.asyncio
async def test_relay_suppresses_recent_duplicate_forward():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=30_000)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    text = "@acme_dev_bot\n\nRequest changes: README quick start is broken."
    await _dispatch(relay, await _mk_event(text, "acme_mgr_bot", True, msg_id=30_100))
    await _dispatch(relay, await _mk_event("  " + text + "  \n", "acme_mgr_bot", True, msg_id=30_101))

    forwards = [
        call for call in client.send_message.await_args_list
        if call.args[1].startswith("@acme_dev_bot")
    ]
    assert len(forwards) == 1
    assert 30_101 in relay._relayed_ids


@pytest.mark.asyncio
async def test_relay_halts_before_exceeding_autonomous_turn_budget():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=31_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=999,
        max_autonomous_turns=2,
    )

    for i in range(3):
        await _dispatch(
            relay,
            await _mk_event(
                f"@acme_dev_bot batch {i}",
                "acme_mgr_bot",
                True,
                msg_id=31_100 + i,
            ),
        )

    forwards = [
        call for call in client.send_message.await_args_list
        if call.args[1].startswith("@acme_dev_bot")
    ]
    notices = [
        call for call in client.send_message.await_args_list
        if "autonomous turn budget" in call.args[1]
    ]
    assert len(forwards) == 2
    assert len(notices) == 1
    assert relay._halted is True


@pytest.mark.asyncio
async def test_relay_observes_authenticated_user_message_after_own_echo_guard():
    from link_project_to_chat.team_safety import TeamAuthority
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=32_000)
    authority = TeamAuthority(team_name="acme")
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        team_authority=authority,
        authenticated_user_id=42,
    )

    # Own relay echo must be ignored before auth parsing.
    relay._own_relay_ids.add(32_100)
    own_echo = await _mk_event("--auth push", "trusted_user", False, msg_id=32_100)
    own_echo.get_sender.return_value.id = 42
    await _dispatch(relay, own_echo)
    assert authority.is_authorized("push") is False

    user_msg = await _mk_event("--auth push", "trusted_user", False, msg_id=32_101)
    user_msg.get_sender.return_value.id = 42
    relay._consecutive_bot_turns = 2
    await _dispatch(relay, user_msg)
    assert authority.is_authorized("push") is True
    assert relay._consecutive_bot_turns == 0


@pytest.mark.asyncio
async def test_relay_peer_response_clears_same_author_streak():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=33_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )

    for i in range(2):
        await _dispatch(
            relay,
            await _mk_event(f"@acme_dev_bot review {i}", "acme_mgr_bot", True, msg_id=33_100 + i),
        )
    assert relay._rounds == 2

    await _dispatch(
        relay,
        await _mk_event("Patched both items.", "acme_dev_bot", True, msg_id=33_200),
    )
    assert relay._rounds == 0

    await _dispatch(
        relay,
        await _mk_event("@acme_dev_bot confirm HEAD", "acme_mgr_bot", True, msg_id=33_300),
    )
    assert relay._rounds == 1
    assert relay._halted is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_team_relay.py -q -k "duplicate_forward or autonomous_turn_budget or observes_authenticated or peer_response_clears"
```

Expected: failures for missing constructor args/fields and no duplicate/budget behavior.

- [ ] **Step 3: Implement relay changes**

In `_telegram_relay.py`:

Add imports:

```python
import hashlib
from ..team_safety import TeamAuthority
```

Add constants near the other relay constants:

```python
_DEDUP_WINDOW_SECONDS = 300.0
_DEDUP_HISTORY_LIMIT = 100
```

Extend `TeamRelay.__init__` with keyword-only args:

```python
        max_autonomous_turns: int = 5,
        team_authority: TeamAuthority | None = None,
        authenticated_user_id: int | str | None = None,
```

Add fields:

```python
        self._max_autonomous_turns = max_autonomous_turns
        self._consecutive_bot_turns = 0
        self._team_authority = team_authority
        self._authenticated_user_id = str(authenticated_user_id) if authenticated_user_id is not None else None
        self._recent_forward_signatures: dict[tuple[str, str, str], float] = {}
```

Add helpers:

```python
    def _observe_user_message(self, msg_id: int | None, sender_id: Any, text: str) -> None:
        if self._authenticated_user_id is None:
            return
        if str(sender_id) != self._authenticated_user_id:
            return
        self._consecutive_bot_turns = 0
        self._round_times.clear()
        self._round_senders.clear()
        self._halted = False
        if self._team_authority is not None and msg_id is not None:
            granted = self._team_authority.record_user_message(msg_id, text)
            if granted:
                logger.info(
                    "TeamRelay: auth granted team=%s scopes=%s msg=%s",
                    self._team_name, sorted(granted), msg_id,
                )

    def _is_recent_duplicate_forward(self, sender_username: str, peer: str, text: str) -> bool:
        now = time.monotonic()
        cutoff = now - _DEDUP_WINDOW_SECONDS
        stale = [
            sig for sig, seen_at in self._recent_forward_signatures.items()
            if seen_at < cutoff
        ]
        for sig in stale:
            self._recent_forward_signatures.pop(sig, None)
        body = re.sub(r"\s+", " ", _body_without_mention(text, peer).lower()).strip()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        signature = (sender_username, peer, digest)
        if signature in self._recent_forward_signatures:
            self._recent_forward_signatures[signature] = now
            return True
        if len(self._recent_forward_signatures) >= _DEDUP_HISTORY_LIMIT:
            oldest = min(self._recent_forward_signatures, key=self._recent_forward_signatures.get)
            self._recent_forward_signatures.pop(oldest, None)
        self._recent_forward_signatures[signature] = now
        return False
```

In `_handle_event`, keep this order:

```python
        msg_id = getattr(msg, "id", None)
        if msg_id is not None and msg_id in self._own_relay_ids:
            return
        if msg_id in self._relayed_ids:
            return
        sender = await event.get_sender()
```

Then in the non-bot branch:

```python
        if not getattr(sender, "bot", False):
            if not is_edit:
                self._observe_user_message(
                    msg_id=msg_id,
                    sender_id=getattr(sender, "id", ""),
                    text=getattr(msg, "message", "") or "",
                )
            return
```

Replace `_delete_pending_for_peer` with a `bool` return:

```python
    async def _delete_pending_for_peer(self, sender_username: str) -> bool:
        to_delete = [
            mid for mid, peer in self._pending_deletes.items()
            if peer == sender_username
        ]
        for mid in to_delete:
            self._pending_deletes.pop(mid, None)
            timer = self._pending_delete_timers.pop(mid, None)
            if timer is not None and not timer.done():
                timer.cancel()
            await self._delete_relay_message(mid)
        return bool(to_delete)
```

Update its call site:

```python
        if not is_edit:
            peer_responded = await self._delete_pending_for_peer(sender_username)
            if peer_responded and not self._halted:
                self._round_times.clear()
                self._round_senders.clear()
```

In `_finalize_relay`, after ack-only handling and before `_relay(...)`:

```python
        if peer is not None and self._is_recent_duplicate_forward(sender_username, peer, text):
            logger.info(
                "TeamRelay: dropping duplicate bot message from @%s to @%s (team=%s)",
                sender_username, peer, self._team_name,
            )
            if msg_id is not None:
                self._relayed_ids.add(msg_id)
            return
        if self._consecutive_bot_turns >= self._max_autonomous_turns:
            self._halted = True
            await self._send_halt_notice(reason="autonomous turn budget exhausted")
            return
        self._consecutive_bot_turns += 1
```

Change `_send_halt_notice` signature and reason handling:

```python
    async def _send_halt_notice(self, reason: str | None = None) -> None:
        if reason is None and self._is_same_author_streak():
            last_sender = self._round_senders[-1] if self._round_senders else "?"
            reason = (
                f"{_MAX_SAME_AUTHOR_STREAK} consecutive forwards from @{last_sender} "
                f"within {int(_ROUND_WINDOW_SECONDS)}s"
            )
        elif reason is None:
            reason = f"{self._rounds} rounds within {int(_ROUND_WINDOW_SECONDS)}s"
        notice = (
            f"Bot-to-bot relay paused: {reason} in team '{self._team_name}'. "
            "Send any message to resume."
        )
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
pytest tests/test_team_relay.py -q
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "feat(team-relay): enforce team safety budget"
```

Expected: all relay tests pass.

## Task 4: Wire Relay Safety Through Bot And TelegramTransport

**Files:**
- Modify: `src/link_project_to_chat/transport/telegram.py`
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `tests/test_bot_team_wiring.py`

- [ ] **Step 1: Write failing wiring tests**

Extend existing relay bootstrap tests in `tests/test_bot_team_wiring.py` so both `enable_team_relay_from_session` and `enable_team_relay_from_session_string` assertions include:

```python
    assert call_kwargs["max_autonomous_turns"] == 5
    assert str(call_kwargs["authenticated_user_id"]) == "1"
    assert call_kwargs["team_authority"] is bot.task_manager.backend.team_authority
```

Use `_make_team_bot_for_relay_test(tmp_path)` and set `bot._trusted_user_ids = [1]` before `bot.build()` if the helper does not already do that.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_bot_team_wiring.py -q -k "relay_when_session_env_set or string_session_env"
```

Expected: missing kwargs assertions fail.

- [ ] **Step 3: Implement TelegramTransport pass-through**

In `src/link_project_to_chat/transport/telegram.py`, add optional kwargs to `enable_team_relay`, `enable_team_relay_from_session`, and `enable_team_relay_from_session_string`:

```python
        max_autonomous_turns: int = 5,
        team_authority: Any | None = None,
        authenticated_user_id: int | str | None = None,
```

Pass them into `TeamRelay(...)`.

Add status properties:

```python
    @property
    def team_relay_consecutive_bot_turns(self) -> int:
        return self._team_relay._consecutive_bot_turns if self._team_relay else 0

    @property
    def team_relay_max_autonomous_turns(self) -> int:
        return self._team_relay._max_autonomous_turns if self._team_relay else 5
```

- [ ] **Step 4: Create and preserve TeamAuthority in ProjectBot**

In `src/link_project_to_chat/bot.py`, update `_refresh_team_system_note`:

```python
        if not self.peer_bot_username:
            backend.team_system_note = None
            backend.team_authority = None
            return
        if getattr(backend, "team_authority", None) is None:
            from .team_safety import TeamAuthority
            backend.team_authority = TeamAuthority(team_name=self.team_name or "")
```

Do not replace an existing authority object on refresh.

In `ProjectBot.build()`, when enabling the relay, compute:

```python
                    trusted_ids = self._get_trusted_user_ids()
                    authenticated_user_id = trusted_ids[0] if trusted_ids else None
                    team_authority = getattr(self.task_manager.backend, "team_authority", None)
                    max_turns = team.max_autonomous_turns if team else 5
```

Pass `team_authority`, `authenticated_user_id`, and `max_autonomous_turns=max_turns` into the relay enable call.

- [ ] **Step 5: Verify and commit**

Run:

```bash
pytest tests/test_bot_team_wiring.py -q -k "relay_when_session_env_set or string_session_env or refresh_team_system_note"
git add src/link_project_to_chat/transport/telegram.py src/link_project_to_chat/bot.py tests/test_bot_team_wiring.py
git commit -m "feat(bot): wire team safety into relay bootstrap"
```

Expected: targeted wiring tests pass.

## Task 5: Backend Safety Gates

**Files:**
- Modify: `src/link_project_to_chat/backends/base.py`
- Modify: `src/link_project_to_chat/backends/claude.py`
- Modify: `src/link_project_to_chat/backends/codex.py`
- Modify: `tests/backends/test_claude_backend.py`
- Modify: `tests/backends/test_codex_backend.py`
- Modify: `tests/backends/test_contract.py`

- [ ] **Step 1: Write failing backend tests**

Add to `tests/backends/test_claude_backend.py`:

```python
def test_claude_team_mode_blocks_external_side_effect_tools(tmp_path):
    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = ClaudeBackend(project_path=tmp_path, skip_permissions=True)
    backend.team_system_note = "team mode"
    backend.team_authority = TeamAuthority("lpct")
    cmd = backend._build_cmd()

    assert "--dangerously-skip-permissions" not in cmd
    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Bash(git push:*)" in blocked
    assert "Bash(gh pr create:*)" in blocked


def test_claude_team_mode_respects_push_grant(tmp_path):
    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.team_safety import TeamAuthority

    authority = TeamAuthority("lpct")
    authority.record_user_message(1, "--auth push")
    backend = ClaudeBackend(project_path=tmp_path)
    backend.team_system_note = "team mode"
    backend.team_authority = authority

    cmd = backend._build_cmd()
    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Bash(git push:*)" not in blocked
    assert "Bash(gh pr create:*)" in blocked
```

Add to `tests/backends/test_codex_backend.py`:

```python
def test_codex_team_mode_downgrades_dangerous_permissions(tmp_path):
    from link_project_to_chat.backends.codex import CodexBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = CodexBackend(tmp_path, state={"permissions": "dangerously-skip-permissions"})
    backend.team_system_note = "team mode"
    backend.team_authority = TeamAuthority("lpct")

    assert backend._permission_args() == ["--full-auto"]


def test_codex_team_mode_all_grant_allows_one_dangerous_turn(tmp_path):
    from link_project_to_chat.backends.codex import CodexBackend
    from link_project_to_chat.team_safety import TeamAuthority

    authority = TeamAuthority("lpct")
    authority.record_user_message(1, "--auth all")
    backend = CodexBackend(tmp_path, state={"permissions": "dangerously-skip-permissions"})
    backend.team_system_note = "team mode"
    backend.team_authority = authority

    assert backend._permission_args() == ["--dangerously-bypass-approvals-and-sandbox"]
    assert backend._permission_args() == ["--full-auto"]


def test_codex_scoped_network_grant_does_not_enable_full_bypass(tmp_path):
    from link_project_to_chat.backends.codex import CodexBackend
    from link_project_to_chat.team_safety import TeamAuthority

    authority = TeamAuthority("lpct")
    authority.record_user_message(1, "--auth network")
    backend = CodexBackend(tmp_path, state={"permissions": "dangerously-skip-permissions"})
    backend.team_system_note = "team mode"
    backend.team_authority = authority

    assert backend._permission_args() == ["--full-auto"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py -q -k "team_mode"
```

Expected: missing `team_authority` / unsafe permission assertions fail.

- [ ] **Step 3: Add Protocol field**

In `src/link_project_to_chat/backends/base.py`, import `TYPE_CHECKING`, then add:

```python
if TYPE_CHECKING:
    from ..team_safety import TeamAuthority
```

Inside `AgentBackend`:

```python
    team_authority: "TeamAuthority | None"
```

- [ ] **Step 4: Implement Claude safety**

In `ClaudeBackend.__init__`:

```python
        self.team_authority: "TeamAuthority | None" = None
```

Add type-checking import.

Add constants:

```python
_TEAM_DISALLOWED_TOOLS: dict[str, tuple[str, ...]] = {
    "push": ("Bash(git push:*)", "Bash(git push)", "Bash(gh pr merge:*)"),
    "pr_create": ("Bash(gh pr create:*)",),
    "release": ("Bash(gh release create:*)",),
    "network": ("Bash(curl:*)", "Bash(wget:*)", "Bash(gh workflow run:*)"),
}
```

In `_build_cmd`, do not append `--dangerously-skip-permissions` when `self.team_system_note` is set:

```python
        if self.skip_permissions and not self.team_system_note:
            cmd.append("--dangerously-skip-permissions")
```

Build effective disallowed tools:

```python
        effective_disallowed = list(self.disallowed_tools)
        if self.team_system_note:
            for scope, patterns in _TEAM_DISALLOWED_TOOLS.items():
                if self.team_authority is not None and self.team_authority.is_authorized(scope):
                    continue
                effective_disallowed.extend(patterns)
        if effective_disallowed:
            cmd.extend(["--disallowedTools", ",".join(effective_disallowed)])
```

- [ ] **Step 5: Implement Codex safety**

In `CodexBackend.__init__`:

```python
        self.team_authority: "TeamAuthority | None" = None
```

Add type-checking import.

In `_permission_args`, change the dangerous branch:

```python
        if mode in ("bypassPermissions", "dangerously-skip-permissions"):
            if self.team_system_note:
                if self.team_authority is not None and self.team_authority.consume_grant("all"):
                    return ["--dangerously-bypass-approvals-and-sandbox"]
                return ["--full-auto"]
            return ["--dangerously-bypass-approvals-and-sandbox"]
```

This is intentionally conservative: scoped `network`, `push`, and `release` grants do not restore a full Codex sandbox bypass because the Codex CLI cannot scope that bypass to one command.

- [ ] **Step 6: Verify and commit**

Run:

```bash
pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/backends/test_contract.py -q
git add src/link_project_to_chat/backends/base.py src/link_project_to_chat/backends/claude.py src/link_project_to_chat/backends/codex.py tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/backends/test_contract.py
git commit -m "feat(backends): enforce team-mode safety gates"
```

Expected: backend tests pass.

## Task 6: Status And Empty Response Surface

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `src/link_project_to_chat/backends/claude.py`
- Modify: `src/link_project_to_chat/backends/codex.py`
- Modify: `tests/test_backend_command.py`
- Modify: `tests/backends/test_claude_backend.py`
- Modify: `tests/backends/test_codex_backend.py`

- [ ] **Step 1: Add backend empty-response tests**

Add focused tests to the backend test files showing `chat()` returns `""` when no `Result` arrives. Use the existing backend test style in `tests/backends/test_claude_backend.py` and `tests/backends/test_codex_backend.py`.

Concrete Claude test:

```python
@pytest.mark.asyncio
async def test_claude_chat_returns_empty_string_when_stream_has_no_result(tmp_path):
    from link_project_to_chat.backends.claude import ClaudeBackend

    backend = ClaudeBackend(project_path=tmp_path)

    async def empty_stream(*_args, **_kwargs):
        if False:
            yield None

    backend.chat_stream = empty_stream  # type: ignore[method-assign]
    assert await backend.chat("ping") == ""
```

Concrete Codex test:

```python
@pytest.mark.asyncio
async def test_codex_chat_returns_empty_string_when_stream_has_no_result(tmp_path):
    from link_project_to_chat.backends.codex import CodexBackend

    backend = CodexBackend(project_path=tmp_path, state={})

    async def empty_stream(*_args, **_kwargs):
        if False:
            yield None

    backend.chat_stream = empty_stream  # type: ignore[method-assign]
    assert await backend.chat("ping") == ""
```

- [ ] **Step 2: Add status renderer test**

Append to `tests/test_backend_command.py`:

```python
def test_render_team_safety_block_shows_grants():
    from link_project_to_chat.bot import _render_team_safety_block
    from link_project_to_chat.team_safety import TeamAuthority

    authority = TeamAuthority("lpct")
    authority.record_user_message(7, "--auth push")

    rendered = _render_team_safety_block(
        authority=authority,
        consecutive_turns=2,
        max_autonomous_turns=5,
    )

    assert "Team safety: strict" in rendered
    assert "Autonomous turns: 2 / 5" in rendered
    assert "push" in rendered
    assert "msg #7" in rendered
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/test_backend_command.py -q -k "empty_string or team_safety_block"
```

- [ ] **Step 4: Implement empty response behavior**

In both `ClaudeBackend.chat()` and `CodexBackend.chat()`, change:

```python
return result_text or "[No response]"
```

to:

```python
return result_text
```

In Codex `chat_stream`, when emitting `Result`, change:

```python
text="\n\n".join(t for t in collected_text if t) or "[No response]"
```

to:

```python
text="\n\n".join(t for t in collected_text if t)
```

The task completion path in `bot.py` already sends `Done` status messages; do not surface a literal `[No response]` chat bubble.

- [ ] **Step 5: Implement status block**

In `bot.py`, add:

```python
def _render_team_safety_block(
    authority: "TeamAuthority",
    consecutive_turns: int,
    max_autonomous_turns: int,
) -> str:
    snap = authority.status_snapshot
    lines = [
        "Team safety: strict",
        f"Autonomous turns: {consecutive_turns} / {max_autonomous_turns}",
    ]
    if snap["active_grants"]:
        lines.append("Active grants:")
        for grant in snap["active_grants"]:
            scopes = ", ".join(grant["scopes"])
            lines.append(
                f"- {scopes} ({grant['age_seconds']}s ago, msg #{grant['user_message_id']})"
            )
    else:
        lines.append("Active grants: none")
    return "\n".join(lines)
```

Use `TYPE_CHECKING` for the `TeamAuthority` type.

In `_compose_status`, append this block when the active backend has `team_authority`:

```python
        authority = getattr(backend, "team_authority", None)
        if authority is not None:
            consecutive = getattr(self._transport, "team_relay_consecutive_bot_turns", 0)
            max_turns = getattr(self._transport, "team_relay_max_autonomous_turns", 5)
            lines.append("")
            lines.append(_render_team_safety_block(authority, consecutive, max_turns))
```

- [ ] **Step 6: Verify and commit**

Run:

```bash
pytest tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/test_backend_command.py -q -k "empty_string or team_safety_block or status"
git add src/link_project_to_chat/bot.py src/link_project_to_chat/backends/claude.py src/link_project_to_chat/backends/codex.py tests/test_backend_command.py tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py
git commit -m "feat(bot): show team safety status"
```

Expected: targeted tests pass.

## Task 7: Persona Review Surface Discipline

**Files:**
- Modify: `src/link_project_to_chat/personas/software_manager.md`
- Modify: `src/link_project_to_chat/personas/software_dev.md`
- Modify: `tests/test_bundled_personas.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bundled_personas.py`:

```python
def test_software_manager_persona_requires_review_surface():
    p = files("link_project_to_chat.personas").joinpath("software_manager.md")
    content = p.read_text(encoding="utf-8").lower()
    assert "review surface" in content
    assert "working tree" in content
    assert "head" in content
    assert "origin" in content


def test_software_dev_persona_reports_review_surface_state():
    p = files("link_project_to_chat.personas").joinpath("software_dev.md")
    content = p.read_text(encoding="utf-8").lower()
    assert "review surface" in content
    assert "unstaged" in content
    assert "staged" in content
    assert "committed" in content
    assert "pushed" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_bundled_personas.py -q -k "review_surface"
```

- [ ] **Step 3: Update personas**

In `software_manager.md`, extend the review protocol paragraph:

```markdown
Review surface: before approving or requesting changes, name exactly what you reviewed: working tree (`git diff` plus direct file reads), staged changes (`git diff --staged`), local commit (`git show HEAD` or `git show <hash>`), or remote comparison (`git diff origin/<branch>..HEAD`). If the Developer says fixes are unstaged but your read still shows stale content, stop repeating findings and ask them to commit for review or confirm the working-tree surface.
```

In `software_dev.md`, add after the execution paragraph:

```markdown
Review surface: when handing work to the Manager, state whether changes are unstaged, staged, committed locally, or pushed. Include the exact commit hash when committed, or say "working tree only" when not. If the Manager appears to review stale content, include `git status --short --branch`, name the surface they should inspect, and ask before committing only to stabilize review.
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
pytest tests/test_bundled_personas.py -q
git add src/link_project_to_chat/personas/software_manager.md src/link_project_to_chat/personas/software_dev.md tests/test_bundled_personas.py
git commit -m "docs(personas): require explicit review surfaces"
```

Expected: bundled persona tests pass.

## Final Verification

- [ ] **Step 1: Run focused suites**

Run:

```bash
pytest tests/test_team_safety.py tests/test_team_relay.py tests/test_bot_team_wiring.py tests/test_backend_command.py tests/test_bundled_personas.py tests/backends/test_claude_backend.py tests/backends/test_codex_backend.py tests/backends/test_contract.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full suite**

Run:

```bash
pytest -q
```

Expected: all pass.

- [ ] **Step 3: Compile and whitespace checks**

Run:

```bash
python -m compileall src/link_project_to_chat -q
git diff --check origin/main..HEAD
```

Expected: no output.

## Deferrals

These are intentionally not part of this implementation:

- Moving prompt composition into backends. The current code already skips persona/history on resumed team sessions and clears session id on persona changes.
- Scoped Codex network/push/release elevation. Codex does not expose a scoped tool allowlist, so scoped grants must not become full sandbox bypasses.
- Skill-discovery flakiness on team-bot first turn.
- Retroactively reverting pushed commits from the 2026-05-15 incident.

## Self-Review

Spec coverage: The corrected plan covers authenticated-user grants, strict team-mode backend gates, autonomous turn budget, duplicate relay suppression, peer-response streak reset, `/status` visibility, empty-response polish, and review-surface persona guidance.

Placeholder scan: There are no TBD/fill-later placeholders. The plan uses concrete repo paths and avoids "adapt this stub" instructions.

Type consistency: `TeamAuthority`, `AuthorityGrant`, `team_authority`, `_consecutive_bot_turns`, `_max_autonomous_turns`, and `_render_team_safety_block` are introduced before use and referenced consistently.
