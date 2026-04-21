from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from link_project_to_chat.config import Config, ProjectConfig, save_config
from link_project_to_chat.manager.process import ProcessManager, _process_popen_kwargs


def _proj_cfg(tmp_path: Path, projects: dict) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": projects}))
    return path


def _pm(tmp_path: Path, projects: dict, command_builder=None) -> ProcessManager:
    return ProcessManager(
        project_config_path=_proj_cfg(tmp_path, projects),
        command_builder=command_builder,
    )


def _sleep_cmd(seconds: int = 60) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _echo_and_sleep_cmd() -> list[str]:
    return [
        sys.executable,
        "-c",
        "import sys,time; print('hello'); print('world'); sys.stdout.flush(); time.sleep(60)",
    ]


def _true_cmd() -> list[str]:
    return [sys.executable, "-c", ""]


def test_list_all_empty(tmp_path: Path):
    assert _pm(tmp_path, {}).list_all() == []


def test_list_all_shows_projects(tmp_path: Path):
    pm = _pm(tmp_path, {"a": {"path": "/a"}, "b": {"path": "/b"}})
    names = [n for n, _ in pm.list_all()]
    assert "a" in names and "b" in names
    assert all(s == "stopped" for _, s in pm.list_all())


def test_start_unknown(tmp_path: Path):
    assert _pm(tmp_path, {}).start("nope") is False


def test_start_stop(tmp_path: Path):
    pm = _pm(tmp_path, {"s": {"path": str(tmp_path)}}, command_builder=lambda n, c: _sleep_cmd())
    assert pm.start("s") is True
    assert pm.status("s") == "running"
    assert pm.start("s") is False  # already running
    assert pm.stop("s") is True
    assert pm.status("s") == "stopped"


def test_stop_not_running(tmp_path: Path):
    assert _pm(tmp_path, {"p": {"path": "/a"}}).stop("p") is False


def test_logs_captured(tmp_path: Path):
    pm = _pm(tmp_path, {"e": {"path": str(tmp_path)}},
             command_builder=lambda n, c: _echo_and_sleep_cmd())
    pm.start("e")
    time.sleep(0.5)
    logs = pm.logs("e")
    assert "hello" in logs and "world" in logs
    pm.stop("e")


def test_start_all_stop_all(tmp_path: Path):
    pm = _pm(tmp_path, {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}},
             command_builder=lambda n, c: _sleep_cmd())
    assert pm.start_all() == 2
    assert pm.stop_all() == 2


def test_start_autostart(tmp_path: Path):
    projects = {
        "a": {"path": str(tmp_path), "autostart": True},
        "b": {"path": str(tmp_path), "autostart": False},
    }
    pm = _pm(tmp_path, projects, command_builder=lambda n, c: _sleep_cmd())
    assert pm.start_autostart() == 1
    assert pm.status("a") == "running"
    assert pm.status("b") == "stopped"
    pm.stop_all()


def test_stale_process_detected(tmp_path: Path):
    pm = _pm(tmp_path, {"fast": {"path": str(tmp_path)}}, command_builder=lambda n, c: _true_cmd())
    pm.start("fast")
    time.sleep(0.3)
    assert pm.status("fast") == "stopped"


def test_start_uses_custom_config_path_and_default_model(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            default_model="opus[1m]",
            projects={
                "proj": ProjectConfig(path=str(tmp_path), telegram_bot_token="tok"),
            },
        ),
        cfg_path,
    )

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 123
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = ProcessManager(project_config_path=cfg_path)
        assert pm.start("proj") is True

    call_args = mock_popen.call_args[0][0]
    assert call_args[:4] == [
        "link-project-to-chat",
        "--config",
        str(cfg_path.resolve()),
        "start",
    ]
    assert "--project" in call_args and "proj" in call_args
    assert "--model" in call_args and "opus[1m]" in call_args


def test_start_uses_process_group_kwargs(tmp_path: Path):
    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 123
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        pm = _pm(tmp_path, {"proj": {"path": str(tmp_path)}})
        assert pm.start("proj") is True

    for key, value in _process_popen_kwargs().items():
        assert mock_popen.call_args.kwargs[key] == value


def test_stop_uses_process_tree_termination(tmp_path: Path, monkeypatch):
    proc = MagicMock()
    proc.pid = 123
    proc.poll.return_value = None
    proc.stdout = []

    with patch("link_project_to_chat.manager.process.subprocess.Popen", return_value=proc):
        pm = _pm(tmp_path, {"proj": {"path": str(tmp_path)}})
        assert pm.start("proj") is True

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.manager.process._terminate_process_tree",
        lambda p: calls.append(p),
    )

    assert pm.stop("proj") is True
    assert calls == [proc]
