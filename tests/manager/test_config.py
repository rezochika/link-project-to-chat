from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    clear_trusted_user_id,
    load_trusted_user_id,
    save_trusted_user_id,
)
from link_project_to_chat.manager.config import (
    load_project_configs,
    save_project_configs,
    set_project_autostart,
)


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


def test_set_project_autostart(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"myproj": {"path": "/p"}}}))
    set_project_autostart("myproj", True, path)
    assert json.loads(path.read_text())["projects"]["myproj"]["autostart"] is True
    set_project_autostart("myproj", False, path)
    assert json.loads(path.read_text())["projects"]["myproj"]["autostart"] is False


def test_trusted_user_id(tmp_path: Path):
    path = tmp_path / "uid.json"
    assert load_trusted_user_id(path) is None
    save_trusted_user_id(12345, path)
    assert load_trusted_user_id(path) == 12345
    clear_trusted_user_id(path)
    assert load_trusted_user_id(path) is None
