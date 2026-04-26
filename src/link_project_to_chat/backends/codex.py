from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import Error, Result, StreamEvent, TextDelta
from .base import BackendCapabilities, BaseBackend, HealthStatus
from .codex_parser import parse_codex_line
from .factory import register

logger = logging.getLogger(__name__)


class CodexStreamError(Exception):
    """Raised by CodexBackend.chat() when the stream returns an Error event."""


CODEX_CAPABILITIES = BackendCapabilities(
    models=(),
    supports_thinking=False,
    supports_permissions=False,
    supports_resume=True,
    supports_compact=False,
    supports_allowed_tools=False,
    supports_usage_cap_detection=False,
)


class CodexBackend(BaseBackend):
    name = "codex"
    capabilities = CODEX_CAPABILITIES
    _env_keep_patterns = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns = (
        "*_TOKEN", "*_KEY", "*_SECRET",
        "ANTHROPIC_*", "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.model: str | None = state.get("model")
        self.model_display: str | None = None
        self.session_id: str | None = state.get("session_id")
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_usage: dict | None = None
        self._total_requests: int = 0

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_cmd(self, user_message: str) -> list[str]:
        if self.session_id:
            cmd = ["codex", "exec", "resume", "--json"]
            if self.model:
                cmd.extend(["--model", self.model])
            cmd.extend([self.session_id, user_message])
            return cmd
        cmd = ["codex", "exec", "--json"]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append(user_message)
        return cmd

    # ------------------------------------------------------------------
    # Process spawning
    # ------------------------------------------------------------------

    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        # Lazy import: task_manager imports backends.base, so importing it at
        # module load creates a circular-import path through backends/__init__.
        from ..task_manager import _command_popen_kwargs
        kwargs = _command_popen_kwargs()
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._prepare_env(),
            **kwargs,
        )
        if kwargs.get("start_new_session"):
            proc._kill_process_tree = True  # type: ignore[attr-defined]
        return proc

    # ------------------------------------------------------------------
    # Streaming — primary API
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd = self._build_cmd(user_message)
        proc = self._popen(cmd)
        self._proc = proc
        if on_proc:
            on_proc(proc)
        logger.info("codex stream subprocess started pid=%s", proc.pid)

        self._last_message = user_message[:80]
        self._started_at = time.monotonic()
        self._total_requests += 1

        collected_text: list[str] = []
        usage: dict | None = None
        try:
            while True:
                raw_line = await asyncio.to_thread(proc.stdout.readline)
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                parsed = parse_codex_line(line)
                if parsed.thread_id:
                    self.session_id = parsed.thread_id
                for event in parsed.events:
                    if isinstance(event, TextDelta):
                        collected_text.append(event.text)
                    yield event
                if parsed.usage is not None:
                    usage = parsed.usage
                if parsed.turn_completed:
                    yield Result(
                        text="".join(collected_text) or "[No response]",
                        session_id=self.session_id,
                        model=self.model_display,
                    )
                    if usage is not None:
                        self._last_usage = usage
                    # Drain stderr and reap the process before returning so
                    # we don't leak fds or leave a zombie. Without this the
                    # `finally` clears `_proc` and the task layer's later
                    # `close_interactive()` has nothing to clean up. Also
                    # surfaces a non-zero exit that arrives after a
                    # syntactically complete turn.
                    stderr_bytes = await asyncio.to_thread(proc.stderr.read)
                    await asyncio.to_thread(proc.wait)
                    if proc.returncode != 0:
                        err = stderr_bytes.decode("utf-8", errors="replace").strip()
                        logger.warning(
                            "codex pid=%s exited %s after turn.completed; stderr=%s",
                            proc.pid,
                            proc.returncode,
                            err[:200] or "(empty)",
                        )
                    return

            # stdout EOF without a turn.completed event
            stderr_bytes = await asyncio.to_thread(proc.stderr.read)
            await asyncio.to_thread(proc.wait)
            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace").strip()
                yield Error(message=err or f"exit code {proc.returncode}")
                raise CodexStreamError(err or f"exit code {proc.returncode}")
        finally:
            if self._proc is proc:
                self._proc = None
                self._started_at = None
            logger.info("codex stream pid=%s turn done", proc.pid)

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> str:
        result_text = ""
        async for event in self.chat_stream(user_message, on_proc=on_proc):
            if isinstance(event, Result):
                result_text = event.text
            elif isinstance(event, Error):
                raise CodexStreamError(event.message)
        return result_text or "[No response]"

    async def probe_health(self) -> HealthStatus:
        try:
            await self.chat("Reply with exactly PONG and do not run any commands.")
        except CodexStreamError as exc:
            return HealthStatus(ok=False, usage_capped=False, error_message=str(exc))
        return HealthStatus(ok=True, usage_capped=False, error_message=None)

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    def close_interactive(self) -> None:
        from ..task_manager import _terminate_process_tree  # lazy: see _popen

        proc = self._proc
        if proc and proc.poll() is None:
            _terminate_process_tree(proc)
        if self._proc is proc:
            self._proc = None
            self._started_at = None

    def cancel(self) -> bool:
        # Mirrors ClaudeBackend.cancel(): bare proc.kill() rather than
        # _terminate_process_tree(). Tree-kill (and full _proc/_started_at
        # cleanup) is invoked by the task layer's CancelledError handler via
        # close_interactive(); the per-backend cancel() exists only to satisfy
        # the AgentBackend Protocol and has no production callers today.
        proc = self._proc
        if proc is None:
            return False
        proc.kill()
        return True

    @property
    def status(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        return {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_usage": self._last_usage,
        }


def _make_codex(project_path: Path, state: dict) -> CodexBackend:
    return CodexBackend(project_path, state)


register("codex", _make_codex)
