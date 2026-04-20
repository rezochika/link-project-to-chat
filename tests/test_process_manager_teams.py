from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
from link_project_to_chat.manager.process import ProcessManager


def test_start_team_unknown_team_returns_false(tmp_path):
    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    pm = ProcessManager(project_config_path=cfg_path)
    assert pm.start_team("ghost", "manager") is False
    assert "team:ghost:manager" not in pm._processes


def test_start_team_unknown_role_returns_false(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)
    pm = ProcessManager(project_config_path=cfg_path)
    assert pm.start_team("acme", "dev") is False
    assert "team:acme:dev" not in pm._processes


def test_start_team_already_running_returns_false(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        assert pm.start_team("acme", "manager") is True
        assert pm.start_team("acme", "manager") is False  # second call: already running
        assert mock_popen.call_count == 1


def test_start_team_builds_correct_cli_and_spawns(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1"),
                    "dev":     TeamBotConfig(telegram_bot_token="t2"),
                },
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        result = pm.start_team("acme", "manager")
        assert result is True
        call_args = mock_popen.call_args[0][0]
        assert call_args[:2] == ["link-project-to-chat", "start"]
        assert "--team" in call_args and "acme" in call_args
        assert "--role" in call_args and "manager" in call_args


def test_start_team_uses_compound_process_key(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        pm.start_team("acme", "manager")
        assert "team:acme:manager" in pm._processes
