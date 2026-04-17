# `/create_team` Manager Command — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/create_team` manager-bot command that, in one conversation, creates two Telegram bots via BotFather, clones a GitHub repo, creates a supergroup via Telethon, writes a new top-level `teams` namespace in `config.json`, and auto-starts both bots.

**Architecture:** New top-level `teams` config key with `TeamConfig`/`TeamBotConfig` dataclasses; a thin `manager/telegram_group.py` module wrapping Telethon raw TL requests; a new `_on_create_team` `ConversationHandler` in the manager bot reusing `/create_project`'s repo picker via extracted helpers; `start` CLI gains `--team`/`--role` flags so the existing `ProcessManager` subprocess path boots team bots without a new command.

**Tech Stack:** Python 3.11+, `python-telegram-bot` (conversation handlers), `telethon` (raw TL requests for group creation), `click` (CLI), `pytest` + `unittest.mock.AsyncMock` for tests.

**Spec:** `docs/superpowers/specs/2026-04-17-create-team-command-design.md`

---

## Prerequisites

- Work on branch `dual-agent-team` (the spec's base branch).
- Main checkout has uncommitted Windows-compat fixes in `config.py` and `test_config.py` — land or stash them before starting this plan so `_atomic_write` uses `os.replace` consistently across tasks.

---

## Task 1: Config dataclasses — `TeamBotConfig`, `TeamConfig`, `Config.teams`

**Files:**
- Modify: `src/link_project_to_chat/config.py`

- [ ] **Step 1.1: Write the failing test for defaults**

Append to `tests/test_config.py`:

```python
def test_team_config_default_empty_dict(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"projects": {}}))
    config = load_config(p)
    assert config.teams == {}
```

Also add `TeamConfig`, `TeamBotConfig` to the import line at the top of `tests/test_config.py`:

```python
from link_project_to_chat.config import (
    _atomic_write,
    Config,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    # ...existing imports...
)
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_team_config_default_empty_dict -v`
Expected: FAIL with `ImportError: cannot import name 'TeamBotConfig'` or similar.

- [ ] **Step 1.3: Add the dataclasses to `config.py`**

Insert after the existing `ProjectConfig` dataclass (around line 37) and before `class Config`:

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

- [ ] **Step 1.4: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_team_config_default_empty_dict -v`
Expected: PASS.

- [ ] **Step 1.5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add TeamConfig, TeamBotConfig, Config.teams"
```

---

## Task 2: Config load for teams

**Files:**
- Modify: `src/link_project_to_chat/config.py:96-133` (the `load_config` function)

- [ ] **Step 2.1: Write the failing roundtrip test**

Append to `tests/test_config.py`:

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
                    "manager": {"telegram_bot_token": "t1", "active_persona": "developer"},
                    "dev": {"telegram_bot_token": "t2", "active_persona": "tester"},
                },
            }
        },
    }))
    config = load_config(p)
    assert "acme" in config.teams
    team = config.teams["acme"]
    assert team.path == "/home/user/acme"
    assert team.group_chat_id == -1001234567890
    assert team.bots["manager"].telegram_bot_token == "t1"
    assert team.bots["manager"].active_persona == "developer"
    assert team.bots["dev"].telegram_bot_token == "t2"
    assert team.bots["dev"].active_persona == "tester"
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_load_config_teams -v`
Expected: FAIL with `assert 'acme' in {}` (teams dict empty because loader ignores it).

- [ ] **Step 2.3: Add teams loading to `load_config`**

In `config.py`, inside `load_config`, after the `for name, proj in raw.get("projects", {}).items(): ...` loop, append:

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

- [ ] **Step 2.4: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_load_config_teams -v`
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): load teams from config.json"
```

---

## Task 3: Config save for teams

**Files:**
- Modify: `src/link_project_to_chat/config.py:145-246` (the `_save_config_unlocked` function)

- [ ] **Step 3.1: Write the failing save+load roundtrip test**

Append to `tests/test_config.py`:

```python
def test_save_and_load_team(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/home/user/acme",
                group_chat_id=-1001234567890,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="developer"),
                    "dev": TeamBotConfig(telegram_bot_token="t2", active_persona="tester"),
                },
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    team = loaded.teams["acme"]
    assert team.path == "/home/user/acme"
    assert team.group_chat_id == -1001234567890
    assert team.bots["manager"].telegram_bot_token == "t1"
    assert team.bots["dev"].active_persona == "tester"


