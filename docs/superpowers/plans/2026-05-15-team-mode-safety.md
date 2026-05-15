# Team-Mode Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the team-mode safety model from `docs/superpowers/specs/2026-05-15-team-mode-safety-design.md` — scoped authority grants via inline `--auth <scope>` directives, bounded autonomous turns per user prompt, codex prompt re-injection fix, relay content-hash dedupe, and surface fixes.

**Architecture:** New `team_safety.py` module owns grants and directive parsing. `TeamRelay` gains content-hash dedupe and autonomous-turn budget enforcement; observes user messages to feed `TeamAuthority`. Each backend gains `compose_user_prompt` so prompt composition moves out of bot.py; codex uses hash-based skip on session resume. Claude gains team-mode `--disallowedTools` augment; codex gets sandbox downgrade in team mode.

**Tech Stack:** Python 3.11+, pytest with asyncio_mode=auto, existing `link_project_to_chat` package layout.

---

## File map

| File | Action |
|---|---|
| `src/link_project_to_chat/team_safety.py` | Create — `AuthorityGrant`, `TeamAuthority`, `parse_auth_directives` |
| `src/link_project_to_chat/config.py` | Modify — add `max_autonomous_turns` and `safety_mode` to `TeamConfig` plus migration |
| `src/link_project_to_chat/transport/_telegram_relay.py` | Modify — content-hash dedupe, autonomous-turn budget, halt-notice rewording, peer-response-resets-streak, `TeamAuthority` observation |
| `src/link_project_to_chat/backends/base.py` | Modify — add `team_authority` and `compose_user_prompt` to Protocol |
| `src/link_project_to_chat/backends/claude.py` | Modify — `compose_user_prompt`, team-mode `--disallowedTools` augment |
| `src/link_project_to_chat/backends/codex.py` | Modify — `compose_user_prompt` with hash-skip, `_permission_args` sandbox downgrade |
| `src/link_project_to_chat/bot.py` | Modify — wire `team_authority` into backend; replace inline prepend with `compose_user_prompt`; suppress empty `[No response]`; `/status` team-safety block |
| `src/link_project_to_chat/personas/software_manager.md` | Modify — review-surface discipline prose |
| `src/link_project_to_chat/personas/software_dev.md` | Modify — review-surface handoff prose |
| `tests/test_team_safety.py` | Create — directive parser, `AuthorityGrant`, `TeamAuthority` |
| `tests/test_team_relay.py` | Extend — dedupe, autonomous-turn budget, halt-notice, peer-response-resets-streak |
| `tests/test_backend_claude.py` | Extend — team-mode disallowed-tools, `compose_user_prompt` |
| `tests/test_backend_codex.py` | Extend — sandbox downgrade, grant consumption |
| `tests/test_codex_resume_no_reinjection.py` | Create — codex hash-skip on resume |
| `tests/test_bundled_personas.py` | Extend — review-surface persona invariants |

Run the targeted test for each task via `pytest <path>::<name> -v`. Run the full suite via `pytest -q` at task boundaries. The team-bot env hermeticity fix from `080fa9d` is already in place, so the suite should not need `env -u LP2C_TELETHON_SESSION_STRING`.

---

## Task 1: `parse_auth_directives` + `AuthorityGrant`

**Files:**
- Create: `src/link_project_to_chat/team_safety.py`
- Create: `tests/test_team_safety.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_team_safety.py`:
```python
import time

import pytest

from link_project_to_chat.team_safety import (
    AuthorityGrant,
    VALID_SCOPES,
    parse_auth_directives,
)


class TestParseAuthDirectives:
    def test_empty_text_returns_empty(self):
        assert parse_auth_directives("") == frozenset()
        assert parse_auth_directives(None) == frozenset()

    def test_single_valid_scope(self):
        assert parse_auth_directives("@bot push --auth push") == frozenset({"push"})

    def test_multiple_scopes_via_repetition(self):
        result = parse_auth_directives("--auth push --auth pr_create")
        assert result == frozenset({"push", "pr_create"})

    def test_unknown_scope_silently_dropped(self):
        assert parse_auth_directives("--auth pushh") == frozenset()
        assert parse_auth_directives("--auth nuclear_launch") == frozenset()

    def test_boundary_anchor_rejects_authentication(self):
        # --authentication should not match --auth, --auth-mode should not match --auth
        assert parse_auth_directives("--authentication push") == frozenset()
        assert parse_auth_directives("--auth-mode push") == frozenset()

    def test_directive_at_message_start(self):
        assert parse_auth_directives("--auth push then do work") == frozenset({"push"})

    def test_directive_at_message_end(self):
        assert parse_auth_directives("do work --auth push") == frozenset({"push"})

    def test_all_wildcard_recognized(self):
        assert parse_auth_directives("--auth all") == frozenset({"all"})


class TestAuthorityGrant:
    def test_covers_exact_scope(self):
        g = AuthorityGrant(user_message_id=1, scopes=frozenset({"push"}), granted_at=0.0)
        assert g.covers("push") is True
        assert g.covers("release") is False

    def test_all_wildcard_covers_any_scope(self):
        g = AuthorityGrant(user_message_id=1, scopes=frozenset({"all"}), granted_at=0.0)
        assert g.covers("push") is True
        assert g.covers("release") is True
        assert g.covers("anything") is True

    def test_is_expired_after_ttl(self):
        g = AuthorityGrant(user_message_id=1, scopes=frozenset({"push"}), granted_at=100.0)
        assert g.is_expired(now=700.0, ttl=600.0) is False  # exactly at TTL
        assert g.is_expired(now=701.0, ttl=600.0) is True

    def test_valid_scopes_set_is_frozen(self):
        assert isinstance(VALID_SCOPES, frozenset)
        assert "push" in VALID_SCOPES
        assert "all" in VALID_SCOPES
        assert "network" in VALID_SCOPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_team_safety.py -v`
Expected: collection error / `ModuleNotFoundError: No module named 'link_project_to_chat.team_safety'`

- [ ] **Step 3: Write the minimal implementation**

Create `src/link_project_to_chat/team_safety.py`:
```python
"""Team-mode safety: authority grants and directive parsing.

See docs/superpowers/specs/2026-05-15-team-mode-safety-design.md for the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

VALID_SCOPES: Final[frozenset[str]] = frozenset({
    "push",       # git push, gh pr merge
    "pr_create",  # gh pr create
    "release",    # gh release create, package publish
    "network",    # generic outbound network ops (curl POST, fetch)
    "all",        # wildcard
})

_GRANT_TTL_SECONDS: Final[float] = 600.0  # 10 minutes

# Anchored to whitespace boundaries so --authentication / --auth-mode don't match.
_AUTH_DIRECTIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)--auth\s+([a-z_]+)(?=\s|$)"
)


@dataclass(frozen=True)
class AuthorityGrant:
    """A user's explicit grant of permission for one or more scopes."""
    user_message_id: int
    scopes: frozenset[str]
    granted_at: float  # time.monotonic() at grant time

    def covers(self, scope: str) -> bool:
        return "all" in self.scopes or scope in self.scopes

    def is_expired(self, now: float, ttl: float = _GRANT_TTL_SECONDS) -> bool:
        return (now - self.granted_at) > ttl


def parse_auth_directives(text: str | None) -> frozenset[str]:
    """Extract --auth <scope> tokens from text. Unknown scopes silently dropped.

    Only scope names in VALID_SCOPES are honored; this prevents typos and
    future-token leakage from accidentally granting authority.
    """
    if not text:
        return frozenset()
    return frozenset(s for s in _AUTH_DIRECTIVE_RE.findall(text) if s in VALID_SCOPES)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_team_safety.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/team_safety.py tests/test_team_safety.py
git commit -m "feat(team-safety): add AuthorityGrant + parse_auth_directives

Closed scope vocabulary, whitespace-boundary-anchored regex. Per
spec docs/superpowers/specs/2026-05-15-team-mode-safety-design.md
section 5.1-5.3."
```

---

## Task 2: `TeamAuthority` class

**Files:**
- Modify: `src/link_project_to_chat/team_safety.py`
- Modify: `tests/test_team_safety.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_team_safety.py`:
```python
from link_project_to_chat.team_safety import TeamAuthority


class TestTeamAuthority:
    def test_record_user_message_no_directives_returns_empty(self):
        auth = TeamAuthority(team_name="test")
        granted = auth.record_user_message(msg_id=1, text="hello world")
        assert granted == frozenset()
        assert auth.is_authorized("push") is False

    def test_record_user_message_with_grant(self):
        auth = TeamAuthority(team_name="test")
        granted = auth.record_user_message(msg_id=42, text="@bot push --auth push")
        assert granted == frozenset({"push"})
        assert auth.is_authorized("push") is True
        assert auth.is_authorized("release") is False

    def test_is_authorized_expired_grant_returns_false(self, monkeypatch):
        auth = TeamAuthority(team_name="test")
        # Inject a stale grant by mutating internal state for the test.
        from link_project_to_chat.team_safety import AuthorityGrant
        import time
        stale = AuthorityGrant(
            user_message_id=1,
            scopes=frozenset({"push"}),
            granted_at=time.monotonic() - 700.0,
        )
        auth._grants.append(stale)
        assert auth.is_authorized("push") is False

    def test_consume_grant_removes_only_matching_grant(self):
        auth = TeamAuthority(team_name="test")
        auth.record_user_message(msg_id=1, text="--auth push")
        auth.record_user_message(msg_id=2, text="--auth release")
        consumed = auth.consume_grant("push")
        assert consumed is not None
        assert "push" in consumed.scopes
        assert auth.is_authorized("push") is False  # consumed
        assert auth.is_authorized("release") is True  # untouched

    def test_consume_grant_no_match_returns_none(self):
        auth = TeamAuthority(team_name="test")
        assert auth.consume_grant("push") is None

    def test_consume_grant_all_wildcard_consumed_on_any_scope(self):
        auth = TeamAuthority(team_name="test")
        auth.record_user_message(msg_id=1, text="--auth all")
        consumed = auth.consume_grant("push")
        assert consumed is not None
        assert "all" in consumed.scopes
        assert auth.is_authorized("release") is False  # the all grant is gone

    def test_grants_deque_caps_at_four(self):
        auth = TeamAuthority(team_name="test")
        for i in range(6):
            auth.record_user_message(msg_id=i, text="--auth push")
        assert len(auth._grants) == 4

    def test_unknown_scope_to_is_authorized_returns_false(self):
        auth = TeamAuthority(team_name="test")
        auth.record_user_message(msg_id=1, text="--auth push")
        assert auth.is_authorized("not_a_real_scope") is False

    def test_status_snapshot_shape(self):
        auth = TeamAuthority(team_name="lpct")
        auth.record_user_message(msg_id=12, text="--auth push")
        snap = auth.status_snapshot
        assert snap["team_name"] == "lpct"
        assert len(snap["active_grants"]) == 1
        assert snap["active_grants"][0]["scopes"] == ["push"]
        assert snap["active_grants"][0]["user_message_id"] == 12
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_team_safety.py::TestTeamAuthority -v`
Expected: ImportError or `ImportError: cannot import name 'TeamAuthority' from 'link_project_to_chat.team_safety'`

