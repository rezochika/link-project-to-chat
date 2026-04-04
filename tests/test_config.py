from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    clear_session,
    clear_trusted_user_id,
    load_config,
    load_sessions,
    load_trusted_user_id,
    save_config,
    save_project_trusted_user_id,
    save_session,
    save_trusted_user_id,
)


def test_load_config_missing(tmp_path: Path):
    config = load_config(tmp_path / "missing.json")
    assert config.allowed_username == ""
    assert config.projects == {}


def test_load_config_strips_at_and_lowercases(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "@Alice", "projects": {}}))
    config = load_config(p)
    assert config.allowed_username == "alice"


def test_save_and_load_config(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_username="bob",
        manager_bot_token="MGR",
        projects={"proj": ProjectConfig(path="/some/path", telegram_bot_token="TOK")},
    )
    save_config(cfg, p)
    assert p.stat().st_mode & 0o777 == 0o600
    loaded = load_config(p)
    assert loaded.allowed_username == "bob"
    assert loaded.manager_bot_token == "MGR"
    assert loaded.projects["proj"].path == "/some/path"
    assert loaded.projects["proj"].telegram_bot_token == "TOK"


def test_save_config_preserves_unknown_keys(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "alice", "future_key": "future_value", "projects": {}}))
    cfg = Config(allowed_username="alice")
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
        allowed_username="alice",
        projects={"myproj": ProjectConfig(path="/p", telegram_bot_token="T")},
    )
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert raw["projects"]["myproj"]["model"] == "opus"


def test_save_config_removes_deleted_projects(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg1 = Config(
        allowed_username="alice",
        projects={
            "a": ProjectConfig(path="/a", telegram_bot_token="Ta"),
            "b": ProjectConfig(path="/b", telegram_bot_token="Tb"),
        },
    )
    save_config(cfg1, p)
    cfg2 = Config(
        allowed_username="alice",
        projects={"a": ProjectConfig(path="/a", telegram_bot_token="Ta")},
    )
    save_config(cfg2, p)
    raw = json.loads(p.read_text())
    assert "a" in raw["projects"]
    assert "b" not in raw["projects"]


def test_save_and_load_per_project_username(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_username="global",
        projects={"proj": ProjectConfig(path="/p", telegram_bot_token="T", allowed_username="perproject")},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].allowed_username == "perproject"
    assert loaded.allowed_username == "global"


def test_save_and_load_per_project_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_username="alice",
        projects={"proj": ProjectConfig(path="/p", telegram_bot_token="T", trusted_user_id=42)},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].trusted_user_id == 42


def test_save_and_load_global_trusted_user_id(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_username="alice", trusted_user_id=99)
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.trusted_user_id == 99


def test_save_session_and_load(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("myproj", "sess-abc", p)
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
    assert loaded.projects["proj"].allowed_username == "alice"
