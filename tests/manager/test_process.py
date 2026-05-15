from __future__ import annotations

import json
import logging
import subprocess
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


def test_pidfile_deleted_when_child_exits_normally(tmp_path: Path):
    """A fast-exiting child leaves _capture_output to clean up self._processes,
    but the pidfile must also be removed — otherwise a later start() or
    reap_orphans() sees a phantom pidfile and either refuses the start or
    'adopts' an unrelated process that happens to land on the recycled pid.
    """
    run_dir = tmp_path / "run"
    pm = ProcessManager(
        project_config_path=_proj_cfg(tmp_path, {"fast": {"path": str(tmp_path)}}),
        command_builder=lambda n, c: _true_cmd(),
        run_dir=run_dir,
    )
    pm.start("fast")
    pidfile = run_dir / "fast.pid"
    assert pidfile.exists()
    # _true_cmd runs `python -c ""` which exits immediately.
    # Wait for _capture_output's reader thread to observe EOF + proc.wait.
    deadline = time.monotonic() + 5
    while pm._processes.get("fast") is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pm.status("fast") == "stopped"
    assert not pidfile.exists(), "pidfile must be cleaned up when child exits normally"


def test_start_writes_pidfile(tmp_path: Path):
    """Each running project must have a `.pid` file on disk so a freshly-spawned
    manager can detect orphans surviving a crash."""
    run_dir = tmp_path / "run"
    pm = ProcessManager(
        project_config_path=_proj_cfg(tmp_path, {"sl": {"path": str(tmp_path)}}),
        command_builder=lambda n, c: _sleep_cmd(),
        run_dir=run_dir,
    )
    pm.start("sl")
    try:
        pidfile = run_dir / "sl.pid"
        assert pidfile.exists()
        assert int(pidfile.read_text().strip()) == pm._processes["sl"].pid
    finally:
        pm.stop("sl")


def test_stop_removes_pidfile(tmp_path: Path):
    run_dir = tmp_path / "run"
    pm = ProcessManager(
        project_config_path=_proj_cfg(tmp_path, {"sl": {"path": str(tmp_path)}}),
        command_builder=lambda n, c: _sleep_cmd(),
        run_dir=run_dir,
    )
    pm.start("sl")
    pidfile = run_dir / "sl.pid"
    assert pidfile.exists()
    pm.stop("sl")
    assert not pidfile.exists()


def test_reap_orphans_clears_dead_pidfiles(tmp_path: Path):
    """A pidfile pointing at a dead pid is just a leftover from a prior crash;
    the reaper must delete it on startup so subsequent start() doesn't see
    'already running' against a phantom process."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # PID 999_999_999 is virtually guaranteed to not exist on Linux.
    (run_dir / "ghost.pid").write_text("999999999")
    pm = ProcessManager(
        project_config_path=_proj_cfg(tmp_path, {"ghost": {"path": str(tmp_path)}}),
        run_dir=run_dir,
    )
    adopted = pm.reap_orphans()
    assert adopted == []
    assert not (run_dir / "ghost.pid").exists()


def test_reap_orphans_adopts_live_orphan(tmp_path: Path):
    """When the manager finds a pidfile pointing at a live process, it must
    adopt it so the next status() / stop() can see and terminate it instead
    of letting it run unmanaged forever."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Spawn a real subprocess outside the ProcessManager to simulate an
    # orphan from a crashed prior manager.
    orphan = subprocess.Popen(
        _sleep_cmd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_process_popen_kwargs(),
    )
    try:
        (run_dir / "lonely.pid").write_text(str(orphan.pid))
        pm = ProcessManager(
            project_config_path=_proj_cfg(tmp_path, {"lonely": {"path": str(tmp_path)}}),
            run_dir=run_dir,
        )
        adopted = pm.reap_orphans()
        assert adopted == ["lonely"]
        assert pm.status("lonely") == "running"

        # A subsequent start() on the same name must refuse — the bot is
        # already running (adopted), so we must not spawn a duplicate.
        assert pm.start("lonely") is False

        assert pm.stop("lonely") is True
        assert not (run_dir / "lonely.pid").exists()
        assert pm.status("lonely") == "stopped"
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


