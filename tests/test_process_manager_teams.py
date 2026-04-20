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


def test_start_team_persists_autostart_true(tmp_path):
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
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        assert pm.start_team("acme", "manager") is True

    raw = json.loads(cfg_path.read_text())
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is True


def test_stop_team_key_persists_autostart_false(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1", autostart=True)},
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 2
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        pm.start_team("acme", "manager")
        assert pm.stop("team:acme:manager") is True

    raw = json.loads(cfg_path.read_text())
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is False


def test_start_autostart_starts_team_bots_with_autostart(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", autostart=True),
                    "dev":     TeamBotConfig(telegram_bot_token="t2", autostart=False),
                },
            ),
            "beta": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1002,
                bots={"manager": TeamBotConfig(telegram_bot_token="t3", autostart=True)},
            ),
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 3
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        count = pm.start_autostart()

    assert count == 2
    assert "team:acme:manager" in pm._processes
    assert "team:beta:manager" in pm._processes
    assert "team:acme:dev" not in pm._processes


def test_start_autostart_skips_teams_with_sentinel_chat_id(tmp_path):
    """group_chat_id=0 means 'not captured yet' — autostarting would waste a process."""
    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "pending": TeamConfig(
                path=str(tmp_path),
                group_chat_id=0,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1", autostart=True)},
            ),
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        pm = ProcessManager(project_config_path=cfg_path)
        count = pm.start_autostart()

    assert count == 0
    assert "team:pending:manager" not in pm._processes
    mock_popen.assert_not_called()
