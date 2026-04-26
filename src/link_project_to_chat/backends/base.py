from __future__ import annotations

import fnmatch
import os
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
    supports_effort: bool = False
    effort_levels: Sequence[str] = ()


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    usage_capped: bool
    error_message: str | None = None


class BaseBackend:
    # ``MODEL_OPTIONS`` powers the `/model` button picker in bot.py — each entry
    # is (model_id, label, description). Backends that don't support model
    # switching keep this empty (and `capabilities.models` is also empty), so
    # bot.py's gate falls through to "this backend doesn't support /model".
    MODEL_OPTIONS: list[tuple[str, str, str]] = []

    _env_keep_patterns: Sequence[str] = ()
    _env_scrub_patterns: Sequence[str] = ()

    def _matches(self, key: str, patterns: Sequence[str]) -> bool:
        return any(fnmatch.fnmatch(key, pattern) for pattern in patterns)

    def _prepare_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        for key in list(env):
            if self._matches(key, self._env_keep_patterns):
                continue
            if self._matches(key, self._env_scrub_patterns):
                del env[key]
        return env


class AgentBackend(Protocol):
    name: str
    capabilities: BackendCapabilities
    project_path: Path
    model: str | None
    # `None` means the backend has no friendlier label than `model` — callers
    # should fall back to `model` in that case.
    model_display: str | None
    session_id: str | None
    # Reasoning-effort level (low/medium/high/...). Backends that don't
    # support it ignore writes; the bot still gates /effort on
    # ``capabilities.supports_effort`` before mutating this attribute.
    effort: str | None

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

    def current_permission(self) -> str:
        pass

    def set_permission(self, mode: str | None) -> None:
        pass

    @property
    def status(self) -> dict:
        pass