- [ ] **Step 3: Implement `TeamAuthority`**

Append to `src/link_project_to_chat/team_safety.py`:
```python
import time
from collections import deque
from typing import Any


_MAX_GRANTS_RETAINED: Final[int] = 4


class TeamAuthority:
    """Per-team state: active authority grants.

    One instance per ProjectBot process. Both bots in a team observe the
    same group chat and independently maintain their own copies; state
    stays synchronized via chat-as-source-of-truth (modulo `consume_grant`
    divergence — see spec section 4).

    Not thread-safe; the relay/bot calls these from a single asyncio task.
    """

    def __init__(self, team_name: str) -> None:
        self._team_name = team_name
        self._grants: deque[AuthorityGrant] = deque(maxlen=_MAX_GRANTS_RETAINED)

    def record_user_message(self, msg_id: int, text: str) -> frozenset[str]:
        """Parse directives from `text` and store a grant if any scopes found.

        Returns the scopes actually granted (empty frozenset if none).
        Caller is responsible for resetting any external turn counter.
        """
        scopes = parse_auth_directives(text)
        if scopes:
            self._grants.append(
                AuthorityGrant(user_message_id=msg_id, scopes=scopes, granted_at=time.monotonic())
            )
        return scopes

    def is_authorized(self, scope: str) -> bool:
        """True iff any non-expired grant covers `scope`."""
        if scope not in VALID_SCOPES:
            return False
        now = time.monotonic()
        return any(g.covers(scope) and not g.is_expired(now) for g in self._grants)

    def consume_grant(self, scope: str) -> AuthorityGrant | None:
        """Find a matching non-expired grant, remove it, return it.

        Used for one-shot irreversible ops (push, release, sandbox elevation).
        Subsequent calls won't see this grant. Returns None if no match.
        """
        if scope not in VALID_SCOPES:
            return None
        now = time.monotonic()
        for i, g in enumerate(self._grants):
            if g.covers(scope) and not g.is_expired(now):
                remaining = [grant for j, grant in enumerate(self._grants) if j != i]
                self._grants.clear()
                self._grants.extend(remaining)
                return g
        return None

    @property
    def status_snapshot(self) -> dict[str, Any]:
        """Snapshot for /status display: team name and active (non-expired) grants."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_team_safety.py -v`
Expected: 20 passed (12 from Task 1 plus 8 new)

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/team_safety.py tests/test_team_safety.py
git commit -m "feat(team-safety): add TeamAuthority grant store

Per-team grant deque, is_authorized vs. consume_grant semantics,
status_snapshot for /status display. Per spec section 5.4."
```

---

## Task 3: `TeamConfig` adds `max_autonomous_turns` and `safety_mode`

**Files:**
- Modify: `src/link_project_to_chat/config.py:87-92`
- Modify: `src/link_project_to_chat/config.py:633-642` (load path)
- Modify: `src/link_project_to_chat/config.py` (serialize path — search for `_serialize_team` and equivalent)
- Test: `tests/test_config.py`

- [ ] **Step 1: Locate the serialize path**

Run: `grep -n "def _serialize_team\b\|serialize_teams\|team_cfg.path" src/link_project_to_chat/config.py | head -20`
Look at the function that converts `TeamConfig` back to a dict for `config.json` write. Note its name and structure.

- [ ] **Step 2: Add the failing test**

Append to `tests/test_config.py`:
```python
def test_team_config_defaults_max_autonomous_turns_to_five():
    """New TeamConfig instances default max_autonomous_turns=5 and safety_mode='strict'."""
    from link_project_to_chat.config import TeamConfig
    cfg = TeamConfig(path="/tmp/project")
    assert cfg.max_autonomous_turns == 5
    assert cfg.safety_mode == "strict"


def test_team_config_load_migrates_missing_fields(tmp_path):
    """A config.json without max_autonomous_turns/safety_mode loads with defaults."""
    from link_project_to_chat.config import load_config
    import json
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "teams": {
            "old_team": {
                "path": "/tmp/old",
                "group_chat_id": 0,
                "bots": {},
            },
        },
    }))
    config = load_config(cfg_path)
    team = config.teams["old_team"]
    assert team.max_autonomous_turns == 5
    assert team.safety_mode == "strict"


def test_team_config_load_preserves_explicit_values(tmp_path):
    """A config.json that sets these fields preserves them on load."""
    from link_project_to_chat.config import load_config
    import json
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "teams": {
            "tight_team": {
                "path": "/tmp/tight",
                "group_chat_id": 0,
                "bots": {},
                "max_autonomous_turns": 3,
                "safety_mode": "strict",
            },
        },
    }))
    config = load_config(cfg_path)
    assert config.teams["tight_team"].max_autonomous_turns == 3
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v -k "team_config" 2>&1 | tail -30`
Expected: AttributeError / `'TeamConfig' has no attribute 'max_autonomous_turns'`

- [ ] **Step 4: Add fields to the dataclass**

In `src/link_project_to_chat/config.py:87-92`, change:
```python
@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None
```
to:
```python
@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None
    # Per-team safety policy. See docs/superpowers/specs/2026-05-15-team-mode-safety-design.md.
    max_autonomous_turns: int = 5
    safety_mode: str = "strict"  # "strict" | "off" (off is reserved; v1 is always strict)
```

- [ ] **Step 5: Wire load path**

In `src/link_project_to_chat/config.py:633-642`, change the `TeamConfig(...)` construction to read the new fields:
```python
team_cfg = TeamConfig(
    path=team["path"],
    group_chat_id=team["group_chat_id"],
    room=_make_room_binding(team.get("room")),
    bots={
        role: _make_team_bot_config(b)
        for role, b in team.get("bots", {}).items()
    },
    max_autonomous_turns=int(team.get("max_autonomous_turns", 5)),
    safety_mode=team.get("safety_mode", "strict"),
)
```

- [ ] **Step 6: Wire serialize path**

Find the function that writes teams to JSON (likely `_serialize_team` or inline in `save_config`). Add the two new fields to the output dict alongside the existing keys. Concrete example pattern — adapt to actual function:
```python
{
    "path": team.path,
    "group_chat_id": team.group_chat_id,
    "bots": {...},
    "room": ...,
    "max_autonomous_turns": team.max_autonomous_turns,
    "safety_mode": team.safety_mode,
}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v -k "team_config"`
Expected: 3 passed (the new tests)

Run: `pytest tests/test_config.py -v`
Expected: all tests pass (no regressions)

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): TeamConfig gains max_autonomous_turns + safety_mode

Defaults: 5 turns, strict mode. Migration is implicit via dataclass
defaults — existing teams pick up safe defaults on first load. Per
spec section 10."
```

---

## Task 4: Relay content-hash dedupe

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py` (or wherever the relay tests live — verify first)

- [ ] **Step 1: Locate the relay test file**

Run: `find tests -name "*.py" | xargs grep -l "TeamRelay\|team_relay" 2>/dev/null | head -5`
Use the file that already imports `TeamRelay`. If multiple, use the most-tested one. If none exists, create `tests/test_team_relay.py` with the imports cloned from a sibling test file.

- [ ] **Step 2: Add the failing test**

Add to the relay test file:
```python
def test_is_recent_duplicate_drops_byte_identical_body(relay_instance):
    """Identical body from same sender within 60s window is flagged as duplicate."""
    relay = relay_instance
    sender = "team_lpct_mgr_claude_bot"
    body = "Status: approved for the 4 non-doc review items."
    assert relay._is_recent_duplicate(sender, body) is False  # first time, not a dup
    assert relay._is_recent_duplicate(sender, body) is True   # second time, dup


def test_is_recent_duplicate_distinguishes_senders(relay_instance):
    """Identical body from different sender is not a duplicate."""
    relay = relay_instance
    body = "approved"
    assert relay._is_recent_duplicate("mgr_bot", body) is False
    assert relay._is_recent_duplicate("dev_bot", body) is False  # different sender


def test_is_recent_duplicate_distinguishes_bodies(relay_instance):
    """Different body from same sender is not a duplicate."""
    relay = relay_instance
    assert relay._is_recent_duplicate("mgr_bot", "first") is False
    assert relay._is_recent_duplicate("mgr_bot", "second") is False


def test_is_recent_duplicate_ignores_whitespace_diffs(relay_instance):
    """body.strip() means trailing/leading whitespace doesn't break the match."""
    relay = relay_instance
    assert relay._is_recent_duplicate("mgr_bot", "approved") is False
    assert relay._is_recent_duplicate("mgr_bot", "  approved  \n") is True
```

The existing test file already provides `_mk_client_with_ids`, `_mk_event`, and `_dispatch` helpers (see `tests/test_team_relay.py` around lines 23-134). Reuse them. Add a small fixture at the top of the new test block:
```python
import pytest

from link_project_to_chat.transport._telegram_relay import TeamRelay


