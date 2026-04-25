from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from ..events import Error, Result, StreamEvent
from .base import BackendCapabilities, HealthStatus
from .claude_parser import parse_stream_line
from .factory import register

logger = logging.getLogger(__name__)


class ClaudeStreamError(Exception):
    """Raised by ClaudeBackend.chat() when the stream returns an Error event."""


_API_KEY_RE = re.compile(r"(sk-[a-zA-Z]+-)\S+")
_MAX_ERROR_LEN = 200


def _sanitize_error(text: str) -> str:
    """Return a safe, single-line summary of a subprocess stderr string."""
    if not text or not text.strip():
        return "Unknown error"
    # Keep only the first line — subsequent lines may contain paths / env vars.
    first_line = text.splitlines()[0].strip()
    # Redact API key patterns.
    first_line = _API_KEY_RE.sub(r"\1***", first_line)
    # Truncate to avoid flooding the user.
    if len(first_line) > _MAX_ERROR_LEN:
        first_line = first_line[:_MAX_ERROR_LEN] + "..."
    return first_line


_USAGE_CAP_PATTERNS = (
    "usage limit",
    "rate_limit_error",
    "anthropic-ratelimit",
    "you've reached your usage",
)


def _detect_usage_cap(stderr: str) -> bool:
    """Return True if the stderr text looks like a Claude usage-cap or rate-limit error."""
    if not stderr:
        return False
    lowered = stderr.lower()
    return any(p in lowered for p in _USAGE_CAP_PATTERNS)


def is_usage_cap_error(message: str | None) -> bool:
    """Return True if `message` was produced as a USAGE_CAP-marked error.

    Single source of truth for "is this string a usage-cap signal?" — used by
    both `_finalize_claude_task` (to branch on the marker) and the cap probe
    (to confirm a probe response is not still capped).
    """
    if not message:
        return False
    return message.startswith("USAGE_CAP:") or _detect_usage_cap(message)


class ClaudeUsageCapError(Exception):
    """Reserved exception type for Claude usage-cap / rate-limit signals.

    Currently the cap signal flows through the existing stream contract as
    ``Error(message="USAGE_CAP:" + ...)`` rather than as a raised exception
    (preserves the AsyncGenerator yield semantics in `_read_events`). The class
    is exported so consumers can isinstance-check if a future revision starts
    raising it directly.
    """


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODELS = ("haiku", "sonnet", "opus", "opus[1m]", "sonnet[1m]")
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")
DEFAULT_MODEL = "sonnet"

# Appended when interactive mode is used so Claude handles dismissed AskUserQuestion gracefully.
_ASK_DISMISSED_HINT = (
    "If your AskUserQuestion tool call is dismissed with an error, do NOT retry it. "
    "Instead, output the question clearly as text and wait. "
    "The user's answer will arrive as a follow-up message."
)

# Tells the agent it is running inside this Telegram bot so it adapts output, suggests
# bot commands when relevant, and treats the channel-carrying files as fragile. The
# command list paragraph is built dynamically from BackendCapabilities so a backend
# without `supports_compact` (etc.) does not advertise commands the bot will reject.
_TELEGRAM_AWARENESS_PREFIX = """\
You are running inside `link-project-to-chat`, a Telegram bot. Your responses are \
delivered to a Telegram user via the bot's message handler in \
`src/link_project_to_chat/bot.py`. Keep these constraints in mind:

OUTPUT: Replies render as Telegram MarkdownV2 (the bot escapes formatting) and are \
auto-split at ~4000 chars; very large code blocks may be sent as `.txt` attachments. \
Prefer concise, scannable replies. The user sees only your text output — not your \
tool calls or thinking — so narrate key actions in one short sentence each.

USER COMMANDS: The user can invoke slash commands directly. Suggest them when \
relevant: """

_TELEGRAM_AWARENESS_SUFFIX = """

CHANNEL FRAGILITY: `src/link_project_to_chat/bot.py`, \
`src/link_project_to_chat/claude_client.py`, and the `link-project-to-chat` systemd \
unit are load-bearing for THIS conversation — a breaking change drops the user's \
only channel to you. Confirm before editing those files, and note that running \
`rebuild.sh` restarts the service (brief gap before the next message gets through)."""


