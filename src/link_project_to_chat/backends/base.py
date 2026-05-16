from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

from ..events import StreamEvent

if TYPE_CHECKING:
    from ..team_safety import TeamAuthority


# Verbatim port of the GitLab fork's claude_client.py:21 SYSTEM_PROMPT.
# Each backend renders this in its native style (Claude:
# --append-system-prompt parts list; Codex: <system-reminder> block).
DEFAULT_SAFETY_SYSTEM_PROMPT = (
    "<important>Only make changes or run commands when explicitly asked "
    "to modify a specific file or perform a specific task. For questions, "
    "analysis, or discussion — answer only, do not act. If you identify "
    "something that could be fixed or improved, describe what and why, "
    "then ask for approval before doing anything. Do not run, start, "
    "stop, or restart anything unless explicitly asked. Do not install "
    "packages, run scripts, or restart services unless explicitly asked "
    "in the current message.</important>"
)


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


class BackendStatus(TypedDict, total=False):
    running: bool
    pid: int | None
    session_id: str | None
    total_requests: int
    last_duration: float | None
    last_message: str | None
    effort: str | None
    permission: str | None
    allowed_tools: list[str]
    disallowed_tools: list[str]
    usage_capped: bool
    last_error: str | None
    last_usage: dict[str, int] | None


class BaseBackend:
    # ``MODEL_OPTIONS`` powers the `/model` button picker in bot.py — each entry
    # is (model_id, label, description). Backends that don't support model
    # switching keep this empty (and `capabilities.models` is also empty), so
    # bot.py's gate falls through to "this backend doesn't support /model".
    MODEL_OPTIONS: list[tuple[str, str, str]] = []

    # Process-essentials forwarded to every backend subprocess. Anything outside
    # this list (and outside the per-backend `_env_keep_patterns`) is dropped —
    # allowlist semantics, so an arbitrary host env var like PGPASSWORD or
    # OPENID_CLIENT_SECRET cannot leak into the agent CLI.
    _env_baseline_patterns: Sequence[str] = (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL",
        "LANG", "LANGUAGE", "LC_*", "TZ", "TERM",
        "TMPDIR", "TMP", "TEMP",
        "XDG_*",
        "PWD", "OLDPWD",
        "HOSTNAME",
        # Node/npm runtime — the Claude/Codex CLIs are node binaries.
        "NODE_*", "NPM_*", "NVM_DIR", "NVM_BIN",
        # Python runtime — some backends are Python.
        "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED",
        # SSL/TLS cert configuration commonly required for HTTPS.
        "SSL_CERT_*", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        # HTTP proxy — operators commonly require proxy passthrough.
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
        # Windows process/profile runtime. Native Node-packaged CLIs can crash
        # before emitting stderr if these are missing, and APPDATA/LOCALAPPDATA
        # also point them at their non-secret config/cache roots.
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "PROGRAMDATA",
        "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles*",
    )
    _env_keep_patterns: Sequence[str] = ()
    _env_scrub_patterns: Sequence[str] = ()

    def __init__(self) -> None:
        # Per-bot system-prompt layer, set once at startup. Sits alongside
        # ``team_system_note`` (initialized in each subclass __init__) — both
        # are system-prompt layers that each backend renders in its native
        # style. ``None`` means "use the backend's default"; the bot resolves
        # ``None`` → ``DEFAULT_SAFETY_SYSTEM_PROMPT`` in ``_build_backend``.
        self.safety_system_prompt: str | None = None

    def _matches(self, key: str, patterns: Sequence[str]) -> bool:
        return any(fnmatch.fnmatch(key, pattern) for pattern in patterns)

    def _prepare_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        for key in list(env):
            if self._matches(key, self._env_keep_patterns):
                # Backend-specific keep wins over scrub (e.g. Codex needs
                # OPENAI_API_KEY despite the *_KEY scrub pattern).
                continue
            if not self._matches(key, self._env_baseline_patterns):
                del env[key]
                continue
            if self._matches(key, self._env_scrub_patterns):
                # Defense-in-depth: scrub still applies to baseline-allowed
                # keys, in case a future baseline pattern accidentally
                # matches a token-shaped name.
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
    # Team-mode routing instructions. ProjectBot sets this for every backend;
    # each backend decides how to inject it into its own CLI surface.
    team_system_note: str | None
    team_authority: TeamAuthority | None

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
    def status(self) -> BackendStatus:
        pass
