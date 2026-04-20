# Dual-Agent Team — Merged Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the full dual-agent Manager+Dev team feature as one coherent unit: canonical `TeamConfig` data model, group-mode routing fed from team runtime, Phase-2 safety rails (round-limit / `/halt` / usage-cap auto-pause), first-message `group_chat_id` auto-capture, Telegram group API helpers, BotFather privacy-toggle helper, and the interactive `/create_team` manager command.

**Architecture:** Unify the two source plans around Plan 2's `TeamConfig` model (which Plan 1's per-project `group_mode`/`group_chat_id`/`role` were a pre-merger rough draft for). The Phase 1 code already shipped in `main` stays in place with one rewiring step — `ProjectBot` receives `team_name` + `role` + `TeamConfig` snapshot at construction time instead of reading from `ProjectConfig` fields. Plan 2's `/create_team` subsumes Plan 1's unimplemented `/create_agent_team`.

**Tech Stack:** Python 3.12, `python-telegram-bot`, `click`, `pytest` (asyncio mode), Telethon for user-API (supergroup creation), `gh` CLI (reused from existing repo picker), BotFatherClient (existing).

**Source plans this merges:**
- `docs/superpowers/plans/2026-04-17-dual-agent-ai-team.md` (Plan 1, Tasks 1–15)
- `docs/superpowers/plans/2026-04-17-create-team-command.md` (Plan 2, Tasks 1–20)

**Currently shipped in `main`** (do not re-implement):
- `src/link_project_to_chat/group_filters.py` — routing predicates (Plan 1 Task 2) — unchanged
- `src/link_project_to_chat/personas/software_manager.md`, `software_dev.md` (Plan 1 Task 5) — unchanged
- `ProjectConfig.active_persona` persistence (Plan 1 Task 4) — unchanged, kept on `ProjectConfig` for the 1:1 flow
- `ProjectBot.group_mode` branch in `_on_text` (Plan 1 Task 3) — rewired by this plan
- `ProjectConfig.group_mode` / `group_chat_id` / `role` (Plan 1 Task 1) — **removed** by Phase A / Task A5 (migrated to `TeamConfig`)

**Referencing convention:** Tasks whose source-plan definition is both long (>100 lines of TDD steps) and carries over **without any merge-induced change** are marked `REUSE(<plan>, Task N)` with a one-paragraph summary and exit criteria reproduced inline. For all other tasks the full code and steps are inlined.

---

## File Structure

| File | Action | Responsibility | Source |
|------|--------|----------------|--------|
| `src/link_project_to_chat/config.py` | Modify | Add `TeamConfig`, `TeamBotConfig`, `Config.teams`, load/save, `patch_team`, `load_teams`; remove `group_mode`/`group_chat_id`/`role` from `ProjectConfig` | Plan 2 T1–5 |
| `src/link_project_to_chat/cli.py` | Modify | Add `start --team NAME --role ROLE` flags; drop `group_mode=proj.group_mode` | Plan 2 T5, T6 |
| `src/link_project_to_chat/bot.py` | Modify | Rewire `ProjectBot.__init__` to accept `team_name`/`role` + team snapshot; wire group-state registry; add `/halt` `/resume`; auto-capture `group_chat_id` into `TeamConfig` | Plan 1 T8–11, T13 + merge rewiring |
| `src/link_project_to_chat/group_state.py` | Create | In-memory per-group `halted`, `bot_to_bot_rounds`, `last_user_activity_ts` + `GroupStateRegistry` | Plan 1 T7 |
| `src/link_project_to_chat/claude_client.py` | Modify | Detect usage-cap error → `ClaudeUsageCapError` | Plan 1 T10 |
| `src/link_project_to_chat/process_manager.py` | Modify | `start_team(team_name)` iterates bots and spawns per-role `ProjectBot` | Plan 2 T7 |
| `src/link_project_to_chat/telegram_group.py` | Create | `create_supergroup`, `add_bot`, `promote_admin`, `invite_user`, flood-wait retry | Plan 2 T8–10 |
| `src/link_project_to_chat/botfather.py` | Modify | Add `disable_privacy(bot_username)` | Plan 2 T11 |
| `src/link_project_to_chat/manager/bot.py` | Modify | `/create_team` ConversationHandler; persona picker helper; shared repo-picker extraction | Plan 2 T12–19 |
| `tests/*` | Modify/Create | TDD for every task above | both plans |

