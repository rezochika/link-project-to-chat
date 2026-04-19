# `/create_team` Manager Command — Design Spec

**Status:** Approved via brainstorming
**Date:** 2026-04-17
**Supersedes (in part):** `2026-04-17-dual-agent-ai-team-design.md` — specifically its "No automated Telegram group creation" non-goal, and its "two project entries with a shared `group_chat_id`" data model.

---

## 1. Overview

Add a `/create_team` command to the manager bot that, in a single flow, creates two Telegram bots via BotFather, clones a GitHub repo, creates a Telegram supergroup, wires both bots into it, writes config, and auto-starts them. After one conversation in the manager DM, the user can walk into the new group and start chatting with the pair.

The command is parallel to `/create_project`: same GitHub browse/URL repo picker, same BotFather-via-Telethon automation, but ending in a team (`teams.<prefix>` in config) rather than a single project (`projects.<prefix>`).

## 2. Goals & non-goals

**Goals**
- One manager command drives the entire setup: two bots → repo clone → group → wiring → auto-start.
- Zero manual config.json editing; zero manual BotFather clicks.
- Clean separation between solo projects (`projects`) and teams (`teams`) in config.
- Reuse existing infrastructure: `BotFatherClient`, `GitHubClient`, repo browse/URL states, persona discovery, project-start helper.

**Non-goals (v1)**
- No `start-team` CLI. Teams are only auto-started once, at creation.
- No `/teams` manager command (list/start/stop/edit). Parallel to `/projects` is a follow-up.
- No post-creation editing (rename, swap persona, replace a bot).
- No programmatic BotFather bot deletion on failure. `/deletebot` requires interactive confirmation; we document manual cleanup.
- No group-title customization — auto-derived as `"<prefix> team"`.
- Exactly 2 bots per team. Role labels hardcoded to `"manager"` / `"dev"` (matches `group_filters.py`).
- No migration for existing dual-agent-team configs. Assumes no real-world config has `group_mode=true` on `ProjectConfig`; dual-agent-team is unreleased. Clean break.

## 3. Decisions driving this design

Outcomes of the brainstorming Q&A on 2026-04-17:

| # | Question | Decision |
|---|----------|----------|
| 1 | Relationship to `/create_project` | New standalone `/create_team` command, parallel to `/create_project` |
| 2 | Project source | Full same flow — GitHub browse + paste URL + clone |
| 3 | Bot names | Auto-derived from project prefix (e.g., `acme_mgr_claude_bot` / `acme_dev_claude_bot`); collision retry with `_1`/`_2`/… up to 5 |
| 4 | Persona selection | Inline button picker, two steps, from discovered personas |
| 5 | Role labels | Keep `"manager"` / `"dev"` — load-bearing for @mention routing |
| 6 | Auto-start | Yes, both bots auto-start when the conversation completes |
| 7 | Code structure | Dedicated module `manager/telegram_group.py` + progressive status message edits |
| 8 | Config shape | New top-level `teams` namespace; solo `projects` untouched. `ProjectConfig` loses `group_mode`/`group_chat_id`/`role` (dead for solo bots). `active_persona` stays — still used for solo-bot `/persona` persistence |

## 4. User flow

```
You → @YourManagerBot: /create_team

Bot: How would you like to pick the repo?
     [Browse my GitHub repos] [Paste a URL]
You: taps Browse → picks <owner>/<repo>

Bot: Short project name? (used for bot usernames)
You: acme

Bot: Pick manager-role persona:
     [software_manager] [developer] [designer] ...
You: taps developer

Bot: Pick dev-role persona:
     [software_dev] [tester] [designer] ...
You: taps tester

Bot: ⟳ Creating bot 1 (@acme_mgr_claude_bot)...
     (edits) ✓ Bot 1 | ⟳ Creating bot 2 (@acme_dev_claude_bot)...
     (edits) ✓ Bots | ⟳ Disabling privacy mode...
     (edits) ✓ Bots ready | ⟳ Cloning repo...
     (edits) ✓ Cloned | ⟳ Creating group "acme team"...
     (edits) ✓ Group | ⟳ Adding + promoting bots...
     (edits) ✓ Group wired | ⟳ Starting both bots...
     (edits) ✓ Team ready. Open the "acme team" group to start chatting.
```

**Username collision:** if `<prefix>_mgr_claude_bot` is taken, retry with `<prefix>_mgr_1_claude_bot`, `<prefix>_mgr_2_…`, up to 5 tries per bot. Fail with a clear error if all five are taken.

**Inviting the requester:** after group creation, Telethon invites the user who ran the command (`update.effective_user.username`). If they have no public username, skip silently.

## 5. Architecture

