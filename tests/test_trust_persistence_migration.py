"""A1 — trusted_user persistence accepts opaque string ids (Web/Discord).

Drops the historical int(user_id) cast in bind_trusted_user /
bind_project_trusted_user so non-numeric native ids round-trip through
config save→load. Legacy int-typed entries continue to load (mixed-key
dicts are tolerated by AuthMixin per PR #6 0ad608e).
"""
from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    bind_project_trusted_user,
    bind_trusted_user,
    load_config,
)


def test_bind_trusted_user_accepts_non_numeric_id(tmp_path: Path) -> None:
    """A Web/Discord user_id (string snowflake or arbitrary id) must persist."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"telegram_bot_token": "x", "allowed_usernames": ["alice"]}))
    bind_trusted_user(username="alice", user_id="web-user-abc-123", path=cfg)
    raw = json.loads(cfg.read_text())
    assert raw["trusted_users"]["alice"] == "web-user-abc-123"


def test_load_config_round_trips_string_trusted_user(tmp_path: Path) -> None:
    """Saved string ids must round-trip through load_config without int-coercion."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "telegram_bot_token": "x",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "web-user-abc-123"},
            }
        )
    )
    loaded = load_config(cfg)
    assert loaded.trusted_users["alice"] == "web-user-abc-123"


def test_legacy_int_trusted_user_still_loads(tmp_path: Path) -> None:
    """Existing user configs with int values must keep working."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "telegram_bot_token": "x",
                "allowed_usernames": ["bob"],
                "trusted_users": {"bob": 42},
            }
        )
    )
    loaded = load_config(cfg)
    # Stored as-is; AuthMixin handles mixed-key lookups (per PR #6 0ad608e).
    assert loaded.trusted_users["bob"] == 42


def test_bind_project_trusted_user_accepts_non_numeric_id(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": "/tmp",
                        "telegram_bot_token": "y",
                        "allowed_usernames": ["carol"],
                    }
                },
            }
        )
    )
    bind_project_trusted_user(
        "myproj",
        username="carol",
        user_id="discord-snowflake-789",
        path=cfg,
    )
    raw = json.loads(cfg.read_text())
    assert raw["projects"]["myproj"]["trusted_users"]["carol"] == "discord-snowflake-789"