---

## Phase A — Config refactor (Plan 2 Tasks 1–5, adapted)

Adds `TeamConfig` and removes the stub fields Plan 1 shipped on `ProjectConfig`.

### Task A1: `TeamBotConfig`, `TeamConfig`, `Config.teams` dataclasses

REUSE(Plan 2, Task 1). Reproduced inline — short enough.

**Files:** Modify `src/link_project_to_chat/config.py` and `tests/test_config.py`.

- [ ] **Step 1: Write the failing default test**

Append to `tests/test_config.py`:

```python
def test_team_config_default_empty_dict(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"projects": {}}))
    config = load_config(p)
    assert config.teams == {}
```

Add `TeamConfig`, `TeamBotConfig` to the existing config import block at the top of `tests/test_config.py`.

- [ ] **Step 2: Run — expect fail**

`pytest tests/test_config.py::test_team_config_default_empty_dict -v` → `ImportError: cannot import name 'TeamBotConfig'`.

- [ ] **Step 3: Add the dataclasses to `config.py`**

Insert after the existing `ProjectConfig` dataclass, before `class Config`:

```python
@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
```

Add `teams` to `Config`:

```python
@dataclass
class Config:
    # ...existing fields unchanged...
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)
```

- [ ] **Step 4: Run — expect pass**
- [ ] **Step 5: Commit**: `git commit -m "feat(config): add TeamConfig, TeamBotConfig, Config.teams"`

### Task A2: Load teams from `config.json`

REUSE(Plan 2, Task 2). Reproduced inline.

**Files:** `src/link_project_to_chat/config.py` (`load_config`), `tests/test_config.py`.

- [ ] **Step 1: Failing roundtrip test**

```python
def test_load_config_teams(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {},
        "teams": {
            "acme": {
                "path": "/home/user/acme",
                "group_chat_id": -1001234567890,
                "bots": {
                    "manager": {"telegram_bot_token": "t1", "active_persona": "software_manager"},
                    "dev":     {"telegram_bot_token": "t2", "active_persona": "software_dev"},
                },
            }
        },
    }))
    config = load_config(p)
    team = config.teams["acme"]
    assert team.path == "/home/user/acme"
    assert team.group_chat_id == -1001234567890
    assert team.bots["manager"].telegram_bot_token == "t1"
    assert team.bots["manager"].active_persona == "software_manager"
    assert team.bots["dev"].telegram_bot_token == "t2"
```

- [ ] **Step 2: Run — expect fail** (`assert 'acme' in {}`)

- [ ] **Step 3: Add loader**

In `load_config`, after the projects loading loop, append:

```python
for name, team in raw.get("teams", {}).items():
    config.teams[name] = TeamConfig(
        path=team["path"],
        group_chat_id=team["group_chat_id"],
        bots={
            role: TeamBotConfig(
                telegram_bot_token=b.get("telegram_bot_token", ""),
                active_persona=b.get("active_persona"),
            )
            for role, b in team.get("bots", {}).items()
        },
    )
```

- [ ] **Step 4: Run — expect pass**
- [ ] **Step 5: Commit**: `git commit -m "feat(config): load teams from config.json"`

### Task A3: Save teams to `config.json`

REUSE(Plan 2, Task 3). **Implement per Plan 2, Task 3, lines 173–266**, reproduced in full there. In short:

- Add failing save+load roundtrip test
- Wire the serialization branch inside `_save_config_unlocked`:

```python
if c.teams:
    data["teams"] = {
        name: {
            "path": t.path,
            "group_chat_id": t.group_chat_id,
            "bots": {
                role: {"telegram_bot_token": b.telegram_bot_token, "active_persona": b.active_persona}
                for role, b in t.bots.items()
            },
        }
        for name, t in c.teams.items()
    }
```

Commit: `feat(config): save teams to config.json`.

### Task A4: `patch_team()` and `load_teams()` helpers

REUSE(Plan 2, Task 4). **Implement per Plan 2, Task 4, lines 267–398.**

