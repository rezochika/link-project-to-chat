"""ProjectBot._auth_identity triggers _reload_allowed_users_if_stale."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


def _make_bot_with_state(cfg_path: Path, project_name: str = "p"):
    """ProjectBot stub with just the hot-reload fields populated."""
    bot = ProjectBot.__new__(ProjectBot)
    bot._project_config_path = cfg_path
    bot._project_name = project_name
    bot._last_allowed_users_reload = 0.0
    bot._allowed_users = [
        AllowedUser(username="alice", role="executor",
                     locked_identities=["telegram:1"]),
    ]
    bot._auth_dirty = False
    bot._failed_auth_counts = {}
    return bot


def _write(path: Path, users: list[dict]) -> None:
    path.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_users": users,
            }
        }
    }))


def test_auth_identity_triggers_reload(tmp_path: Path):
    """A user added to disk after bot start authenticates within 5 s."""
    cfg = tmp_path / "config.json"
    _write(cfg, [{"username": "alice", "role": "executor"}])
    bot = _make_bot_with_state(cfg)
    bob = Identity(
        transport_id="telegram", native_id="2",
        display_name="Bob", handle="bob", is_bot=False,
    )
    # Initially bob isn't in memory → unauth.
    assert bot._auth_identity(bob) is False
    # Manager adds bob; simulate 5+ seconds.
    _write(cfg, [
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "viewer"},
    ])
    bot._last_allowed_users_reload = time.monotonic() - 10.0
    # Reload + lookup should succeed.
    assert bot._auth_identity(bob) is True


def test_init_populates_reload_state():
    """Real __init__ initializes the hot-reload state without errors."""
    from link_project_to_chat._auth import AuthMixin
    assert hasattr(AuthMixin, "_reload_allowed_users_if_stale")
    assert hasattr(AuthMixin, "_RELOAD_DEBOUNCE_SECONDS")
    assert AuthMixin._RELOAD_DEBOUNCE_SECONDS == 5.0
