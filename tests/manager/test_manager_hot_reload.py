"""Manager bot picks up global allowed_users changes from disk within 5 s."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from link_project_to_chat.config import AllowedUser
from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager
from link_project_to_chat.transport.base import Identity


def _write_global(path: Path, users: list[dict]) -> None:
    path.write_text(json.dumps({
        "allowed_users": users,
        "projects": {},
    }))


def test_manager_auth_identity_triggers_reload(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_global(cfg, [{"username": "alice", "role": "executor"}])
    pm = ProcessManager(project_config_path=cfg)
    bot = ManagerBot(
        "TOKEN", pm,
        allowed_users=[AllowedUser(
            username="alice", role="executor",
            locked_identities=["telegram:1"],
        )],
        project_config_path=cfg,
    )
    bob = Identity(
        transport_id="telegram", native_id="2",
        display_name="Bob", handle="bob", is_bot=False,
    )
    assert bot._auth_identity(bob) is False
    _write_global(cfg, [
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "executor"},
    ])
    bot._last_allowed_users_reload = time.monotonic() - 10.0
    assert bot._auth_identity(bob) is True


def test_manager_init_sets_project_name_none(tmp_path: Path):
    """Manager scope: _project_name is None so reload reads global
    allowed_users, not a project's."""
    cfg = tmp_path / "config.json"
    _write_global(cfg, [])
    pm = ProcessManager(project_config_path=cfg)
    bot = ManagerBot("TOKEN", pm, allowed_users=[], project_config_path=cfg)
    assert bot._project_name is None
    assert bot._project_config_path == cfg