def test_teams_coexist_with_projects(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={"solo": ProjectConfig(path="/a", telegram_bot_token="tx")},
        teams={
            "acme": TeamConfig(
                path="/b",
                group_chat_id=-100,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert "solo" in loaded.projects
    assert "acme" in loaded.teams
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/test_config.py::test_save_and_load_team tests/test_config.py::test_teams_coexist_with_projects -v`
Expected: FAIL — the saver doesn't emit a `teams` key, so reloaded `config.teams` is empty.

- [ ] **Step 3.3: Add teams save logic to `_save_config_unlocked`**

In `config.py`, inside `_save_config_unlocked`, after the projects-merge block (just before `raw["projects"] = ...`), insert:

```python
    # Merge teams
    existing_teams: dict = raw.get("teams", {})
    for name, team in config.teams.items():
        entry = existing_teams.get(name, {})
        entry["path"] = team.path
        entry["group_chat_id"] = team.group_chat_id
        entry["bots"] = {
            role: {
                "telegram_bot_token": b.telegram_bot_token,
                **({"active_persona": b.active_persona} if b.active_persona else {}),
            }
            for role, b in team.bots.items()
        }
        existing_teams[name] = entry
    raw["teams"] = {k: v for k, v in existing_teams.items() if k in config.teams}
    if not raw["teams"]:
        raw.pop("teams", None)
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `pytest tests/test_config.py::test_save_and_load_team tests/test_config.py::test_teams_coexist_with_projects -v`
Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): save teams to config.json, coexist with projects"
```

---

## Task 4: `patch_team` + `load_teams` helpers

**Files:**
- Modify: `src/link_project_to_chat/config.py` (add helpers after the existing `patch_project`)
- Modify: `tests/test_config.py`

- [ ] **Step 4.1: Write failing `patch_team` tests**

Append to `tests/test_config.py`:

```python
def test_patch_team_creates_entry(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/home/user/acme",
            "group_chat_id": -1001,
            "bots": {"manager": {"telegram_bot_token": "t1"}},
        },
        p,
    )
    raw = json.loads(p.read_text())
    assert raw["teams"]["acme"]["path"] == "/home/user/acme"
    assert raw["teams"]["acme"]["group_chat_id"] == -1001
    assert raw["teams"]["acme"]["bots"]["manager"]["telegram_bot_token"] == "t1"


def test_patch_team_replaces_at_top_level(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team("acme", {"path": "/a", "group_chat_id": -1, "bots": {"manager": {"telegram_bot_token": "t1"}}}, p)
    patch_team("acme", {"bots": {"dev": {"telegram_bot_token": "t2"}}}, p)
    raw = json.loads(p.read_text())
    # Entire bots dict replaced; path and group_chat_id preserved from first call
    assert raw["teams"]["acme"]["bots"] == {"dev": {"telegram_bot_token": "t2"}}
    assert raw["teams"]["acme"]["path"] == "/a"


def test_patch_team_none_removes_key(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team("acme", {"path": "/a", "group_chat_id": -1}, p)
    patch_team("acme", {"path": None}, p)
    raw = json.loads(p.read_text())
    assert "path" not in raw["teams"]["acme"]


def test_load_teams_helper(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/a",
            "group_chat_id": -1,
            "bots": {"manager": {"telegram_bot_token": "t1", "active_persona": "developer"}},
        },
        p,
    )
    teams = load_teams(p)
    assert teams["acme"].bots["manager"].active_persona == "developer"


def test_load_teams_missing_file_returns_empty(tmp_path: Path):
    assert load_teams(tmp_path / "nope.json") == {}
```

Add `patch_team, load_teams` to the imports at the top of `tests/test_config.py`.

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k "patch_team or load_teams" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 4.3: Add `patch_team` and `load_teams` to `config.py`**

Append after `patch_project` (around line 312):

```python
def patch_team(team_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Update specific fields on a team entry. None values remove the key.

    Top-level replacement only: passing {"bots": {...}} replaces the entire
    `bots` dict (not a deep merge). Callers that need to update one bot must
    read the current team, modify the bots dict, and write it back whole.
    """
    def _patch(raw: dict) -> None:
        team = raw.setdefault("teams", {}).setdefault(team_name, {})
        for k, v in fields.items():
            if v is None:
                team.pop(k, None)
            else:
                team[k] = v
    _patch_json(_patch, path)


def load_teams(path: Path = DEFAULT_CONFIG) -> dict[str, TeamConfig]:
    """Load all team entries. Returns empty dict if the file is missing or invalid."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {
                name: TeamConfig(
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
                for name, team in raw.get("teams", {}).items()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return {}
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -k "patch_team or load_teams" -v`
Expected: 5 PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add patch_team and load_teams helpers"
```

---

## Task 5: Remove dead `group_mode`, `group_chat_id`, `role` fields from `ProjectConfig`

Removes `group_mode`, `group_chat_id`, `role` from `ProjectConfig` (migrated to `TeamConfig`). **Keeps `active_persona`** — it's still used by the solo-bot `/persona` persistence feature.

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `src/link_project_to_chat/cli.py:330`
- Modify: `src/link_project_to_chat/bot.py:1412`
- Modify: `tests/test_config.py` (delete two now-obsolete tests)

- [ ] **Step 5.1: Delete the two obsolete tests**

Open `tests/test_config.py`. Delete:
- `test_project_config_group_fields_default_false_none` (lines 430-442)
- `test_project_config_group_fields_roundtrip` (lines 445-463)

- [ ] **Step 5.2: Run the suite, expect the deleted tests to no longer run and the rest to pass**

Run: `pytest tests/test_config.py -v`
Expected: all remaining tests PASS (the deleted two are simply gone).

- [ ] **Step 5.3: Remove the three fields from `ProjectConfig`**

In `config.py`, from the `ProjectConfig` dataclass, delete these three lines:

```python
    group_mode: bool = False
    group_chat_id: int | None = None
    role: str | None = None  # "manager" or "dev" when group_mode=true
```

Keep `active_persona`. Also in `load_config` (around lines 128-130), delete the three corresponding loader lines:

```python
                group_mode=proj.get("group_mode", False),
                group_chat_id=proj.get("group_chat_id"),
                role=proj.get("role"),
```

And in `_save_config_unlocked` (around lines 230-240), delete the save branches for `group_mode`, `group_chat_id`, `role`. Leave `active_persona`'s save logic untouched.

- [ ] **Step 5.4: Update `cli.py:330`**

Open `src/link_project_to_chat/cli.py`. Find:

```python
            group_mode=proj.group_mode,
            active_persona=proj.active_persona,
```

Replace with:

```python
            active_persona=proj.active_persona,
```

(Deletes the `group_mode=proj.group_mode` line entirely. `run_bot` defaults `group_mode` to `False`.)

- [ ] **Step 5.5: Update `bot.py:1412`**

Open `src/link_project_to_chat/bot.py`. Find the call site (around line 1412):

```python
            group_mode=proj.group_mode,
            active_persona=proj.active_persona,
```

Replace with:

```python
            active_persona=proj.active_persona,
```

(Same deletion.)

- [ ] **Step 5.6: Run the full suite**

Run: `pytest -x`
Expected: all tests PASS.

- [ ] **Step 5.7: Commit**

```bash
git add src/link_project_to_chat/config.py src/link_project_to_chat/cli.py src/link_project_to_chat/bot.py tests/test_config.py
git commit -m "refactor(config): remove dead group_mode/group_chat_id/role from ProjectConfig (migrated to TeamConfig)"
```

---

## Task 6: CLI `start --team NAME --role ROLE` flags

Enables `link-project-to-chat start --team acme --role manager` as the subprocess path used by auto-start. Solo `--project NAME` remains unchanged.

**Files:**
- Modify: `src/link_project_to_chat/cli.py` (add flags to `start`, handle the team branch)
- Modify: `tests/test_cli.py`

- [ ] **Step 6.1: Write failing CLI test for team flags**

In `tests/test_cli.py`, add:

```python
def test_start_team_and_role_invokes_run_bot_with_team_primitives(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="developer"),
                    "dev": TeamBotConfig(telegram_bot_token="t2", active_persona="tester"),
                },
            )
        }
    )
    save_config(config, cfg_path)

    calls = []
    def fake_run_bot(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(cli, "run_bot", fake_run_bot)

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"])
    assert result.exit_code == 0, result.output
    assert calls, "run_bot should have been called"
    _, kwargs = calls[0]
    assert kwargs["group_mode"] is True
    assert kwargs["active_persona"] == "developer"
    assert kwargs["token"] == "t1" or args[2] == "t1"  # third positional = token
```

(Adjust assertion if `run_bot` takes positional args — inspect `bot.py:1340` signature.)

- [ ] **Step 6.2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_start_team_and_role_invokes_run_bot_with_team_primitives -v`
Expected: FAIL with `No such option: --team`.

- [ ] **Step 6.3: Add `--team` and `--role` flags to `start`**

In `cli.py`, find the `@main.command()` decorator for `start` (around line 260). Add flags:

```python
@click.option("--team", default=None, help="Start a team bot (mutually exclusive with --project)")
@click.option("--role", default=None, type=click.Choice(["manager", "dev"]), help="Which team bot role to start")
```

In the `start` function body, add a team branch BEFORE the existing `--project` branch:

```python
    if team:
        if not role:
            raise SystemExit("--role is required when --team is given")
        if team not in config.teams:
            raise SystemExit(f"Team '{team}' not found in config.")
        t = config.teams[team]
        if role not in t.bots:
            raise SystemExit(f"Role '{role}' not in team '{team}'. Known roles: {list(t.bots)}")
        bot_cfg = t.bots[role]
        effective_usernames = config.allowed_usernames
        effective_trusted_ids = config.trusted_user_ids
        run_bot(
            f"{team}_{role}",
            Path(t.path),
            bot_cfg.telegram_bot_token,
            allowed_usernames=effective_usernames,
            trusted_user_ids=effective_trusted_ids,
            transcriber=transcriber,
            synthesizer=synthesizer,
            group_mode=True,
            group_chat_id=t.group_chat_id,
            role=role,
            active_persona=bot_cfg.active_persona,
        )
        return
    if project:
        # ...existing --project branch unchanged...
```

(Verify `run_bot` accepts `group_chat_id` and `role` keyword args; if not, add them to its signature in `bot.py:1340`.)

- [ ] **Step 6.4: Extend `run_bot` signature if needed**

In `bot.py`, check `run_bot` signature around line 1340. If it lacks `group_chat_id` or `role`, add them:

```python
def run_bot(
    name: str,
    path: Path,
    token: str,
    # ...existing kwargs...
    group_mode: bool = False,
    group_chat_id: int | None = None,
    role: str | None = None,
    active_persona: str | None = None,
) -> None:
    # ...existing body; pass group_chat_id and role to ProjectBot(...)...
```

Update the `ProjectBot(...)` constructor call inside `run_bot` to include `group_chat_id=group_chat_id, role=role` if `ProjectBot.__init__` also takes them. If `ProjectBot.__init__` only takes `group_mode`, extend it to accept `group_chat_id` and `role` too.

- [ ] **Step 6.5: Run test to verify it passes**

Run: `pytest tests/test_cli.py::test_start_team_and_role_invokes_run_bot_with_team_primitives -v`
Expected: PASS.

- [ ] **Step 6.6: Ensure existing `--project` path still works**

Run: `pytest tests/test_cli.py -v`
Expected: all PASS. If any solo-project start test fails due to the `run_bot` signature change, update the test fixture.

- [ ] **Step 6.7: Commit**

```bash
git add src/link_project_to_chat/cli.py src/link_project_to_chat/bot.py tests/test_cli.py
git commit -m "feat(cli): add start --team --role flags for team bot startup"
```

---

## Task 7: `ProcessManager.start_team` method

Adds team-bot subprocess spawning. Process keys use `team:<team>:<role>` so team entries coexist with solo entries in the manager's `_processes` dict.

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Create: `tests/test_process_manager_teams.py`

- [ ] **Step 7.1: Write failing test**

Create `tests/test_process_manager_teams.py`:

```python
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
from link_project_to_chat.manager.process import ProcessManager


def test_start_team_builds_correct_cli_and_spawns(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1"),
                    "dev": TeamBotConfig(telegram_bot_token="t2"),
                },
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        # pm needs a config path that knows teams; since the existing ProcessManager reads
        # project_config_path, we'll need a minimal team-loader injection. For now, point
        # at the same cfg_path and require start_team to call load_config().teams.
        result = pm.start_team("acme", "manager")
        assert result is True
        call_args = mock_popen.call_args[0][0]
        assert call_args[:2] == ["link-project-to-chat", "start"]
        assert "--team" in call_args and "acme" in call_args
        assert "--role" in call_args and "manager" in call_args


def test_start_team_uses_compound_process_key(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        pm.start_team("acme", "manager")
        assert "team:acme:manager" in pm._processes
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run: `pytest tests/test_process_manager_teams.py -v`
Expected: FAIL with `AttributeError: 'ProcessManager' object has no attribute 'start_team'`.

- [ ] **Step 7.3: Add `start_team` to `ProcessManager`**

In `src/link_project_to_chat/manager/process.py`, after the `start` method, add:

```python
    def _team_command_builder(self, team_name: str, role: str) -> list[str]:
        cmd = ["link-project-to-chat", "start", "--team", team_name, "--role", role]
        return cmd

    def start_team(self, team_name: str, role: str) -> bool:
        key = f"team:{team_name}:{role}"
        if key in self._processes and self._processes[key].poll() is None:
            return False
        cmd = self._team_command_builder(team_name, role)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self._processes[key] = proc
        self._logs[key] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(key, proc), daemon=True)
        thread.start()
        self._log_threads[key] = thread
        logger.info("Started team %s/%s (pid=%d)", team_name, role, proc.pid)
        return True
```

(Does not touch `_set_autostart` — team bots aren't in the projects config, so they can't persist autostart. Future work.)

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `pytest tests/test_process_manager_teams.py -v`
Expected: PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_teams.py
git commit -m "feat(process): add ProcessManager.start_team for team bot subprocess spawning"
```

---

## Task 8: `telegram_group.py` — `create_supergroup`

Creates a Telegram supergroup and returns the `-100…` chat_id.

**Files:**
- Create: `src/link_project_to_chat/manager/telegram_group.py`
- Create: `tests/test_telegram_group.py`

- [ ] **Step 8.1: Write failing test**

Create `tests/test_telegram_group.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
async def test_create_supergroup_returns_negative_chat_id():
    from link_project_to_chat.manager.telegram_group import create_supergroup

    # Mock Telethon response: channels.CreateChannelRequest returns an Updates object
    # whose .chats[0].id is a large positive int; caller must prepend -100 to get
    # the full -100... form used by the Bot API.
    mock_chat = MagicMock()
    mock_chat.id = 1234567890
    mock_response = MagicMock()
    mock_response.chats = [mock_chat]

    client = AsyncMock()
    client.return_value = mock_response  # calling client(request) returns the response

    chat_id = await create_supergroup(client, "acme team")
    assert chat_id == -1001234567890
```

- [ ] **Step 8.2: Run test to verify it fails**

Run: `pytest tests/test_telegram_group.py::test_create_supergroup_returns_negative_chat_id -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'link_project_to_chat.manager.telegram_group'`.

- [ ] **Step 8.3: Create the module**

Create `src/link_project_to_chat/manager/telegram_group.py`:

```python
"""Telethon group operations for the /create_team flow."""
from __future__ import annotations

import asyncio
import logging

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChatAdminRights

logger = logging.getLogger(__name__)

_FLOOD_WAIT_RETRY_THRESHOLD_SECONDS = 30


async def _call_with_flood_retry(client, request):
    """Invoke a Telethon TL request, retrying once on short FloodWaits."""
    try:
        return await client(request)
    except FloodWaitError as e:
        if e.seconds > _FLOOD_WAIT_RETRY_THRESHOLD_SECONDS:
            raise
        logger.info("FloodWait %ds, sleeping and retrying once", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await client(request)


async def create_supergroup(client, title: str) -> int:
    """Create a Telegram supergroup. Returns the Bot-API-style chat_id (-100...)."""
    resp = await _call_with_flood_retry(
        client,
        CreateChannelRequest(title=title, about="", megagroup=True),
    )
    raw_id = resp.chats[0].id
    return int(f"-100{raw_id}")
```

- [ ] **Step 8.4: Run test to verify it passes**

Run: `pytest tests/test_telegram_group.py::test_create_supergroup_returns_negative_chat_id -v`
Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/link_project_to_chat/manager/telegram_group.py tests/test_telegram_group.py
git commit -m "feat(manager): add telegram_group.create_supergroup with flood-wait retry"
```

---

## Task 9: `telegram_group.py` — `add_bot`, `promote_admin`, `invite_user`

**Files:**
- Modify: `src/link_project_to_chat/manager/telegram_group.py`
- Modify: `tests/test_telegram_group.py`

- [ ] **Step 9.1: Write failing tests**

Append to `tests/test_telegram_group.py`:

```python
@pytest.mark.asyncio
async def test_add_bot_invokes_invite_to_channel():
    from link_project_to_chat.manager.telegram_group import add_bot
    from telethon.tl.functions.channels import InviteToChannelRequest

    bot_entity = MagicMock()
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=bot_entity)

    await add_bot(client, -1001, "acme_mgr_claude_bot")

    # Assert client was called with an InviteToChannelRequest
    call_args = client.call_args_list
    assert any(isinstance(call.args[0], InviteToChannelRequest) for call in call_args)


@pytest.mark.asyncio
async def test_promote_admin_sets_correct_rights():
    from link_project_to_chat.manager.telegram_group import promote_admin
    from telethon.tl.functions.channels import EditAdminRequest

    bot_entity = MagicMock()
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=bot_entity)

    await promote_admin(client, -1001, "acme_mgr_claude_bot")

    call_args = client.call_args_list
    admin_calls = [c for c in call_args if isinstance(c.args[0], EditAdminRequest)]
    assert admin_calls, "EditAdminRequest must be issued"
    request = admin_calls[0].args[0]
    assert request.admin_rights.post_messages is True
    assert request.admin_rights.delete_messages is True
    assert request.admin_rights.invite_users is True


@pytest.mark.asyncio
async def test_invite_user_uses_invite_to_channel():
    from link_project_to_chat.manager.telegram_group import invite_user
    from telethon.tl.functions.channels import InviteToChannelRequest

    user_entity = MagicMock()
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user_entity)

    await invite_user(client, -1001, "alice")

    call_args = client.call_args_list
    assert any(isinstance(call.args[0], InviteToChannelRequest) for call in call_args)
```

- [ ] **Step 9.2: Run tests to verify they fail**

Run: `pytest tests/test_telegram_group.py -v`
Expected: 3 FAIL with ImportError.

- [ ] **Step 9.3: Implement the three functions**

Append to `src/link_project_to_chat/manager/telegram_group.py`:

```python
async def add_bot(client, chat_id: int, bot_username: str) -> None:
    """Invite a bot to the group."""
    bot_entity = await client.get_entity(bot_username)
    await _call_with_flood_retry(
        client,
        InviteToChannelRequest(channel=chat_id, users=[bot_entity]),
    )


async def promote_admin(client, chat_id: int, bot_username: str) -> None:
    """Promote a user/bot to admin with the rights group-mode bots need."""
    entity = await client.get_entity(bot_username)
    rights = ChatAdminRights(
        change_info=False,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        add_admins=False,
        anonymous=False,
        manage_call=False,
        other=False,
    )
    await _call_with_flood_retry(
        client,
        EditAdminRequest(channel=chat_id, user_id=entity, admin_rights=rights, rank=""),
    )


async def invite_user(client, chat_id: int, username: str) -> None:
    """Invite a user (by @username, no @ prefix) to the group."""
    user_entity = await client.get_entity(username)
    await _call_with_flood_retry(
        client,
        InviteToChannelRequest(channel=chat_id, users=[user_entity]),
    )
```

- [ ] **Step 9.4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_group.py -v`
Expected: 4 PASS.

- [ ] **Step 9.5: Commit**

```bash
git add src/link_project_to_chat/manager/telegram_group.py tests/test_telegram_group.py
git commit -m "feat(manager): add telegram_group add_bot/promote_admin/invite_user"
```

---

## Task 10: `telegram_group.py` — flood-wait retry coverage

**Files:**
- Modify: `tests/test_telegram_group.py`

- [ ] **Step 10.1: Write failing test for short-wait retry**

Append to `tests/test_telegram_group.py`:

```python
@pytest.mark.asyncio
async def test_flood_wait_under_30s_retries_once():
    from link_project_to_chat.manager.telegram_group import create_supergroup
    from telethon.errors import FloodWaitError

    mock_chat = MagicMock()
    mock_chat.id = 123
    mock_resp = MagicMock(chats=[mock_chat])

    call_count = {"n": 0}
    async def side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise FloodWaitError(request=None, capture=None, seconds=5)
        return mock_resp

    client = AsyncMock(side_effect=side_effect)
    result = await create_supergroup(client, "acme team")
    assert result == -100123
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_flood_wait_over_30s_aborts():
    from link_project_to_chat.manager.telegram_group import create_supergroup
    from telethon.errors import FloodWaitError

    client = AsyncMock(side_effect=FloodWaitError(request=None, capture=None, seconds=180))
    with pytest.raises(FloodWaitError):
        await create_supergroup(client, "acme team")
```

- [ ] **Step 10.2: Run tests to verify they pass (already implemented in Task 8)**

Run: `pytest tests/test_telegram_group.py -k flood -v`
Expected: 2 PASS (the retry logic was already implemented as `_call_with_flood_retry` in Task 8).

- [ ] **Step 10.3: Commit**

```bash
git add tests/test_telegram_group.py
git commit -m "test(telegram_group): cover flood-wait retry and abort paths"
```

---

## Task 11: `BotFatherClient.disable_privacy`

Sends `/setprivacy` → selects bot → taps "Disable" in the BotFather dialog. Needed so group-mode bots receive non-command messages.

**Files:**
- Modify: `src/link_project_to_chat/botfather.py`
- Modify: `tests/test_botfather.py`

- [ ] **Step 11.1: Write failing test**

Append to `tests/test_botfather.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_disable_privacy_sends_correct_dialog(tmp_path):
    from link_project_to_chat.botfather import BotFatherClient

    # Build a fake BotFatherClient with a pre-mocked Telethon client
    client_mock = AsyncMock()
    client_mock.send_message = AsyncMock()
    # iter_messages returns a coroutine-iterable yielding mocked reply messages
    reply_msg = MagicMock()
    reply_msg.message = "'Disable' has been enabled for @acme_mgr_claude_bot"

    async def fake_get_messages(*args, **kwargs):
        return [reply_msg]

    client_mock.get_messages = AsyncMock(side_effect=fake_get_messages)

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock
    bfc._client_initialized = True  # skip _ensure_client

    await bfc.disable_privacy("acme_mgr_claude_bot")

    # Assert the three messages sent: /setprivacy, bot username, "Disable"
    sent = [c.args for c in client_mock.send_message.call_args_list]
    messages = [args[1] for args in sent]
    assert "/setprivacy" in messages
    assert "@acme_mgr_claude_bot" in messages or "acme_mgr_claude_bot" in messages
    assert "Disable" in messages
```

(Adjust `bfc._client_initialized` and internal attribute names after reading `botfather.py`'s `_ensure_client()` implementation. The test may need minor tuning to match the real client attribute name.)

- [ ] **Step 11.2: Run test to verify it fails**

Run: `pytest tests/test_botfather.py::test_disable_privacy_sends_correct_dialog -v`
Expected: FAIL with `AttributeError: 'BotFatherClient' object has no attribute 'disable_privacy'`.

- [ ] **Step 11.3: Implement `disable_privacy`**

In `src/link_project_to_chat/botfather.py`, add a method to `BotFatherClient`:

```python
    async def disable_privacy(self, bot_username: str) -> None:
        """Send /setprivacy to BotFather, select the bot, tap Disable."""
        await self._ensure_client()
        entity = await self._client.get_entity("BotFather")
        await self._client.send_message(entity, "/setprivacy")
        # BotFather replies with a reply-keyboard listing bots; selecting a bot
        # with its @username works as a regular text message.
        await self._client.send_message(entity, f"@{bot_username}")
        # Then BotFather asks Enable/Disable
        await self._client.send_message(entity, "Disable")
        # Optional: poll for confirmation message (same pattern as create_bot).
        # Caller treats failure as non-fatal, so we don't raise if parsing fails.
```

(Inspect the existing `create_bot` flow to confirm message-sending shape; mirror it exactly.)

- [ ] **Step 11.4: Run test to verify it passes**

Run: `pytest tests/test_botfather.py::test_disable_privacy_sends_correct_dialog -v`
Expected: PASS.

- [ ] **Step 11.5: Commit**

```bash
git add src/link_project_to_chat/botfather.py tests/test_botfather.py
git commit -m "feat(botfather): add disable_privacy method for group-mode bots"
```

---

## Task 12: Persona picker keyboard helper

Reuses the existing `load_personas()` from `skills.py` to build an inline button keyboard.

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager.py` (or create `tests/test_manager_create_team.py`)

- [ ] **Step 12.1: Write failing test**

Create `tests/test_manager_create_team.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_persona_keyboard_lists_discovered_personas(tmp_path):
    from link_project_to_chat.manager.bot import _build_persona_keyboard

    # Create a fake persona file
    personas_dir = tmp_path / ".claude" / "personas"
    personas_dir.mkdir(parents=True)
    (personas_dir / "developer.md").write_text("# Developer")
    (personas_dir / "tester.md").write_text("# Tester")

    kb = _build_persona_keyboard(tmp_path, callback_prefix="team_persona_mgr")
    # InlineKeyboardMarkup with at least 2 buttons
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = {btn.text for btn in buttons}
    assert "developer" in labels
    assert "tester" in labels
    # Callbacks are prefixed
    assert all(btn.callback_data.startswith("team_persona_mgr:") for btn in buttons)
```

- [ ] **Step 12.2: Run test to verify it fails**

Run: `pytest tests/test_manager_create_team.py::test_persona_keyboard_lists_discovered_personas -v`
Expected: FAIL with `ImportError: cannot import name '_build_persona_keyboard'`.

- [ ] **Step 12.3: Implement the helper**

In `src/link_project_to_chat/manager/bot.py`, at module scope (near the top-level helpers), add:

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _build_persona_keyboard(project_path: Path, callback_prefix: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing discovered personas for the given project.
    Each button's callback_data is f'{callback_prefix}:{persona_name}'.
    """
    from ..skills import load_personas
    personas = load_personas(project_path)
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{callback_prefix}:{name}")]
        for name in sorted(personas)
    ]
    return InlineKeyboardMarkup(buttons)
```

- [ ] **Step 12.4: Run test to verify it passes**

Run: `pytest tests/test_manager_create_team.py::test_persona_keyboard_lists_discovered_personas -v`
Expected: PASS.

- [ ] **Step 12.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_team.py
git commit -m "feat(manager): add _build_persona_keyboard helper for /create_team picker"
```

---

## Task 13: `_on_create_team` — pre-flight checks

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_team.py`

- [ ] **Step 13.1: Write failing tests for pre-flight**

Append to `tests/test_manager_create_team.py`:

```python
import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    save_config,
)


def test_preflight_rejects_existing_team_name(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        teams={
            "acme": TeamConfig(path="/a", group_chat_id=-1, bots={})
        },
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")  # fake session file

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "already configured" in err


def test_preflight_rejects_legacy_project_name_collision(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        projects={"acme_mgr": ProjectConfig(path="/a", telegram_bot_token="t")},
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "project names are taken" in err or "acme_mgr" in err


def test_preflight_rejects_missing_telethon_config(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(github_pat="ghp_x")  # telegram_api_id/hash missing
    save_config(config, cfg_path)

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "/setup" in err


def test_preflight_passes_when_all_good(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x")
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is None
```

- [ ] **Step 13.2: Run tests to verify they fail**

Run: `pytest tests/test_manager_create_team.py -k preflight -v`
Expected: 4 FAIL with ImportError.

- [ ] **Step 13.3: Implement `_create_team_preflight`**

In `src/link_project_to_chat/manager/bot.py` at module scope:

```python
def _create_team_preflight(cfg_path: Path, prefix: str) -> str | None:
    """Return an error string if pre-flight fails, None if OK."""
    from ..config import load_config
    from ..github_client import _gh_available

    config = load_config(cfg_path)

    if not config.telegram_api_id or not config.telegram_api_hash:
        return "Run `/setup` first — Telegram API credentials are not configured."
    session_file = cfg_path.parent / "telethon.session"
    if not session_file.exists():
        return "Run `/setup` first — Telethon session is not established."

    if not config.github_pat and not _gh_available():
        return "GitHub auth missing — run `/setup` with a PAT, or authenticate `gh` CLI."

    if prefix in config.teams:
        return f"Team `{prefix}` is already configured."

    legacy_names = [f"{prefix}_mgr", f"{prefix}_dev"]
    taken = [n for n in legacy_names if n in config.projects]
    if taken:
        return f"Those project names are taken: {', '.join(taken)}. Pick a different prefix."

    return None
```

- [ ] **Step 13.4: Run tests to verify they pass**

Run: `pytest tests/test_manager_create_team.py -k preflight -v`
Expected: 4 PASS.

- [ ] **Step 13.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_team.py
git commit -m "feat(manager): add _create_team_preflight checks"
```

---

## Task 14: `_on_create_team` — state enum + entry handler + source picker

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 14.1: Add state enum constants**

In `manager/bot.py`, find the existing `CREATE_*` constants (around line 193-194). Append below them:

```python
(
    CREATE_TEAM_SOURCE,
    CREATE_TEAM_REPO_LIST,
    CREATE_TEAM_REPO_URL,
    CREATE_TEAM_NAME,
    CREATE_TEAM_PERSONA_MGR,
    CREATE_TEAM_PERSONA_DEV,
) = range(18, 24)
```

- [ ] **Step 14.2: Write the entry handler**

Add a method to the manager bot class (near `_on_create_project`):

```python
async def _on_create_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /create_team."""
    if not await self._auth(update, ctx):
        return ConversationHandler.END

    ctx.user_data["create_team"] = {}
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Browse my GitHub repos", callback_data="ct_source:github")],
        [InlineKeyboardButton("Paste a URL", callback_data="ct_source:url")],
    ])
    await update.effective_message.reply_text(
        "How would you like to pick the repo?",
        reply_markup=keyboard,
    )
    return CREATE_TEAM_SOURCE
```

- [ ] **Step 14.3: Write the source-pick callback**

```python
async def _create_team_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, source = query.data.split(":", 1)
    ctx.user_data["create_team"]["source"] = source
    if source == "github":
        return await self._show_repo_page(query, ctx, page=1, user_data_key="create_team")
    await query.edit_message_text("Paste the repo URL (e.g. https://github.com/owner/repo):")
    return CREATE_TEAM_REPO_URL
```

(`_show_repo_page` will be refactored in Task 15 to accept `user_data_key`.)

- [ ] **Step 14.4: Commit scaffold**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat(manager): scaffold _on_create_team entry + source picker"
```

---

## Task 15: Extract `_show_repo_page` and URL-validate helpers to support both `/create_project` and `/create_team`

Parameterize the existing helpers on a `user_data_key` so they work with both conversations' state dicts.

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 15.1: Write failing test that exercises both user_data keys**

Append to `tests/test_manager_create_team.py`:

```python
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_show_repo_page_supports_user_data_key(tmp_path):
    """_show_repo_page must read/write to the key passed in, not hardcoded 'create'."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot(config_path=cfg_path)
    ctx = MagicMock()
    ctx.user_data = {"create_team": {}}
    query = AsyncMock()
    query.edit_message_text = AsyncMock()

    # Monkeypatch GitHubClient.list_repos to avoid network
    async def fake_list_repos(*a, **kw):
        repo = MagicMock()
        repo.full_name = "me/acme"
        repo.description = "example"
        return [repo], False

    from link_project_to_chat import github_client
    github_client.GitHubClient.list_repos = fake_list_repos

    await mb._show_repo_page(query, ctx, page=1, user_data_key="create_team")
    # Assert the repos landed in ctx.user_data["create_team"], not ["create"]
    assert "repos" in ctx.user_data["create_team"]
    assert "me/acme" in ctx.user_data["create_team"]["repos"]
```

- [ ] **Step 15.2: Run test to verify it fails**

Run: `pytest tests/test_manager_create_team.py::test_show_repo_page_supports_user_data_key -v`
Expected: FAIL — current `_show_repo_page` writes to `ctx.user_data["create"]` hardcoded.

- [ ] **Step 15.3: Parameterize `_show_repo_page`**

In `manager/bot.py`, find `_show_repo_page` (around line 550). Change its signature to accept a `user_data_key` parameter with default `"create"` (preserves callers):

```python
async def _show_repo_page(self, query, ctx, page: int, user_data_key: str = "create") -> int:
    # ...existing body, but replace every ctx.user_data["create"] with
    # ctx.user_data[user_data_key]...
```

Do the same for `_create_repo_list_callback` and `_create_repo_url` (accept `user_data_key="create"`, and replace hardcoded `"create"` with the parameter). Also return the right state:

```python
    if user_data_key == "create_team":
        return CREATE_TEAM_REPO_LIST
    return CREATE_REPO_LIST
```

(Pattern: helpers know which conversation they're serving via `user_data_key`.)

- [ ] **Step 15.4: Run tests to verify they pass**

Run: `pytest tests/test_manager_create_team.py -v && pytest tests/test_manager.py -v`
Expected: all PASS — both conversations continue to work.

- [ ] **Step 15.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_team.py
git commit -m "refactor(manager): parameterize repo helpers on user_data_key for reuse"
```

---

## Task 16: `_on_create_team` — name state + persona pickers

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 16.1: Implement the name and persona state handlers**

In `manager/bot.py`, add these methods on the manager bot class:

```python
async def _create_team_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    prefix = update.message.text.strip().lower()
    if not prefix.isidentifier() or not prefix.isascii():
        await update.message.reply_text("Prefix must be lowercase ascii word characters only. Try again:")
        return CREATE_TEAM_NAME

    err = _create_team_preflight(self._config_path, prefix)
    if err:
        await update.message.reply_text(f"✗ {err}")
        return ConversationHandler.END

    ctx.user_data["create_team"]["project_prefix"] = prefix

    # Persona picker needs a project path; use the cloned-repo path once we have it.
    # Until clone happens, we can use a temp/placeholder path and just list global personas.
    from ..config import DEFAULT_CONFIG
    fake_path = Path(DEFAULT_CONFIG).parent  # has no project-scoped personas; OK for picker
    keyboard = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_mgr")
    await update.message.reply_text(
        "Pick manager-role persona:",
        reply_markup=keyboard,
    )
    return CREATE_TEAM_PERSONA_MGR


async def _create_team_persona_mgr_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, persona = query.data.split(":", 1)
    ctx.user_data["create_team"]["persona_mgr"] = persona

    from ..config import DEFAULT_CONFIG
    fake_path = Path(DEFAULT_CONFIG).parent
    keyboard = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_dev")
    await query.edit_message_text(
        f"Manager persona: {persona}\n\nPick dev-role persona:",
        reply_markup=keyboard,
    )
    return CREATE_TEAM_PERSONA_DEV


async def _create_team_persona_dev_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, persona = query.data.split(":", 1)
    ctx.user_data["create_team"]["persona_dev"] = persona

    # At this point all user inputs are captured. Kick off the "do the work" phase.
    return await self._create_team_execute(update, ctx)
```

- [ ] **Step 16.2: Commit the state handlers (scaffold)**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat(manager): add _create_team name + persona state handlers"
```

---

## Task 17: `_on_create_team` — username collision retry helper

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_team.py`

- [ ] **Step 17.1: Write failing test**

Append to `tests/test_manager_create_team.py`:

```python
@pytest.mark.asyncio
async def test_create_bot_with_retry_tries_suffixes(monkeypatch):
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    # Mock a BotFatherClient whose create_bot fails 2 times then succeeds
    attempts = []

    async def fake_create_bot(display_name: str, username: str) -> str:
        attempts.append(username)
        if len(attempts) < 3:
            raise ValueError("username taken")
        return "FAKE_TOKEN"

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    token, username = await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")
    assert token == "FAKE_TOKEN"
    assert username == "acme_mgr_2_claude_bot"
    assert attempts == ["acme_mgr_claude_bot", "acme_mgr_1_claude_bot", "acme_mgr_2_claude_bot"]


@pytest.mark.asyncio
async def test_create_bot_with_retry_fails_after_5_tries():
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    async def fake_create_bot(display_name: str, username: str) -> str:
        raise ValueError("username taken")

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    with pytest.raises(RuntimeError, match="5 attempts"):
        await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")
```

- [ ] **Step 17.2: Run tests to verify they fail**

Run: `pytest tests/test_manager_create_team.py -k "create_bot_with_retry" -v`
Expected: 2 FAIL with ImportError.

- [ ] **Step 17.3: Implement the helper**

In `manager/bot.py` at module scope:

```python
async def _create_bot_with_retry(bfc, display_name: str, base_username: str, max_attempts: int = 5) -> tuple[str, str]:
    """Try creating a bot with base_username; on failure append _1/_2/..., up to max_attempts."""
    suffix_insert_at = base_username.rfind("_claude_bot")
    if suffix_insert_at == -1:
        suffix_insert_at = len(base_username)

    for attempt in range(max_attempts):
        if attempt == 0:
            candidate = base_username
        else:
            candidate = base_username[:suffix_insert_at] + f"_{attempt}" + base_username[suffix_insert_at:]
        try:
            token = await bfc.create_bot(display_name, candidate)
            return token, candidate
        except Exception:
            if attempt == max_attempts - 1:
                break
    raise RuntimeError(f"Bot username unavailable after {max_attempts} attempts (base={base_username})")
```

- [ ] **Step 17.4: Run tests to verify they pass**

Run: `pytest tests/test_manager_create_team.py -k "create_bot_with_retry" -v`
Expected: 2 PASS.

- [ ] **Step 17.5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_team.py
git commit -m "feat(manager): add _create_bot_with_retry helper with 5-try collision backoff"
```

---

## Task 18: `_on_create_team` — orchestrator (`_create_team_execute`)

The workhorse: creates bots, disables privacy, clones, creates group, adds bots, commits config, promotes, invites, auto-starts. Handles partial failures.

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 18.1: Implement the orchestrator**

Add to the manager bot class:

```python
async def _create_team_execute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    from ..config import load_config, patch_team
    from ..botfather import BotFatherClient, sanitize_bot_username
    from ..github_client import GitHubClient
    from .telegram_group import (
        create_supergroup,
        add_bot,
        promote_admin,
        invite_user,
    )

    data = ctx.user_data["create_team"]
    prefix = data["project_prefix"]
    mgr_persona = data["persona_mgr"]
    dev_persona = data["persona_dev"]
    repo = data["repo"]

    config = load_config(self._config_path)
    chat = update.effective_chat

    status = await self._app.bot.send_message(chat.id, "⟳ Creating bot 1...")

    async def edit(text: str):
        try:
            await status.edit_text(text)
        except Exception:
            pass

    bfc = BotFatherClient(
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        session_path=self._config_path.parent / "telethon.session",
    )

    completed: dict[str, str] = {}
    try:
        # --- Bot 1 ---
        mgr_base = sanitize_bot_username(f"{prefix}_mgr")
        mgr_token, mgr_username = await _create_bot_with_retry(
            bfc, f"{prefix} Manager", mgr_base
        )
        completed["bot1"] = f"@{mgr_username}"
        await edit(f"✓ Bot 1 (@{mgr_username}) | ⟳ Creating bot 2...")

        # --- Bot 2 ---
        dev_base = sanitize_bot_username(f"{prefix}_dev")
        dev_token, dev_username = await _create_bot_with_retry(
            bfc, f"{prefix} Dev", dev_base
        )
        completed["bot2"] = f"@{dev_username}"
        await edit(f"✓ Bots | ⟳ Disabling privacy mode...")

        # --- Privacy disable (non-fatal) ---
        for username in (mgr_username, dev_username):
            try:
                await bfc.disable_privacy(username)
            except Exception as exc:
                logger.warning("Privacy disable failed for %s: %s", username, exc)
        await edit(f"✓ Bots ready | ⟳ Cloning repo...")

        # --- Clone ---
        dest = Path(self._clone_root) / prefix  # adapt to the existing clone-root pattern
        gh = GitHubClient(pat=config.github_pat)
        await gh.clone_repo(repo, dest)
        completed["repo"] = str(dest)
        await edit(f"✓ Cloned | ⟳ Creating group \"{prefix} team\"...")

        # --- Group ---
        client = bfc._client  # reuse authenticated Telethon client
        group_id = await create_supergroup(client, f"{prefix} team")
        completed["group"] = str(group_id)
        await edit(f"✓ Group | ⟳ Adding + promoting bots...")

        await add_bot(client, group_id, mgr_username)
        await add_bot(client, group_id, dev_username)

        # --- COMMIT config ---
        patch_team(
            prefix,
            {
                "path": str(dest),
                "group_chat_id": group_id,
                "bots": {
                    "manager": {
                        "telegram_bot_token": mgr_token,
                        "active_persona": mgr_persona,
                    },
                    "dev": {
                        "telegram_bot_token": dev_token,
                        "active_persona": dev_persona,
                    },
                },
            },
            self._config_path,
        )

        # --- Post-commit (all non-fatal) ---
        for username in (mgr_username, dev_username):
            try:
                await promote_admin(client, group_id, username)
            except Exception as exc:
                logger.warning("Promote admin failed for %s: %s", username, exc)

        requester = update.effective_user.username
        if requester:
            try:
                await invite_user(client, group_id, requester)
            except Exception as exc:
                logger.warning("Invite requester %s failed: %s", requester, exc)

        await edit(f"✓ Group wired | ⟳ Starting both bots...")
        self._pm.start_team(prefix, "manager")
        self._pm.start_team(prefix, "dev")
        await edit(f"✓ Team ready. Open the \"{prefix} team\" group to start chatting.")

    except Exception as exc:
        await self._send_partial_failure_report(chat.id, exc, completed)

    return ConversationHandler.END


async def _send_partial_failure_report(self, chat_id: int, exc: Exception, completed: dict[str, str]) -> None:
    lines = [
        f"✗ Team creation failed: {type(exc).__name__}: {exc}",
        "",
    ]
    if completed:
        lines.append("Completed (needs manual cleanup):")
        if "bot1" in completed:
            lines.append(f"  - Bot {completed['bot1']} (delete via BotFather /deletebot)")
        if "bot2" in completed:
            lines.append(f"  - Bot {completed['bot2']} (delete via BotFather /deletebot)")
        if "repo" in completed:
            lines.append(f"  - Directory {completed['repo']} (remove if not needed)")
        if "group" in completed:
            lines.append(f"  - Group {completed['group']} (delete via Telegram)")
        lines.append("")
    lines.append("Config not saved. Safe to retry with a different prefix.")
    await self._app.bot.send_message(chat_id, "\n".join(lines))
```

Adjust `self._clone_root` / `self._pm` / `self._app` references to match the actual attribute names already on the manager class.

- [ ] **Step 18.2: Commit the orchestrator**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat(manager): implement _create_team_execute orchestrator + partial failure report"
```

---

## Task 19: Wire `/create_team` `ConversationHandler` into `build()`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 19.1: Register the ConversationHandler**

Find the `build()` method (around line 1004). After the `/create_project` ConversationHandler registration (around line 1042), add:

```python
        application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("create_team", self._on_create_team)],
                states={
                    CREATE_TEAM_SOURCE: [
                        CallbackQueryHandler(self._create_team_source_callback, pattern=r"^ct_source:"),
                    ],
                    CREATE_TEAM_REPO_LIST: [
                        CallbackQueryHandler(
                            lambda u, c: self._create_repo_list_callback(u, c, user_data_key="create_team"),
                            pattern=r"^(repo_select|repo_page):",
                        ),
                    ],
                    CREATE_TEAM_REPO_URL: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            lambda u, c: self._create_repo_url(u, c, user_data_key="create_team"),
                        ),
                    ],
                    CREATE_TEAM_NAME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_team_name),
                    ],
                    CREATE_TEAM_PERSONA_MGR: [
                        CallbackQueryHandler(self._create_team_persona_mgr_callback, pattern=r"^ct_persona_mgr:"),
                    ],
                    CREATE_TEAM_PERSONA_DEV: [
                        CallbackQueryHandler(self._create_team_persona_dev_callback, pattern=r"^ct_persona_dev:"),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._on_cancel)],
            )
        )
