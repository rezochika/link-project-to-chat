from __future__ import annotations

import subprocess
from typing import Protocol


class ProcessRunner(Protocol):
    def run(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]: ...


class TelegramUser(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def username(self) -> str | None: ...
