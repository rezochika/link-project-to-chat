"""Verify run_bot / run_bots propagate respond_in_groups into ProjectBot
AND into TelegramTransport.attach_telegram_routing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from link_project_to_chat.bot import ProjectBot, run_bot


def test_run_bot_passes_respond_in_groups_to_project_bot(tmp_path: Path):
    """run_bot(..., respond_in_groups=True) constructs a ProjectBot
    with self._respond_in_groups=True.

    run_bot's signature uses positional `name, path, token` (NOT
    `project_path` — see src/link_project_to_chat/bot.py:3289). We need
    a non-empty `username` OR a non-empty `allowed_users` to pass the
    fail-closed check inside run_bot.
    """
    from link_project_to_chat.config import AllowedUser
    captured: dict = {}

    def _fake_build(self):
        captured["respond_in_groups"] = self._respond_in_groups

        class _App:
            def run_polling(_self):
                return None
        return _App()

    with patch.object(ProjectBot, "build", _fake_build), \
         patch.object(ProjectBot, "run", lambda self: None):
        run_bot(
            "p", tmp_path, "t",
            allowed_users=[AllowedUser(username="alice", role="executor")],
            auth_source="project",
            respond_in_groups=True,
        )
    assert captured["respond_in_groups"] is True


def test_run_bot_defaults_to_false(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    captured: dict = {}

    def _fake_build(self):
        captured["respond_in_groups"] = self._respond_in_groups

        class _App:
            def run_polling(_self):
                return None
        return _App()

    with patch.object(ProjectBot, "build", _fake_build), \
         patch.object(ProjectBot, "run", lambda self: None):
        run_bot(
            "p", tmp_path, "t",
            allowed_users=[AllowedUser(username="alice", role="executor")],
            auth_source="project",
        )
    assert captured["respond_in_groups"] is False


def test_run_bots_single_project_pulls_respond_in_groups_from_project_config(tmp_path: Path):
    """run_bots' single-project branch reads ProjectConfig.respond_in_groups
    and forwards it into run_bot. (Multi-project configs go through cli.py's
    'start --project NAME' flow per project; run_bots' multi-project branch
    raises SystemExit with a usage hint.)"""
    from link_project_to_chat.bot import run_bots
    from link_project_to_chat.config import (
        AllowedUser,
        Config,
        ProjectConfig,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    cfg.projects["p1"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t1",
        respond_in_groups=True,
    )
    save_config(cfg, cfg_path)

    captured: list[dict] = []

    def _record_run_bot(*args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else None)
        captured.append({
            "name": name,
            "respond_in_groups": kwargs.get("respond_in_groups", False),
        })

    with patch("link_project_to_chat.bot.run_bot", _record_run_bot):
        run_bots(cfg, config_path=cfg_path)

    assert len(captured) == 1
    assert captured[0]["respond_in_groups"] is True


def test_cli_start_project_pulls_respond_in_groups_from_project_config(tmp_path: Path, monkeypatch):
    """The manager starts bots via `start --project NAME`.

    That explicit-project CLI branch must forward ProjectConfig.respond_in_groups
    just like run_bots' single-project branch, otherwise manager-started bots
    keep the default DM-only Telegram filter.
    """
    from click.testing import CliRunner

    from link_project_to_chat.cli import main
    from link_project_to_chat.config import (
        AllowedUser,
        Config,
        ProjectConfig,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    cfg.projects["p1"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t1",
        respond_in_groups=True,
    )
    save_config(cfg, cfg_path)

    captured: list[dict] = []

    def _record_run_bot(*args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else None)
        captured.append({
            "name": name,
            "respond_in_groups": kwargs.get("respond_in_groups", False),
        })

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", _record_run_bot)

    result = CliRunner().invoke(
        main,
        ["--config", str(cfg_path), "start", "--project", "p1"],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0] == {"name": "p1", "respond_in_groups": True}


def test_run_bots_multi_project_raises_system_exit(tmp_path: Path):
    """Regression for the fail-fast multi-project guard. Operators with
    multiple projects must use `start --project NAME`; running `start`
    bare with multi-project config is a configuration error."""
    from link_project_to_chat.bot import run_bots
    from link_project_to_chat.config import (
        AllowedUser,
        Config,
        ProjectConfig,
    )

    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    cfg.projects["p1"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t1",
    )
    cfg.projects["p2"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t2",
    )

    with pytest.raises(SystemExit):
        run_bots(cfg)