```

(Confirm the exact pattern strings for `/create_project`'s repo_list callback; mirror them here but include them in the same handler with `user_data_key="create_team"`.)

Transition from source to name is missing — the source callback lands on `CREATE_TEAM_REPO_LIST` or `CREATE_TEAM_REPO_URL`, and from either of those, after a repo is picked, the code should transition to `CREATE_TEAM_NAME`. Adjust `_create_repo_list_callback` (the return value when `user_data_key="create_team"`) to return `CREATE_TEAM_NAME` and prompt for the project prefix with `update.effective_message.reply_text("Short project name?")`.

- [ ] **Step 19.2: Update `/help` text**

Find the `/help` handler in `manager/bot.py` and add `/create_team` to the command list.

- [ ] **Step 19.3: Run the whole suite**

Run: `pytest`
Expected: all PASS.

- [ ] **Step 19.4: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat(manager): register /create_team ConversationHandler"
```

---

## Task 20: Manual QA

Run the manual acceptance checklist from the spec. No code changes — this task is the go/no-go gate.

- [ ] **Step 20.1: Set up test environment**

On Windows, ensure:
- `link-project-to-chat` CLI installed editable from this branch
- Manager bot token set via `link-project-to-chat configure --manager-token <TOKEN>`
- Your Telegram username in `allowed_usernames`
- `/setup` completed: GitHub PAT (or `gh` auth), Telegram API id/hash, Telethon session phone-authenticated
- At least one GitHub repo you can access for the test