@pytest.fixture
def relay_instance():
    client = _mk_client_with_ids(start_id=20_000)
    return TeamRelay(
        client=client,
        team_name="test_team",
        group_chat_id=-100_111,
        bot_usernames={"mgr_bot", "dev_bot"},
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_team_relay.py::test_is_recent_duplicate_drops_byte_identical_body -v`
Expected: AttributeError / `'TeamRelay' object has no attribute '_is_recent_duplicate'`

- [ ] **Step 4: Implement the dedupe gate**

In `src/link_project_to_chat/transport/_telegram_relay.py`, add inside `class TeamRelay`:

```python
import hashlib  # add to imports at top of file


_DEDUPE_WINDOW_SECONDS: float = 60.0
_DEDUPE_HISTORY_PER_SENDER: int = 4
```

Add to `TeamRelay.__init__`:
```python
self._recent_forwards: dict[str, deque[tuple[str, float]]] = {}
```

Add method on `TeamRelay`:
```python
def _is_recent_duplicate(self, sender: str, body: str) -> bool:
    """True iff `body` (stripped) matches a recent forward from `sender`.

    Catches verbatim relay duplicates (the 2026-05-15 manager loop shape)
    before the peer wastes a turn re-processing them.
    """
    h = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]
    history = self._recent_forwards.setdefault(
        sender, deque(maxlen=_DEDUPE_HISTORY_PER_SENDER)
    )
    now = time.monotonic()
    while history and (now - history[0][1]) > _DEDUPE_WINDOW_SECONDS:
        history.popleft()
    if any(prev_h == h for prev_h, _ in history):
        return True
    history.append((h, now))
    return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_team_relay.py -v -k "is_recent_duplicate"`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "feat(team-relay): content-hash dedupe for verbatim duplicate forwards

Catches the 2026-05-15 manager loop shape where codex re-sent
byte-identical 'Request changes' payloads. SHA-256 prefix on
stripped body, deque(maxlen=4) per sender, 60s window. Per
spec section 6.3."
```

---

## Task 5: Wire dedupe into the forward path + add turn counter

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py`

- [ ] **Step 1: Find the forward path**

Run: `grep -n "def _relay\b\|def _on_new_message\|def _maybe_forward\|_record_round" src/link_project_to_chat/transport/_telegram_relay.py`
The forward path is the function that calls `self._relay(...)` after deciding to send. Read 50 lines around it to understand its structure. The dedupe call must go BEFORE the existing `_record_round` and `_halted` checks.

- [ ] **Step 2: Add the failing tests**

Append to `tests/test_team_relay.py`:
```python
def test_consecutive_bot_turns_increments_on_forward(relay_instance):
    """Each accepted forward bumps the relay's autonomous-turn counter."""
    relay = relay_instance
    relay._consecutive_bot_turns = 0
    relay._consecutive_bot_turns += 1  # simulate one forward
    assert relay._consecutive_bot_turns == 1


def test_consecutive_bot_turns_halts_at_max(relay_instance):
    """Counter reaching _max_autonomous_turns triggers _halted."""
    relay = relay_instance
    relay._max_autonomous_turns = 3
    # Simulate three forwards
    for _ in range(3):
        relay._consecutive_bot_turns += 1
    halted = relay._consecutive_bot_turns >= relay._max_autonomous_turns
    assert halted is True


def test_user_message_resets_counter(relay_instance):
    """Observing a user message zeros the autonomous-turn counter."""
    relay = relay_instance
    relay._consecutive_bot_turns = 4
    relay._consecutive_bot_turns = 0  # simulating reset
    assert relay._consecutive_bot_turns == 0
```

These are minimal sanity tests for the new fields. A full integration test for the forward path will live in Task 8 after `TeamAuthority` is wired in.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_team_relay.py -v -k "consecutive_bot_turns or user_message_resets"`
Expected: AttributeError / `'TeamRelay' object has no attribute '_consecutive_bot_turns'`

- [ ] **Step 4: Add the counter fields and constructor wiring**

In `_telegram_relay.py`, extend `TeamRelay.__init__` signature:
```python
def __init__(
    self,
    client: Any,
    team_name: str,
    group_chat_id: int,
    bot_usernames: set[str],
    *,
    max_consecutive_bot_relays: int = _MAX_CONSECUTIVE_BOT_RELAYS,
    max_autonomous_turns: int = 5,  # NEW: per-team budget for bot-to-bot turns
) -> None:
    ...
    # existing body ...
    self._max_autonomous_turns = max_autonomous_turns  # NEW
    self._consecutive_bot_turns: int = 0  # NEW
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_team_relay.py -v -k "consecutive_bot_turns or user_message_resets"`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "feat(team-relay): add autonomous-turn counter scaffolding

Adds _max_autonomous_turns and _consecutive_bot_turns to TeamRelay.
Field-only commit; wiring into the forward path comes in the next
task once TeamAuthority observation lands. Per spec section 6.2."
```

---

## Task 6: Halt-notice wording fix

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py` (specifically `_send_halt_notice` at ~line 570)
- Modify: `tests/test_team_relay.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_team_relay.py`:
```python
@pytest.mark.asyncio
async def test_halt_notice_streak_wording_no_peer_reply_claim(relay_instance):
    """Same-author-streak halt notice describes the streak, not peer-reply ordering."""
    relay = relay_instance
    sent_messages = []

    async def fake_send(_chat_id, text):
        sent_messages.append(text)
        return MagicMock(id=999)

    relay._client.send_message = fake_send
    relay._round_senders.extend(["mgr_bot", "mgr_bot", "mgr_bot"])
    relay._round_times.extend([100.0, 110.0, 120.0])

    await relay._send_halt_notice()

    assert len(sent_messages) == 1
    notice = sent_messages[0]
    assert "@mgr_bot" in notice
    assert "consecutive forwards" in notice
    assert "no peer reply between" not in notice  # old wording must be gone
    assert "within" in notice and "s" in notice  # new wording mentions time window
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_team_relay.py::test_halt_notice_streak_wording_no_peer_reply_claim -v`
Expected: AssertionError on `"no peer reply between" not in notice` (current wording still says it)

- [ ] **Step 3: Update halt-notice wording**

In `_telegram_relay.py`, locate `_send_halt_notice` (around line 570). Replace the streak branch:

```python
async def _send_halt_notice(self) -> None:
    if self._is_same_author_streak():
        last_sender = self._round_senders[-1] if self._round_senders else "?"
        reason = (
            f"{_MAX_SAME_AUTHOR_STREAK} consecutive forwards from @{last_sender} "
            f"within {int(_ROUND_WINDOW_SECONDS)}s"
        )
    else:
        reason = (
            f"{self._rounds} rounds within {int(_ROUND_WINDOW_SECONDS)}s"
        )
    notice = (
        f"Bot-to-bot relay paused: {reason} in team '{self._team_name}'. "
        f"Send any message to resume."
    )
    # ... existing send logic unchanged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_team_relay.py::test_halt_notice_streak_wording_no_peer_reply_claim -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "fix(team-relay): halt-notice wording no longer claims missing peer reply

The streak rule checks last-N-forwards-same-sender; it does not
verify peer-reply ordering. Old wording 'with no peer reply between'
was misleading (Dev #47 on 2026-05-15 DID land between the forwards,
but the streak still tripped because of streaming-edit timing).
Per spec section 6.4."
```

---

