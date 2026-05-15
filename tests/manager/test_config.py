from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.manager.config import (
    load_project_configs,
    save_project_configs,
    set_project_autostart,
    set_project_backend,
    set_team_bot_autostart,
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


def test_load_project_configs_skips_phantom_entries(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "projects": {
            "good": {"path": "/some/path", "telegram_bot_token": "BOT_TOKEN"},
            "acme_manager": {"active_persona": "software_manager"},
        },
        "allowed_username": "someone",
    }))
    projects = load_project_configs(path)
    assert projects == {"good": {"path": "/some/path", "telegram_bot_token": "BOT_TOKEN"}}
    raw = json.loads(path.read_text())
    assert raw["projects"] == {"good": {"path": "/some/path", "telegram_bot_token": "BOT_TOKEN"}}
    assert raw["allowed_username"] == "someone"


def test_save_project_configs_preserves_other_keys(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"allowed_username": "someone", "projects": {}}))
    save_project_configs({"newproj": {"path": "/new"}}, path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["newproj"]["path"] == "/new"
    assert raw["allowed_username"] == "someone"


def test_save_project_configs_preserves_teams(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "projects": {},
        "teams": {
            "acme": {
                "path": "/a",
                "group_chat_id": -1,
                "bots": {"manager": {"telegram_bot_token": "t1"}},
            }
        },
    }))
    save_project_configs({"newproj": {"path": "/new"}}, path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["newproj"]["path"] == "/new"
    assert raw["teams"]["acme"]["bots"]["manager"]["telegram_bot_token"] == "t1"


def test_save_project_configs_creates_file(tmp_path: Path):
    path = tmp_path / "config.json"
    save_project_configs({"proj": {"path": "/p"}}, path)
    assert json.loads(path.read_text())["projects"]["proj"]["path"] == "/p"


def test_save_project_configs_preserves_backend_state(tmp_path: Path):
    """The raw-dict passthrough save must preserve the new backend/backend_state
    shape on disk so callers writing the new shape see it round-trip cleanly."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "projects": {
            "demo": {
                "path": "/tmp/demo",
                "backend": "claude",
                "backend_state": {"claude": {"model": "opus"}},
            }
        }
    }))
    save_project_configs(load_project_configs(path), path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["demo"]["backend"] == "claude"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"


def test_set_project_backend_updates_active_backend(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"myproj": {"path": "/p"}}}))
    set_project_backend("myproj", "anthropic_api", path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["myproj"]["backend"] == "anthropic_api"


def test_set_project_backend_unknown_project_is_noop(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"myproj": {"path": "/p"}}}))
    set_project_backend("ghost", "anthropic_api", path)
    raw = json.loads(path.read_text())
    assert "ghost" not in raw["projects"]
    assert "backend" not in raw["projects"]["myproj"]


def test_set_project_autostart(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"myproj": {"path": "/p"}}}))
    set_project_autostart("myproj", True, path)
    assert json.loads(path.read_text())["projects"]["myproj"]["autostart"] is True
    set_project_autostart("myproj", False, path)
    assert json.loads(path.read_text())["projects"]["myproj"]["autostart"] is False


def test_set_project_autostart_unknown_project_is_noop(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"myproj": {"path": "/p"}}}))
    set_project_autostart("ghost", True, path)
    raw = json.loads(path.read_text())
    assert "ghost" not in raw["projects"]
    assert "autostart" not in raw["projects"]["myproj"]


def test_set_team_bot_autostart(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "teams": {
            "acme": {
                "path": "/a",
                "group_chat_id": -1,
                "bots": {"manager": {"telegram_bot_token": "t1"}},
            }
        }
    }))
    set_team_bot_autostart("acme", "manager", True, path)
    raw = json.loads(path.read_text())
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is True
    # Preserves sibling fields
    assert raw["teams"]["acme"]["bots"]["manager"]["telegram_bot_token"] == "t1"
    assert raw["teams"]["acme"]["group_chat_id"] == -1

    set_team_bot_autostart("acme", "manager", False, path)
    raw = json.loads(path.read_text())
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is False


def test_set_team_bot_autostart_unknown_team_is_noop(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"teams": {}}))
    set_team_bot_autostart("ghost", "manager", True, path)
    raw = json.loads(path.read_text())
    assert "ghost" not in raw.get("teams", {})


def test_set_team_bot_autostart_unknown_role_is_noop(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "teams": {
            "acme": {
                "path": "/a",
                "group_chat_id": -1,
                "bots": {"manager": {"telegram_bot_token": "t1"}},
            }
        }
    }))
    set_team_bot_autostart("acme", "dev", True, path)
    raw = json.loads(path.read_text())
    assert "dev" not in raw["teams"]["acme"]["bots"]
    assert "autostart" not in raw["teams"]["acme"]["bots"]["manager"]


# Removed in Task 5 Step 12: ``test_trusted_user_id`` exercised
# ``load_trusted_user_id`` / ``save_trusted_user_id`` / ``clear_trusted_user_id``,
# all deleted along with the legacy fields.