Run: `link-project-to-chat start-manager` in a terminal. Open a DM with the manager bot.

- [ ] **Step 20.2: Pre-flight failure tests**

- [ ] Send `/create_team` when Telethon session file is missing (rename it temporarily). Verify the bot replies with "run `/setup` first" and doesn't proceed.
- [ ] Restore session. Pre-create a team named `test_fail` in config. Run `/create_team`, choose source, at the name prompt enter `test_fail`. Verify the bot rejects with "already configured".
- [ ] Clean up `test_fail`.

- [ ] **Step 20.3: Happy path**

- [ ] Run `/create_team` → Browse → pick a repo → type `qatest` → pick personas → watch progressive status updates → verify "Team ready" at the end.
- [ ] Confirm `~/.link-project-to-chat/config.json` has a `teams.qatest` entry with two bots and a negative `group_chat_id`.
- [ ] Open the `qatest team` group in Telegram. Verify both bots are present and are admins.
- [ ] In the group, `@qatest_mgr_claude_bot hello` → verify manager bot responds using the chosen persona.
- [ ] In the group, manager bot @mentions `@qatest_dev_claude_bot` (via your prompt or via the manager persona delegating) → verify dev bot responds.
- [ ] Verify both bots read non-command messages (privacy disabled): send a plain message containing neither bot's @mention and confirm the @mention-filtering logic behaves as expected (neither responds to non-mentions).