def _telegram_command_summary(capabilities: BackendCapabilities) -> str:
    """Produce the comma-joined, backtick-wrapped command list for the preamble.

    Bot-level commands (always available regardless of backend) come first; the
    capability-gated ones are appended only when the backend declares support,
    so a backend without (e.g.) `supports_compact` doesn't advertise `/compact`.
    The returned string is concatenated directly after the prefix's "Suggest
    them when relevant: " — it carries no leading label.
    """
    commands = [
        "`/run <cmd>`",
        "`/tasks`",
        "`/effort low|medium|high|xhigh|max`",
        "`/skills`",
        "`/use [name]`",
        "`/stop_skill`",
        "`/persona [name]`",
        "`/stop_persona`",
        "`/voice`",
        "`/lang`",
        "`/reset`",
        "`/status`",
        "`/help`",
    ]
    if capabilities.supports_thinking:
        commands.append("`/thinking on|off`")
    if capabilities.models:
        commands.append("`/model " + "|".join(capabilities.models) + "`")
    if capabilities.supports_permissions:
        commands.append("`/permissions <mode>`")
    if capabilities.supports_compact:
        commands.append("`/compact`")

    return ", ".join(commands) + "."


def _build_telegram_awareness(capabilities: BackendCapabilities) -> str:
    return (
        _TELEGRAM_AWARENESS_PREFIX
        + _telegram_command_summary(capabilities)
        + _TELEGRAM_AWARENESS_SUFFIX
    )


