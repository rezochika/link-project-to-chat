from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

from link_project_to_chat.config import Config, ProjectConfig, TeamBotConfig, TeamConfig, save_config
from link_project_to_chat.manager.process import ProcessManager


def _running_proc(pid: int, stop_return: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.stdout = iter(())

    def _wait(timeout=None):
        if timeout == 0.1:
            raise subprocess.TimeoutExpired(cmd="test", timeout=timeout)
        return stop_return

    proc.wait.side_effect = _wait
    return proc


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
        mock_popen.return_value = _running_proc(12345)

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
        mock_popen.return_value = _running_proc(12345)

        pm = ProcessManager(project_config_path=cfg_path)
        result = pm.start_team("acme", "manager")
        assert result is True
        call_args = mock_popen.call_args[0][0]
        assert call_args[:6] == [
            sys.executable,
            "-m",
            "link_project_to_chat.cli",
            "--config",
            str(cfg_path),
            "start",
        ]
        assert "--team" in call_args and "acme" in call_args
        assert "--role" in call_args and "manager" in call_args


def test_start_project_uses_current_python_and_config_path(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="t1",
            )
        }
    )
    save_config(config, cfg_path)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _running_proc(12345)

        pm = ProcessManager(project_config_path=cfg_path)
        result = pm.start("alpha")
        assert result is True
        call_args = mock_popen.call_args[0][0]
        assert call_args[:8] == [
            sys.executable,
            "-m",
            "link_project_to_chat.cli",
            "--config",
            str(cfg_path),
            "start",
            "--project",
            "alpha",
        ]


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
        mock_popen.return_value = _running_proc(12345)

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
        mock_popen.return_value = _running_proc(1)

        pm = ProcessManager(project_config_path=cfg_path)
        assert pm.start_team("acme", "manager") is True

    raw = json.loads(cfg_path.read_text())
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is True


def test_stop_team_key_persists_autostart_false(tmp_path, monkeypatch):
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

    # Replace _terminate_process_tree so it doesn't try to run taskkill/kill
    # on the mocked Popen object.
    monkeypatch.setattr(
        "link_project_to_chat.manager.process._terminate_process_tree",
        lambda p: None,
    )

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _running_proc(2)

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
        mock_popen.return_value = _running_proc(3)

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


def test_team_bot_spawn_passes_telethon_session_env_var(tmp_path):
    """Team-mode bot subprocess gets LP2C_TELETHON_SESSION pointing at the
    manager's Telethon session file so the project bot can attach a relay."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    env = _build_project_bot_env(team_name="acme", config_dir=tmp_path)
    assert env.get("LP2C_TELETHON_SESSION") == str(session_path)


def test_solo_bot_spawn_does_not_set_telethon_session_env_var(tmp_path):
    """Solo-mode bots (team_name=None) don't get the Telethon session env var
    even if the file exists — they don't need the relay."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    (tmp_path / "telethon.session").touch()
    env = _build_project_bot_env(team_name=None, config_dir=tmp_path)
    assert "LP2C_TELETHON_SESSION" not in env


def test_team_bot_spawn_without_session_file_does_not_set_env_var(tmp_path):
    """Team-mode bot launched before /setup (no session file yet) doesn't
    get the env var — pointing at a missing file would be misleading."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    # Don't touch session_path — file absent.
    env = _build_project_bot_env(team_name="acme", config_dir=tmp_path)
    assert "LP2C_TELETHON_SESSION" not in env


def test_build_project_bot_env_does_not_mutate_os_environ(tmp_path, monkeypatch):
    """Helper returns a fresh copy of os.environ — no shared state across spawns."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    monkeypatch.delenv("LP2C_TELETHON_SESSION", raising=False)
    (tmp_path / "telethon.session").touch()
    env = _build_project_bot_env(team_name="acme", config_dir=tmp_path)
    assert env.get("LP2C_TELETHON_SESSION") is not None
    import os
    assert "LP2C_TELETHON_SESSION" not in os.environ


def test_build_project_bot_env_uses_absolute_session_path(tmp_path, monkeypatch):
    """Even if config_dir is relative, the env var carries an absolute path
    so subprocess cwd changes can't desync the relay session."""
    from link_project_to_chat.manager.process import _build_project_bot_env
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "telethon.session").touch()
    env = _build_project_bot_env(team_name="acme", config_dir=Path("."))
    assert "LP2C_TELETHON_SESSION" in env
    session = Path(env["LP2C_TELETHON_SESSION"])
    assert session.is_absolute()
    assert session == (tmp_path / "telethon.session").resolve()


