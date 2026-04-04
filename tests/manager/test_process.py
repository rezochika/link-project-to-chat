from __future__ import annotations

import json
import time
from pathlib import Path

from link_project_to_chat.manager.config import ManagerConfig
from link_project_to_chat.manager.process import ProcessManager


def _proj_cfg(tmp_path: Path, projects: dict) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": projects}))
    return path


def _pm(tmp_path: Path, projects: dict, command_builder=None) -> ProcessManager:
    return ProcessManager(
        config=ManagerConfig(),
        project_config_path=_proj_cfg(tmp_path, projects),
        state_path=tmp_path / "state.json",
        command_builder=command_builder,
    )


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
    pm = _pm(tmp_path, {"s": {"path": str(tmp_path)}}, command_builder=lambda n, c, f: ["sleep", "60"])
    assert pm.start("s") is True
    assert pm.status("s") == "running"
    assert pm.start("s") is False  # already running
    assert pm.stop("s") is True
    assert pm.status("s") == "stopped"
    state = json.loads((tmp_path / "state.json").read_text())
    assert "s" not in state["running"]


def test_stop_not_running(tmp_path: Path):
    assert _pm(tmp_path, {"p": {"path": "/a"}}).stop("p") is False


def test_logs_captured(tmp_path: Path):
    pm = _pm(tmp_path, {"e": {"path": str(tmp_path)}},
             command_builder=lambda n, c, f: ["bash", "-c", "echo hello; echo world; sleep 60"])
    pm.start("e")
    time.sleep(0.5)
    logs = pm.logs("e")
    assert "hello" in logs and "world" in logs
    pm.stop("e")


def test_start_all_stop_all(tmp_path: Path):
    pm = _pm(tmp_path, {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}},
             command_builder=lambda n, c, f: ["sleep", "60"])
    assert pm.start_all() == 2
    assert pm.stop_all() == 2


def test_restore(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"running": ["a"]}))
    pm = ProcessManager(
        config=ManagerConfig(),
        project_config_path=_proj_cfg(tmp_path, {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}}),
        state_path=state_path,
        command_builder=lambda n, c, f: ["sleep", "60"],
    )
    assert pm.restore() == 1
    assert pm.status("a") == "running"
    assert pm.status("b") == "stopped"
    pm.stop_all()


def test_stale_process_detected(tmp_path: Path):
    pm = _pm(tmp_path, {"fast": {"path": str(tmp_path)}}, command_builder=lambda n, c, f: ["true"])
    pm.start("fast")
    time.sleep(0.3)
    assert pm.status("fast") == "stopped"
