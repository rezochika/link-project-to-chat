from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from link_project_to_chat.config import (
    _atomic_write,
    Config,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    add_project_trusted_user_id,
    add_trusted_user_id,
    clear_session,
    clear_trusted_user_id,
    load_config,
    load_sessions,
    load_teams,
    load_trusted_user_id,
    patch_team,
    save_config,
    save_project_trusted_user_id,
    save_session,
    save_trusted_user_id,
)


def test_load_config_missing(tmp_path: Path):
    config = load_config(tmp_path / "missing.json")
    assert config.allowed_usernames == []
    assert config.projects == {}


def test_load_config_strips_at_and_lowercases(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "@Alice", "projects": {}}))
    config = load_config(p)
    assert config.allowed_usernames == ["alice"]


def test_save_and_load_config(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["bob"],
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(path="/some/path", telegram_bot_token="TOK")},
    )
    save_config(cfg, p)
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    loaded = load_config(p)
    assert loaded.allowed_usernames == ["bob"]
    assert loaded.manager_telegram_bot_token == "MGR"
    assert loaded.projects["proj"].path == "/some/path"
    assert loaded.projects["proj"].telegram_bot_token == "TOK"


def test_save_config_preserves_unknown_keys(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "alice", "future_key": "future_value", "projects": {}}))
    cfg = Config(allowed_usernames=["alice"])
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert raw["future_key"] == "future_value"


def test_save_config_preserves_project_unknown_keys(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "projects": {"myproj": {"path": "/p", "telegram_bot_token": "T", "model": "opus"}},
    }))
    cfg = Config(
        allowed_usernames=["alice"],
        projects={"myproj": ProjectConfig(path="/p", telegram_bot_token="T")},
    )
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["myproj"]["model"] == "opus"


def test_save_config_removes_deleted_projects(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg1 = Config(
        allowed_usernames=["alice"],
        projects={
            "a": ProjectConfig(path="/a", telegram_bot_token="Ta"),
            "b": ProjectConfig(path="/b", telegram_bot_token="Tb"),
        },
    )
    save_config(cfg1, p)
    cfg2 = Config(
        allowed_usernames=["alice"],
        projects={"a": ProjectConfig(path="/a", telegram_bot_token="Ta")},
    )
    save_config(cfg2, p)
    raw = json.loads(p.read_text())
    assert "a" in raw["projects"]
    assert "b" not in raw["projects"]


def test_save_and_load_per_project_username(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["global"],
        projects={"proj": ProjectConfig(path="/p", telegram_bot_token="T", allowed_usernames=["perproject"])},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].allowed_usernames == ["perproject"]
    assert loaded.allowed_usernames == ["global"]


def test_save_and_load_per_project_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["alice"],
        projects={"proj": ProjectConfig(path="/p", telegram_bot_token="T", trusted_user_ids=[42])},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].trusted_user_ids == [42]


def test_save_and_load_global_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_usernames=["alice"], trusted_user_ids=[99])
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.trusted_user_ids == [99]


def test_save_session_and_load(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("myproj", "sess-abc", p)
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    sessions = load_sessions(p)
    assert sessions["myproj"] == "sess-abc"


def test_save_session_merges(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("a", "id-a", p)
    save_session("b", "id-b", p)
    sessions = load_sessions(p)
    assert sessions["a"] == "id-a"
    assert sessions["b"] == "id-b"


def test_clear_session(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("x", "id-x", p)
    clear_session("x", p)
    assert "x" not in load_sessions(p)


def test_clear_session_missing_key(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("a", "id-a", p)
    clear_session("nope", p)  # should not raise
    assert load_sessions(p)["a"] == "id-a"


def test_load_sessions_missing(tmp_path: Path):
    assert load_sessions(tmp_path / "nope.json") == {}


def test_load_sessions_corrupt(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text("not json")
    assert load_sessions(p) == {}


def test_trusted_user_id_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    assert load_trusted_user_id(p) is None
    save_trusted_user_id(12345, p)
    assert load_trusted_user_id(p) == 12345
    clear_trusted_user_id(p)
    assert load_trusted_user_id(p) is None


def test_load_trusted_user_id_corrupt(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text("???")
    assert load_trusted_user_id(p) is None


def test_save_project_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"projects": {"myproj": {"path": "/p", "telegram_bot_token": "T"}}}))
    save_project_trusted_user_id("myproj", 77, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["myproj"]["trusted_user_id"] == 77
    # Other project data preserved
    assert raw["projects"]["myproj"]["path"] == "/p"


def test_save_project_trusted_user_id_creates_entry(tmp_path: Path):
    p = tmp_path / "cfg.json"
    save_project_trusted_user_id("newproj", 55, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["newproj"]["trusted_user_id"] == 55


def test_per_project_username_strips_at(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "global",
        "projects": {"proj": {"path": "/p", "telegram_bot_token": "T", "username": "@Alice"}},
    }))
    loaded = load_config(p)
    assert loaded.projects["proj"].allowed_usernames == ["alice"]


# --- New multi-user tests ---

def test_load_config_multi_user(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice", "bob"],
        "trusted_user_ids": [10, 20],
        "github_pat": "ghp_test",
        "telegram_api_id": 12345,
        "telegram_api_hash": "abc123",
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "allowed_usernames": ["alice"],
                "trusted_user_ids": [10],
            }
        },
    }))
    config = load_config(p)
    assert config.allowed_usernames == ["alice", "bob"]
    assert config.trusted_user_ids == [10, 20]
    assert config.github_pat == "ghp_test"
    assert config.telegram_api_id == 12345
    assert config.telegram_api_hash == "abc123"
    assert config.projects["proj"].allowed_usernames == ["alice"]
    assert config.projects["proj"].trusted_user_ids == [10]