def test_build_project_bot_env_passes_session_string_when_provided(tmp_path, monkeypatch):
    """Spec D′: when the caller supplies a StringSession export, the helper
    sets LP2C_TELETHON_SESSION_STRING and skips the file-path fallback so
    subprocesses don't open the shared SQLite session."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    monkeypatch.delenv("LP2C_TELETHON_SESSION", raising=False)
    monkeypatch.delenv("LP2C_TELETHON_SESSION_STRING", raising=False)
    (tmp_path / "telethon.session").touch()
    env = _build_project_bot_env(
        team_name="acme",
        config_dir=tmp_path,
        session_string="1$abc",
    )
    assert env.get("LP2C_TELETHON_SESSION_STRING") == "1$abc"
    assert "LP2C_TELETHON_SESSION" not in env


def test_build_project_bot_env_solo_mode_ignores_session_string(tmp_path, monkeypatch):
    """Solo bots have no relay; even an explicit session_string must not leak
    into their env (would be ignored anyway, but cleaner to omit)."""
    from link_project_to_chat.manager.process import _build_project_bot_env

    monkeypatch.delenv("LP2C_TELETHON_SESSION_STRING", raising=False)
    env = _build_project_bot_env(
        team_name=None,
        config_dir=tmp_path,
        session_string="1$abc",
    )
    assert "LP2C_TELETHON_SESSION_STRING" not in env


def test_export_telethon_session_string_roundtrips_real_session(tmp_path):
    """Exporting a real (empty but schema-initialised) Telethon SQLite session
    yields a StringSession string that Telethon can parse back. Empty session
    counts as 'no auth_key' → helper returns None so callers fall back."""
    from telethon.sessions import SQLiteSession

    from link_project_to_chat.manager.process import _export_telethon_session_string

    session_path = tmp_path / "telethon.session"
    # SQLiteSession's __init__ runs the schema migrations, leaving a valid but
    # unauthorized session file on disk.
    sql = SQLiteSession(str(session_path))
    sql.close()

    result = _export_telethon_session_string(session_path)
    # Empty session = no auth_key → no useful StringSession, helper returns None.
    assert result is None


def test_export_telethon_session_string_returns_string_for_authorized_session(tmp_path):
    """An authorized session (auth_key + dc info present) round-trips through
    StringSession.save → StringSession(...) cleanly."""
    from telethon.crypto import AuthKey
    from telethon.sessions import SQLiteSession, StringSession

    from link_project_to_chat.manager.process import _export_telethon_session_string

    session_path = tmp_path / "telethon.session"
    sql = SQLiteSession(str(session_path))
    sql.set_dc(2, "149.154.167.51", 443)
    sql.auth_key = AuthKey(b"\x01" * 256)
    sql.save()
    sql.close()

    result = _export_telethon_session_string(session_path)
    assert isinstance(result, str)
    assert result.startswith("1")  # StringSession version prefix
    # And it round-trips:
    parsed = StringSession(result)
    assert parsed.dc_id == 2
    assert parsed.server_address == "149.154.167.51"
    assert parsed.port == 443
    assert parsed.auth_key.key == b"\x01" * 256


def test_export_telethon_session_string_returns_none_when_file_missing(tmp_path):
    """Missing session file → None (caller falls back to path-mode env var
    or skips relay entirely)."""
    from link_project_to_chat.manager.process import _export_telethon_session_string

    result = _export_telethon_session_string(tmp_path / "does-not-exist.session")
    assert result is None


def test_start_team_passes_string_session_env_to_subprocess(tmp_path, monkeypatch):
    """Spec D′ end-to-end: ``start_team`` exports the manager's Telethon session
    once and passes it to the subprocess via ``LP2C_TELETHON_SESSION_STRING``,
    so concurrent team-bot starts don't race for the SQLite write lock.
    """
    from telethon.crypto import AuthKey
    from telethon.sessions import SQLiteSession

    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-100,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(config, cfg_path)

    # Authorize a real session file in the manager's config dir so the export
    # produces a non-empty StringSession.
    session_path = tmp_path / "telethon.session"
    sql = SQLiteSession(str(session_path))
    sql.set_dc(2, "149.154.167.51", 443)
    sql.auth_key = AuthKey(b"\x02" * 256)
    sql.save()
    sql.close()

    captured: dict = {}

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _running_proc(pid=99)

    pm = ProcessManager(project_config_path=cfg_path)
    monkeypatch.setattr("link_project_to_chat.manager.process.subprocess.Popen", fake_popen)
    assert pm.start_team("acme", "manager") is True

    env = captured["env"]
    assert env is not None
    assert env.get("LP2C_TELETHON_SESSION_STRING")
    # Path-mode env var is suppressed when the string variant is set.
    assert "LP2C_TELETHON_SESSION" not in env