- [ ] **Step 20.4: `/cancel` and retry**

- [ ] Run `/create_team` and `/cancel` after choosing source. Verify a clean "cancelled" message.
- [ ] Run `/create_team` with a taken bot username (e.g., pick a prefix matching an existing well-known bot's namespace) to exercise the `_1` retry. Verify the retry kicks in.

- [ ] **Step 20.5: Record any bugs**

Log bugs as GitHub issues or a follow-up list. If any are blocking, fix them and re-run affected checklist items.

- [ ] **Step 20.6: Commit QA notes**

If you added a markdown record of the manual run (optional), commit it under `docs/superpowers/plans/2026-04-17-create-team-qa-notes.md`.

---

## Self-Review Checklist

After implementation:

- [ ] All spec sections 2-7 have a corresponding task (Section 1 is overview, 8 is testing, 9 is files-summary, 10 is phase order, 11 is future work)
- [ ] Each task has test → fail → implement → pass → commit steps
- [ ] No TODO/TBD/"add appropriate error handling" placeholders in this plan
- [ ] Function names are consistent across tasks: `patch_team`, `load_teams`, `create_supergroup`, `add_bot`, `promote_admin`, `invite_user`, `disable_privacy`, `_build_persona_keyboard`, `_create_team_preflight`, `_create_bot_with_retry`, `_create_team_execute`, `start_team`
- [ ] Config key names consistent: `teams`, `group_chat_id`, `bots`, `active_persona`, `telegram_bot_token`, `role`
- [ ] State enum values non-overlapping with existing (18-23 for CREATE_TEAM_*)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-17-create-team-command.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