## Task 7: Peer response resets same-author streak

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py`

**Rationale:** The existing `_MAX_SAME_AUTHOR_STREAK` cap halts after N consecutive forwards from the same sender. Its halt-notice (post-Task 6) says "consecutive forwards from @X within Ns" — accurate, but the cap is overly aggressive when the *peer actually did reply* in between: streaming-edit timing can cause the peer's reply to land in the relay's round-recording *after* the next same-author forward, so the streak counts mgr→mgr→mgr even though the peer responded. Reset the streak when a peer responds. This complements (not replaces) the bot-turn budget from Task 8 — the budget is "stop after N total bot turns since last user msg"; the streak reset is "the streak rule fires only if the peer truly hasn't engaged."

- [ ] **Step 1: Add the failing test**

Append to `tests/test_team_relay.py`:
```python
@pytest.mark.asyncio
async def test_peer_response_clears_same_author_streak():
    """A peer-bot response zeros the round buffer so the streak cap can't
    trip on prior same-author forwards.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=15_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )

    for i in range(2):
        ev = await _mk_event(
            f"@acme_dev_bot review request {i}",
            sender_username="acme_mgr_bot",
            sender_is_bot=True,
            msg_id=15_100 + i,
        )
        await _dispatch(relay, ev)
    assert relay._rounds == 2
    assert relay._halted is False

    peer_reply = await _mk_event(
        "Patched both items; re-review please.",
        sender_username="acme_dev_bot",
        sender_is_bot=True,
        msg_id=15_200,
    )
    await _dispatch(relay, peer_reply)
    assert relay._rounds == 0
    assert relay._halted is False

    # After reset, a fresh manager forward should be round=1, not round=3.
    ev = await _mk_event(
        "@acme_dev_bot please confirm the committed diff",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
        msg_id=15_300,
    )
    await _dispatch(relay, ev)
    assert relay._rounds == 1
    assert relay._halted is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_team_relay.py::test_peer_response_clears_same_author_streak -q`
Expected: `AssertionError: assert 2 == 0` (the streak is not reset on peer reply yet).

- [ ] **Step 3: Make `_delete_pending_for_peer` report whether anything was deleted**

In `src/link_project_to_chat/transport/_telegram_relay.py`, replace `_delete_pending_for_peer`:
```python
async def _delete_pending_for_peer(self, sender_username: str) -> bool:
    """Delete relay forwards waiting on `sender_username`; return True iff any existed."""
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

- [ ] **Step 4: Reset round state when a pending peer response arrives**

Find the call site of `_delete_pending_for_peer` (likely inside `_handle_event` or `_on_new_message`). Run: `grep -n "_delete_pending_for_peer" src/link_project_to_chat/transport/_telegram_relay.py`. The existing call looks like:
```python
if not is_edit:
    await self._delete_pending_for_peer(sender_username)
```

Replace with:
```python
if not is_edit:
    peer_responded = await self._delete_pending_for_peer(sender_username)
    if peer_responded and not self._halted:
        # The peer engaged; zero the streak so the same-author cap won't
        # fire on forwards that preceded the peer's reply. The bot-turn
        # budget (Task 8) still bounds total autonomous activity.
        self._round_times.clear()
        self._round_senders.clear()
```

- [ ] **Step 5: Run the test**

Run: `pytest tests/test_team_relay.py::test_peer_response_clears_same_author_streak -q`
Expected: 1 passed.

- [ ] **Step 6: Run all relay tests**

Run: `pytest tests/test_team_relay.py -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "fix(team-relay): peer response resets same-author streak

The streak cap's intent is 'halt when one bot dominates without
peer engagement.' Streaming-edit timing on 2026-05-15 caused the
peer's reply to land after the next same-author forward in
round-recording order, tripping the streak even though the peer
DID respond. Reset the round buffer when a pending peer response
arrives. The bot-turn budget from Task 8 remains the hard cap on
total autonomous activity. Idea adapted from the team-bot plan
docs/superpowers/plans/2026-05-15-team-relay-review-surface-fixes.md
Task 1, layered on top of (not replacing) the existing caps."
```

---

## Task 8: Wire `TeamAuthority` into `TeamRelay` + forward-path enforcement

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `tests/test_team_relay.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_team_relay.py`:
```python
@pytest.fixture
def relay_with_authority():
    from link_project_to_chat.team_safety import TeamAuthority
    client = MagicMock()
    auth = TeamAuthority(team_name="test_team")
    relay = TeamRelay(
        client=client,
        team_name="test_team",
        group_chat_id=-1001234567890,
        bot_usernames={"mgr_bot", "dev_bot"},
        team_authority=auth,
        max_autonomous_turns=3,
        authenticated_user_id=8206818037,
    )
    return relay, auth


def test_relay_records_user_message_with_auth_directive(relay_with_authority):
    """A user message containing --auth push is forwarded to TeamAuthority."""
    relay, auth = relay_with_authority
    relay._observe_user_message(msg_id=100, from_id=8206818037, text="@mgr_bot push --auth push")
    assert auth.is_authorized("push") is True
    assert relay._consecutive_bot_turns == 0  # counter was reset


def test_relay_ignores_non_authenticated_user(relay_with_authority):
    """A message from a different from_id is not parsed for auth directives."""
    relay, auth = relay_with_authority
    relay._observe_user_message(msg_id=101, from_id=99999, text="--auth push")
    assert auth.is_authorized("push") is False


def test_relay_user_message_resets_counter_even_without_auth(relay_with_authority):
    """Any user message resets the autonomous-turn counter."""
    relay, auth = relay_with_authority
    relay._consecutive_bot_turns = 2
    relay._observe_user_message(msg_id=102, from_id=8206818037, text="just checking in")
    assert relay._consecutive_bot_turns == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_team_relay.py -v -k "records_user_message or ignores_non_authenticated or resets_counter_even_without_auth"`
Expected: TypeError on missing `team_authority` / `authenticated_user_id` kwargs

- [ ] **Step 3: Extend `__init__` and add `_observe_user_message`**

In `_telegram_relay.py`:

Add imports at the top:
```python
from link_project_to_chat.team_safety import TeamAuthority
```

Extend `TeamRelay.__init__` signature:
```python
def __init__(
    self,
    client: Any,
    team_name: str,
    group_chat_id: int,
    bot_usernames: set[str],
    *,
    max_consecutive_bot_relays: int = _MAX_CONSECUTIVE_BOT_RELAYS,
    max_autonomous_turns: int = 5,
    team_authority: TeamAuthority | None = None,
    authenticated_user_id: int | None = None,
) -> None:
    # ... existing body ...
    self._team_authority = team_authority
    self._authenticated_user_id = authenticated_user_id
```

Add method on `TeamRelay`:
```python
def _observe_user_message(self, msg_id: int, from_id: int, text: str) -> None:
    """Observe a non-bot group message. Parse --auth directives if from
    the authenticated user; always reset the autonomous-turn counter on
    any authenticated-user message.
    """
    if self._authenticated_user_id is None:
        return
    if from_id != self._authenticated_user_id:
        return
    self._consecutive_bot_turns = 0
    if self._team_authority is not None:
        granted = self._team_authority.record_user_message(msg_id, text)
        if granted:
            logger.info(
                "team-mode auth granted: team=%s scopes=%s user_msg=%s",
                self._team_name, sorted(granted), msg_id,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_team_relay.py -v -k "records_user_message or ignores_non_authenticated or resets_counter_even_without_auth"`
Expected: 3 passed

- [ ] **Step 5: Hook `_observe_user_message` into the Telethon event handler**

Run: `grep -n "_on_new_message\|_on_message_edited" src/link_project_to_chat/transport/_telegram_relay.py | head -5`
Read the body of `_on_new_message` to see where messages are inspected. Add a call to `self._observe_user_message(msg_id, from_id, text)` for messages whose sender is NOT in `self._bot_usernames` (i.e., human messages). Place this before any return-early branches.

Example placement (adapt to actual function shape):
```python
async def _on_new_message(self, event):
    # ... existing chat_id filter etc. ...
    msg = event.message
    sender = await msg.get_sender()
    sender_username = (getattr(sender, "username", "") or "").lower()
    if sender_username not in self._bot_usernames:
        # Human message — observe for auth directives + counter reset.
        text = msg.message or ""
        self._observe_user_message(
            msg_id=msg.id,
            from_id=getattr(sender, "id", 0),
            text=text,
        )
    # ... rest of existing handler logic ...
```

- [ ] **Step 6: Add the integration test using existing helpers**

Append to `tests/test_team_relay.py` (uses `_mk_event` and `_dispatch` from the existing module):
```python
@pytest.mark.asyncio
async def test_forward_path_halts_at_autonomous_turn_budget():
    """3 bot forwards with no user reset triggers halt via _max_autonomous_turns."""
    from link_project_to_chat.team_safety import TeamAuthority

    client = _mk_client_with_ids(start_id=14_000)
    auth = TeamAuthority(team_name="acme")
    relay = TeamRelay(
        client=client,
        team_name="acme",
        group_chat_id=-100_111,
        bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
        max_autonomous_turns=3,
        team_authority=auth,
        authenticated_user_id=42,
    )

    # Three manager-bot forwards in a row, no peer reply, no user reset.
    for i in range(4):  # 4 attempts — the 4th should be blocked by halt
        ev = await _mk_event(
            f"@acme_dev_bot please review batch {i}",
            sender_username="acme_mgr_bot",
            sender_is_bot=True,
            msg_id=14_100 + i,
        )
        await _dispatch(relay, ev)

    # By the 3rd accepted forward the relay should have halted.
    assert relay._halted is True
    # Halt notice should have been sent at least once.
    halt_messages = [
        c for c in client.send_message.await_args_list
        if "paused" in str(c.args[1]).lower()
    ]
    assert len(halt_messages) >= 1


@pytest.mark.asyncio
async def test_user_message_resets_autonomous_turn_counter():
    """An authenticated-user message resets the consecutive-turn counter."""
    from link_project_to_chat.team_safety import TeamAuthority

    client = _mk_client_with_ids(start_id=14_500)
    relay = TeamRelay(
        client=client,
        team_name="acme",
        group_chat_id=-100_111,
        bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
        max_autonomous_turns=5,
        team_authority=TeamAuthority(team_name="acme"),
        authenticated_user_id=42,
    )

    # Two manager-bot forwards.
    for i in range(2):
        ev = await _mk_event(
            f"@acme_dev_bot batch {i}",
            sender_username="acme_mgr_bot",
            sender_is_bot=True,
            msg_id=14_600 + i,
        )
        await _dispatch(relay, ev)
    assert relay._consecutive_bot_turns == 2

    # User message arrives.
    user_ev = await _mk_event(
        "let me check that --auth push",
        sender_username="rezo",
        sender_is_bot=False,
        msg_id=14_700,
    )
    # Need to set the sender id on the mock so it matches authenticated_user_id.
    user_ev.message.sender_id = 42
    await _dispatch(relay, user_ev)

    assert relay._consecutive_bot_turns == 0
```

(Adapt the `_mk_event` arguments if the real helper requires different kwargs — the signature is at `tests/test_team_relay.py:98-118`.)

- [ ] **Step 7: Run all relay tests**

Run: `pytest tests/test_team_relay.py -v`
Expected: all tests pass

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "feat(team-relay): wire TeamAuthority + autonomous-turn budget

TeamRelay now observes group user messages, parses --auth directives,
and resets the autonomous-turn counter on any authenticated-user
message. _observe_user_message is invoked from the existing Telethon
NewMessage handler. Per spec section 6.1-6.2."
```

---

## Task 9: Backend Protocol gains `team_authority` and `compose_user_prompt`

**Files:**
- Modify: `src/link_project_to_chat/backends/base.py`
- Modify: `tests/test_backend_protocol.py` (or `tests/backends/...`)

- [ ] **Step 1: Locate the backend test directory**

Run: `find tests -name "test_backend*.py" -o -name "test_*backend*.py" | head -10`
Use the existing pattern. If none for Protocol specifically, create `tests/test_backend_protocol.py`.

- [ ] **Step 2: Add the failing test**

In the chosen test file:
```python
def test_agent_backend_protocol_has_team_authority_attr():
    """AgentBackend Protocol declares team_authority: TeamAuthority | None."""
    from link_project_to_chat.backends.base import AgentBackend
    # The Protocol annotation must exist for type-checkers; at runtime,
    # we assert the attribute name is in the Protocol's annotations.
    annotations = getattr(AgentBackend, "__annotations__", {})
    assert "team_authority" in annotations


def test_agent_backend_protocol_has_compose_user_prompt():
    """AgentBackend Protocol declares compose_user_prompt method."""
    from link_project_to_chat.backends.base import AgentBackend
    assert hasattr(AgentBackend, "compose_user_prompt")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_backend_protocol.py -v`
Expected: AssertionError on missing `team_authority` annotation

- [ ] **Step 4: Extend the Protocol**

In `src/link_project_to_chat/backends/base.py`, in the `AgentBackend` Protocol class, add (around the existing `team_system_note` field):

```python
class AgentBackend(Protocol):
    name: str
    capabilities: BackendCapabilities
    project_path: Path
    model: str | None
    model_display: str | None
    session_id: str | None
    effort: str | None
    team_system_note: str | None
    # NEW: per-team safety state shared with the relay (None outside team mode).
    team_authority: "TeamAuthority | None"

    # ... existing methods ...

    def compose_user_prompt(
        self,
        raw_message: str,
        persona: str | None,
        history: str,
    ) -> str:
        """Assemble the full user prompt for this backend's turn.

        Bot.py hands over the raw user message, persona text, and history
        block; each backend decides whether to include them in the prompt
        sent to its CLI. Codex elides persona/history on session resume
        (see backends/codex.py); claude always includes them since its
        --append-system-prompt channel keeps the cache stable.
        """
        ...
```

Add at the top of the file (forward reference for the Protocol):
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..team_safety import TeamAuthority
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_protocol.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/backends/base.py tests/test_backend_protocol.py
git commit -m "feat(backends): Protocol gains team_authority + compose_user_prompt

team_authority enables backend-side authorization checks for
side-effecting tools. compose_user_prompt moves prompt composition
into backends so codex can skip persona/history re-injection on
session resume. Per spec sections 7.1, 8.2."
```

---

## Task 10: Claude backend — `team_authority` field + team-mode `--disallowedTools` augment

**Files:**
- Modify: `src/link_project_to_chat/backends/claude.py:203-219` (constructor) and `:240-283` (`_build_cmd`)
- Modify: `tests/test_backend_claude.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_backend_claude.py`:
```python
def test_claude_team_mode_blocks_git_push():
    """In team mode with no grants, git push is in --disallowedTools."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    backend.team_system_note = "team mode active"
    backend.team_authority = TeamAuthority(team_name="t")

    cmd = backend._build_cmd()
    idx = cmd.index("--disallowedTools")
    blocked = cmd[idx + 1].split(",")
    assert "Bash(git push:*)" in blocked
    assert "Bash(gh pr create:*)" in blocked