### 5.1 New module: `src/link_project_to_chat/manager/telegram_group.py`

Thin async wrapper over Telethon raw TL requests. No class; module-level functions. Caller (manager handler) owns the `TelegramClient` lifecycle.

```python
async def create_supergroup(client, title: str) -> int:
    """channels.CreateChannelRequest(title=title, about='', megagroup=True)
    Returns the -100... chat_id from the response."""

async def add_bot(client, chat_id: int, bot_username: str) -> None:
    """channels.InviteToChannelRequest(channel, users=[get_entity(bot_username)])"""

async def promote_admin(client, chat_id: int, bot_username: str) -> None:
    """channels.EditAdminRequest with ChatAdminRights(
        post_messages=True, delete_messages=True, invite_users=True,
        ban_users=True, pin_messages=True, manage_call=False)"""

async def invite_user(client, chat_id: int, username: str) -> None:
    """Same pattern as add_bot; used once for the requester."""
```

All four functions catch `FloodWaitError`: if `seconds <= 30`, sleep and retry once; else raise. Caller translates to the partial-failure report.

### 5.2 Augment `src/link_project_to_chat/botfather.py`

Add one method to `BotFatherClient`:

```python
async def disable_privacy(self, bot_username: str) -> None:
    """Send /setprivacy to BotFather, select bot, tap Disable.
    Parses BotFather's confirmation reply."""
```

Needed because group mode reads non-command messages for @mention routing. Parsing follows the same pattern as `create_bot`.

### 5.3 New handler `_on_create_team` in `src/link_project_to_chat/manager/bot.py`

Own `ConversationHandler` with its own state enum:

```
CREATE_TEAM_SOURCE        # pick GitHub browse / paste URL
CREATE_TEAM_REPO_LIST     # paginated repo list
CREATE_TEAM_REPO_URL      # URL input
CREATE_TEAM_NAME          # project prefix
CREATE_TEAM_PERSONA_MGR   # pick manager persona (inline buttons)
CREATE_TEAM_PERSONA_DEV   # pick dev persona (inline buttons)
```

**Reuses helpers, not states.** The existing `_show_repo_page`, repo-URL validation, and clone logic from `/create_project` are extracted into state-neutral helpers (no dependency on a specific state enum) and called by both conversations. Each conversation owns its own states; the helpers operate on `ctx.user_data` with an explicit key (`create` vs `create_team`) passed in. Avoids duplicated GitHub code while keeping the two flows isolated.

**Persona picker keyboard** built from the existing persona-discovery used by `/persona` (per-project + app-global + Claude Code user skills, same priority order).

### 5.4 Auto-start

Reuses the subprocess-spawn path that `/projects` Start button already uses. A new team-aware start helper takes `(team_name: str, role: str)`, looks up the team, synthesizes the primitives `ProjectBot` already accepts (path, token, `group_mode=True`, group_chat_id, role, active_persona), and spawns. Called twice, once per role.

### 5.5 Dependency graph

```
manager/bot.py (_on_create_team)
    ├── pre-flight checks (config.py reads)
    ├── GitHubClient.list_repos / validate_repo_url / clone_repo
    ├── BotFatherClient.create_bot()         × 2  (with 5-retry username loop)
    ├── BotFatherClient.disable_privacy()    × 2  (non-fatal; warn on error)
    ├── telegram_group.create_supergroup()
    ├── telegram_group.add_bot()             × 2
    ├── *** config.patch_team() — COMMIT POINT ***
    ├── telegram_group.promote_admin()       × 2  (non-fatal)
    ├── telegram_group.invite_user()         × 1  (non-fatal)
    └── team-aware start helper              × 2  (non-fatal)
```

## 6. Data model

### 6.1 New top-level `teams` key in `config.json`

```json
{
  "projects": {
    "my_solo_bot": { "path": "...", "telegram_bot_token": "..." }
  },
  "teams": {
    "acme": {
      "path": "/home/user/projects/acme",
      "group_chat_id": -1001234567890,
      "bots": {
        "manager": { "telegram_bot_token": "<token1>", "active_persona": "developer" },
        "dev":     { "telegram_bot_token": "<token2>", "active_persona": "tester" }
      }
    }
  }
}
```

### 6.2 New dataclasses in `src/link_project_to_chat/config.py`

```python
@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None

@dataclass
class TeamConfig:
    path: str
    group_chat_id: int
    bots: dict[str, TeamBotConfig]  # keys: "manager", "dev"

@dataclass
class Config:
    # existing fields unchanged
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)  # NEW
```

### 6.3 `ProjectConfig` field removals