- `patch_team(team_name, updates: dict)` — atomic read-modify-write through the lock (mirrors existing `patch_project`)
- `load_teams(config_path)` — convenience wrapper returning `dict[str, TeamConfig]`

Exit criteria: the two new helpers have their own unit tests (patch partial-update, patch nonexistent team, load returns snapshot). Commit: `feat(config): add patch_team and load_teams helpers`.

### Task A5: Remove dead fields from `ProjectConfig` and call sites

REUSE(Plan 2, Task 5) — reproduced inline with one merge-delta. Source: Plan 2 lines 399–486.

**Merge delta:** Plan 2's Task 5 deletes `group_mode=proj.group_mode` at `bot.py:1412` and `cli.py:330`. Since we also add a `--team/--role` CLI flag in Task B1 and rewire `ProjectBot` in Task B2, the `group_mode` kwarg on `run_bot` itself stays alive during Phase A but is only ever set to its default `False` after this task lands. Phase B starts passing `team_name` / `role` / `team` to construct group bots.

**Files:** `src/link_project_to_chat/config.py`, `src/link_project_to_chat/cli.py:330`, `src/link_project_to_chat/bot.py:1412`, `tests/test_config.py`.

- [ ] **Step 1: Delete the two obsolete tests**

In `tests/test_config.py`, delete:
- `test_project_config_group_fields_default_false_none`
- `test_project_config_group_fields_roundtrip`

- [ ] **Step 2: Run suite** — expect the rest to pass.

- [ ] **Step 3: Remove fields from `ProjectConfig`**

Delete from the dataclass:

```python
    group_mode: bool = False
    group_chat_id: int | None = None
    role: str | None = None
```

Delete the matching loader lines in `load_config`:

```python
                group_mode=proj.get("group_mode", False),
                group_chat_id=proj.get("group_chat_id"),
                role=proj.get("role"),
```

Delete the save branches for those three fields in `_save_config_unlocked`. **Keep `active_persona` unchanged.**

- [ ] **Step 4: Drop `group_mode=proj.group_mode` at `cli.py:330`**
- [ ] **Step 5: Drop `group_mode=proj.group_mode` at `bot.py:1412`**
- [ ] **Step 6: `pytest -x` — all green**
- [ ] **Step 7: Commit**: `refactor(config): remove dead group_mode/group_chat_id/role from ProjectConfig (migrated to TeamConfig)`

---

## Phase B — Bot runtime wiring

Teaches `ProjectBot` to take team membership from constructor args (populated by CLI + `ProcessManager.start_team`) instead of from removed `ProjectConfig` fields.

### Task B1: CLI `start --team NAME --role ROLE`

REUSE(Plan 2, Task 6). **Implement per Plan 2, Task 6, lines 488–625.**

Summary: add mutually-exclusive click options `--team` and `--role` to `start` command; when both supplied, bypass the existing per-project loop and call `ProcessManager.start_team(team_name, role)` instead. Validation: `--team` without `--role` errors out, and `--role` must be one of `manager|dev`. Tests exist for both happy path (spawns one ProjectBot) and error paths (missing role, unknown team).

Commit: `feat(cli): add start --team NAME --role ROLE`.

### Task B2: `ProcessManager.start_team(team_name, role | None)`

REUSE(Plan 2, Task 7). **Implement per Plan 2, Task 7, lines 626–752.**

Summary: looks up `config.teams[team_name]`, and for each `(role, bot)` in `team.bots` (or just the single requested role), constructs a `ProjectBot` with `path=team.path`, `token=bot.telegram_bot_token`, `active_persona=bot.active_persona`, **plus new kwargs** `team_name=team_name`, `role=role`, `group_chat_id=team.group_chat_id` — see Task B3 for the constructor change.

Tests: spawns both bots when role is omitted; spawns one bot when role is supplied; raises `KeyError` on unknown team/role.

Commit: `feat(process): add ProcessManager.start_team`.

### Task B3: Thread `team_name`/`role`/`group_chat_id` through `ProjectBot`

**New merge task.** `ProjectBot` currently takes `group_mode: bool`. Replace that signal with team-membership args and derive `group_mode` internally.

**Files:** Modify `src/link_project_to_chat/bot.py` (`ProjectBot.__init__`, `_build_project_bot` helper, `run_bot`), `tests/` (touch existing test that asserts group_mode-based branches).