def test_claude_team_mode_allows_git_push_with_push_grant():
    """When TeamAuthority has --auth push, git push is NOT blocked."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    backend.team_system_note = "team mode active"
    auth = TeamAuthority(team_name="t")
    auth.record_user_message(msg_id=1, text="--auth push")
    backend.team_authority = auth

    cmd = backend._build_cmd()
    if "--disallowedTools" in cmd:
        blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    else:
        blocked = []
    assert "Bash(git push:*)" not in blocked


def test_claude_team_mode_all_grant_removes_every_block():
    """--auth all removes every team-mode block."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    backend.team_system_note = "team mode active"
    auth = TeamAuthority(team_name="t")
    auth.record_user_message(msg_id=1, text="--auth all")
    backend.team_authority = auth

    cmd = backend._build_cmd()
    if "--disallowedTools" in cmd:
        blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    else:
        blocked = []
    assert all(not b.startswith("Bash(git push") for b in blocked)
    assert all(not b.startswith("Bash(gh ") for b in blocked)


def test_claude_outside_team_mode_no_team_blocks():
    """When team_system_note is None, no team-mode blocks added."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    backend.team_system_note = None
    backend.team_authority = None

    cmd = backend._build_cmd()
    if "--disallowedTools" in cmd:
        blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    else:
        blocked = []
    assert "Bash(git push:*)" not in blocked
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_claude.py -v -k "team_mode"`
Expected: AttributeError or AssertionError (no `team_authority` attribute, or git push not blocked)

- [ ] **Step 3: Implement the augment**

In `src/link_project_to_chat/backends/claude.py`:

Add near the top of the file (after imports):
```python
_TEAM_MODE_DISALLOWED_BASH: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(git push)",
    "Bash(gh pr create:*)",
    "Bash(gh pr merge:*)",
    "Bash(gh release create:*)",
    "Bash(gh workflow run:*)",
)

# Maps a Bash pattern to the team-safety scope that releases it.
_TEAM_MODE_BLOCK_SCOPES: dict[str, str] = {
    "Bash(git push:*)": "push",
    "Bash(git push)": "push",
    "Bash(gh pr create:*)": "pr_create",
    "Bash(gh pr merge:*)": "push",
    "Bash(gh release create:*)": "release",
    "Bash(gh workflow run:*)": "network",
}
```

Add to `ClaudeBackend.__init__`:
```python
self.team_authority: "TeamAuthority | None" = None
```

Add a top-of-module type import:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..team_safety import TeamAuthority
```

In `_build_cmd`, replace:
```python
if self.disallowed_tools:
    cmd.extend(["--disallowedTools", ",".join(self.disallowed_tools)])
```
with:
```python
effective_disallowed = list(self.disallowed_tools)
if self.team_system_note:
    for pattern in _TEAM_MODE_DISALLOWED_BASH:
        scope = _TEAM_MODE_BLOCK_SCOPES.get(pattern)
        if scope is None:
            effective_disallowed.append(pattern)
            continue
        if self.team_authority is not None and self.team_authority.is_authorized(scope):
            continue  # authorized; do not block
        effective_disallowed.append(pattern)
if effective_disallowed:
    cmd.extend(["--disallowedTools", ",".join(effective_disallowed)])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_claude.py -v -k "team_mode"`
Expected: 4 passed

Run: `pytest tests/test_backend_claude.py -v`
Expected: all tests pass (no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/claude.py tests/test_backend_claude.py
git commit -m "feat(claude): team-mode --disallowedTools blocks push and gh ops

Blocks git push, gh pr create/merge, gh release create, gh workflow
run when team_system_note is set. is_authorized(scope) on the
backend's TeamAuthority unblocks a specific tool when the user
granted the matching scope via --auth. Per spec section 7.2."
```

---

## Task 11: Codex backend — `team_authority` field + sandbox downgrade in team mode

**Files:**
- Modify: `src/link_project_to_chat/backends/codex.py:58-93` (constructor) and `:125-135` (`_permission_args`)
- Modify: `tests/test_backend_codex.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_backend_codex.py`:
```python
def test_codex_team_mode_downgrades_dangerous_to_full_auto():
    """In team mode, 'dangerously-skip-permissions' is downgraded to --full-auto."""
    from pathlib import Path
    from link_project_to_chat.backends.codex import CodexBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = CodexBackend(
        project_path=Path("/tmp/x"),
        state={"permissions": "dangerously-skip-permissions"},
    )
    backend.team_system_note = "team mode active"
    backend.team_authority = TeamAuthority(team_name="t")

    args = backend._permission_args()
    assert args == ["--full-auto"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_codex_team_mode_consumes_network_grant_for_one_elevation():
    """A --auth network grant elevates ONE turn, then is consumed."""
    from pathlib import Path
    from link_project_to_chat.backends.codex import CodexBackend
    from link_project_to_chat.team_safety import TeamAuthority

    backend = CodexBackend(
        project_path=Path("/tmp/x"),
        state={"permissions": "dangerously-skip-permissions"},
    )
    backend.team_system_note = "team mode active"
    auth = TeamAuthority(team_name="t")
    auth.record_user_message(msg_id=1, text="--auth network")
    backend.team_authority = auth

    first_args = backend._permission_args()
    second_args = backend._permission_args()

    assert first_args == ["--dangerously-bypass-approvals-and-sandbox"]
    assert second_args == ["--full-auto"]  # grant consumed; back to safe


def test_codex_outside_team_mode_passes_through_dangerous():
    """Outside team mode, 'dangerously-skip-permissions' is unchanged."""
    from pathlib import Path
    from link_project_to_chat.backends.codex import CodexBackend

    backend = CodexBackend(
        project_path=Path("/tmp/x"),
        state={"permissions": "dangerously-skip-permissions"},
    )
    backend.team_system_note = None
    backend.team_authority = None

    args = backend._permission_args()
    assert args == ["--dangerously-bypass-approvals-and-sandbox"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_codex.py -v -k "team_mode or outside_team_mode"`
Expected: AttributeError on `team_authority`, or assertion failure on downgrade

- [ ] **Step 3: Implement the downgrade**

In `src/link_project_to_chat/backends/codex.py`:

Add to `CodexBackend.__init__`:
```python
self.team_authority: "TeamAuthority | None" = None
```

Add at the top of the file:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..team_safety import TeamAuthority
```

Replace `_permission_args`:
```python
def _permission_args(self) -> list[str]:
    mode = self.permissions
    if mode in (None, "default"):
        return []
    if mode == "plan":
        return ["-c", "sandbox_mode='read-only'", "-c", "approval_policy='never'"]
    if mode in ("acceptEdits", "dontAsk", "auto"):
        return ["--full-auto"]
    if mode in ("bypassPermissions", "dangerously-skip-permissions"):
        if self.team_system_note:
            # Team mode: refuse to fully bypass the sandbox unless the user
            # explicitly granted network/push scope via --auth.
            if self.team_authority is not None and self.team_authority.consume_grant("network"):
                logger.info(
                    "codex team-safety: --auth network consumed; elevating sandbox for this turn",
                )
                return ["--dangerously-bypass-approvals-and-sandbox"]
            logger.info(
                "codex team-safety: sandbox kept at full-auto despite '%s' configured", mode,
            )
            return ["--full-auto"]
        return ["--dangerously-bypass-approvals-and-sandbox"]
    raise ValueError(f"Unsupported Codex permissions mode: {mode}")
```

Also alias `push` and `release` grants to the same elevation by checking them in the consume sequence. Replace the consume line:
```python
if self.team_authority is not None and self.team_authority.consume_grant("network"):
```
with:
```python
if self.team_authority is not None and (
    self.team_authority.consume_grant("network")
    or self.team_authority.consume_grant("push")
    or self.team_authority.consume_grant("release")
):
```

Note the `or` short-circuit: at most one `consume_grant` actually executes per call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_codex.py -v -k "team_mode or outside_team_mode"`
Expected: 3 passed

Run: `pytest tests/test_backend_codex.py -v`
Expected: all tests pass (no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/codex.py tests/test_backend_codex.py
git commit -m "feat(codex): sandbox downgrade in team mode without --auth

dangerously-skip-permissions and bypassPermissions get silently
downgraded to --full-auto (workspace-write sandbox: writes ok,
network blocked) in team mode. A --auth network/push/release grant
elevates one turn via consume_grant. Per spec section 7.3."
```

---

## ⚠ Note before Task 12 — re-injection already partially fixed

While writing this plan, verification of [bot.py:958-974](src/link_project_to_chat/bot.py:958) showed that `_team_session_active()` **already** skips persona + history injection on team-mode resume turns. The docstring there explicitly references the 2026-04-27 codex loop. The memory note that drove the spec's Section 8 design was outdated.

**What's still missing in the existing fix:** the current blanket-skip approach does not detect persona/team-note CHANGES mid-session — if the user runs `/use other_persona`, the agent will never see the new persona because injection stays off for the resumed session. Tasks 12-14 below add the hash-based detection that fixes this corner case AND move the composition into backends so each backend owns its own re-injection policy.

**Priority:** Tasks 12-14 are **architectural improvement + corner-case bug fix**, not "fix the loop." If you want the minimum viable safety landing, ship Tasks 1-11, 15, 16, 17, 18 and defer 12-14 to a follow-up.

---

## Task 12: Claude `compose_user_prompt` (current behavior preserved)

**Files:**
- Modify: `src/link_project_to_chat/backends/claude.py`
- Modify: `tests/test_backend_claude.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_backend_claude.py`:
```python
def test_claude_compose_user_prompt_preserves_existing_behavior():
    """compose_user_prompt mirrors bot.py's current per-turn concatenation."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    result = backend.compose_user_prompt(
        raw_message="hello",
        persona="[PERSONA: dev]\ncontext\n[END PERSONA]",
        history="[History — last 2 turns]\nT1: foo\nT2: bar\n",
    )
    # Order: history, then persona, then raw message, separated by blank lines.
    assert "[History" in result
    assert "[PERSONA: dev]" in result
    assert "hello" in result
    assert result.index("[History") < result.index("[PERSONA")
    assert result.index("[PERSONA") < result.index("hello")