Delete the following fields from `ProjectConfig` (added by dual-agent-team, now migrated to `TeamConfig`):
- `group_mode`
- `group_chat_id`
- `role`

Loader no longer reads them; saver no longer writes them. No runtime migration — dual-agent-team is unreleased.

**`active_persona` stays on `ProjectConfig`.** It's load-bearing for the solo-bot `/persona` command which persists the selected persona to config via `patch_project` ([bot.py:643](src/link_project_to_chat/bot.py:643)); removing it would regress that feature. `TeamConfig` has its own `active_persona` per bot-role, set at team creation.

### 6.4 New helpers in `config.py`

```python
def patch_team(team_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Atomic read-modify-write on the `teams` dict. None values remove keys.

    Top-level replacement only: passing {"bots": {...}} replaces the entire `bots`
    dict (not a deep merge). Callers that need to update one bot should read the
    current team, modify the bots dict, and write the whole dict back.
    Mirrors patch_project's top-level-replace semantics."""

def load_teams(path: Path = DEFAULT_CONFIG) -> dict[str, TeamConfig]:
    """Convenience; mirrors load_sessions."""
```

`patch_team` uses the same `_patch_json` + `_atomic_write` mechanism as `patch_project`.

### 6.5 Ephemeral conversation state

`ctx.user_data["create_team"]`:
```python
{
  "source": "github" | "url",
  "repo": RepoInfo,
  "project_prefix": str,
  "bot1_token": str,
  "bot1_username": str,
  "bot2_token": str,
  "bot2_username": str,
  "group_chat_id": int,
  "persona_mgr": str,
  "persona_dev": str,
  "status_msg_id": int,   # for progressive edits
}
```

### 6.6 Naming conventions

- **Team key:** `<prefix>` (user-supplied, lowercase, ascii word characters only; validated at `CREATE_TEAM_NAME`).
- **Bot usernames:** `<prefix>_mgr_claude_bot` / `<prefix>_dev_claude_bot`; on collision append `_1` / `_2` / … up to 5.
- **Group title:** `"<prefix> team"`.
- **Persona values:** filename stems (e.g., `"developer"`, not `"developer.md"`) — matches existing convention.

## 7. Error handling

**Principle: fail-forward with structured reporting.** BotFather actions aren't reversible programmatically; auto-rollback is fragile. Do risky work first, commit config last, report what succeeded and what needs manual cleanup.

### 7.1 Pre-flight (no side effects)

1. Telethon configured: `config.telegram_api_id`, `config.telegram_api_hash`, and the Telethon session file all present → else "run `/setup` first".
2. GitHub auth available: `gh` CLI authenticated OR `config.github_pat` set → reuse `/create_project`'s existing check.
3. `teams["<prefix>"]` doesn't exist → "team `<prefix>` already configured".
4. Legacy-safety: `projects["<prefix>_mgr"]` and `projects["<prefix>_dev"]` don't exist (defends against pre-D configs) → "those project names are taken".

### 7.2 Commit point

Config is written via `patch_team` **only after** all of: both bots created, both tokens obtained, repo cloned, group created, both bots joined. Everything after the commit (promote-to-admin, invite-user, auto-start) is non-fatal.

### 7.3 Failure classification

| Step | On failure | Orphans on failure |
|------|-----------|---------------------|
| Bot 1 creation (incl. 5-retry username loop) | Abort | — |
| Bot 1 privacy disable | Warn, continue | — |
| Bot 2 creation | Abort | Bot 1 |
| Bot 2 privacy disable | Warn, continue | — |
| Repo clone | Abort | Bot 1, Bot 2 |
| Group creation | Abort | Bot 1, Bot 2, repo |
| Add bot 1 or 2 to group | Abort | Bot 1, Bot 2, repo, group |
| **→ `config.patch_team` commit** | Abort (atomic; all-or-nothing from `_atomic_write`) | Bot 1, Bot 2, repo, group |
| Promote to admin | Warn, continue | — |
| Invite requester | Warn, continue | — |
| Auto-start | Warn, continue | — |

### 7.4 Flood-wait policy

Every Telethon call catches `FloodWaitError`:
- `seconds <= 30` → `asyncio.sleep(seconds + 1)` and retry once.
- `seconds > 30` → abort with duration surfaced in the error message.

### 7.5 Partial-failure report

Sent as a new message (not a status-edit) when any step aborts:

```
✗ Team creation failed at: "Create group"
Error: FloodWaitError: wait 180s

Completed (needs manual cleanup):
  - Bot @acme_mgr_claude_bot (delete via BotFather /deletebot)
  - Bot @acme_dev_claude_bot (delete via BotFather /deletebot)
  - Directory /home/user/projects/acme (remove if not needed)

Config not saved. Safe to retry with a different prefix once flood lifts.
```

