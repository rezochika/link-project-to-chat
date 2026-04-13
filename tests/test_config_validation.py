"""Tests for Pydantic config validation and session separation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import load_config
from link_project_to_chat.exceptions import ConfigError
from link_project_to_chat.sessions import clear_session, load_sessions, save_session

# --- Config Validation ---


class TestConfigValidation:
    def test_empty_config_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "missing.json")
        assert cfg.allowed_username == ""
        assert cfg.projects == {}

    def test_valid_config_loads(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "allowed_username": "Alice",
            "projects": {
                "myproj": {
                    "path": "/home/user/project",
                    "telegram_bot_token": "tok123",
                    "model": "opus",
                }
            }
        }))
        cfg = load_config(config_file)
        assert cfg.allowed_username == "alice"
        assert "myproj" in cfg.projects
        assert cfg.projects["myproj"].model == "opus"

    def test_corrupt_json_raises_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text("not json at all")
        with pytest.raises(ConfigError, match="Malformed"):
            load_config(config_file)

    def test_invalid_model_raises_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "projects": {
                "proj": {
                    "path": "/tmp/project",
                    "model": "invalid-model",
                }
            }
        }))
        with pytest.raises(ConfigError, match="Invalid"):
            load_config(config_file)

    def test_invalid_permission_mode_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "projects": {
                "proj": {
                    "path": "/tmp/project",
                    "permission_mode": "yolo",
                }
            }
        }))
        with pytest.raises(ConfigError, match="Invalid"):
            load_config(config_file)

    def test_empty_path_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "projects": {
                "proj": {
                    "path": "",
                }
            }
        }))
        with pytest.raises(ConfigError):
            load_config(config_file)

    def test_username_normalized(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "allowed_username": "@BobSmith",
            "projects": {
                "p": {"path": "/tmp", "username": "@Alice"}
            }
        }))
        cfg = load_config(config_file)
        assert cfg.allowed_username == "bobsmith"
        assert cfg.projects["p"].allowed_username == "alice"

    def test_backward_compat_manager_bot_token(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "manager_bot_token": "old-tok",
        }))
        cfg = load_config(config_file)
        assert cfg.manager_telegram_bot_token == "old-tok"


# --- Sessions ---


class TestSessions:
    def test_save_and_load(self, tmp_path: Path) -> None:
        sessions_file = tmp_path / "sessions.json"
        config_file = tmp_path / "config.json"
        save_session("proj1", "sess-abc", path=sessions_file)
        sessions = load_sessions(sessions_path=sessions_file, config_path=config_file)
        assert sessions["proj1"] == "sess-abc"

    def test_clear_session(self, tmp_path: Path) -> None:
        sessions_file = tmp_path / "sessions.json"
        config_file = tmp_path / "config.json"
        save_session("proj1", "sess-abc", path=sessions_file)
        clear_session("proj1", path=sessions_file)
        sessions = load_sessions(sessions_path=sessions_file, config_path=config_file)
        assert "proj1" not in sessions

    def test_load_empty_returns_empty(self, tmp_path: Path) -> None:
        sessions = load_sessions(
            sessions_path=tmp_path / "sessions.json",
            config_path=tmp_path / "config.json",
        )
        assert sessions == {}

    def test_migrate_from_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        sessions_file = tmp_path / "sessions.json"
        config_file.write_text(json.dumps({
            "projects": {
                "proj1": {"path": "/tmp", "session_id": "sess-old"},
                "proj2": {"path": "/tmp"},
            }
        }))
        sessions = load_sessions(sessions_path=sessions_file, config_path=config_file)
        assert sessions["proj1"] == "sess-old"
        assert "proj2" not in sessions
        # sessions.json should now exist
        assert sessions_file.exists()