- [ ] **Step 1: Failing test**

Append to `tests/test_bot_team_wiring.py` (create the file):

```python
from pathlib import Path
from link_project_to_chat.bot import ProjectBot


def test_project_bot_derives_group_mode_from_team_args(tmp_path):
    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-1001234567890,
    )
    assert bot.group_mode is True
    assert bot.team_name == "acme"
    assert bot.role == "manager"
    assert bot.group_chat_id == -1001234567890


def test_project_bot_solo_mode_when_no_team(tmp_path):
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    assert bot.group_mode is False
    assert bot.team_name is None
    assert bot.role is None
```

- [ ] **Step 2: Run — expect fail** (`TypeError: unexpected keyword argument 'team_name'`).

- [ ] **Step 3: Update `ProjectBot.__init__` signature**

Replace the existing `group_mode: bool = False` kwarg with:

```python
team_name: str | None = None,
role: str | None = None,
group_chat_id: int | None = None,
```

In the `__init__` body:

```python
self.team_name = team_name
self.role = role
self.group_chat_id = group_chat_id
self.group_mode = team_name is not None  # derived, not stored independently
```

- [ ] **Step 4: Update callers**

- `run_bot` (function definition + its body) — accept the same three kwargs, pass through to `ProjectBot(...)`, delete any `group_mode=...` kwargs.
- `_build_project_bot` (or equivalent internal factory) — same.
- CLI `start` (when `--team/--role` present): populate the three kwargs from `TeamConfig`.
- `ProcessManager.start_team` (Task B2): populate the three kwargs from `TeamConfig`.

- [ ] **Step 5: Run failing test — expect pass**
- [ ] **Step 6: `pytest -x` — all green**
- [ ] **Step 7: Commit**: `refactor(bot): derive group_mode from team_name; thread role and group_chat_id`

### Task B4: Enforce `group_chat_id` in `_on_text`

**New merge task — trivial but essential.** Currently the `group_mode` branch in `_on_text` (bot.py:314) accepts group messages regardless of which group they came from. Now that `group_chat_id` is known at construction time, reject messages from other groups.

- [ ] **Step 1: Failing test**

Append to `tests/test_bot_team_wiring.py`:

```python
import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_group_mode_rejects_wrong_chat_id(tmp_path):
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    update = MagicMock()
    update.effective_message.chat_id = -100_222  # wrong group
    update.effective_message.text = "@acme_manager hi"
    ctx = MagicMock()
    # Expect: early return, no reply_text call
    await bot._on_text(update, ctx)
    update.effective_message.reply_text.assert_not_called()
```

- [ ] **Step 2: Run — expect fail** (currently no chat_id check).

- [ ] **Step 3: Add the guard**

In `_on_text`, inside the `if self.group_mode:` branch (before the `is_from_self` check), add:

```python
if self.group_chat_id is not None and msg.chat_id != self.group_chat_id:
    return  # message from a different group — silently ignore
```

When `group_chat_id is None` (not yet captured — see Task D1), no rejection happens; that's how the first-message capture flow works.

- [ ] **Step 4: Run — expect pass**
- [ ] **Step 5: Commit**: `feat(bot): silently ignore group messages from wrong chat_id`

---

## Phase C — Safety rails (Plan 1 Phase 2)

In-memory per-group state. Does not touch `TeamConfig` — process-restart is a reset, per design.

### Task C1: `group_state.py` module

REUSE(Plan 1, Task 7). **Implement per Plan 1, Task 7, lines 769–910.**

Module exports:

```python
@dataclass
class GroupState:
    halted: bool = False
    bot_to_bot_rounds: int = 0
    last_user_activity_ts: float = 0.0

class GroupStateRegistry:
    def get(self, chat_id: int) -> GroupState: ...
    def reset_on_user_activity(self, chat_id: int) -> None: ...
    def increment_round(self, chat_id: int) -> int: ...  # returns new count
    def halt(self, chat_id: int) -> None: ...
    def resume(self, chat_id: int) -> None: ...
```

Full pytest coverage of the state transitions (default state on first `get`, round increment, user-activity resets count and clears halt, halt / resume idempotence).