def test_claude_compose_user_prompt_handles_empty_persona():
    """When persona is None or empty, only history + raw_message are used."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend

    backend = ClaudeBackend(project_path=Path("/tmp/x"))
    result = backend.compose_user_prompt(
        raw_message="hello",
        persona=None,
        history="[History]\nT1: foo\n",
    )
    assert "PERSONA" not in result
    assert "hello" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_claude.py::test_claude_compose_user_prompt_preserves_existing_behavior -v`
Expected: AttributeError / `'ClaudeBackend' object has no attribute 'compose_user_prompt'`

- [ ] **Step 3: Implement `compose_user_prompt`**

Add to `ClaudeBackend`:
```python
def compose_user_prompt(
    self,
    raw_message: str,
    persona: str | None,
    history: str,
) -> str:
    """Assemble the per-turn user prompt for claude.

    Claude's persona/team-note also live in --append-system-prompt (a
    stable system-prompt channel), so re-including them in the user
    message is cache-friendly. We keep the existing bot.py concatenation
    order: history + persona + raw_message, joined by blank lines.
    """
    parts: list[str] = []
    if history:
        parts.append(history)
    if persona:
        parts.append(persona)
    parts.append(raw_message)
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_claude.py -v -k "compose_user_prompt"`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/claude.py tests/test_backend_claude.py
git commit -m "feat(claude): add compose_user_prompt preserving existing behavior

Claude's persona/history concatenation moves from bot.py into the
backend so codex can override with hash-skip-on-resume semantics
in the next commit. Claude's behavior is identical to before.
Per spec section 8.2."
```

---

## Task 13: Codex `compose_user_prompt` with hash-skip on resume

**Files:**
- Modify: `src/link_project_to_chat/backends/codex.py`
- Create: `tests/test_codex_resume_no_reinjection.py`

- [ ] **Step 1: Add the failing tests**

Create `tests/test_codex_resume_no_reinjection.py`:
```python
"""Codex must NOT re-inject persona/team-note/history on session resume.

Root cause of the manager review loops on 2026-04-27 and 2026-05-15:
codex re-read the persona block as a new instruction each turn,
re-executing the review protocol from scratch. See:
docs/superpowers/specs/2026-05-15-team-mode-safety-design.md section 8.
"""
from pathlib import Path

from link_project_to_chat.backends.codex import CodexBackend


def _backend(session_id: str | None = None, team_note: str | None = None) -> CodexBackend:
    backend = CodexBackend(project_path=Path("/tmp/x"), state={})
    backend.session_id = session_id
    backend.team_system_note = team_note
    return backend


def test_first_turn_includes_everything():
    """No session_id yet: persona + team-note + history all included."""
    backend = _backend()
    prompt = backend.compose_user_prompt(
        raw_message="user msg",
        persona="[PERSONA: dev]\nfoo\n[END PERSONA]",
        history="[History — last 1 turn]\nT1: prior\n",
    )
    assert "PERSONA" in prompt
    assert "History" in prompt
    assert "user msg" in prompt


def test_first_turn_with_team_note_includes_system_reminder():
    """First turn includes team_system_note wrapped in <system-reminder>."""
    backend = _backend(team_note="you are the manager")
    prompt = backend.compose_user_prompt(
        raw_message="user msg",
        persona="[PERSONA]\np\n[/PERSONA]",
        history="",
    )
    assert "<system-reminder>" in prompt
    assert "you are the manager" in prompt


def test_resume_skips_unchanged_persona_and_team_note():
    """Second turn with same persona/team-note: only raw_message in prompt."""
    backend = _backend(team_note="manager")
    persona = "[PERSONA: dev]\nfoo\n[END PERSONA]"
    # Prime hashes by calling compose once.
    backend.compose_user_prompt(raw_message="t1", persona=persona, history="h1")
    # Now simulate session resume: set session_id.
    backend.session_id = "abc-123"
    prompt = backend.compose_user_prompt(
        raw_message="t2", persona=persona, history="h2",
    )
    assert "PERSONA" not in prompt
    assert "<system-reminder>" not in prompt
    assert "h2" not in prompt   # history also skipped on resume
    assert prompt.strip() == "t2"


def test_resume_re_injects_when_persona_changes():
    """Mid-session persona swap: new persona is included once, hash updates."""
    backend = _backend(team_note="manager")
    backend.compose_user_prompt(raw_message="t1", persona="P1", history="h1")
    backend.session_id = "abc-123"
    backend.compose_user_prompt(raw_message="t2", persona="P1", history="h2")
    prompt = backend.compose_user_prompt(
        raw_message="t3",
        persona="P2_NEW_PERSONA",
        history="h3",
    )
    assert "P2_NEW_PERSONA" in prompt


def test_resume_re_injects_when_team_note_changes():
    """Mid-session team-note change: new note included once."""
    backend = _backend(team_note="manager v1")
    backend.compose_user_prompt(raw_message="t1", persona="P", history="h1")
    backend.session_id = "abc-123"
    backend.team_system_note = "manager v2"
    prompt = backend.compose_user_prompt(raw_message="t2", persona="P", history="h2")
    assert "manager v2" in prompt


def test_first_turn_no_team_note_omits_system_reminder():
    """No team_system_note: no <system-reminder> wrapper anywhere."""
    backend = _backend(team_note=None)
    prompt = backend.compose_user_prompt(
        raw_message="hello", persona=None, history="",
    )
    assert "<system-reminder>" not in prompt
    assert prompt.strip() == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codex_resume_no_reinjection.py -v`
Expected: AttributeError / `'CodexBackend' object has no attribute 'compose_user_prompt'`

- [ ] **Step 3: Implement `compose_user_prompt` on `CodexBackend`**

In `src/link_project_to_chat/backends/codex.py`, add to `CodexBackend.__init__`:
```python
self._last_persona_hash: str | None = None
self._last_team_note_hash: str | None = None
```

Add a module-level helper:
```python
import hashlib

def _hash(s: str | None) -> str | None:
    if s is None:
        return None
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
```

Add to `CodexBackend`:
```python
def compose_user_prompt(
    self,
    raw_message: str,
    persona: str | None,
    history: str,
) -> str:
    """Assemble the per-turn user prompt for codex with re-injection skip.

    On session resume, persona/team-note/history are already in codex's
    conversation history. Re-injecting them per turn causes codex to read
    them as new instructions, re-executing procedural content. We track
    the last-injected content hash and skip when unchanged.
    """
    is_resume = self.session_id is not None
    parts: list[str] = []

    team_hash = _hash(self.team_system_note)
    if (not is_resume) or team_hash != self._last_team_note_hash:
        if self.team_system_note:
            parts.append(f"<system-reminder>\n{self.team_system_note}\n</system-reminder>")
        self._last_team_note_hash = team_hash

    persona_hash = _hash(persona)
    if (not is_resume) or persona_hash != self._last_persona_hash:
        if persona:
            parts.append(persona)
        self._last_persona_hash = persona_hash

    # History block is by nature "delta" but codex resume already has the
    # prior turns in conversation. Skip on resume entirely.
    if (not is_resume) and history:
        parts.append(history)

    parts.append(raw_message)
    return "\n\n".join(parts)
```

Also delete the old `_build_prompt` method in `codex.py:115-123` and update `_build_cmd` (line ~104) to use `compose_user_prompt` directly OR keep `_build_prompt` as a thin wrapper that calls `compose_user_prompt` with no persona/history. The simpler route: keep `_build_cmd` unchanged and have bot.py pass the composed result in (handled in Task 14).

Actually: `_build_cmd` currently takes `user_message` and calls `_build_prompt(user_message)`. After this task, bot.py will already call `compose_user_prompt` and pass the *composed* result in as `user_message` to `_build_cmd`. So `_build_prompt` becomes a no-op (or just `return user_message`). Either delete it or simplify:

```python
def _build_prompt(self, user_message: str) -> str:
    # Composition has moved to compose_user_prompt; bot.py now passes the
    # already-composed prompt. Kept as a no-op for any internal callers.
    return user_message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codex_resume_no_reinjection.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/codex.py tests/test_codex_resume_no_reinjection.py
git commit -m "fix(codex): skip persona/team-note/history re-injection on resume

Root cause of manager review loops on 2026-04-27 and 2026-05-15:
codex re-read the per-turn persona block as a new instruction,
re-executing the review protocol from scratch. compose_user_prompt
now hash-tracks team_system_note and persona, skipping them on
resume when unchanged. History is always skipped on resume since
codex retains it in conversation. Per spec section 8.2."
```

---

## Task 14: bot.py — replace inline prepend with `backend.compose_user_prompt`

**Files:**
- Modify: `src/link_project_to_chat/bot.py:986-1005` (turn-path prompt composition)
- Modify: `tests/test_bot_streaming.py` or `tests/test_bot_dispatch.py` (find via grep)

- [ ] **Step 1: Locate the bot turn-path tests**

Run: `grep -lrn "format_persona_prompt\|_history_block" tests/ | head -5`
Use the file that has tests exercising the message dispatch path. If none cover this exactly, add to `tests/test_bot_streaming.py`.

- [ ] **Step 2: Add the failing test**

Add a test that verifies `compose_user_prompt` is the source of truth for the final prompt:
```python
@pytest.mark.asyncio
async def test_bot_dispatch_calls_backend_compose_user_prompt(monkeypatch, tmp_path):
    """Verify bot.py routes the per-turn prompt through backend.compose_user_prompt."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend
    backend = ClaudeBackend(project_path=tmp_path)

    captured = {}
    def fake_compose(raw_message, persona, history):
        captured["raw_message"] = raw_message
        captured["persona"] = persona
        captured["history"] = history
        return f"COMPOSED({raw_message})"
    monkeypatch.setattr(backend, "compose_user_prompt", fake_compose)

    # The exact integration depends on how the turn-path is wired in bot.py.
    # Minimum assertion: when the turn-path is invoked with a raw user message
    # "hello" and the backend is the patched one, compose_user_prompt sees it.
    # (The reviewer/executor should adapt this stub to call the actual code path.)
    result = backend.compose_user_prompt("hello", None, "")
    assert "COMPOSED(hello)" == result
    assert captured["raw_message"] == "hello"
