from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..events import Error, Result, StreamEvent, TextDelta
from .base import BackendCapabilities, BackendStatus, BaseBackend, HealthStatus
from .codex_parser import parse_codex_line
from .factory import register

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..team_safety import TeamAuthority


class CodexStreamError(Exception):
    """Raised by CodexBackend.chat() when the stream returns an Error event."""


CODEX_MODEL_OPTIONS = [
    ("gpt-5.5", "GPT-5.5", "Frontier coding (default)", ()),
    ("gpt-5.4", "GPT-5.4", "", ()),
    ("gpt-5.4-mini", "GPT-5.4-Mini", "Fast, lighter reasoning", ()),
    ("gpt-5.3-codex", "GPT-5.3-Codex", "", ()),
    ("gpt-5.2", "GPT-5.2", "", ()),
]

# Reasoning-effort levels accepted by `codex exec -c model_reasoning_effort=...`.
# Codex tops out at ``xhigh`` (Claude also exposes ``max``, but Codex doesn't).
CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
CODEX_MODELS = tuple(opt[0] for opt in CODEX_MODEL_OPTIONS)
CODEX_PERMISSION_MODES = {
    None,
    "default",
    "plan",
    "acceptEdits",
    "dontAsk",
    "auto",
    "bypassPermissions",
    "dangerously-skip-permissions",
}

CODEX_CAPABILITIES = BackendCapabilities(
    models=CODEX_MODELS,
    supports_thinking=False,
    supports_permissions=True,
    supports_resume=True,
    supports_compact=False,
    supports_allowed_tools=False,
    supports_usage_cap_detection=False,
    supports_effort=True,
    effort_levels=CODEX_EFFORT_LEVELS,
)


class CodexBackend(BaseBackend):
    name = "codex"
    capabilities = CODEX_CAPABILITIES
    # `/model` button picker entries. Keep the default frontier model first.
    # Codex slugs ARE the wire identifiers — no aliases needed (passed
    # straight through `--model <slug>`). Empty alias tuples keep the
    # 4-tuple shape consistent with Claude's MODEL_OPTIONS.
    MODEL_OPTIONS = CODEX_MODEL_OPTIONS
    _env_keep_patterns = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns = (
        "*_TOKEN", "*_KEY", "*_SECRET",
        "ANTHROPIC_*", "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.model: str | None = state.get("model")
        self.model_display: str | None = self._display_for_model(self.model)
        self.session_id: str | None = state.get("session_id")
        self.effort: str | None = state.get("effort")
        self.permissions: str | None = state.get("permissions")
        self.team_system_note: str | None = None
        self.team_authority: TeamAuthority | None = None
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_usage: dict | None = None
        self._last_error: str | None = None
        self._total_requests: int = 0

    @classmethod
    def _display_for_model(cls, model: str | None) -> str:
        target = model or cls.MODEL_OPTIONS[0][0]
        for model_id, label, desc, *_ in cls.MODEL_OPTIONS:
            if model_id == target:
                return f"{label} — {desc}" if desc else label
        return model or cls.MODEL_OPTIONS[0][1]

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_cmd(self, user_message: str) -> list[str]:
        cmd = ["codex", "exec"]
        if self.session_id:
            cmd.append("resume")
        cmd.append("--json")
        prompt = self._build_prompt(user_message)
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["-c", f"model_reasoning_effort={self.effort}"])
        cmd.extend(self._permission_args())
        if self.session_id:
            cmd.append(self.session_id)
        cmd.append(prompt)
        return cmd

    def _build_prompt(self, user_message: str) -> str:
        """Wrap user message with system-reminder blocks for the layers that
        Codex doesn't expose as separate CLI flags. Order matches Claude's
        parts-list order: safety → team → user message.

        Each layer renders as its own <system-reminder>...</system-reminder>
        block so Codex parses them as distinct reminders.
        """
        blocks: list[str] = []
        if self.safety_system_prompt:
            blocks.append(
                f"<system-reminder>\n{self.safety_system_prompt}\n</system-reminder>"
            )
        if self.team_system_note:
            blocks.append(
                f"<system-reminder>\n{self.team_system_note}\n</system-reminder>"
            )
        if not blocks:
            return user_message
        return "\n\n".join(blocks) + "\n\n" + user_message

    def _permission_args(self) -> list[str]:
        mode = self.permissions
        if mode in (None, "default"):
            return []
        if mode == "plan":
            return ["-c", "sandbox_mode='read-only'", "-c", "approval_policy='never'"]
        if mode in ("acceptEdits", "dontAsk", "auto"):
            return ["--full-auto"]
        if mode in ("bypassPermissions", "dangerously-skip-permissions"):
            if self.team_system_note:
                if self.team_authority is not None and self.team_authority.consume_grant("all"):
                    return ["--dangerously-bypass-approvals-and-sandbox"]
                return ["--full-auto"]
            return ["--dangerously-bypass-approvals-and-sandbox"]
        raise ValueError(f"Unsupported Codex permissions mode: {mode}")

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
        *,
        recent_discussion: str = "",
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream events for one Codex turn.

        ``recent_discussion`` is accepted for Protocol compatibility (v1.2.0
        sub-feature 3). Native rendering as a ``<system-reminder>`` block is
        a follow-up task; for now the value is ignored so existing callers
        remain unaffected.
        """
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
        completed = False
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
                    completed = True
                    self._last_error = None
                    # Codex emits one item.completed agent_message per logical
                    # paragraph (planning preamble, mid-action update, final
                    # answer). Joining with "" produced run-on bubbles like
                    # "I'll review... I'm running... Done." in the 2026-04-27
                    # manager logs. \n\n between non-empty parts renders them
                    # as visibly distinct paragraphs in the chat.
                    yield Result(
                        text="\n\n".join(t for t in collected_text if t),
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
                self._last_error = err or f"exit code {proc.returncode}"
                yield Error(message=self._last_error)
                raise CodexStreamError(self._last_error)
        finally:
            if not completed and proc.poll() is None:
                # CA-4: _popen sets `start_new_session=True` so the codex
                # subprocess is a process-group leader. A bare proc.kill()
                # only signals the leader; helper children survive. Route
                # through the shared tree-terminator (which also waits for
                # the proc internally) so the whole group dies with the
                # parent. Wrapped in to_thread because it blocks on
                # signalling + wait().
                from ..task_manager import _terminate_process_tree
                await asyncio.to_thread(_terminate_process_tree, proc)
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
        return result_text

    async def probe_health(self) -> HealthStatus:
        try:
            await self.chat("Reply with exactly PONG and do not run any commands.")
        except CodexStreamError as exc:
            self._last_error = str(exc)
            return HealthStatus(ok=False, usage_capped=False, error_message=self._last_error)
        self._last_error = None
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

    def current_permission(self) -> str:
        return self.permissions or "default"

    def set_permission(self, mode: str | None) -> None:
        if mode not in CODEX_PERMISSION_MODES:
            raise ValueError(f"Unsupported Codex permissions mode: {mode}")
        self.permissions = None if mode in (None, "default") else mode

    @property
    def status(self) -> BackendStatus:
        running = self._proc is not None and self._proc.poll() is None
        return {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_usage": self._last_usage,
            "permission": self.current_permission(),
            "last_error": self._last_error,
        }


def _make_codex(project_path: Path, state: dict) -> CodexBackend:
    return CodexBackend(project_path, state)


register("codex", _make_codex)