### 7.6 `/cancel`

Aborts conversation at any state. If side effects already happened, show the same partial-failure report.

### 7.7 Re-run semantics

Pre-flight checks catch same-prefix re-runs before any network call. Recovery: manually clean up BotFather/Telegram orphans, then retry with the same prefix OR pick a new prefix.

## 8. Testing

### 8.1 Unit tests (deterministic, no network)

**`tests/test_config.py` additions:**
- `test_save_and_load_team` — `TeamConfig` roundtrip
- `test_teams_coexist_with_projects` — config with both loads + saves
- `test_patch_team_creates_entry`
- `test_patch_team_partial_update`
- `test_load_config_missing_teams_key_is_empty_dict`
- **Delete:** `test_project_config_group_fields_default_false_none`, `test_project_config_group_fields_roundtrip` (fields removed)

**New `tests/test_telegram_group.py`** (uses `unittest.mock.AsyncMock` for the Telethon client):
- `test_create_supergroup_returns_negative_chat_id`
- `test_add_bot_invokes_invite_to_channel`
- `test_promote_admin_sets_correct_rights`
- `test_flood_wait_under_30s_retries_once`
- `test_flood_wait_over_30s_aborts`

**New `tests/test_botfather_disable_privacy.py`** (or extend `test_botfather.py` if it exists):
- `test_disable_privacy_sends_correct_dialog`
- `test_disable_privacy_parses_confirmation`

**`tests/test_manager_create_team.py`** (or extend the existing manager test file):
- `test_preflight_rejects_existing_team_name`
- `test_preflight_rejects_legacy_project_name_collision`
- `test_config_write_shape` — given a populated `ctx.user_data["create_team"]` fixture, assert `patch_team` called with the correct dict
- `test_username_collision_retries_5_times_then_fails`

### 8.2 Manual test checklist (acceptance criteria)

- [ ] Pre-flight fails cleanly when Telethon not configured
- [ ] Pre-flight fails cleanly when team prefix already exists
- [ ] Happy path: two bots created, group created, configs written, both bots auto-started, can send message in group
- [ ] Username collision: pick a prefix where `<prefix>_mgr_claude_bot` is known taken; verify `_1` retry kicks in
- [ ] `/cancel` mid-flow: partial-failure report lists orphans correctly
- [ ] Both bots are group admins; both can read non-command messages (privacy disabled)
- [ ] `@{manager_bot}` responds with manager persona; it can @mention `@{dev_bot}` and dev responds with dev persona

## 9. Files changed (summary)

**New:**
- `src/link_project_to_chat/manager/telegram_group.py`
- `tests/test_telegram_group.py`
- `tests/test_manager_create_team.py` (or extend existing)
- `docs/superpowers/specs/2026-04-17-create-team-command-design.md` (this file)

**Modified:**
- `src/link_project_to_chat/config.py` — add `TeamConfig`, `TeamBotConfig`, `teams` field, `patch_team`, `load_teams`; remove group-mode fields from `ProjectConfig`; update load/save
- `src/link_project_to_chat/botfather.py` — add `disable_privacy` method
- `src/link_project_to_chat/manager/bot.py` — add `_on_create_team` handler, new ConversationHandler, new state enum, pre-flight checks, reuse repo helpers, auto-start call
- `src/link_project_to_chat/project_bot.py` — callers that used to read `ProjectConfig.group_mode` / etc. now receive those as explicit init args from the team-start helper (already the pattern; just removing the config-side plumbing)
- `tests/test_config.py` — add team tests, delete dead `ProjectConfig` group-field tests

## 10. Phased implementation order

Each step ends green (tests pass, `ruff`/`mypy` clean):

1. **Config schema refactor** — add `TeamConfig`/`TeamBotConfig`, `patch_team`, `load_teams`; delete dead `ProjectConfig` fields; update tests. *No feature behavior yet.*
2. **Telegram group module** — `manager/telegram_group.py` + its tests. Unit tests only.
3. **BotFather privacy disable** — `disable_privacy` + its tests.
4. **Manager handler** — `_on_create_team` + ConversationHandler wiring + handler tests.
5. **Auto-start wiring** — team-aware helper + integration with existing start path.
6. **Manual QA** — run the acceptance checklist end-to-end on Windows.

## 11. Future work (not in scope)

- `start-team <name>` CLI (restart-after-reboot story)
- `/teams` manager command: list / start / stop / logs / edit, paralleling `/projects`
- Post-creation team editing (rename, swap persona, replace bot)
- Arbitrary role labels (beyond `manager`/`dev`)
- >2 bots per team
- Group-title customization at creation