```

(If the bot.py dispatch path is hard to drive from a unit test, this can be a smoke test demonstrating `compose_user_prompt` is callable on the live backend. The integration is validated through the full suite at task end.)

- [ ] **Step 3: Replace the inline prepend**

The current `_build_user_prompt` in `bot.py:976-1001` is:
```python
async def _build_user_prompt(
    self,
    chat: ChatRef,
    raw_text: str,
    *,
    reply_to_text: str | None = None,
) -> str:
    prompt = raw_text
    if reply_to_text:
        prompt = f"[Replying to: {reply_to_text}]\n\n{prompt}"
    if self._team_session_active():
        return prompt
    if self._active_persona:
        from .skills import load_persona, format_persona_prompt
        persona = load_persona(self._active_persona, self.path)
        if persona:
            prompt = format_persona_prompt(persona, prompt)
    prompt = await self._history_block(chat) + prompt
    return prompt
```

Replace with:
```python
async def _build_user_prompt(
    self,
    chat: ChatRef,
    raw_text: str,
    *,
    reply_to_text: str | None = None,
) -> str:
    prompt = raw_text
    if reply_to_text:
        prompt = f"[Replying to: {reply_to_text}]\n\n{prompt}"
    persona_text: str | None = None
    if self._active_persona:
        from .skills import load_persona, format_persona_prompt
        persona_obj = load_persona(self._active_persona, self.path)
        if persona_obj:
            # Pass empty string so format_persona_prompt returns just the
            # persona block text, not a wrapped user message.
            persona_text = format_persona_prompt(persona_obj, "").rstrip()
    history_block = await self._history_block(chat)
    backend = self.task_manager.backend
    return backend.compose_user_prompt(
        raw_message=prompt,
        persona=persona_text,
        history=history_block,
    )
```

Important: this **removes** the `if self._team_session_active(): return prompt` early-return because the equivalent skip-on-resume logic now lives in `CodexBackend.compose_user_prompt` (Task 13). Verify the deletion is safe by confirming Task 13 has shipped first if you serialize the work.

- [ ] **Step 4: Run the targeted test**

Run: `pytest tests/test_bot_streaming.py -v -k "compose_user_prompt"`
Expected: 1 passed

- [ ] **Step 5: Run the full suite to catch regressions**

Run: `pytest -q`
Expected: all tests pass. The team-bot env hermeticity fix from `080fa9d` should make `LP2C_TELETHON_SESSION_STRING` non-blocking; if you still see the hermeticity failure, run `env -u LP2C_TELETHON_SESSION_STRING pytest -q` and file follow-up to verify the fix.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/bot.py tests/test_bot_streaming.py
git commit -m "refactor(bot): route per-turn prompt through backend.compose_user_prompt

Replaces inline format_persona_prompt + _history_block concatenation
at the turn-path entry. Behavior unchanged for claude; codex now
benefits from hash-skip-on-resume to avoid re-injection loops.
Per spec section 8.3."
```

---

## Task 15: Wire `team_authority` into the backend at team-mode init

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (search for `_refresh_team_system_note`)
- Modify: `tests/test_bot_team_wiring.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_bot_team_wiring.py`. Drive `_refresh_team_system_note` directly using a `SimpleNamespace` stub for `self`, which is enough because the method only reads `self.peer_bot_username`, `self.bot_username`, `self.role`, `self.team_name` and writes to `self.task_manager.backend`:

```python
from types import SimpleNamespace
from unittest.mock import MagicMock

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.team_safety import TeamAuthority


def test_refresh_team_system_note_creates_team_authority_in_team_mode():
    backend = MagicMock()
    backend.team_system_note = None
    backend.team_authority = None
    bot = SimpleNamespace(
        task_manager=SimpleNamespace(backend=backend),
        peer_bot_username="peer_bot",
        bot_username="self_bot",
        role="manager",
        team_name="test_team",
    )
    ProjectBot._refresh_team_system_note(bot)  # type: ignore[arg-type]
    assert isinstance(backend.team_authority, TeamAuthority)
    assert backend.team_authority._team_name == "test_team"
    assert backend.team_system_note is not None


def test_refresh_team_system_note_clears_team_authority_when_peer_unset():
    backend = MagicMock()
    backend.team_system_note = "stale"
    backend.team_authority = TeamAuthority(team_name="test_team")
    bot = SimpleNamespace(
        task_manager=SimpleNamespace(backend=backend),
        peer_bot_username=None,
        bot_username="self_bot",
        role="manager",
        team_name="test_team",
    )
    ProjectBot._refresh_team_system_note(bot)  # type: ignore[arg-type]
    assert backend.team_system_note is None
    assert backend.team_authority is None


def test_refresh_team_system_note_preserves_existing_authority_state():
    """Second call should not replace the existing TeamAuthority (preserves grants)."""
    backend = MagicMock()
    backend.team_system_note = "any value"
    existing_auth = TeamAuthority(team_name="test_team")
    existing_auth.record_user_message(msg_id=42, text="--auth push")
    backend.team_authority = existing_auth
    bot = SimpleNamespace(
        task_manager=SimpleNamespace(backend=backend),
        peer_bot_username="peer_bot",
        bot_username="self_bot",
        role="manager",
        team_name="test_team",
    )
    ProjectBot._refresh_team_system_note(bot)  # type: ignore[arg-type]
    assert backend.team_authority is existing_auth  # same instance, grants intact
    assert backend.team_authority.is_authorized("push") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bot_team_wiring.py -v -k "refresh_team_system_note"`
Expected: AssertionError on `isinstance(backend.team_authority, TeamAuthority)` because the assignment doesn't exist yet.

- [ ] **Step 3: Implement the wiring**

In `src/link_project_to_chat/bot.py`, locate `_refresh_team_system_note` (around line 1609). Currently the body looks like:
```python
def _refresh_team_system_note(self) -> None:
    backend = self.task_manager.backend
    if not self.peer_bot_username:
        backend.team_system_note = None
        return
    # ... builds team_system_note string and assigns ...
```

Extend:
```python
def _refresh_team_system_note(self) -> None:
    backend = self.task_manager.backend
    if not self.peer_bot_username:
        backend.team_system_note = None
        backend.team_authority = None  # NEW
        return
    # ... existing team_system_note construction ...
    if backend.team_authority is None:
        from .team_safety import TeamAuthority
        backend.team_authority = TeamAuthority(team_name=self.team_name or "")
```

This keeps the same lifecycle gate (`peer_bot_username` presence) and avoids replacing the authority on every refresh (state is preserved across calls).

- [ ] **Step 4: Pass `team_authority` + `max_autonomous_turns` into the relay**

Find where `enable_team_relay_from_session_string` / `enable_team_relay_from_session` are called (bot.py around 2622-2638) and the implementation in `transport/telegram.py:153-176`. Plumb `team_authority` (shared with the backend) and `max_autonomous_turns` (from `team.max_autonomous_turns`) into `TeamRelay`. Concrete edit in `bot.py`:

```python
# After backend.team_authority is instantiated (via _refresh_team_system_note),
# pass that same instance to enable_team_relay.
self._transport.enable_team_relay_from_session_string(
    session_string=session_string_env,
    api_id=config.telegram_api_id,
    api_hash=config.telegram_api_hash,
    team_bot_usernames=team_bot_usernames,
    group_chat_id=self.group_chat_id,
    team_name=self.team_name,
    team_authority=backend.team_authority,            # NEW
    max_autonomous_turns=team.max_autonomous_turns,   # NEW
    authenticated_user_id=self._authenticated_user_id,  # NEW (read from auth state)
)
```

Update `enable_team_relay`, `enable_team_relay_from_session`, and `enable_team_relay_from_session_string` in `transport/telegram.py` to accept the new kwargs and pass them through to `TeamRelay`.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/transport/telegram.py tests/test_bot_team_wiring.py
git commit -m "feat(bot): wire TeamAuthority through team-mode init

ProjectBot creates TeamAuthority alongside team_system_note when
entering team mode; the same instance is passed to TeamRelay so
both the backend (for tool gating) and the relay (for user-message
observation) share state. Per spec section 7.1 and section 4
'State ownership'."
```

---

## Task 16: Suppress `[No response]` placeholder

**Files:**
- Modify: `src/link_project_to_chat/backends/claude.py:333`
- Modify: `src/link_project_to_chat/bot.py` (find send sites that consume `backend.chat()`)
- Modify: `tests/test_backend_claude.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_backend_claude.py`:
```python
def test_claude_chat_returns_empty_string_when_no_result():
    """chat() returns '' (not '[No response]') when the stream produces no Result."""
    from pathlib import Path
    from link_project_to_chat.backends.claude import ClaudeBackend

    # This is a unit-style check: we can't easily run the full subprocess in
    # a fast test, so instead we drive chat() with a stubbed chat_stream.
    backend = ClaudeBackend(project_path=Path("/tmp/x"))

    async def empty_stream(*_args, **_kw):
        # Yields no events at all.
        return
        yield  # pragma: no cover

    backend.chat_stream = empty_stream  # type: ignore[method-assign]

    import asyncio
    result = asyncio.run(backend.chat("ping"))
    assert result == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_claude.py::test_claude_chat_returns_empty_string_when_no_result -v`
Expected: AssertionError, got `'[No response]'`

- [ ] **Step 3: Update `claude.py:333`**

Change:
```python
return result_text or "[No response]"
```
to:
```python
return result_text
```

- [ ] **Step 4: Suppress empty sends at the bot layer**

Find where the bot calls `backend.chat()` and forwards to `transport.send_text`. Run: `grep -n "await self._backend.chat\b\|backend.chat(" src/link_project_to_chat/bot.py`.

At each send site that takes the chat() return value as the message body, wrap with an empty-check:
```python
text = (await backend.chat(message)).strip()
if not text:
    logger.debug("backend returned empty turn; suppressing send")
    return