def test_start_team_writes_pidfile_under_team_key(tmp_path: Path):
    """Team-bot subprocesses must also be tracked by pidfile so the orphan
    reaper covers them on the next manager start. The filename uses the
    full ``team:NAME:ROLE`` key so reap_orphans can round-trip via Path.stem.
    """
    from link_project_to_chat.config import (
        Config, TeamBotConfig, TeamConfig, save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "demo": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=1,
                    bots={"dev": TeamBotConfig(telegram_bot_token="tok")},
                ),
            },
        ),
        cfg_path,
    )
    run_dir = tmp_path / "run"
    pm = ProcessManager(project_config_path=cfg_path, run_dir=run_dir)

    with patch("link_project_to_chat.manager.process.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_proc.poll.return_value = None
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc
        assert pm.start_team("demo", "dev") is True

    pidfile = run_dir / "team:demo:dev.pid"
    assert pidfile.exists()
    assert int(pidfile.read_text().strip()) == 4242
    # And Path.stem round-trips the team key for reap_orphans:
    assert pidfile.stem == "team:demo:dev"


def test_reap_orphans_adopts_team_orphan(tmp_path: Path):
    """A surviving team-bot subprocess from a crashed manager must be adopted
    on next start, same as project bots. Without this the team bot keeps
    polling Telegram with its token and the new manager spawns a duplicate
    that hits 'Conflict: terminated by other getUpdates request'.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orphan = subprocess.Popen(
        _sleep_cmd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_process_popen_kwargs(),
    )
    try:
        (run_dir / "team:t1:dev.pid").write_text(str(orphan.pid))
        pm = ProcessManager(
            project_config_path=_proj_cfg(tmp_path, {}),
            run_dir=run_dir,
        )
        adopted = pm.reap_orphans()
        assert "team:t1:dev" in adopted
        assert pm.status("team:t1:dev") == "running"
        assert pm.stop("team:t1:dev") is True
        assert not (run_dir / "team:t1:dev.pid").exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


def test_start_team_refuses_when_pidfile_points_at_live_process(tmp_path: Path):
    """Same fence as start() — if a previous manager left a team pidfile
    pointing at a live foreign pid, start_team must refuse so we don't
    spawn a duplicate token-poller before the operator has run reap_orphans.
    """
    from link_project_to_chat.config import (
        Config, TeamBotConfig, TeamConfig, save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "t1": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=1,
                    bots={"mgr": TeamBotConfig(telegram_bot_token="tok")},
                ),
            },
        ),
        cfg_path,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orphan = subprocess.Popen(
        _sleep_cmd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_process_popen_kwargs(),
    )
    try:
        (run_dir / "team:t1:mgr.pid").write_text(str(orphan.pid))
        pm = ProcessManager(project_config_path=cfg_path, run_dir=run_dir)
        assert pm.start_team("t1", "mgr") is False
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


def test_start_autostart_skips_adopted_orphan(tmp_path: Path):
    """Reaping must run BEFORE start_autostart so an adopted survivor isn't
    duplicated into a second running bot polling the same Telegram token."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orphan = subprocess.Popen(
        _sleep_cmd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_process_popen_kwargs(),
    )
    try:
        (run_dir / "auto.pid").write_text(str(orphan.pid))
        pm = ProcessManager(
            project_config_path=_proj_cfg(tmp_path, {
                "auto": {"path": str(tmp_path), "autostart": True},
            }),
            command_builder=lambda n, c: _sleep_cmd(),
            run_dir=run_dir,
        )
        adopted = pm.reap_orphans()
        assert adopted == ["auto"]
        # autostart honors adoption: no new process spawned for "auto" because
        # start() short-circuits on the already-managed entry.
        started = pm.start_autostart()
        assert started == 0
        assert pm.status("auto") == "running"
        pm.stop("auto")
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


def test_start_refuses_when_pidfile_points_at_live_process(tmp_path: Path):
    """start() must return False if a pidfile already points at a live process,
    even if that process isn't in self._processes — covers the 'manager just
    restarted, hasn't called reap_orphans yet' window."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orphan = subprocess.Popen(
        _sleep_cmd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_process_popen_kwargs(),
    )
    try:
        (run_dir / "twin.pid").write_text(str(orphan.pid))
        pm = ProcessManager(
            project_config_path=_proj_cfg(tmp_path, {"twin": {"path": str(tmp_path)}}),
            command_builder=lambda n, c: _sleep_cmd(),
            run_dir=run_dir,
        )
        assert pm.start("twin") is False
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


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
    assert call_args[:6] == [
        sys.executable,
        "-m",
        "link_project_to_chat.cli",
        "--config",
        str(cfg_path),
        "start",
    ]
    assert "--project" in call_args and "proj" in call_args
    assert "--model" in call_args and "opus[1m]" in call_args


def test_start_codex_project_ignores_claude_default_model(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            default_model_claude="opus[1m]",
            projects={
                "proj": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    backend="codex",
                ),
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
    assert "--project" in call_args and "proj" in call_args
    assert "--model" not in call_args


def test_start_codex_project_uses_codex_backend_state_model(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            default_model_claude="opus[1m]",
            projects={
                "proj": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    backend="codex",
                    backend_state={"codex": {"model": "gpt-5.5"}},
                ),
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
    assert "--model" in call_args and "gpt-5.5" in call_args


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
    import subprocess as _subp
    proc = MagicMock()
    proc.pid = 123
    proc.poll.return_value = None
    proc.stdout = []
    # Keep the capture thread blocked in wait() so _capture_output doesn't
    # prematurely mark the process as stopped before the test calls pm.stop().
    proc.wait.side_effect = _subp.TimeoutExpired(cmd="x", timeout=0.1)

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


def test_child_output_is_forwarded_to_logger(tmp_path: Path, caplog):
    pm = _pm(
        tmp_path,
        {"echo": {"path": str(tmp_path)}},
        command_builder=lambda n, c: [
            sys.executable,
            "-c",
            "import sys,time; print('hello from child', flush=True); time.sleep(0.2)",
        ],
    )
    with caplog.at_level(logging.INFO):
        pm.start("echo")
        time.sleep(0.4)
    assert any("[echo] hello from child" in rec.getMessage() for rec in caplog.records)


def test_child_exit_is_logged(tmp_path: Path, caplog):
    pm = _pm(
        tmp_path,
        {"boom": {"path": str(tmp_path)}},
        command_builder=lambda n, c: [
            sys.executable,
            "-c",
            "import sys; print('about to fail', flush=True); sys.exit(7)",
        ],
    )
    with caplog.at_level(logging.INFO):
        pm.start("boom")
        time.sleep(0.4)
    assert any("boom exited with code 7" in rec.getMessage() for rec in caplog.records)
    assert pm.status("boom") == "stopped"
