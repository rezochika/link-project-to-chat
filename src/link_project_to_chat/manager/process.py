from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .config import load_project_configs, set_project_autostart, set_team_bot_autostart
from ..config import DEFAULT_CONFIG, load_config

logger = logging.getLogger(__name__)


def _build_project_bot_env(team_name: str | None, config_dir: Path) -> dict[str, str]:
    """Build the env dict for a project-bot subprocess.

    Returns a fresh copy of ``os.environ`` so callers can mutate it without
    leaking state into the parent process. When the bot is team-mode AND the
    manager's Telethon session file exists, exposes its absolute path via
    ``LP2C_TELETHON_SESSION`` so the project bot can construct its own
    Telethon client and call ``enable_team_relay`` (spec #0c).

    A solo-mode bot (``team_name is None``) never receives the env var — it
    has no relay to attach. A team-mode bot whose ``telethon.session`` file
    is absent (i.e. ``/setup`` hasn't run yet) also doesn't receive the var,
    so the project bot can detect "no session" by env-var absence rather than
    by stat'ing a missing path.
    """
    env = os.environ.copy()
    if team_name is not None:
        session_path = (config_dir / "telethon.session").resolve()
        if session_path.exists():
            env["LP2C_TELETHON_SESSION"] = str(session_path)
    return env


class ProcessManager:
    def __init__(
        self,
        project_config_path: Path | None = None,
        command_builder: Callable[[str, dict], list[str]] | None = None,
    ):
        self._project_config_path = project_config_path
        self._command_builder = command_builder or self._default_command_builder
        self._processes: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, collections.deque] = {}
        self._log_threads: dict[str, threading.Thread] = {}

    def _load_projects(self) -> dict[str, dict]:
        if self._project_config_path is not None:
            return load_project_configs(self._project_config_path)
        return load_project_configs()

    def _config_dir(self) -> Path:
        """Directory containing the manager's config.json (and telethon.session)."""
        return (self._project_config_path or DEFAULT_CONFIG).parent

    def _base_cli(self) -> list[str]:
        cmd = ["link-project-to-chat"]
        if self._project_config_path is not None:
            cmd.extend(["--config", str(self._project_config_path.resolve())])
        return cmd

    def _default_command_builder(self, project_name: str, project_config: dict) -> list[str]:
        cmd = self._base_cli() + ["start", "--project", project_name]

        permissions = project_config.get("permissions")
        if permissions == "dangerously-skip-permissions":
            cmd.append("--dangerously-skip-permissions")
        elif permissions and permissions != "default":
            cmd.extend(["--permission-mode", permissions])
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        model = project_config.get("model") or config.default_model
        if model:
            cmd.extend(["--model", model])
        return cmd

    def _capture_output(self, name: str, proc: subprocess.Popen) -> None:
        buf = self._logs[name]
        try:
            for raw_line in proc.stdout:
                buf.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
        except (ValueError, OSError):
            pass

    def start(self, project_name: str) -> bool:
        if project_name in self._processes and self._processes[project_name].poll() is None:
            return False
        projects = self._load_projects()
        if project_name not in projects:
            return False
        cmd = self._command_builder(project_name, projects[project_name])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self._processes[project_name] = proc
        self._logs[project_name] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(project_name, proc), daemon=True)
        thread.start()
        self._log_threads[project_name] = thread
        logger.info("Started %s (pid=%d)", project_name, proc.pid)
        self._set_autostart(project_name, True)
        return True

    def _team_command_builder(self, team_name: str, role: str) -> list[str]:
        return self._base_cli() + ["start", "--team", team_name, "--role", role]

    def start_team(self, team_name: str, role: str) -> bool:
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        if team_name not in config.teams:
            logger.error("Team %s not found in config", team_name)
            return False
        if role not in config.teams[team_name].bots:
            logger.error("Role %s not in team %s", role, team_name)
            return False
        key = f"team:{team_name}:{role}"
        if key in self._processes and self._processes[key].poll() is None:
            return False
        cmd = self._team_command_builder(team_name, role)
        env = _build_project_bot_env(team_name=team_name, config_dir=self._config_dir())
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self._processes[key] = proc
        self._logs[key] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(key, proc), daemon=True)
        thread.start()
        self._log_threads[key] = thread
        logger.info("Started team %s/%s (pid=%d)", team_name, role, proc.pid)
        self._set_team_bot_autostart(team_name, role, True)
        return True

    def stop(self, project_name: str) -> bool:
        proc = self._processes.get(project_name)
        if not proc or proc.poll() is not None:
            self._processes.pop(project_name, None)
            return False
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._processes.pop(project_name, None)
        self._log_threads.pop(project_name, None)
        logger.info("Stopped %s", project_name)
        if project_name.startswith("team:"):
            _, team_name, role = project_name.split(":", 2)
            self._set_team_bot_autostart(team_name, role, False)
        else:
            self._set_autostart(project_name, False)
        return True

    def status(self, project_name: str) -> str:
        proc = self._processes.get(project_name)
        if proc and proc.poll() is None:
            return "running"
        self._processes.pop(project_name, None)
        return "stopped"

    def logs(self, project_name: str, n: int = 50) -> str:
        buf = self._logs.get(project_name)
        if not buf:
            return "(no logs)"
        return "\n".join(list(buf)[-n:])

    def list_all(self) -> list[tuple[str, str]]:
        return [(name, self.status(name)) for name in self._load_projects()]

    def start_all(self) -> int:
        return sum(1 for name in self._load_projects() if self.start(name))

    def stop_all(self) -> int:
        return sum(1 for name in list(self._processes) if self.stop(name))

    def start_autostart(self) -> int:
        """Start all projects and team bots that have autostart=true in config.

        Teams whose ``group_chat_id`` is still the ``0`` sentinel (group not yet
        captured after ``/create_team``) are skipped — starting them would
        produce a bot with no group to attach to.
        """
        count = 0
        for name, proj in self._load_projects().items():
            if proj.get("autostart") and self.start(name):
                count += 1
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        for team_name, team in config.teams.items():
            if not team.group_chat_id:
                continue
            for role, bot in team.bots.items():
                if bot.autostart and self.start_team(team_name, role):
                    count += 1
        return count

    def _set_autostart(self, project_name: str, value: bool) -> None:
        if self._project_config_path:
            set_project_autostart(project_name, value, self._project_config_path)
        else:
            set_project_autostart(project_name, value)

    def _set_team_bot_autostart(self, team_name: str, role: str, value: bool) -> None:
        if self._project_config_path:
            set_team_bot_autostart(team_name, role, value, self._project_config_path)
        else:
            set_team_bot_autostart(team_name, role, value)

    def rename(self, old_name: str, new_name: str) -> None:
        for store in (self._processes, self._logs, self._log_threads):
            if old_name in store:
                store[new_name] = store.pop(old_name)