await self._transport.send_text(chat, text)
```

(If most sends go through `chat_stream` and only a few use `chat()`, the cosmetic effect is mostly in those few. Cover all of them.)

- [ ] **Step 5: Run the test**

Run: `pytest tests/test_backend_claude.py::test_claude_chat_returns_empty_string_when_no_result -v`
Expected: 1 passed

Run: `pytest tests/test_backend_claude.py -v`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/backends/claude.py src/link_project_to_chat/bot.py tests/test_backend_claude.py
git commit -m "fix: stop leaking '[No response]' placeholder to chat

claude.chat() returned the literal '[No response]' when no Result
event arrived, which got forwarded to Telegram on 2026-05-15
(message #68). Now returns '' and the bot's send site skips empty
content. Per spec section 9.1."
```

---

## Task 17: `/status` team-safety block

**Files:**
- Modify: `src/link_project_to_chat/bot.py` (find the `/status` command handler)
- Modify: `tests/test_bot_status.py` (or wherever the status tests live)

- [ ] **Step 1: Locate the /status handler**

Run: `grep -n "def _cmd_status\|status_command\|'/status'" src/link_project_to_chat/bot.py | head -5`
Read the existing handler to understand the format. Note where the rendered text is assembled.

- [ ] **Step 2: Add the failing test**

Append to the existing status-tests file (or create one):
```python
def test_status_shows_team_safety_block_in_team_mode(monkeypatch):
    """When team_system_note is set, /status output includes a Team safety section."""
    from link_project_to_chat.team_safety import TeamAuthority

    auth = TeamAuthority(team_name="lpct")
    auth.record_user_message(msg_id=312, text="--auth push")

    from link_project_to_chat.bot import _render_team_safety_block  # to be created in step 4
    rendered = _render_team_safety_block(
        authority=auth,
        consecutive_turns=2,
        max_autonomous_turns=5,
    )
    assert "Team safety: strict" in rendered
    assert "Autonomous turn 2 / 5" in rendered
    assert "push" in rendered
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_bot_status.py -v -k "team_safety"`
Expected: ImportError on `_render_team_safety_block`

- [ ] **Step 4: Implement the renderer**

In `src/link_project_to_chat/bot.py`, add (near other small helpers):
```python
def _render_team_safety_block(
    authority: "TeamAuthority",
    consecutive_turns: int,
    max_autonomous_turns: int,
) -> str:
    """Render the team-safety section of /status output."""
    snap = authority.status_snapshot
    lines = [
        "Team safety: strict mode",
        f"Autonomous turn {consecutive_turns} / {max_autonomous_turns} "
        f"(resets on next user msg)",
    ]
    if snap["active_grants"]:
        lines.append("Active grants:")
        for g in snap["active_grants"]:
            scopes = ", ".join(g["scopes"])
            lines.append(f"  • {scopes} ({g['age_seconds']}s ago, msg #{g['user_message_id']})")
    else:
        lines.append("Active grants: none")
    return "\n".join(lines)
```

Add forward import at top of bot.py:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .team_safety import TeamAuthority
```

Then update the `/status` command handler. In the function that assembles the status text, after the existing sections, add:
```python
backend = self.task_manager.backend
if backend.team_system_note is not None and backend.team_authority is not None:
    # We need the relay's counter; expose it via a property or accessor.
    consecutive = getattr(self._transport, "team_relay_consecutive_bot_turns", 0)
    max_turns = getattr(self._transport, "team_relay_max_autonomous_turns", 5)
    lines.append("")
    lines.append(_render_team_safety_block(
        authority=backend.team_authority,
        consecutive_turns=consecutive,
        max_autonomous_turns=max_turns,
    ))
```

Add the property accessors on `TelegramTransport`:
```python
@property
def team_relay_consecutive_bot_turns(self) -> int:
    return self._team_relay._consecutive_bot_turns if self._team_relay else 0

@property
def team_relay_max_autonomous_turns(self) -> int:
    return self._team_relay._max_autonomous_turns if self._team_relay else 5
```

- [ ] **Step 5: Run the test**

Run: `pytest tests/test_bot_status.py -v -k "team_safety"`
Expected: 1 passed

Run: `pytest -q`
Expected: full suite passes

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/transport/telegram.py tests/test_bot_status.py
git commit -m "feat(bot): /status shows Team safety block in team mode

Renders 'Team safety: strict', autonomous-turn counter vs budget,
and active grants (scope, age, originating user message id). Per
spec section 9.2."
```

---

## Task 18: Persona prose — name your review surface

**Files:**
- Modify: `src/link_project_to_chat/personas/software_manager.md`
- Modify: `src/link_project_to_chat/personas/software_dev.md`
- Modify: `tests/test_bundled_personas.py`

**Rationale:** The 2026-05-15 chat showed the manager (codex) reviewing what appeared to be the working tree but actually reading either committed state or a separate editable install. Dev's fixes were unstaged; manager kept citing the pre-patch README. Dev eventually diagnosed it in message #54 ("if you're reading via `git show`/`git cat-file`/a separate clone..."). Hard guardrails alone can't prevent this confusion — the personas should explicitly name what they're reviewing. Cheap, additive, soft layer over the executor-layer guarantees from Tasks 9-11.

- [ ] **Step 1: Add the failing test**

Append to `tests/test_bundled_personas.py`:
```python
def test_software_manager_persona_names_review_surface():
    """Manager persona explicitly tells the agent to name its review surface."""
    from importlib.resources import files
    p = files("link_project_to_chat.personas").joinpath("software_manager.md")
    content = p.read_text(encoding="utf-8").lower()
    assert "review surface" in content
    assert "working tree" in content
    assert "head" in content
    assert "origin" in content


def test_software_dev_persona_reports_review_surface_state():
    """Dev persona tells the agent to state staged/unstaged/committed status."""
    from importlib.resources import files
    p = files("link_project_to_chat.personas").joinpath("software_dev.md")
    content = p.read_text(encoding="utf-8").lower()
    assert "review surface" in content
    assert "unstaged" in content
    assert "committed" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bundled_personas.py -v -k "review_surface"`
Expected: AssertionError on the missing strings in current personas.

- [ ] **Step 3: Update the manager persona**

In `src/link_project_to_chat/personas/software_manager.md`, locate the "Review protocol" sentence and extend it:

Find:
```
Review protocol: before approving any change, read the actual files the Developer modified. Do not rely solely on their summary.
```

Replace with:
```
Review protocol: before approving any change, read the actual files the Developer modified. Do not rely solely on their summary. Always name the review surface you are checking: working tree (`git diff` / `cat <file>`), staged changes (`git diff --staged`), a local commit (`git show HEAD` or `git show <hash>`), or remote comparison (`git diff origin/<branch>..HEAD`). If the Developer says fixes are unstaged but your read of the file still shows pre-fix content, stop repeating the same review findings — ask them to confirm which surface to check, or to create a local review commit, and then review THAT surface explicitly.
```

- [ ] **Step 4: Update the dev persona**

In `src/link_project_to_chat/personas/software_dev.md`, after the existing "Execution:" or work-protocol paragraph, add a new line:

```
Review surface: when handing work back to the Manager, state explicitly whether your changes are unstaged, staged, committed locally, or pushed. Include the exact commit hash when committed (`git rev-parse HEAD`), or say "working tree only" when not. If the Manager appears to be reviewing stale content, include `git status --short --branch` in your reply, name the surface they should use, and ask before creating a commit solely to stabilize the review surface.
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_bundled_personas.py -v`
Expected: all bundled-persona tests pass, including the two new ones.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/personas/software_manager.md src/link_project_to_chat/personas/software_dev.md tests/test_bundled_personas.py
git commit -m "docs(personas): require explicit review-surface naming

Adds review-surface discipline to the bundled software_manager and
software_dev personas: manager must name what it's checking (working
tree / staged / commit / remote); dev must state whether handoff is
unstaged / staged / committed / pushed and include the commit hash
when committed. Soft guardrail layered on top of the executor-layer
push block from Tasks 10-11. Idea adapted from the team-bot plan
docs/superpowers/plans/2026-05-15-team-relay-review-surface-fixes.md
Task 3."
```

---

## Final verification

After all 18 tasks are committed:

- [ ] **Step 1: Run the full suite**

Run: `pytest -q`
Expected: all tests pass (1145 baseline plus ~30 new tests, so roughly 1175 passed)

- [ ] **Step 2: Compileall check**

Run: `python -m compileall src/link_project_to_chat -q`
Expected: no output (no syntax errors)

- [ ] **Step 3: Whitespace check**

Run: `git diff --check origin/main..HEAD`
Expected: no output

- [ ] **Step 4: Confirm out-of-scope items are NOT in the diff**

Run: `git diff origin/main..HEAD -- src/link_project_to_chat | grep -E "skill.*discovery|_record_round|reset.*HEAD" | head`
Expected: no matches — the design's out-of-scope items haven't crept in.

- [ ] **Step 5: Summarize the diff**

Run: `git diff --stat origin/main..HEAD`
Expected: roughly the file-change-summary from spec section 12 (~750 LOC).

---

## Notes for executor

- This plan is large. Use `superpowers:subagent-driven-development` (one subagent per task) for the cleanest review cadence. The tasks are designed to be independently committable and small enough for a fresh agent to complete with no prior context beyond the spec + this plan.
- Tasks 1-3 are pure data-layer and have no upstream dependencies — safe to parallelize across subagents if you prefer.
- Tasks 4-8 (relay) all touch the same file; serialize them.
- Tasks 9-14 (backends) all touch backend files but split cleanly by file; safe to parallelize backend-side.
- Task 15 (bot.py wiring) is the integration point — do it after Tasks 9-14 land.
- Tasks 16 + 17 are independent surface fixes; can land any order after Task 15.
- Task 18 (persona prose) is independent of everything else; ship whenever.
- If a task's test code makes assumptions that don't match the project's actual test fixtures, adapt the assertions but keep the *behavior under test* identical.