def test_load_config_migrates_single_username(tmp_path: Path):
    """Old single-value keys auto-migrate to lists."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "username": "bob",
                "trusted_user_id": 99,
            }
        },
    }))
    config = load_config(p)
    assert config.allowed_usernames == ["alice"]
    assert config.trusted_user_ids == [42]
    assert config.projects["proj"].allowed_usernames == ["bob"]
    assert config.projects["proj"].trusted_user_ids == [99]


def test_load_config_empty_username_no_migration(tmp_path: Path):
    """Empty old username should result in empty list, not ['']."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "", "projects": {}}))
    config = load_config(p)
    assert config.allowed_usernames == []


def test_save_config_multi_user_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["alice", "bob"],
        trusted_user_ids=[10, 20],
        github_pat="ghp_xxx",
        telegram_api_id=111,
        telegram_api_hash="hash",
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(
            path="/p", telegram_bot_token="T",
            allowed_usernames=["alice"], trusted_user_ids=[10],
        )},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.allowed_usernames == ["alice", "bob"]
    assert loaded.trusted_user_ids == [10, 20]
    assert loaded.github_pat == "ghp_xxx"
    assert loaded.telegram_api_id == 111
    assert loaded.telegram_api_hash == "hash"
    assert loaded.projects["proj"].allowed_usernames == ["alice"]
    assert loaded.projects["proj"].trusted_user_ids == [10]