Commit: `feat(group): add in-memory GroupStateRegistry for round-limit and halt`.

### Task C2: Wire registry into `_on_text`

REUSE(Plan 1, Task 8). **Implement per Plan 1, Task 8, lines 911–995.**

Merge deltas:
- Registry lives on `ProjectBot` as `self._group_state = GroupStateRegistry()` (not shared across process because we have two ProjectBot instances per team — but they'll read the same in-memory dict only if we share one registry **per process**; leave it per-bot-instance — each bot tracks its own view, simpler and correct because the round count is about "messages this bot sent in response to the other bot").
- Increment round whenever a bot answers a message that `is_from_other_bot` returns true for.
- Reset to 0 + clear halt when a trusted-user message arrives.
- On hitting the round cap (default 20), call `registry.halt(chat_id)` and post `"Auto-paused after 20 bot-to-bot rounds. Send any message to resume."` to the group once, then early-return.

Commit: `feat(bot): enforce 20-round bot-to-bot cap with auto-halt`.

### Task C3: `/halt` and `/resume` commands

REUSE(Plan 1, Task 9). **Implement per Plan 1, Task 9, lines 996–1056.**

Both registered alongside existing `/` commands in the group handler list, gated on trusted-user + `group_mode=True`. `/halt` → `halt(chat_id)` + post `"Halted. Send any message or /resume to continue."`. `/resume` → `resume(chat_id)` + post `"Resumed."`.

Commit: `feat(bot): add /halt and /resume for trusted user`.

### Task C4: `ClaudeUsageCapError` detection in `claude_client.py`

REUSE(Plan 1, Task 10). **Implement per Plan 1, Task 10, lines 1057–1161.**

Summary: new exception class `ClaudeUsageCapError`; new helper `_detect_usage_cap(stderr: str) -> bool` matching the Claude-Max cap error pattern (captured in fixtures under `tests/fixtures/claude_usage_cap_stderr.txt`); stderr in `_read_events` re-raised as `ClaudeUsageCapError` instead of generic `Error`.

Commit: `feat(claude): detect Max usage-cap errors as ClaudeUsageCapError`.

### Task C5: Auto-pause + 30-minute probe on cap

REUSE(Plan 1, Task 11). **Implement per Plan 1, Task 11, lines 1162–1253.**

Summary: on `ClaudeUsageCapError`, post `"Hit Max usage cap. Pausing until reset."` to the group, call `registry.halt(chat_id)`, and schedule an `asyncio.create_task(_probe_cap_clears(...))` that sleeps 30 min, fires a trivial `"echo hi"` request, and if it succeeds, calls `registry.resume(chat_id)` + posts `"Cap cleared. Team resumed."`. If still capped, reschedule another 30-min probe.

Commit: `feat(bot): auto-pause team on Claude Max cap, probe every 30min`.

### Task C6: Phase-C manual verification

Adapted exit criterion from Plan 1 Task 12.

- [ ] Deliberately give the bots a vague task and step away; verify the 20-round cap catches the loop and posts the halt message.
- [ ] Inject the usage-cap stderr into a fake `ClaudeClient` (test harness) and verify the pause-and-probe cycle resumes the team when the injected cap clears.
- [ ] Commit a short section in `docs/superpowers/specs/2026-04-17-dual-agent-ai-team-design.md` noting that Phase 2 passed verification.

---

## Phase D — First-message `group_chat_id` auto-capture

Plan 1's Task 13, rewritten to write into `TeamConfig.group_chat_id` via `patch_team()` instead of `ProjectConfig.group_chat_id`.

### Task D1: Capture `group_chat_id` on first trusted-user group message

**Files:** Modify `src/link_project_to_chat/bot.py`. Touch `tests/test_bot_team_wiring.py`.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_first_group_message_captures_chat_id(tmp_path, monkeypatch):
    # team exists in config with group_chat_id=0 (sentinel "not captured yet")
    calls = []
    def fake_patch_team(name, updates):
        calls.append((name, updates))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,  # 0 means not yet captured
    )
    update = _make_update(chat_id=-100_999, sender_id=TRUSTED_USER_ID, text="@acme_manager hi")
    await bot._on_text(update, MagicMock())
    assert calls == [("acme", {"group_chat_id": -100_999})]
    assert bot.group_chat_id == -100_999  # in-memory also updated
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Add capture logic**

