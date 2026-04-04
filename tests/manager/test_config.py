from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    clear_trusted_user_id,
    load_trusted_user_id,
    save_trusted_user_id,
)
from link_project_to_chat.manager.config import (
    ManagerConfig,
    PermissionDefaults,
    load_manager_config,
    load_project_configs,
    load_state,
    resolve_flags,
    save_manager_config,
    save_project_configs,
    save_state,
)


def test_load_missing_config(tmp_path: Path):
    config = load_manager_config(tmp_path / "missing.json")
    assert config.telegram_bot_token == ""
    assert config.defaults.skip_permissions is False
    assert config.overrides == {}


def test_save_and_load_config(tmp_path: Path):
    path = tmp_path / "config.json"
    config = ManagerConfig()
    save_manager_config(config, path)
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_manager_config(path)
    assert loaded.defaults.skip_permissions is False


def test_load_project_configs(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "allowed_username": "someone",
        "projects": {"myproj": {"path": "/some/path", "telegram_bot_token": "BOT_TOKEN"}},
    }))
    projects = load_project_configs(path)
    assert projects["myproj"]["path"] == "/some/path"
    assert projects["myproj"]["telegram_bot_token"] == "BOT_TOKEN"


def test_load_project_configs_missing(tmp_path: Path):
    assert load_project_configs(tmp_path / "nope.json") == {}


def test_state_save_and_load(tmp_path: Path):
    path = tmp_path / "state.json"
    save_state(["proj-a", "proj-b"], path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_state(path) == ["proj-a", "proj-b"]


def test_state_load_missing(tmp_path: Path):
    assert load_state(tmp_path / "missing.json") == []


def test_trusted_user_id(tmp_path: Path):
    path = tmp_path / "uid.json"
    assert load_trusted_user_id(path) is None
    save_trusted_user_id(12345, path)
    assert load_trusted_user_id(path) == 12345
    clear_trusted_user_id(path)
    assert load_trusted_user_id(path) is None


def test_resolve_flags_defaults_only():
    defaults = PermissionDefaults(permission_mode="auto", model="sonnet")
    flags = resolve_flags(defaults, overrides={}, project_name="myproj")
    assert flags["permission_mode"] == "auto"
    assert flags["model"] == "sonnet"
    assert flags["skip_permissions"] is False


def test_resolve_flags_with_override():
    defaults = PermissionDefaults(permission_mode="auto", model="sonnet")
    overrides = {"myproj": {"model": "opus", "skip_permissions": True}}
    flags = resolve_flags(defaults, overrides, "myproj")
    assert flags["model"] == "opus"
    assert flags["skip_permissions"] is True
    assert flags["permission_mode"] == "auto"


def test_resolve_flags_no_match():
    defaults = PermissionDefaults(model="haiku")
    flags = resolve_flags(defaults, {"other": {"model": "opus"}}, "myproj")
    assert flags["model"] == "haiku"


def test_save_project_configs_preserves_other_keys(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"allowed_username": "someone", "projects": {}}))
    save_project_configs({"newproj": {"path": "/new"}}, path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["newproj"]["path"] == "/new"
    assert raw["allowed_username"] == "someone"


def test_save_project_configs_creates_file(tmp_path: Path):
    path = tmp_path / "config.json"
    save_project_configs({"proj": {"path": "/p"}}, path)
    assert json.loads(path.read_text())["projects"]["proj"]["path"] == "/p"
