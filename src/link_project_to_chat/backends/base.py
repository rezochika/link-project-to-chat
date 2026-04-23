from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..events import StreamEvent


@dataclass(frozen=True)
class BackendCapabilities:
    models: Sequence[str]
    supports_thinking: bool
    supports_permissions: bool
    supports_resume: bool
    supports_compact: bool
    supports_allowed_tools: bool
    supports_usage_cap_detection: bool


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    usage_capped: bool
    error_message: str | None = None


class AgentBackend(Protocol):
    name: str
    capabilities: BackendCapabilities
    project_path: Path
    model: str | None
    session_id: str | None

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        pass

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> str:
        pass

    async def probe_health(self) -> HealthStatus:
        pass

    def close_interactive(self) -> None:
        pass

    def cancel(self) -> bool:
        pass

    @property
    def status(self) -> dict:
        pass
