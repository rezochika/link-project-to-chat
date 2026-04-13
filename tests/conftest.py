"""Shared test fixtures and helpers for link-project-to-chat tests."""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@dataclass
class FakeUser:
    """Lightweight test double for TelegramUser protocol."""

    id: int
    username: str | None = None


class FakeRunner:
    """Records subprocess commands and returns a fake Popen.

    Shared fixture — replaces the inline FakeRunner in test_claude_client.py.
    """

    def __init__(self, stdout_lines: list[str] | None = None, returncode: int = 0) -> None:
        self.last_cmd: list[str] = []
        self.last_env: dict[str, str] = {}
        self.last_cwd: str = ""
        self._stdout_lines = stdout_lines or []
        self._returncode = returncode

    def run(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        self.last_cmd = cmd
        self.last_env = env
        self.last_cwd = cwd

        raw = "\n".join(self._stdout_lines).encode()
        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = io.BytesIO(raw)
        proc.stderr = io.BytesIO(b"")
        proc.returncode = self._returncode
        proc.pid = 12345
        proc.poll.return_value = self._returncode
        proc.wait.return_value = self._returncode
        return proc


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Path:
    """Return a temporary config file path."""
    return tmp_path / "config.json"


@pytest.fixture()
def fake_user() -> FakeUser:
    """Return a default authenticated FakeUser."""
    return FakeUser(id=42, username="alice")