In `_on_text`, inside `if self.group_mode:`, *after* the `is_from_self` check, *before* the wrong-chat_id guard from Task B4:

```python
if self.group_chat_id in (0, None) and self._auth(update.effective_user):
    # First group message from trusted user — capture this chat_id into the team config.
    new_chat_id = msg.chat_id
    patch_team(self.team_name, {"group_chat_id": new_chat_id})
    self.group_chat_id = new_chat_id
    # Fall through so this same message still gets processed normally.
```

Also update the `wrong chat_id` guard from Task B4 so it only fires when `self.group_chat_id not in (0, None)` and mismatches.

- [ ] **Step 4: Run — expect pass**
- [ ] **Step 5: Update `TeamConfig.group_chat_id` default-at-creation**

`/create_team` (Phase F) will write `group_chat_id=0` when creating a new team row (sentinel). Also update `load_config` / the `TeamConfig` dataclass to accept `group_chat_id: int = 0`.

Tests: add roundtrip test that a team with `group_chat_id=0` loads and saves cleanly.

- [ ] **Step 6: Commit**: `feat(bot): auto-capture group_chat_id into TeamConfig on first trusted-user message`

---

## Phase E — Telegram group API + BotFather privacy

### Task E1: `telegram_group.py` — `create_supergroup`

REUSE(Plan 2, Task 8). **Implement per Plan 2, Task 8, lines 753–855.** Uses Telethon (user API, not bot API) to create a supergroup. Exports:

```python
async def create_supergroup(client: TelegramClient, title: str) -> int:
    """Return the new supergroup's chat_id (negative, -100...)"""
```

Tests: mock the Telethon client; verify a `CreateChannelRequest` is sent with `megagroup=True`, `title=title`; chat_id is returned correctly.

Commit: `feat(telegram): add create_supergroup helper`.

### Task E2: `telegram_group.py` — `add_bot`, `promote_admin`, `invite_user`

REUSE(Plan 2, Task 9). **Implement per Plan 2, Task 9, lines 856–981.**

All three are async wrappers over Telethon requests. `promote_admin` requires `change_info=True, invite_users=True, pin_messages=True` at minimum. `invite_user` takes a `@username` and resolves through `client.get_entity`.

Commit: `feat(telegram): add add_bot, promote_admin, invite_user helpers`.

### Task E3: Flood-wait retry coverage

REUSE(Plan 2, Task 10). **Implement per Plan 2, Task 10, lines 982–1037.** Wrap each helper in a decorator that catches `FloodWaitError`, sleeps for `e.seconds`, and retries once. Test with an injected exception.

Commit: `feat(telegram): retry once on FloodWaitError`.

### Task E4: `BotFatherClient.disable_privacy(bot_username)`

REUSE(Plan 2, Task 11). **Implement per Plan 2, Task 11, lines 1038–1126.** Drives BotFather's `/mybots` → `<bot>` → `Bot Settings` → `Group Privacy` → `Turn off` flow. Tests mock the BotFather session with scripted replies.

Commit: `feat(botfather): add disable_privacy helper`.

---

## Phase F — Manager `/create_team` command

### Task F1: Persona picker keyboard helper

REUSE(Plan 2, Task 12). **Implement per Plan 2, Task 12, lines 1127–1203.** Shared helper that reads `~/.link-project-to-chat/personas/` + built-in personas and renders a 2-column `InlineKeyboardMarkup`. Used for both manager and dev persona selection. Tests render keyboards for 0, 1, 5 personas.

Commit: `feat(manager): add persona-picker keyboard helper`.

### Task F2: `_on_create_team` — pre-flight checks

REUSE(Plan 2, Task 13). **Implement per Plan 2, Task 13, lines 1204–1338.** On `/create_team` entry: verify `BotFatherClient` is authenticated, `gh` CLI present, Telethon session exists, and `--team` name is slug-valid and not already in config. If any fail, post a diagnostic message and short-circuit the conversation.

Commit: `feat(manager): pre-flight checks for /create_team`.

### Task F3: `_on_create_team` — state enum + entry + source picker

