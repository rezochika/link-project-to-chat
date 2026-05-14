from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager


def _make_bot(tmp_path: Path) -> ManagerBot:
    from link_project_to_chat.config import AllowedUser
    cfg = tmp_path / "config.json"
    cfg.write_text('{"projects": {}}')
    pm = ProcessManager(project_config_path=cfg, command_builder=lambda n, c: ["echo", n])
    return ManagerBot(
        token="test-token",
        process_manager=pm,
        allowed_users=[
            AllowedUser(username="testuser", role="executor", locked_identities=["telegram:1"]),
        ],
        project_config_path=cfg,
    )


def test_create_project_states_defined(tmp_path):
    bot = _make_bot(tmp_path)
    assert hasattr(bot, "CREATE_SOURCE")
    assert hasattr(bot, "CREATE_REPO_LIST")
    assert hasattr(bot, "CREATE_REPO_URL")
    assert hasattr(bot, "CREATE_NAME")
    assert hasattr(bot, "CREATE_BOT")
    assert hasattr(bot, "CREATE_CLONE")