def test_save_config_removes_old_singular_keys(tmp_path: Path):
    """After save, old singular keys should not be in the JSON."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {"proj": {"path": "/p", "telegram_bot_token": "T", "username": "bob"}},
    }))
    cfg = load_config(p)
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "allowed_username" not in raw
    assert "trusted_user_id" not in raw
    assert "username" not in raw["projects"]["proj"]


def test_add_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    add_trusted_user_id(10, p)
    raw = json.loads(p.read_text())
    assert raw["trusted_user_ids"] == [10]
    # Adding again should not duplicate
    add_trusted_user_id(10, p)
    raw = json.loads(p.read_text())
    assert raw["trusted_user_ids"] == [10]
    # Adding a different id
    add_trusted_user_id(20, p)
    raw = json.loads(p.read_text())
    assert raw["trusted_user_ids"] == [10, 20]


def test_add_project_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    add_project_trusted_user_id("proj", 10, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["proj"]["trusted_user_ids"] == [10]
    # Adding again should not duplicate
    add_project_trusted_user_id("proj", 10, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["proj"]["trusted_user_ids"] == [10]
    # Adding a different id
    add_project_trusted_user_id("proj", 20, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["proj"]["trusted_user_ids"] == [10, 20]


def test_load_config_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice"],
        "stt_backend": "whisper-api",
        "openai_api_key": "sk-test123",
        "whisper_model": "whisper-1",
        "whisper_language": "en",
        "projects": {},
    }))
    config = load_config(p)
    assert config.stt_backend == "whisper-api"
    assert config.openai_api_key == "sk-test123"
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == "en"


def test_load_config_voice_defaults(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_usernames": ["alice"], "projects": {}}))
    config = load_config(p)
    assert config.stt_backend == ""
    assert config.openai_api_key == ""
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == ""


def test_save_config_voice_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["alice"],
        stt_backend="whisper-api",
        openai_api_key="sk-xxx",
        whisper_model="small",
        whisper_language="ka",
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.stt_backend == "whisper-api"
    assert loaded.openai_api_key == "sk-xxx"
    assert loaded.whisper_model == "small"
    assert loaded.whisper_language == "ka"


def test_save_config_omits_empty_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_usernames=["alice"])
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "stt_backend" not in raw
    assert "openai_api_key" not in raw
    assert "whisper_language" not in raw
    # whisper_model defaults to "whisper-1" and is omitted when at default
    assert "whisper_model" not in raw


def test_save_config_persists_non_default_model(tmp_path: Path):
    """Non-default whisper_model must round-trip."""
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_usernames=["alice"], whisper_model="small")
    save_config(cfg, p)
    assert json.loads(p.read_text())["whisper_model"] == "small"
    assert load_config(p).whisper_model == "small"


class TestAtomicWrite:
    def test_writes_file_correctly(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, '{"key": "value"}\n')
        assert target.read_text() == '{"key": "value"}\n'
        if sys.platform != "win32":
            assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_cleans_up_on_rename_failure(self, tmp_path):
        target = tmp_path / "test.json"
        with patch("os.replace", side_effect=OSError("rename failed")):
            try:
                _atomic_write(target, "data")
            except OSError:
                pass
        temps = list(tmp_path.glob("*.tmp"))
        assert len(temps) == 0
        assert not target.exists()


def test_team_config_default_empty_dict(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"projects": {}}))
    config = load_config(p)
    assert config.teams == {}


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


def test_save_config_removes_deleted_teams(tmp_path: Path):
    """Saving a config where a team was dropped from config.teams removes it from the JSON too."""
    p = tmp_path / "cfg.json"
    # First save: two teams
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a", group_chat_id=-100,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            ),
            "beta": TeamConfig(
                path="/b", group_chat_id=-200,
                bots={"manager": TeamBotConfig(telegram_bot_token="t2")},
            ),
        }
    )
    save_config(cfg, p)
    # Second save: drop "beta"
    cfg.teams.pop("beta")
    save_config(cfg, p)
    # Reload: only "acme" should remain
    loaded = load_config(p)
    assert "acme" in loaded.teams
    assert "beta" not in loaded.teams


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


def test_team_with_sentinel_chat_id_roundtrips(tmp_path: Path):
    """A team with group_chat_id=0 (not yet captured) saves and loads cleanly."""
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a",
                group_chat_id=0,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.teams["acme"].group_chat_id == 0


def test_team_bot_autostart_defaults_false(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {},
        "teams": {
            "acme": {
                "path": "/a",
                "group_chat_id": -1,
                "bots": {"manager": {"telegram_bot_token": "t1"}},
            }
        },
    }))
    loaded = load_config(p)
    assert loaded.teams["acme"].bots["manager"].autostart is False


def test_team_bot_autostart_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a",
                group_chat_id=-1,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", autostart=True),
                    "dev": TeamBotConfig(telegram_bot_token="t2", autostart=False),
                },
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.teams["acme"].bots["manager"].autostart is True
    assert loaded.teams["acme"].bots["dev"].autostart is False
    # Default-false autostart should not be written to JSON to keep files clean
    raw = json.loads(p.read_text())
    assert "autostart" not in raw["teams"]["acme"]["bots"]["dev"]
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is True


def test_load_teams_reads_autostart(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/a",
            "group_chat_id": -1,
            "bots": {"manager": {"telegram_bot_token": "t1", "autostart": True}},
        },
        p,
    )
    teams = load_teams(p)
    assert teams["acme"].bots["manager"].autostart is True


def test_project_show_thinking_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={
            "proj": ProjectConfig(
                path="/x",
                telegram_bot_token="T",
                show_thinking=True,
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is True


def test_project_show_thinking_defaults_false(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {"proj": {"path": "/x", "telegram_bot_token": "T"}}
    }))
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is False


def test_load_config_skips_phantom_project_without_path(tmp_path: Path, capsys):
    """Pre-34b8dc5 buggy /persona on team bots created projects[<team>_<role>]
    entries with only active_persona (no path). load_config must tolerate them
    so the bot still starts; save_config then filters them out on next write."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
    }))
    loaded = load_config(p)
    assert "good" in loaded.projects
    assert "acme_manager" not in loaded.projects
    err = capsys.readouterr().err
    assert "acme_manager" in err


def test_load_config_self_heals_phantom_project_without_path(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
        "future_key": "kept",
    }))
    load_config(p)
    raw = json.loads(p.read_text())
    assert raw["projects"] == {"good": {"path": "/x", "telegram_bot_token": "T"}}
    assert raw["future_key"] == "kept"


def test_save_config_drops_phantom_project_on_next_write(tmp_path: Path):
    """After a tolerant load, saving the config removes the phantom entry from disk."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
    }))
    cfg = load_config(p)
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "acme_manager" not in raw["projects"]
    assert "good" in raw["projects"]