REUSE(Plan 2, Task 14). **Implement per Plan 2, Task 14, lines 1339–1405.** `CreateTeamState` enum: `SOURCE, REPO_LIST, REPO_URL, NAME, MGR_PERSONA, DEV_PERSONA, USERNAME_RETRY, EXECUTING, DONE`. Entry handler prompts "From GitHub / Paste URL / New empty repo" inline keyboard, sets state to `SOURCE`.

Commit: `feat(manager): /create_team conversation entry + source picker`.

### Task F4: Extract `_show_repo_page` + URL-validate into shared helpers

REUSE(Plan 2, Task 15). **Implement per Plan 2, Task 15, lines 1406–1490.** Factor `_show_repo_page` (currently private to `/create_project`) into a module-level helper so `/create_team` can reuse it. Same for URL validation. Keep the existing `/create_project` entry untouched — it calls the new helper.

**Merge note:** the existing `_show_repo_page` was already modified by recent commit `5cf3216` to use `gh api /user/repos` for org repos. That change carries over automatically — no action needed here beyond the extraction.

Commit: `refactor(manager): extract repo picker + URL validator for /create_team reuse`.

### Task F5: `_on_create_team` — name + persona states

REUSE(Plan 2, Task 16). **Implement per Plan 2, Task 16, lines 1491–1560.** After repo selection: prompt for project name (default derived from repo name); after name, prompt for manager persona (Task F1 keyboard, default `software_manager`); after manager persona, prompt for dev persona (default `software_dev`).

Commit: `feat(manager): /create_team name + persona state handlers`.

### Task F6: `_on_create_team` — username-collision retry

REUSE(Plan 2, Task 17). **Implement per Plan 2, Task 17, lines 1561–1651.** When BotFather says `Sorry, this username is already taken.`, re-prompt with a retry keyboard (or auto-append a numeric suffix and retry once). Helper is isolated so `/create_project` can also use it in the future.

Commit: `feat(manager): username-collision retry for /create_team`.

### Task F7: `_on_create_team` — orchestrator

REUSE(Plan 2, Task 18). **Implement per Plan 2, Task 18, lines 1652–1815.** This is the meat of the command. Pseudocode:

```
1. Create manager bot via BotFatherClient (username: {team}_manager_bot)
2. Create dev bot similarly
3. BotFatherClient.disable_privacy on each
4. Create supergroup via telegram_group.create_supergroup(f"{team} dev team")
5. add_bot(manager_bot); add_bot(dev_bot); promote_admin on both
6. invite_user(trusted_user_username) into the group
7. Create project folder + docs/ src/ tests/
8. patch_team(team, TeamConfig(path, group_chat_id=supergroup_id, bots={
       manager: TeamBotConfig(token=mgr_token, active_persona=mgr_persona),
       dev:     TeamBotConfig(token=dev_token, active_persona=dev_persona),
   }))
9. ProcessManager.start_team(team)  # spawns both bots
10. Post "Team '{team}' ready. Check the group." back to the manager chat.
```

Rollback on partial failure: each step wrapped in try/except with reverse-order cleanup (delete bot via BotFather, delete supergroup, delete config entry, rmtree project folder). Tests exercise each rollback path with an injected failure.

Commit: `feat(manager): /create_team orchestrator`.

### Task F8: Wire the ConversationHandler in `build()`

REUSE(Plan 2, Task 19). **Implement per Plan 2, Task 19, lines 1816–1881.** Register the handler in `manager/bot.py`'s `build()` alongside the existing `/create_project` handler. Trusted-user filter. Fallback `/cancel` handler that aborts mid-conversation cleanly.

Commit: `feat(manager): wire /create_team ConversationHandler`.

---

## Phase G — End-to-end QA

### Task G1: Manual E2E — create_team happy path

Adapted from Plan 1 Task 15 + Plan 2 Task 20.

- [ ] Pre-flight: config is clean (`config.teams == {}`), BotFather session valid, `gh` authenticated, Telethon session valid.
- [ ] In manager-bot 1:1 chat: `/create_team smoketest`
- [ ] Pick "From GitHub" → choose an empty scratch repo
- [ ] Accept default persona picks (`software_manager` / `software_dev`)
- [ ] Verify in Telegram:
  - Two new bots exist (`smoketest_manager_bot`, `smoketest_dev_bot`)
  - A new supergroup named "smoketest dev team" exists and you are invited
  - Both bots are members and admins
  - Group privacy is off on both bots (verify via `/mybots` in BotFather)