class ClaudeBackend:
    name = "claude"
    capabilities = BackendCapabilities(
        models=MODELS,
        supports_thinking=True,
        supports_permissions=True,
        supports_resume=True,
        supports_compact=True,
        supports_allowed_tools=True,
        supports_usage_cap_detection=True,
    )

    def __init__(
        self,
        project_path: Path,
        model: str = DEFAULT_MODEL,
        skip_permissions: bool = True,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        self.model = model
        self.model_display: str | None = None
        self.project_path = project_path
        self.effort: str = "medium"
        self.skip_permissions: bool = skip_permissions
        self.permission_mode: str | None = permission_mode
        self.allowed_tools: list[str] = allowed_tools or []
        self.disallowed_tools: list[str] = disallowed_tools or []
        self.append_system_prompt: str | None = None
        # Optional team-mode context note (set by ProjectBot when team_name and
        # peer_bot_username are both known). Injected alongside the Telegram
        # awareness preamble so it survives `/use <skill>` overwriting
        # append_system_prompt.
        self.team_system_note: str | None = None
        self.session_id: str | None = None
        self.show_thinking: bool = False
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_duration: float | None = None
        self._total_requests: int = 0

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_cmd(self) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--effort",
            self.effort,
            "--input-format",
            "stream-json",
            # Emit `stream_event` records with `content_block_delta` as text and
            # thinking are produced. Required for live streaming in bot.py —
            # without it, text/thinking arrive in one chunk at the end of the turn.
            "--include-partial-messages",
        ]

        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.allowed_tools)])

        if self.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self.disallowed_tools)])

        # Combine Telegram awareness, AskUserQuestion hint, team context (if any),
        # and any user/skill prompt.
        parts = [_build_telegram_awareness(self.capabilities), _ASK_DISMISSED_HINT]
        if self.team_system_note:
            parts.append(self.team_system_note)
        if self.append_system_prompt:
            parts.append(self.append_system_prompt)
        cmd.extend(["--append-system-prompt", "\n\n".join(parts)])

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        return cmd

    # ------------------------------------------------------------------
    # Streaming — primary API
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a message and yield stream events.

        If there is already a live interactive process (from a previous turn
        that ended with an AskQuestion), the message is sent on its stdin.
        Otherwise a new subprocess is spawned.
        """
        reuse = self._proc is not None and self._proc.poll() is None

        if reuse:
            proc = self._proc
            self._send_stdin(proc, user_message)
        else:
            proc = self._start_proc(user_message, on_proc)

        self._last_message = user_message[:80]
        started_at = time.monotonic()
        self._started_at = started_at
        self._total_requests += 1

        try:
            async for event in self._read_events(proc):
                yield event
        finally:
            self._last_duration = time.monotonic() - started_at
            if self._proc is proc:
                self._started_at = None
            logger.info(
                "claude stream pid=%s turn done, alive=%s",
                proc.pid,
                proc.poll() is None,
            )

    async def chat(self, user_message: str, on_proc=None) -> str:
        result_text = ""
        async for event in self.chat_stream(user_message, on_proc=on_proc):
            if isinstance(event, Result):
                result_text = event.text
            elif isinstance(event, Error):
                raise ClaudeStreamError(event.message)
        return result_text or "[No response]"

    async def probe_health(self) -> HealthStatus:
        """Send a trivial prompt to verify the backend is reachable.

        Returns HealthStatus(ok=True) when chat returns a normal response;
        HealthStatus(ok=False, usage_capped=True) when a usage-cap signal is
        detected (either via stream Error or direct result string);
        HealthStatus(ok=False) for other stream errors.
        """
        try:
            result = await self.chat("ping")
        except ClaudeStreamError as exc:
            message = str(exc)
            return HealthStatus(
                ok=False,
                usage_capped=is_usage_cap_error(message),
                error_message=message,
            )
        if is_usage_cap_error(result):
            return HealthStatus(ok=False, usage_capped=True, error_message=result)
        return HealthStatus(ok=True, usage_capped=False, error_message=None)

    # ------------------------------------------------------------------
    # Interactive process management
    # ------------------------------------------------------------------

    def _start_proc(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> subprocess.Popen:
        cmd = self._build_cmd()

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        _SCRUB_PATTERNS = (
            "*_TOKEN", "*_KEY", "*_SECRET",
            "AWS_*", "OPENAI_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
        )
        for key in [k for k in env if any(fnmatch.fnmatch(k, p) for p in _SCRUB_PATTERNS)]:
            del env[key]

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._proc = proc
        if on_proc:
            on_proc(proc)
        logger.info("claude stream subprocess started pid=%s", proc.pid)

        self._send_stdin(proc, user_message)
        return proc

    @staticmethod
    def _send_stdin(proc: subprocess.Popen, message: str) -> None:
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": message},
        })
        proc.stdin.write((msg + "\n").encode())
        proc.stdin.flush()

    async def _read_events(
        self, proc: subprocess.Popen
    ) -> AsyncGenerator[StreamEvent, None]:
        """Read stdout line-by-line, yielding events until a Result is seen."""
        while True:
            raw_line = await asyncio.to_thread(proc.stdout.readline)
            if not raw_line:
                break  # EOF — process exited
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if not line.strip():
                continue
            for event in parse_stream_line(line):
                if isinstance(event, Result):
                    self.session_id = event.session_id or self.session_id
                    if event.model:
                        self.model_display = event.model
                yield event
                if isinstance(event, Result):
                    return  # Turn finished; process stays alive for follow-ups

        # stdout EOF without a Result → process died
        stderr_bytes = await asyncio.to_thread(proc.stderr.read)
        await asyncio.to_thread(proc.wait)
        if proc.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="replace").strip()
            if _detect_usage_cap(err):
                yield Error(message="USAGE_CAP:" + _sanitize_error(err))
            else:
                yield Error(message=_sanitize_error(err) if err else f"exit code {proc.returncode}")
        # Clean up reference
        if self._proc is proc:
            self._proc = None

    def close_interactive(self) -> None:
        """Shut down the interactive subprocess (close stdin → EOF)."""
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if self._proc is proc:
            self._proc = None
            self._started_at = None

    async def aclose_interactive(self) -> None:
        """Async wrapper that avoids blocking the event loop while waiting."""
        await asyncio.to_thread(self.close_interactive)

    # ------------------------------------------------------------------
    # Status / control
    # ------------------------------------------------------------------

    @property
    def status(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        info = {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_duration": round(self._last_duration, 1)
            if self._last_duration
            else None,
        }
        if running and self._started_at:
            info["elapsed"] = round(time.monotonic() - self._started_at, 1)
        return info

    def cancel(self) -> bool:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            return True
        return False


def _make_claude(project_path: Path, state: dict) -> ClaudeBackend:
    permissions = state.get("permissions")
    backend = ClaudeBackend(
        project_path=project_path,
        model=state.get("model") or DEFAULT_MODEL,
        skip_permissions=(permissions == "dangerously-skip-permissions"),
        permission_mode=permissions if permissions != "dangerously-skip-permissions" else None,
        allowed_tools=state.get("allowed_tools"),
        disallowed_tools=state.get("disallowed_tools"),
    )
    backend.session_id = state.get("session_id")
    backend.show_thinking = bool(state.get("show_thinking"))
    backend.effort = state.get("effort") or "medium"
    return backend


register("claude", _make_claude)
