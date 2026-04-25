import json
from pathlib import Path

from link_project_to_chat.config import load_config, save_config


def test_legacy_project_fields_migrate_into_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_model": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "model": "opus",
                        "effort": "high",
                        "permissions": "plan",
                        "session_id": "sess-1",
                        "show_thinking": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    project = config.projects["demo"]

    assert project.backend == "claude"
    assert project.backend_state["claude"]["model"] == "opus"
    assert project.backend_state["claude"]["session_id"] == "sess-1"
    assert config.default_backend == "claude"
    assert config.default_model_claude == "sonnet"


def test_legacy_team_bot_fields_migrate_into_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "alpha": {
                        "path": str(tmp_path),
                        "group_chat_id": -100,
                        "bots": {
                            "primary": {
                                "telegram_bot_token": "tok",
                                "model": "opus",
                                "effort": "high",
                                "permissions": "plan",
                                "session_id": "sess-1",
                                "show_thinking": True,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    team = config.teams["alpha"].bots["primary"]

    assert team.backend == "claude"
    assert team.backend_state["claude"]["model"] == "opus"
    assert team.backend_state["claude"]["session_id"] == "sess-1"


def test_new_shape_round_trip_preserves_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_backend": "claude",
                "default_model_claude": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {
                            "claude": {
                                "model": "opus",
                                "session_id": "sess-1",
                                "permissions": "plan",
                                "show_thinking": True,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    save_config(config, path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend"] == "claude"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
    assert raw["projects"]["demo"]["session_id"] == "sess-1"
    assert raw["default_model"] == "sonnet"


def test_save_session_writes_backend_state_and_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-1", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "sess-1"
    assert raw["projects"]["demo"]["session_id"] == "sess-1"


def test_save_session_uses_active_non_claude_backend_without_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "codex",
                        "backend_state": {"codex": {}, "claude": {"session_id": "old-claude"}},
                        "session_id": "old-claude",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-codex", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["codex"]["session_id"] == "sess-codex"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "old-claude"
    assert raw["projects"]["demo"]["session_id"] == "old-claude"


def test_load_session_prefers_backend_state(tmp_path: Path):
    from link_project_to_chat.config import load_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "new-shape"}},
                        "session_id": "legacy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_session("demo", path) == "new-shape"


def test_clear_session_removes_backend_state_and_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import clear_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "sess-1"}},
                        "session_id": "sess-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    clear_session("demo", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert "session_id" not in raw["projects"]["demo"]["backend_state"]["claude"]
    assert "session_id" not in raw["projects"]["demo"]