- [ ] Verify on disk:
  - `~/.link-project-to-chat/config.json` has a `smoketest` team entry with correct paths, tokens, personas, and `group_chat_id` matching the supergroup
  - Project folder exists with `docs/`, `src/`, `tests/` subdirs
- [ ] Verify processes:
  - `systemctl status link-project-to-chat` shows both new bots as child processes (or log lines show their init)
- [ ] Send `@smoketest_manager_bot Build a 3-endpoint todo REST API` in the group
- [ ] Watch the manager → dev → manager loop; interrupt with a message before round 20 to verify round-counter reset
- [ ] Step away and let it hit 20 rounds; verify auto-halt message
- [ ] `/resume` — verify loop resumes
- [ ] `/halt` — verify loop stops, user still gets responses
- [ ] Inspect `src/`, `tests/`, `docs/` in the project folder — real code + docs written by the bots

### Task G2: Rollback smoke test

- [ ] Temporarily break `disable_privacy` (raise from inside the BotFather client).
- [ ] Run `/create_team smoketest2` — expect orchestrator to roll back cleanly: no stray bot accounts, no orphan supergroup, no config entry, no project folder.
- [ ] Restore `disable_privacy`.

### Task G3: Update the design docs with "implemented" status

- [ ] In `docs/superpowers/specs/2026-04-17-dual-agent-ai-team-design.md` and `docs/superpowers/specs/2026-04-17-create-team-command-design.md`, add a `**Status: implemented — <date>**` line at the top.
- [ ] Commit: `docs: mark dual-agent team and /create_team as implemented`.

---

## Post-implementation

**Explicit non-goals retained from Plan 1:**
- No automated Telegram group creation from scratch without user consent (we do create, but via Telethon on the user's session — the user authorized)
- No per-group ACL (solo-use assumption)
- No voice input in group mode
- No multi-team orchestration per group — one team per supergroup

**Known deferred items (future plan):**
- `/delete_team` command (current workaround: use existing `/delete_project` twice + manual group cleanup)
- Bot-to-bot rounds configurable per team (currently hard-coded 20)
- Team-level model override (current workaround: set `model` on each `TeamBotConfig`)

---

## Self-review

**Spec coverage** (maps merged requirements back to tasks):

| Requirement | Source | Task(s) |
|---|---|---|
| `TeamConfig` data model | Plan 2 §2 | A1–A4 |
| Remove dead fields | Plan 2 Task 5 | A5 |
| `group_mode` from runtime args | (merge glue) | B1–B4 |
| Group message routing | Plan 1 Task 2 (shipped) | — |
| Bundled personas | Plan 1 Task 5 (shipped) | — |
| `active_persona` persistence | Plan 1 Task 4 (shipped) | — |
| Round-limit + `/halt` | Plan 1 Tasks 7–9 | C1–C3 |
| Claude usage-cap auto-pause | Plan 1 Tasks 10–11 | C4–C5 |
| Auto-capture `group_chat_id` | Plan 1 Task 13 | D1 |
| Telegram group API | Plan 2 Tasks 8–10 | E1–E3 |
| BotFather privacy toggle | Plan 2 Task 11 | E4 |
| `/create_team` full flow | Plan 2 Tasks 12–19 | F1–F8 |
| QA + rollback | both | G1–G3 |

**Placeholder scan:** searched for "TBD", "TODO", "later", "similar to" — none left in this document.

**Type consistency:** `TeamConfig.group_chat_id` is `int` (sentinel `0` for "not captured yet"). `TeamBotConfig.telegram_bot_token` is `str`. `GroupStateRegistry.get()` returns `GroupState`. `ClaudeUsageCapError` extends `Exception`. Method names match across tasks.

**Execution note:** the bot this plan modifies is the same bot the developer is using to communicate. After any task that rebuilds/restarts the service (all of them, effectively), there will be a brief gap before the next message is answered. Commit frequency per task protects against losing the channel mid-refactor.
