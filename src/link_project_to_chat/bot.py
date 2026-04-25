from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcriber import Synthesizer, Transcriber

from .config import (
    Config,
    DEFAULT_CONFIG,
    bind_project_trusted_user,
    clear_session,
    load_config,
    load_session,
    load_teams,
    patch_project,
    patch_team,
    resolve_permissions,
    resolve_project_auth_scope,
    save_session,
)
from ._auth import AuthMixin
from .formatting import md_to_telegram, split_html, strip_html
from .backends.claude import (
    EFFORT_LEVELS,
    MODELS,
    PERMISSION_MODES,
    ClaudeBackend,
    ClaudeStreamError,
    is_usage_cap_error,
)
from .transport import Button, Buttons, ChatKind, ChatRef, MessageRef
from .transport.streaming import StreamingMessage
from .stream import Question, StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType

logger = logging.getLogger(__name__)

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List all tasks"),
    ("model", "Set Claude model (haiku/sonnet/opus)"),
    ("effort", "Set thinking depth (low/medium/high/max)"),
    ("thinking", "Toggle live thinking display (on/off)"),
    ("permissions", "Set permission mode"),
    ("compact", "Compress session context"),
    ("status", "Bot status"),
    ("reset", "Clear Claude session"),
    ("version", "Show version"),
    ("help", "Show available commands"),
    ("skills", "List skills or activate one"),
    ("stop_skill", "Deactivate current skill"),
    ("create_skill", "Create a new skill"),
    ("delete_skill", "Delete a skill"),
    ("persona", "Activate a persona (per-message)"),
    ("stop_persona", "Deactivate current persona"),
    ("create_persona", "Create a new persona"),
    ("delete_persona", "Delete a persona"),
    ("voice", "Show voice transcription status"),
    ("lang", "Switch voice message language"),
    ("halt", "Pause bot-to-bot iteration (group only)"),
    ("resume", "Resume bot-to-bot iteration (group only)"),
]

_CMD_HELP = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)


def _parse_task_id(data: str) -> int:
    return int(data.split("_")[-1])


class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_username: str = "",
        trusted_user_id: int | None = None,
        on_trust: Callable[[int, str], None] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        allowed_usernames: list[str] | None = None,
        trusted_users: dict[str, int] | None = None,
        trusted_user_ids: list[int] | None = None,
        transcriber: "Transcriber | None" = None,
        synthesizer: "Synthesizer | None" = None,
        active_persona: str | None = None,
        show_thinking: bool = False,
        team_name: str | None = None,
        group_chat_id: int | None = None,
        role: str | None = None,
        peer_bot_username: str = "",
        config_path: Path | None = None,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self._config_path = config_path
        if allowed_usernames is not None:
            self._allowed_usernames = allowed_usernames
        else:
            self._allowed_username = allowed_username
        if trusted_users is not None:
            self._trusted_users = dict(trusted_users)
        if trusted_user_ids is not None:
            self._trusted_user_ids = trusted_user_ids
        else:
            self._trusted_user_id = trusted_user_id
        self._on_trust_fn = on_trust
        self._started_at = time.monotonic()
        self._app = None
        self._transport = None  # TelegramTransport — set in _build_app
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._live_text: dict[int, StreamingMessage] = {}
        self._live_thinking: dict[int, StreamingMessage] = {}
        # Tasks whose live message start() failed once — don't retry for the
        # rest of the turn; fall back to post-completion paths instead.
        self._live_text_failed: set[int] = set()
        self._live_thinking_failed: set[int] = set()
        self._thinking_buf: dict[int, str] = {}   # task_id → accumulated thinking (toggle-off path OR live fallback)
        self._thinking_store: dict[int, str] = {}  # task_id → thinking text
        self._init_auth()
        self._active_skill: str | None = None
        self._active_persona = active_persona
        # Pending skill/persona capture — set by /create_skill + scope-button click,
        # consumed by the next plain-text message. Initialized here so _on_text
        # can read them unconditionally without a getattr dance.
        self._pending_skill_name: str | None = None
        self._pending_skill_scope: str | None = None
        self._pending_persona_name: str | None = None
        self._pending_persona_scope: str | None = None
        self.show_thinking = show_thinking
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._voice_tasks: set[int] = set()
        from .backends.factory import create as _create_backend
        backend_state = {
            "permissions": (
                "dangerously-skip-permissions"
                if skip_permissions
                else permission_mode
            ),
            "allowed_tools": allowed_tools or [],
            "disallowed_tools": disallowed_tools or [],
            "show_thinking": show_thinking,
        }
        _backend = _create_backend("claude", self.path, backend_state)
        self.task_manager = TaskManager(
            project_path=self.path,
            backend=_backend,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            on_waiting_input=self._on_waiting_input,
        )
        self.team_name = team_name
        self.group_mode = team_name is not None
        self.group_chat_id = group_chat_id
        self.role = role
        self.peer_bot_username = peer_bot_username
        self.bot_username: str = ""  # populated in _after_ready via transport.on_ready
        # Tell Claude about its own + peer @handle so it uses the correct
        # usernames instead of placeholders ("@developer") or hallucinating a
        # pre-suffix-bump name it remembers from the persona. Called once here
        # so existing peer info is available, and again in _after_ready once
        # the transport's on_ready fires with the bot's identity.
        self._refresh_team_system_note()
        from .group_state import GroupStateRegistry
        self._group_state = GroupStateRegistry(max_bot_rounds=20)
        self._probe_tasks: set[asyncio.Task] = set()

    @property
    def _claude(self) -> ClaudeBackend:
        """Tier-2 accessor for Claude-specific behavior (effort, permissions,
        append_system_prompt, team_system_note, model_display). Only valid
        while the configured backend is ClaudeBackend; asserts so other
        backends surface a clear error rather than silent attribute misses."""
        backend = self.task_manager.backend
        assert isinstance(backend, ClaudeBackend), "Tier-2 Claude-only access requires ClaudeBackend"
        return backend

    def _effective_config_path(self) -> Path:
        return self._config_path or DEFAULT_CONFIG

    def _on_trust(self, user_id: int, username: str) -> None:
        if self._on_trust_fn:
            self._on_trust_fn(user_id, username)

    async def _on_task_started(self, task: Task) -> None:
        # Only show typing indicator for Claude tasks, not /run commands
        if task.type == TaskType.COMMAND:
            return
        assert self._transport is not None
        self._typing_tasks[task.id] = asyncio.create_task(
            self._keep_typing(self._transport, task.chat)
        )
        # Team bots skip per-delta livestreaming (_on_stream_event returns
        # early), but we still need to emit *one* message early so the relay's
        # event-driven auto-delete (_delete_pending_for_peer) can drop the
        # forwarded trigger message before its 60-second fallback deletes it.
        # Sending the placeholder now — while the forward still exists —
        # means `reply_to` resolves; finalize() later *edits* this same
        # message, so reply_to is never re-validated against a vanished
        # target.
        if self.group_mode and task.id not in self._live_text:
            live = StreamingMessage(
                transport=self._transport,
                chat=task.chat,
                reply_to=task.message,
            )
            try:
                await live.start()
            except Exception:
                logger.exception(
                    "StreamingMessage.start failed for team placeholder (task #%d); "
                    "falling back to send-at-finalize",
                    task.id,
                )
                self._live_text_failed.add(task.id)
                return
            self._live_text[task.id] = live

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, TextDelta):
            # Team bots do not livestream to the group chat. Streaming edits
            # produce partial content that team_relay forwards before the
            # message is complete; disabling livestream here means task.result
            # is built from collected_text in task_manager and sent once at
            # finalize via _send_to_chat, eliminating the partial-relay race.
            if self.group_mode:
                return
            live = self._live_text.get(task.id)
            if live is None and task.id not in self._live_text_failed:
                assert self._transport is not None
                live = StreamingMessage(
                    self._transport,
                    task.chat,
                    reply_to=task.message,
                )
                try:
                    await live.start()
                except Exception:
                    logger.exception(
                        "StreamingMessage.start failed for live text (task #%d); "
                        "falling back to send-at-finalize",
                        task.id,
                    )
                    self._live_text_failed.add(task.id)
                    # task.result will be populated from collected_text in
                    # task_manager; _finalize_claude_task's "no live_text"
                    # branch will then _send_to_chat it in one shot.
                    return
                self._live_text[task.id] = live
            if live is not None:
                await live.append(event.text)
        elif isinstance(event, ThinkingDelta):
            if self.show_thinking and task.id not in self._live_thinking_failed and not self.group_mode:
                live = self._live_thinking.get(task.id)
                if live is None:
                    assert self._transport is not None
                    live = StreamingMessage(
                        self._transport,
                        task.chat,
                        reply_to=task.message,
                        prefix="💭 ",
                    )
                    try:
                        await live.start()
                    except Exception:
                        logger.exception(
                            "StreamingMessage.start failed for live thinking (task #%d); "
                            "falling back to post-completion Thinking button",
                            task.id,
                        )
                        self._live_thinking_failed.add(task.id)
                        # Fall through to the buffer-accumulation branch so
                        # _finalize_claude_task can still populate _thinking_store
                        # (which powers the /tasks → Thinking button).
                        live = None
                    else:
                        self._live_thinking[task.id] = live
                if live is not None:
                    await live.append(event.text)
                    return
            # Toggle-off path, group-mode path, OR toggle-on with failed live start.
            buf = self._thinking_buf.setdefault(task.id, "")
            sep = "\n\n" if buf else ""
            self._thinking_buf[task.id] = buf + sep + event.text
        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(task.chat, event.path, reply_to=task.message)

    async def _send_html(
        self,
        chat: ChatRef,
        html: str,
        reply_to: MessageRef | None = None,
        reply_markup: Buttons | None = None,
    ) -> MessageRef | None:
        """Send HTML message(s), attaching buttons to the last chunk. Returns the last sent ref."""
        assert self._transport is not None
        chunks = split_html(html)
        last_ref: MessageRef | None = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            btns = reply_markup if is_last else None
            try:
                last_ref = await self._transport.send_text(
                    chat, chunk, html=True, reply_to=reply_to, buttons=btns,
                )
            except Exception as exc:
                # The transport already retries on deleted-reply-target. Anything
                # reaching here is genuinely unexpected (parse error, malformed
                # HTML, network issue). Fall back to plain so the user gets *some*
                # output; log so the cause is recoverable.
                logger.warning("HTML send failed, falling back to plain: %s", exc, exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    try:
                        last_ref = await self._transport.send_text(
                            chat,
                            plain[:self._transport.max_text_length] if len(plain) > self._transport.max_text_length else plain,
                            reply_to=reply_to,
                            buttons=btns,
                        )
                    except Exception:
                        logger.error("Plain-text fallback also failed", exc_info=True)
        return last_ref

    async def _send_to_chat(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        html = md_to_telegram(text or "[No output]").replace("\x00", "")
        await self._send_html(chat, html, reply_to)

    async def _send_raw(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        escaped = (text or "[No output]").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._send_html(chat, f"<pre>{escaped}</pre>", reply_to)

    async def _cancel_live_for(self, task_id: int, note: str = "(cancelled)") -> None:
        """Seal any live text/thinking messages for this task with a cancellation marker."""
        live_text = self._live_text.pop(task_id, None)
        self._live_text_failed.discard(task_id)
        if live_text is not None:
            try:
                await live_text.cancel(note)
            except Exception:
                logger.warning("Failed to cancel live text for task %d", task_id, exc_info=True)
        live_thinking = self._live_thinking.pop(task_id, None)
        self._live_thinking_failed.discard(task_id)
        if live_thinking is not None:
            try:
                await live_thinking.cancel(note)
            except Exception:
                logger.warning("Failed to cancel live thinking for task %d", task_id, exc_info=True)

    async def _finalize_claude_task(self, task: Task) -> None:
        live_text = self._live_text.pop(task.id, None)
        live_thinking = self._live_thinking.pop(task.id, None)
        self._live_text_failed.discard(task.id)
        self._live_thinking_failed.discard(task.id)
        thinking = self._thinking_buf.pop(task.id, None)
        is_voice = task.id in self._voice_tasks
        self._voice_tasks.discard(task.id)

        if task._compact:
            text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
            # Compact tasks don't stream, but clean up defensively.
            if live_text is not None:
                await live_text.cancel("(compacted)")
            if live_thinking is not None:
                await live_thinking.cancel("(compacted)")
            await self._send_to_chat(task.chat, text, reply_to=task.message)
            return

        if task.status == TaskStatus.DONE:
            if live_text is not None:
                # Don't pass task.result — it contains only the LAST assistant text block.
                # The StreamingMessage buffer already holds every streamed text delta (narration
                # + final answer); overwriting it would make intermediate narration vanish.
                # Fall back to task.result only when the buffer is empty (stream dropped).
                has_buffer = bool(live_text.buffer.strip())
                has_result = bool((task.result or "").strip())
                if not has_buffer and not has_result:
                    # Claude turn ended with only tool_use blocks (no text output). The
                    # "…" placeholder would otherwise linger forever — replace with a
                    # short notice so the user knows the turn finished.
                    await live_text.finalize(
                        "(done — no text response; check files/tools the bot touched)",
                        render=False,
                    )
                else:
                    fallback = task.result if not has_buffer else None
                    await live_text.finalize(fallback, render=True)
            else:
                await self._send_to_chat(task.chat, task.result, reply_to=task.message)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)
            elif thinking:
                self._thinking_store[task.id] = thinking
            if is_voice and self._synthesizer and task.result:
                await self._send_voice_response(task.chat, task.result, reply_to=task.message)
        else:
            error_text = f"Error: {task.error}"
            if is_usage_cap_error(task.error) and self.group_mode:
                if live_text is not None:
                    await live_text.finalize(error_text, render=False)
                if live_thinking is not None:
                    await live_thinking.finalize(render=False)
                self._group_state.halt(task.chat)
                await self._send_to_chat(
                    task.chat,
                    "Hit Max usage cap. Pausing until reset. Will retry every 30 min.",
                    reply_to=task.message,
                )
                self._schedule_cap_probe(task.chat)
                return
            if live_text is not None:
                await live_text.finalize(error_text, render=False)
            else:
                await self._send_to_chat(task.chat, error_text, reply_to=task.message)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)

    def _schedule_cap_probe(self, chat: ChatRef, interval_s: int = 1800) -> None:
        """Probe the backend every `interval_s` seconds; on success, resume the group."""
        async def _probe() -> None:
            while self._group_state.get(chat).halted:
                await asyncio.sleep(interval_s)
                if not self._group_state.get(chat).halted:
                    return  # user manually resumed
                try:
                    status = await self.task_manager.backend.probe_health()
                except Exception:
                    logger.warning("cap probe failed", exc_info=True)
                    continue
                if status.ok and not status.usage_capped:
                    self._group_state.resume(chat)
                    await self._send_to_chat(chat, "Usage cap cleared. Resumed.")
                    return
        task = asyncio.create_task(_probe())
        self._probe_tasks.add(task)
        task.add_done_callback(self._probe_tasks.discard)

    async def _send_voice_response(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        assert self._transport is not None
        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "tts"
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plain = strip_html(md_to_telegram(text))
        if len(plain) > self._transport.max_text_length:
            plain = plain[:self._transport.max_text_length - 3] + "..."
        out_path = voice_dir / f"tts_{uuid.uuid4().hex}.opus"

        try:
            await self._synthesizer.synthesize(plain, out_path)
            await self._transport.send_voice(chat, out_path, reply_to=reply_to)
        except Exception:
            logger.warning("TTS failed", exc_info=True)
        finally:
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass

    async def _finalize_command_task(self, task: Task) -> None:
        output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated, use /log)"
        if task.status == TaskStatus.DONE:
            await self._send_raw(task.chat, f"{output}\n[exit 0]")
        else:
            await self._send_raw(task.chat, f"[exit {task.exit_code}]\n\n{output}")

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.AGENT:
            if self.task_manager.backend.session_id:
                try:
                    save_session(
                        self.name,
                        self.task_manager.backend.session_id,
                        self._effective_config_path(),
                        team_name=self.team_name,
                        role=self.role,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist session_id for task #%d", task.id
                    )
            await self._finalize_claude_task(task)
        else:
            await self._finalize_command_task(task)

    def _question_buttons(self, task_id: int, q_idx: int, question: Question) -> Buttons:
        rows: list[list[Button]] = []
        for opt_idx, opt in enumerate(question.options):
            label = opt.label[:64] or f"Option {opt_idx + 1}"
            rows.append([
                Button(label=label, value=f"ask_{task_id}_{q_idx}_{opt_idx}")
            ])
        return Buttons(rows=rows)

    @staticmethod
    def _render_question_html(question) -> str:
        """Render an AskUserQuestion Question as Telegram-compatible HTML.

        Used by _on_waiting_input (initial send) and _on_button (ask-answer
        annotation after user picks an option).
        """
        def _esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        header = question.header.strip() if question.header else ""
        body = question.question.strip() or "(question)"
        lines: list[str] = []
        if header:
            lines.append(f"<b>{_esc(header)}</b>")
        lines.append(_esc(body))
        if question.multi_select:
            lines.append("<i>(Multi-select: tap an option or reply with comma-separated values.)</i>")
        else:
            lines.append("<i>(Tap an option or reply with free text.)</i>")
        for opt in question.options:
            if opt.description:
                lines.append(f"• <b>{_esc(opt.label)}</b> — {_esc(opt.description)}")
        return "\n".join(lines)

    async def _on_waiting_input(self, task: Task) -> None:
        """Render pending questions as Telegram messages with option buttons."""
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        # Finalize any live messages so the question buttons appear after the sealed stream.
        live_text = self._live_text.pop(task.id, None)
        self._live_text_failed.discard(task.id)
        if live_text is not None:
            await live_text.finalize(task.result or None, render=True)
        elif task.result and task.result.strip():
            await self._send_to_chat(task.chat, task.result, reply_to=task.message)
        live_thinking = self._live_thinking.pop(task.id, None)
        self._live_thinking_failed.discard(task.id)
        if live_thinking is not None:
            await live_thinking.finalize(render=False)

        for q_idx, question in enumerate(task.pending_questions):
            await self._send_html(
                task.chat,
                self._render_question_html(question),
                reply_to=task.message,
                reply_markup=self._question_buttons(task.id, q_idx, question),
            )

    async def _on_start(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        await self._transport.send_text(
            ci.chat,
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with Claude.\n{_CMD_HELP}",
            reply_to=ci.message,
        )

    async def _on_text_from_transport(self, incoming) -> None:
        """Unified entry point for inbound messages from the Transport.

        Branch order:
          1. Voice (audio mime) → _on_voice_from_transport
          2. Non-audio files → _on_file_from_transport
          3. Plain text → _on_text (legacy shim)
          4. Nothing actionable → generic unsupported reply
        """
        # 1. Voice (audio mime).
        if incoming.files and any(
            (f.mime_type or "").startswith("audio/") for f in incoming.files
        ):
            await self._on_voice_from_transport(incoming)
            return

        # 2. Non-audio files.
        if incoming.files:
            await self._on_file_from_transport(incoming)
            return

        # 3. Text (or unsupported media that needs the specific rejection in
        # `_on_text`). Caption-less stickers / muted videos / locations have
        # `text==""` but `has_unsupported_media=True` — without the second
        # clause they'd fall through to the generic branch 4 reply.
        if incoming.text.strip() or incoming.has_unsupported_media:
            if self.group_mode:
                handled = await self._handle_group_text(incoming)
                if handled:
                    return
                # Bot-to-bot path bypasses auth; submit directly.
                if incoming.is_relayed_bot_to_bot or incoming.sender.is_bot:
                    await self._submit_group_message_to_claude(incoming)
                    return
                # Human message in group — fall through to full auth/rate-limit/
                # pending-skill/pending-persona flow below.
            await self._on_text(incoming)
            return

        # 4. Nothing actionable — unsupported.
        if not self._auth_identity(incoming.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            incoming.chat,
            "This message type is not supported. Please send text, a voice message, or a file.",
        )

    async def _handle_group_text(self, incoming) -> bool:
        """Route a group-mode text message via the Transport-native path.

        Returns True if handled (caller should return immediately).
        Returns False if the message should proceed to further processing
        (normal user flow OR bot-to-bot direct-to-Claude).
        """
        from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot

        # Auto-capture: if chat_id not yet bound and sender is trusted, write it.
        if self.group_chat_id in (0, None):
            if self._auth_identity(incoming.sender) and self.team_name:
                new_chat_id = int(incoming.chat.native_id)
                patch_team(
                    self.team_name,
                    {"group_chat_id": new_chat_id},
                    self._effective_config_path(),
                )
                self.group_chat_id = new_chat_id
                # Fall through so this message still gets processed.
        elif int(incoming.chat.native_id) != self.group_chat_id:
            return True  # wrong group — silent ignore

        if is_from_self(incoming, self.bot_username):
            return True  # self-silence

        if not is_directed_at_me(incoming, self.bot_username):
            return True  # not addressed to this bot

        if incoming.is_relayed_bot_to_bot or is_from_other_bot(incoming, self.bot_username):
            # Bot-to-bot path — via relay (is_relayed_bot_to_bot) or native (non-Telegram transports).
            if self._group_state.get(incoming.chat).halted:
                return True
            self._group_state.note_bot_to_bot(incoming.chat)
            if self._group_state.get(incoming.chat).halted:
                assert self._transport is not None
                await self._transport.send_text(
                    incoming.chat,
                    f"Auto-paused after {self._group_state.max_bot_rounds} bot-to-bot rounds. "
                    "Send any message to resume.",
                )
                return True
            # Caller submits to Claude on False return via _submit_group_message_to_claude.
            return False

        # Human (trusted user) message — reset the round counter and clear any halt.
        self._group_state.resume(incoming.chat)
        return False

    async def _submit_group_message_to_claude(self, incoming) -> None:
        """Bypass auth + rate-limit and submit a bot-to-bot message to Claude.

        Called when _handle_group_text returned False AND the sender is a bot/relay
        (so the message has already been validated as peer-bot-to-this-bot and
        the round counter has been incremented).

        Human messages in groups go through the full auth/rate-limit path via
        the legacy _on_text shim — not through this method.
        """
        assert self._transport is not None
        prompt = incoming.text
        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)
        self.task_manager.submit_agent(
            chat=incoming.chat,
            message=incoming.message,
            prompt=prompt,
        )

    async def _on_text(self, incoming) -> None:
        """Handle a plain-text message: auth, rate-limit, pending skill/persona
        capture, waiting-input routing, supersede, then Claude submission.

        All routing keys are read from `IncomingMessage` directly — no
        telegram/ctx knowledge required.
        """
        assert self._transport is not None
        # Empty text is only actionable when the dispatcher routed an
        # unsupported-media message here for the specific rejection.
        if not incoming.text.strip() and not incoming.has_unsupported_media:
            return
        if not self._auth_identity(incoming.sender):
            await self._transport.send_text(
                incoming.chat, "Unauthorized.", reply_to=incoming.message,
            )
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            await self._transport.send_text(
                incoming.chat, "Rate limited. Try again shortly.", reply_to=incoming.message,
            )
            return

        if incoming.has_unsupported_media:
            await self._transport.send_text(
                incoming.chat,
                "Unsupported media type. I can read text, photos, documents, voice notes, and audio.",
                reply_to=incoming.message,
            )
            return

        # Pending skill capture (set by /create_skill + scope-button click).
        pending_skill = self._pending_skill_name
        pending_scope = self._pending_skill_scope
        if pending_skill and pending_scope:
            from .skills import save_skill
            save_skill(pending_skill, incoming.text, self.path, scope=pending_scope)
            self._pending_skill_name = None
            self._pending_skill_scope = None
            icon = "🌐" if pending_scope == "global" else "📁"
            await self._transport.send_text(
                incoming.chat,
                f"{icon} Skill '{pending_skill}' created ({pending_scope}). "
                f"Use /use {pending_skill} to activate.",
                reply_to=incoming.message,
            )
            return

        # Pending persona capture (same shape as pending skill).
        pending_persona = self._pending_persona_name
        pending_persona_scope = self._pending_persona_scope
        if pending_persona and pending_persona_scope:
            from .skills import save_persona
            save_persona(pending_persona, incoming.text, self.path, scope=pending_persona_scope)
            self._pending_persona_name = None
            self._pending_persona_scope = None
            icon = "🌐" if pending_persona_scope == "global" else "📁"
            await self._transport.send_text(
                incoming.chat,
                f"{icon} Persona '{pending_persona}' created ({pending_persona_scope}). "
                f"Use /persona {pending_persona} to activate.",
                reply_to=incoming.message,
            )
            return

        message_ref = incoming.message

        # Active Claude turn waiting on a question? Route as the answer.
        waiting = self.task_manager.waiting_input_task(incoming.chat)
        if waiting:
            self.task_manager.submit_answer(waiting.id, incoming.text)
            return

        # Supersede any in-flight task tied to this message (edit-message case).
        for prev in self.task_manager.find_by_message(message_ref):
            self.task_manager.cancel(prev.id)
            await self._cancel_live_for(prev.id, "(superseded)")
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        prompt = incoming.text
        if incoming.reply_to_text:
            prompt = f"[Replying to: {incoming.reply_to_text}]\n\n{prompt}"
        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)
        self.task_manager.submit_agent(
            chat=incoming.chat,
            message=message_ref,
            prompt=prompt,
        )

    async def _on_run(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /run <command>", reply_to=ci.message)
            return
        command = " ".join(ci.args)
        self.task_manager.run_command(
            chat=ci.chat,
            message=ci.message,
            command=command,
        )

    _TASK_ICONS = {
        TaskStatus.WAITING: "~",
        TaskStatus.RUNNING: ">",
        TaskStatus.DONE: "+",
        TaskStatus.FAILED: "!",
        TaskStatus.CANCELLED: "x",
    }

    def _tasks_buttons(self, chat: ChatRef) -> Buttons | None:
        all_tasks = self.task_manager.list_tasks(chat=chat, limit=100)
        active = [t for t in all_tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
        finished = [t for t in all_tasks if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)][:5]
        tasks = active + finished
        if not tasks:
            return None
        rows: list[list[Button]] = []
        for t in tasks:
            icon = self._TASK_ICONS.get(t.status, "?")
            elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:40]
            rows.append([Button(label=f"{icon} #{t.id}{elapsed} {label}", value=f"task_info_{t.id}")])
        return Buttons(rows=rows)

    async def _render_tasks(self, chat: ChatRef, msg_ref: MessageRef) -> None:
        """Render the tasks list into an existing message (edit)."""
        buttons = self._tasks_buttons(chat)
        assert self._transport is not None
        await self._transport.edit_text(
            msg_ref,
            "Tasks:" if buttons else "No tasks.",
            buttons=buttons,
        )

    async def _on_tasks(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        buttons = self._tasks_buttons(ci.chat)
        assert self._transport is not None
        await self._transport.send_text(
            ci.chat,
            "Tasks:" if buttons else "No tasks.",
            buttons=buttons,
        )

    MODEL_OPTIONS = [
        ("opus[1m]", "Opus 4.7 1M", "Most capable, 1M context"),
        ("opus", "Opus 4.7", "Most capable"),
        ("sonnet[1m]", "Sonnet 4.6 1M", "Everyday tasks, 1M context"),
        ("sonnet", "Sonnet 4.6", "Best for everyday tasks"),
        ("haiku", "Haiku 4.5", "Fastest for quick answers"),
    ]

    def _model_buttons(self) -> Buttons:
        current = self.task_manager.backend.model
        rows: list[list[Button]] = []
        for model_id, label, _ in self.MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([Button(label=f"{prefix}{label}", value=f"model_set_{model_id}")])
        return Buttons(rows=rows)

    def _current_model(self) -> str:
        raw = self.task_manager.backend.model
        for model_id, label, desc in self.MODEL_OPTIONS:
            if model_id == raw:
                return f"{label} — {desc}"
        return self._claude.model_display or raw

    async def _on_model(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            ci.chat,
            f"Select model\nCurrent: {self._current_model()}",
            buttons=self._model_buttons(),
        )

    def _effort_buttons(self) -> Buttons:
        return Buttons(rows=[
            [Button(label=e, value=f"effort_set_{e}")]
            for e in EFFORT_LEVELS
        ])

    def _current_effort(self) -> str:
        return self._claude.effort

    async def _on_effort(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            ci.chat,
            f"Current: {self._current_effort()}",
            buttons=self._effort_buttons(),
        )

    def _thinking_buttons(self) -> Buttons:
        return Buttons(rows=[[
            Button(label="On", value="thinking_set_on"),
            Button(label="Off", value="thinking_set_off"),
        ]])

    def _current_thinking(self) -> str:
        return "on" if self.show_thinking else "off"

    async def _on_thinking(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        args = ci.args or []
        if args:
            arg = args[0].lower()
            if arg in ("on", "off"):
                self.show_thinking = arg == "on"
                self._patch_config({"show_thinking": self.show_thinking})
                await self._transport.send_text(
                    ci.chat, f"Live thinking: {self._current_thinking()}"
                )
                return
            await self._transport.send_text(ci.chat, "Usage: /thinking on|off")
            return
        await self._transport.send_text(
            ci.chat,
            f"Live thinking: {self._current_thinking()}",
            buttons=self._thinking_buttons(),
        )

    _PERMISSION_OPTIONS = (
        *PERMISSION_MODES,
        "dangerously-skip-permissions",
    )

    def _permissions_buttons(self) -> Buttons:
        return Buttons(rows=[
            [Button(label=o, value=f"permissions_set_{o}")]
            for o in self._PERMISSION_OPTIONS
        ])

    def _current_permission(self) -> str:
        claude = self._claude
        if claude.skip_permissions:
            return "dangerously-skip-permissions"
        return claude.permission_mode or "default"

    async def _on_permissions(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            ci.chat,
            f"Current: {self._current_permission()}",
            buttons=self._permissions_buttons(),
        )

    async def _on_compact(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not self.task_manager.backend.session_id:
            await self._transport.send_text(ci.chat, "No active session.")
            return
        self.task_manager.submit_compact(
            chat=ci.chat,
            message=ci.message,
        )

    async def _on_version_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        from . import __version__
        assert self._transport is not None
        await self._transport.send_text(ci.chat, f"link-project-to-chat v{__version__}")

    async def _on_help_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(ci.chat, _CMD_HELP)

    async def _on_reset(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        buttons = Buttons(rows=[[
            Button(label="Yes, reset", value="reset_confirm"),
            Button(label="Cancel", value="reset_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat,
            "Are you sure? This will clear the Claude session.",
            buttons=buttons,
        )

    def _picker_buttons(self, items: dict, prefix: str) -> Buttons | None:
        if not items:
            return None
        btns = [
            Button(label=name, value=f"{prefix}_{name}")
            for name in sorted(items)
        ]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        return Buttons(rows=rows)

    async def _on_skills(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if ci.args:
            name = ci.args[0].lower()
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                await self._transport.send_text(ci.chat, f"Skill '{name}' not found.")
                return
            self._claude.append_system_prompt = skill.content
            self._active_skill = name
            await self._transport.send_text(
                ci.chat, f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate."
            )
            return
        from .skills import load_skills
        skills = load_skills(self.path)
        if not skills:
            await self._transport.send_text(
                ci.chat, "No skills available.\nCreate one with /create_skill <name>"
            )
            return
        lines = ["Available skills:"]
        for name, skill in sorted(skills.items()):
            icon = "\U0001f4c1" if skill.source == "project" else "\U0001f310" if skill.source == "global" else "\U0001f916"
            lines.append(f"  {icon} {name} ({skill.source})")
        if self._active_skill:
            lines.append(f"\nActive: {self._active_skill}")
        buttons = self._picker_buttons(skills, "pick_skill")
        await self._transport.send_text(ci.chat, "\n".join(lines), buttons=buttons)

    async def _on_stop_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not self._active_skill:
            await self._transport.send_text(ci.chat, "No active skill.")
            return
        old = self._active_skill
        self._active_skill = None
        self._claude.append_system_prompt = None
        await self._transport.send_text(ci.chat, f"Skill '{old}' deactivated.")

    async def _on_create_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /create_skill <name>")
            return
        name = ci.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "skill")
        except ValueError as e:
            await self._transport.send_text(ci.chat, str(e))
            return
        # Stash pending name so the scope-click handler knows which skill to create.
        self._pending_skill_name = name
        buttons = Buttons(rows=[[
            Button(label="📁 Project", value=f"skill_scope_project_{name}"),
            Button(label="🌐 Global", value=f"skill_scope_global_{name}"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Where should skill '{name}' be saved?", buttons=buttons,
        )

    async def _on_delete_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /delete_skill <name>")
            return
        name = ci.args[0].lower()
        from .skills import load_skill
        skill = load_skill(name, self.path)
        if not skill:
            await self._transport.send_text(ci.chat, f"Skill '{name}' not found.")
            return
        icon = "🌐" if skill.source == "global" else "📁"
        buttons = Buttons(rows=[[
            Button(label="Delete", value=f"skill_delete_confirm_{skill.source}_{name}"),
            Button(label="Cancel", value="skill_delete_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Delete {icon} {skill.source} skill '{name}'?", buttons=buttons,
        )

    # --- Persona handlers ---

    def _refresh_team_system_note(self) -> None:
        """(Re)build the Claude system note that pins the bot's own + peer @handle.

        Called from __init__ (self handle still unknown — note carries peer only)
        and from _after_ready after get_me() fills self.bot_username (note now
        carries both). Without the self handle Claude tends to invent one from
        the persona name — e.g. the pre-suffix-bump ``@..._dev_claude_bot``
        when the real handle is ``@..._dev_2_claude_bot``.
        """
        if not self.peer_bot_username:
            self._claude.team_system_note = None
            return
        peer_role = "developer" if self.role == "manager" else "manager"
        self_line = (
            f"Your own Telegram @handle in this group is @{self.bot_username}. "
            if self.bot_username
            else ""
        )
        self._claude.team_system_note = (
            f"You are the '{self.role}' role bot in a dual-agent team group. "
            f"{self_line}"
            f"Your team peer (role: {peer_role}) in this group is @{self.peer_bot_username}. "
            f"Use the exact @handles pinned above — never placeholders like "
            f"'@developer'/'@manager' and never a different suffix. "
            f"\n\nRecipient routing: the group has two audiences — the human user and "
            f"your peer — and you choose who hears each reply:\n"
            f"- To hand work to your peer, ask them a substantive question, or deliver "
            f"actionable feedback: BEGIN the reply with @{self.peer_bot_username} on "
            f"its own line, followed by a blank line, then the body. The group relay "
            f"forwards any reply that starts with this @mention to your peer.\n"
            f"- To respond to the user: reply normally WITHOUT @{self.peer_bot_username}. "
            f"The relay does not forward those, so they go only to the human.\n"
            f"- For pure acknowledgments ('ok', 'agreed', 'noted', 'standing by', '👍'): "
            f"do not reply at all. Silence is the correct answer when there is nothing "
            f"actionable to add — echoing an acknowledgment back to your peer just "
            f"creates a ping-pong loop. The relay drops ack-only messages as a safety "
            f"net, but you should not produce them in the first place."
        )

    def _backfill_own_bot_username(self, config_path: Path | None = None) -> None:
        """One-time migration: write self.bot_username into TeamConfig if empty.

        For teams created before bot_username was stored at /create_team time,
        this lets the next team-bot startup discover each bot's @handle and
        populate it in config.json so the peer role can read it.
        """
        cfg = config_path or self._effective_config_path()
        teams = load_teams(cfg)
        team = teams.get(self.team_name or "")
        if team is None:
            return
        bot = team.bots.get(self.role or "")
        if bot is None or bot.bot_username == self.bot_username:
            return
        self._patch_config({"bot_username": self.bot_username}, cfg)
        logger.info(
            "Backfilled bot_username=%s for team=%s role=%s in config",
            self.bot_username, self.team_name, self.role,
        )

    def _patch_config(self, fields: dict, config_path: Path | None = None) -> None:
        """Persist config fields for this bot, routing to team or project config.

        Team bots live under ``config.teams[team].bots[role]``; solo bots under
        ``config.projects[name]``. ``patch_project`` on a team bot would create
        a stray projects entry and never touch the real team config, so the
        team path re-serialises the full ``bots`` dict via ``patch_team``.
        """
        cfg = config_path or self._effective_config_path()
        if self.team_name:
            teams = load_teams(cfg)
            team = teams.get(self.team_name)
            if team is None:
                logger.warning("Team %r not in config; change %r not persisted.", self.team_name, fields)
                return
            bots_dict: dict[str, dict] = {}
            for role, bot in team.bots.items():
                entry: dict = {"telegram_bot_token": bot.telegram_bot_token}
                if bot.active_persona is not None:
                    entry["active_persona"] = bot.active_persona
                if bot.autostart:
                    entry["autostart"] = True
                if bot.permissions is not None:
                    entry["permissions"] = bot.permissions
                if bot.bot_username:
                    entry["bot_username"] = bot.bot_username
                if bot.session_id is not None:
                    entry["session_id"] = bot.session_id
                if bot.model is not None:
                    entry["model"] = bot.model
                if bot.effort is not None:
                    entry["effort"] = bot.effort
                if bot.show_thinking:
                    entry["show_thinking"] = True

                if role == self.role:
                    for k, v in fields.items():
                        if v is None:
                            entry.pop(k, None)
                        else:
                            entry[k] = v
                bots_dict[role] = entry
            patch_team(self.team_name, {"bots": bots_dict}, cfg)
        else:
            patch_project(self.name, fields, cfg)

    def _persist_active_persona(self, name: str | None, config_path: Path | None = None) -> None:
        """Persist this bot's active_persona."""
        self._patch_config({"active_persona": name}, config_path)

    async def _on_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            if self._active_persona:
                await self._transport.send_text(
                    ci.chat,
                    f"Active persona: {self._active_persona}\nUse /stop_persona to deactivate.",
                )
                return
            from .skills import load_personas
            buttons = self._picker_buttons(load_personas(self.path), "pick_persona")
            if not buttons:
                await self._transport.send_text(
                    ci.chat, "No personas available.\nCreate one with /create_persona <name>"
                )
                return
            await self._transport.send_text(ci.chat, "Pick a persona to activate:", buttons=buttons)
            return
        name = ci.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            await self._transport.send_text(ci.chat, f"Persona '{name}' not found.")
            return
        self._active_persona = name
        self._persist_active_persona(name)
        await self._transport.send_text(
            ci.chat, f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate."
        )

    async def _on_stop_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not self._active_persona:
            await self._transport.send_text(ci.chat, "No active persona.")
            return
        old = self._active_persona
        self._active_persona = None
        self._persist_active_persona(None)
        await self._transport.send_text(ci.chat, f"Persona '{old}' deactivated.")

    async def _on_halt(self, ci) -> None:
        assert self._transport is not None
        if not self.group_mode:
            await self._transport.send_text(
                ci.chat, "/halt is only available in group mode.", reply_to=ci.message,
            )
            return
        if self.group_chat_id is not None and int(ci.chat.native_id) != self.group_chat_id:
            return  # silently ignore — wrong group
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        self._group_state.halt(ci.chat)
        await self._transport.send_text(
            ci.chat, "Halted. Use /resume to continue.", reply_to=ci.message,
        )

    async def _on_resume(self, ci) -> None:
        assert self._transport is not None
        if not self.group_mode:
            await self._transport.send_text(
                ci.chat, "/resume is only available in group mode.", reply_to=ci.message,
            )
            return
        if self.group_chat_id is not None and int(ci.chat.native_id) != self.group_chat_id:
            return  # silently ignore — wrong group
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        self._group_state.resume(ci.chat)
        await self._transport.send_text(ci.chat, "Resumed.", reply_to=ci.message)

    async def _on_create_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /create_persona <name>")
            return
        name = ci.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "persona")
        except ValueError as e:
            await self._transport.send_text(ci.chat, str(e))
            return
        self._pending_persona_name = name
        buttons = Buttons(rows=[[
            Button(label="📁 Project", value=f"persona_scope_project_{name}"),
            Button(label="🌐 Global", value=f"persona_scope_global_{name}"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Where should persona '{name}' be saved?", buttons=buttons,
        )

    async def _on_delete_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /delete_persona <name>")
            return
        name = ci.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            await self._transport.send_text(ci.chat, f"Persona '{name}' not found.")
            return
        icon = "🌐" if persona.source == "global" else "📁"
        buttons = Buttons(rows=[[
            Button(label="Delete", value=f"persona_delete_confirm_{persona.source}_{name}"),
            Button(label="Cancel", value="persona_delete_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Delete {icon} {persona.source} persona '{name}'?", buttons=buttons,
        )

    async def _on_voice_status(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if self._transcriber:
            backend = type(self._transcriber).__name__
            await self._transport.send_text(
                ci.chat, f"Voice: enabled ({backend})", reply_to=ci.message,
            )
        else:
            await self._transport.send_text(
                ci.chat,
                "Voice: disabled\nConfigure with: link-project-to-chat setup",
                reply_to=ci.message,
            )

    LANGUAGES = [
        ("auto", "Auto-detect"),
        ("en", "English"),
        ("ka", "Georgian"),
        ("ru", "Russian"),
        ("de", "German"),
        ("fr", "French"),
        ("es", "Spanish"),
        ("tr", "Turkish"),
        ("uk", "Ukrainian"),
        ("ja", "Japanese"),
        ("zh", "Chinese"),
    ]

    async def _on_lang(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not self._transcriber:
            await self._transport.send_text(ci.chat, "Voice is not configured.")
            return
        if ci.args:
            code = ci.args[0].lower()
            if code == "auto":
                code = ""
            self._transcriber._language = code
            label = code or "auto-detect"
            await self._transport.send_text(ci.chat, f"Voice language set to: {label}")
            return
        current = getattr(self._transcriber, "_language", "") or "auto-detect"
        btns = [
            Button(
                label=f"{'● ' if (code or '') == getattr(self._transcriber, '_language', '') else ''}{label}",
                value=f"lang_set_{code}",
            )
            for code, label in self.LANGUAGES
        ]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        await self._transport.send_text(
            ci.chat,
            f"Current language: {current}\nSelect voice language:",
            buttons=Buttons(rows=rows),
        )

    async def _on_button(self, click) -> None:
        """Transport-native button-click handler. Replaces legacy _on_callback."""
        if not self._auth_identity(click.sender):
            return
        assert self._transport is not None
        value = click.value
        msg_ref = click.message
        chat = click.chat

        if value.startswith("model_set_"):
            name = value[len("model_set_"):]
            valid = {m[0] for m in self.MODEL_OPTIONS}
            if name in valid:
                self.task_manager.backend.model = name
                self._claude.model_display = None
                self._patch_config({"model": name})
            await self._transport.edit_text(
                msg_ref,
                f"Select model\nCurrent: {self._current_model()}",
                buttons=self._model_buttons(),
            )
        elif value.startswith("effort_set_"):
            level = value[len("effort_set_"):]
            if level in EFFORT_LEVELS:
                self._claude.effort = level
                self._patch_config({"effort": level})
            await self._transport.edit_text(
                msg_ref,
                f"Effort: {self._current_effort()}",
                buttons=self._effort_buttons(),
            )
        elif value.startswith("thinking_set_"):
            val = value[len("thinking_set_"):]
            if val in ("on", "off"):
                self.show_thinking = val == "on"
                self._patch_config({"show_thinking": self.show_thinking})
            await self._transport.edit_text(
                msg_ref,
                f"Live thinking: {self._current_thinking()}",
                buttons=self._thinking_buttons(),
            )
        elif value.startswith("permissions_set_"):
            mode = value[len("permissions_set_"):]
            if mode == "dangerously-skip-permissions" or mode in PERMISSION_MODES:
                skip, pm = resolve_permissions(mode)
                self._claude.skip_permissions = skip
                self._claude.permission_mode = pm
                self._patch_config({"permissions": mode if mode != "default" else None})
            await self._transport.edit_text(
                msg_ref,
                f"Permissions: {self._current_permission()}",
                buttons=self._permissions_buttons(),
            )
        elif value.startswith("lang_set_"):
            code = value[len("lang_set_"):]
            if code == "auto":
                code = ""
            if self._transcriber:
                self._transcriber._language = code
            label = code or "auto-detect"
            btns = [
                Button(
                    label=f"{'● ' if (c or '') == code else ''}{l}",
                    value=f"lang_set_{c}",
                )
                for c, l in self.LANGUAGES
            ]
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
            await self._transport.edit_text(
                msg_ref,
                f"Voice language set to: {label}",
                buttons=Buttons(rows=rows),
            )
        elif value == "reset_confirm":
            live_task_ids = list({*self._live_text.keys(), *self._live_thinking.keys()})
            for tid in live_task_ids:
                await self._cancel_live_for(tid)
            self.task_manager.cancel_all()
            self.task_manager.backend.session_id = None
            self._active_skill = None
            self._active_persona = None
            self._claude.append_system_prompt = None
            clear_session(
                self.name,
                self._effective_config_path(),
                team_name=self.team_name,
                role=self.role,
            )
            await self._transport.edit_text(msg_ref, "Session reset.")
        elif value == "reset_cancel":
            await self._transport.edit_text(msg_ref, "Reset cancelled.")
        # Skill callbacks
        elif value.startswith("skill_delete_confirm_"):
            rest = value[len("skill_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_skill as _delete_skill
            _delete_skill(name, self.path, scope=scope)
            if self._active_skill == name:
                self._active_skill = None
                self._claude.append_system_prompt = None
            await self._transport.edit_text(msg_ref, f"Skill '{name}' deleted.")
        elif value == "skill_delete_cancel":
            await self._transport.edit_text(msg_ref, "Cancelled.")
        elif value.startswith("skill_scope_"):
            rest = value[len("skill_scope_"):]
            scope, _, name = rest.partition("_")
            self._pending_skill_name = name
            self._pending_skill_scope = scope
            await self._transport.edit_text(msg_ref, f"Send the content for skill '{name}':")
        elif value.startswith("pick_skill_"):
            name = value[len("pick_skill_"):]
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                await self._transport.edit_text(msg_ref, f"Skill '{name}' not found.")
                return
            self._claude.append_system_prompt = skill.content
            self._active_skill = name
            await self._transport.edit_text(
                msg_ref, f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate."
            )
        # Persona callbacks
        elif value.startswith("persona_delete_confirm_"):
            rest = value[len("persona_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_persona as _delete_persona
            _delete_persona(name, self.path, scope=scope)
            if self._active_persona == name:
                self._active_persona = None
            await self._transport.edit_text(msg_ref, f"Persona '{name}' deleted.")
        elif value == "persona_delete_cancel":
            await self._transport.edit_text(msg_ref, "Cancelled.")
        elif value.startswith("persona_scope_"):
            rest = value[len("persona_scope_"):]
            scope, _, name = rest.partition("_")
            self._pending_persona_name = name
            self._pending_persona_scope = scope
            await self._transport.edit_text(msg_ref, f"Send the content for persona '{name}':")
        elif value.startswith("pick_persona_"):
            name = value[len("pick_persona_"):]
            from .skills import load_persona
            persona = load_persona(name, self.path)
            if not persona:
                await self._transport.edit_text(msg_ref, f"Persona '{name}' not found.")
                return
            self._active_persona = name
            self._persist_active_persona(name)
            await self._transport.edit_text(
                msg_ref, f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate."
            )
        elif value.startswith("ask_"):
            parts = value.split("_")
            if len(parts) != 4:
                return
            try:
                task_id = int(parts[1])
                q_idx = int(parts[2])
                opt_idx = int(parts[3])
            except ValueError:
                return
            task = self.task_manager.get(task_id)
            if not task or task.status != TaskStatus.WAITING_INPUT:
                # Drop the buttons by editing the text without buttons.
                return
            if q_idx >= len(task.pending_questions):
                return
            question = task.pending_questions[q_idx]
            if opt_idx >= len(question.options):
                return
            label = question.options[opt_idx].label
            if self.task_manager.submit_answer(task_id, label):
                # Annotate the selection into the existing message body.
                try:
                    original_html = self._render_question_html(question)
                    escaped = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    await self._transport.edit_text(
                        msg_ref,
                        f"{original_html}\n\n<i>Selected:</i> {escaped}",
                        html=True,
                    )
                except Exception:
                    logger.debug("could not annotate selected option", exc_info=True)
        elif value.startswith("task_info_"):
            task_id = _parse_task_id(value)
            task = self.task_manager.get(task_id)
            if not task:
                await self._transport.edit_text(msg_ref, f"Task #{task_id} not found.")
                return
            elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
            text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:200]}"
            rows: list[list[Button]] = []
            if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                rows.append([Button(label="Cancel", value=f"task_cancel_{task_id}")])
            if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                rows.append([Button(label="Log", value=f"task_log_{task_id}")])
            if task_id in self._thinking_store:
                rows.append([Button(label="Thinking", value=f"show_thinking_{task_id}")])
            rows.append([Button(label="« Back", value="tasks_back")])
            await self._transport.edit_text(msg_ref, text, buttons=Buttons(rows=rows))
        elif value == "tasks_back":
            await self._render_tasks(chat, msg_ref)
        elif value.startswith("task_cancel_"):
            task_id = _parse_task_id(value)
            await self._cancel_live_for(task_id)
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await self._transport.edit_text(msg_ref, f"#{task_id} cancelled.")
            else:
                await self._transport.edit_text(
                    msg_ref, f"#{task_id} not found or already finished."
                )
        elif value.startswith("task_log_"):
            task_id = _parse_task_id(value)
            task = self.task_manager.get(task_id)
            if not task:
                await self._transport.edit_text(msg_ref, f"Task #{task_id} not found.")
                return
            if task._log:
                output = "\n".join(task._log)
            else:
                output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            rows = [[Button(label="« Back", value=f"task_info_{task_id}")]]
            await self._transport.edit_text(
                msg_ref, f"#{task_id} log:\n{output}", buttons=Buttons(rows=rows),
            )
        elif value.startswith("show_thinking_"):
            try:
                task_id = int(value[len("show_thinking_"):])
            except ValueError:
                return
            thinking = self._thinking_store.get(task_id)
            if not thinking:
                return
            await self._send_to_chat(chat, f"💭 {thinking}")

    def _compose_status(self) -> str:
        uptime = time.monotonic() - self._started_at
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)

        st = self.task_manager.backend.status
        lines = [
            f"Project: {self.name}",
            f"Path: {self.path}",
            f"Model: {self._claude.model_display or self.task_manager.backend.model}",
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st['session_id'] or 'none'}",
            f"Claude: {'RUNNING' if st['running'] else 'idle'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
            f"Skill: {self._active_skill or 'none'}",
            f"Persona: {self._active_persona or 'none'}",
        ]
        return "\n".join(lines)

    async def _on_status_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(ci.chat, self._compose_status())

    async def _on_file_from_transport(self, incoming) -> None:
        """Transport-native file handler. Copies each incoming file from the
        transport's temp dir into the platform temp root under
        link-project-to-chat/<project>/uploads/ and submits it to Claude (or
        the waiting-input task)."""
        import shutil

        if not self._auth_identity(incoming.sender):
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            assert self._transport is not None
            await self._transport.send_text(incoming.chat, "Rate limited. Try again shortly.")
            return
        if not incoming.files:
            return

        uploads_dir = (
            Path(tempfile.gettempdir())
            / "link-project-to-chat"
            / self.name
            / "uploads"
        )
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Single-file behavior identical to legacy _on_file.
        f = incoming.files[0]
        # Sanitize the original name.
        raw_name = f.original_name or f"file_{int(time.monotonic() * 1000)}"
        filename = "".join(
            c for c in raw_name.replace("/", "_").replace("\\", "_")
            if c.isalnum() or c in "._- "
        )[:200] or "file"

        dest = uploads_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 2
            while dest.exists():
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        shutil.copyfile(f.path, dest)

        caption = incoming.text or ""
        prompt = f"[User uploaded {dest}]"
        if caption:
            prompt += f"\n\n{caption}"

        waiting = self.task_manager.waiting_input_task(incoming.chat)
        if waiting:
            self.task_manager.submit_answer(waiting.id, prompt)
            return

        self.task_manager.submit_agent(
            chat=incoming.chat,
            message=incoming.message,
            prompt=prompt,
        )

    async def _on_voice_from_transport(self, incoming) -> None:
        if not self._auth_identity(incoming.sender):
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            assert self._transport is not None
            await self._transport.send_text(incoming.chat, "Rate limited. Try again shortly.")
            return
        assert self._transport is not None

        if not self._transcriber:
            await self._transport.send_text(
                incoming.chat,
                "Voice messages aren't configured. "
                "Set up STT with: link-project-to-chat setup --stt-backend whisper-api",
            )
            return

        if not incoming.files:
            return
        audio = incoming.files[0]

        MAX_VOICE_BYTES = 20 * 1024 * 1024
        if audio.size_bytes > MAX_VOICE_BYTES:
            size_mb = audio.size_bytes // (1024 * 1024)
            await self._transport.send_text(
                incoming.chat, f"Audio too large ({size_mb} MB). 20 MB limit.",
            )
            return

        status_ref = await self._transport.send_text(incoming.chat, "🎤 Transcribing...")

        try:
            text = await self._transcriber.transcribe(audio.path)

            if not text or not text.strip():
                await self._transport.edit_text(
                    status_ref, "Could not transcribe the voice message (empty result).",
                )
                return

            display = text if len(text) <= 200 else text[:200] + "..."
            await self._transport.edit_text(status_ref, f'🎤 "{display}"')

            waiting = self.task_manager.waiting_input_task(incoming.chat)
            if waiting:
                self.task_manager.submit_answer(waiting.id, text)
                return

            prompt = text
            if incoming.reply_to_text:
                prompt = f"[Replying to: {incoming.reply_to_text}]\n\n{prompt}"

            if self._active_persona:
                from .skills import load_persona, format_persona_prompt
                persona = load_persona(self._active_persona, self.path)
                if persona:
                    prompt = format_persona_prompt(persona, prompt)

            task = self.task_manager.submit_agent(
                chat=incoming.chat,
                message=incoming.message,
                prompt=prompt,
            )
            if self._synthesizer:
                self._voice_tasks.add(task.id)

        except Exception as e:
            logger.exception("Voice transcription failed")
            error_summary = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            await self._transport.edit_text(
                status_ref, f"Transcription failed: {error_summary}",
            )

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath

        return PurePosixPath(path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _send_image(
        self, chat: ChatRef, file_path: str, reply_to: MessageRef | None = None,
    ) -> None:
        path = (
            self.path / file_path if not file_path.startswith("/") else Path(file_path)
        )
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            logger.warning("Invalid image path: %s", file_path)
            return
        if not resolved.is_relative_to(self.path.resolve()):
            logger.warning("Image path traversal blocked: %s", file_path)
            return
        if not resolved.exists():
            logger.warning("Image file not found: %s", resolved)
            return
        assert self._transport is not None
        try:
            # Oversized (>10MB) or SVG — Transport.send_file picks document-mode
            # for non-image suffixes automatically; SVG has .svg suffix not in
            # _IMAGE_SUFFIXES, so it's routed to send_document. Size-based fall-
            # back for large PNG/JPG files: send_file uses send_photo which may
            # reject >10MB — legacy code switched to send_document. For now we
            # rely on transport's suffix heuristic; rare-case large images fall
            # back via exception handling below.
            await self._transport.send_file(chat, path, caption=path.name)
        except Exception:
            logger.warning("Failed to send image %s", path, exc_info=True)

    @staticmethod
    async def _keep_typing(transport, chat_ref: ChatRef) -> None:
        try:
            while True:
                try:
                    await transport.send_typing(chat_ref)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    async def _after_ready(self, self_identity) -> None:
        """Called once after Transport.start() completes platform post-init.

        The Transport has already run delete_webhook, get_me, and
        set_my_commands. This callback does the bot-specific post-ready
        work: backfill missing team metadata, refresh the Claude system
        note with the discovered @handle, and send startup pings to
        trusted users.
        """
        self.bot_username = self_identity.handle or ""
        if self.team_name and self.role and self.bot_username:
            self._backfill_own_bot_username()
        self._refresh_team_system_note()

        # Startup ping to trusted users.
        assert self._transport is not None
        for uid in self._get_trusted_user_ids():
            chat = ChatRef(
                transport_id=self._transport.TRANSPORT_ID,
                native_id=str(uid),
                kind=ChatKind.DM,
            )
            try:
                await self._transport.send_text(
                    chat, f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                )
            except Exception:
                logger.error("Failed to send startup message to %d", uid, exc_info=True)

    def build(self):
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport.build(self.token, menu=COMMANDS)
        self._app = self._transport.app
        self._transport.on_ready(self._after_ready)

        async def _pre_authorize(identity) -> bool:
            return self._auth_identity(identity)

        self._transport.set_authorizer(_pre_authorize)

        app = self._app

        # All commands consume CommandInvocation directly — no legacy shim.
        ported_commands = (
            ("help", self._on_help_t),
            ("version", self._on_version_t),
            ("status", self._on_status_t),
            ("tasks", self._on_tasks),
            ("model", self._on_model),
            ("effort", self._on_effort),
            ("thinking", self._on_thinking),
            ("permissions", self._on_permissions),
            ("compact", self._on_compact),
            ("reset", self._on_reset),
            ("skills", self._on_skills),
            ("stop_skill", self._on_stop_skill),
            ("create_skill", self._on_create_skill),
            ("delete_skill", self._on_delete_skill),
            ("persona", self._on_persona),
            ("stop_persona", self._on_stop_persona),
            ("create_persona", self._on_create_persona),
            ("delete_persona", self._on_delete_persona),
            ("lang", self._on_lang),
            ("start", self._on_start),
            ("run", self._on_run),
            ("voice", self._on_voice_status),
            ("halt", self._on_halt),
            ("resume", self._on_resume),
        )
        self._transport.on_message(self._on_text_from_transport)
        self._transport.on_button(self._on_button)
        for _name, _handler in ported_commands:
            self._transport.on_command(_name, _handler)

        # Main routing — registers MessageHandler/CommandHandler/CallbackQueryHandler.
        self._transport.attach_telegram_routing(
            group_mode=self.group_mode,
            command_names=[n for n, _ in ported_commands],
        )

        # Team-mode bot: if the manager passed a Telethon session path, wire the
        # bot-to-bot relay. Spec #0c: project bot owns its relay; the manager
        # exposes the session-file absolute path via LP2C_TELETHON_SESSION when
        # spawning team-mode subprocesses (see manager/process.py).
        if self.team_name and self.group_chat_id:
            session_env = os.environ.get("LP2C_TELETHON_SESSION")
            if session_env:
                cfg_path = self._effective_config_path()
                config = load_config(cfg_path)
                teams = load_teams(cfg_path)
                team = teams.get(self.team_name)
                team_bot_usernames = {
                    b.bot_username for b in team.bots.values() if b.bot_username
                } if team else set()
                if not team_bot_usernames:
                    logger.warning(
                        "Team %r missing from config or has no bot usernames; "
                        "skipping enable_team_relay (bot will operate without relay).",
                        self.team_name,
                    )
                else:
                    self._transport.enable_team_relay_from_session(
                        session_path=session_env,
                        api_id=config.telegram_api_id,
                        api_hash=config.telegram_api_hash,
                        team_bot_usernames=team_bot_usernames,
                        group_chat_id=self.group_chat_id,
                        team_name=self.team_name,
                    )

        return app

    def run(self) -> None:
        """Run the transport's main loop. Owns the lifecycle from here on.

        Synchronous: matches the underlying Transport.run() contract.
        """
        assert self._transport is not None
        self._transport.run()


def run_bot(
    name: str,
    path: Path,
    token: str,
    username: str = "",
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    trusted_user_id: int | None = None,
    on_trust: Callable[[int, str], None] | None = None,
    allowed_usernames: list[str] | None = None,
    trusted_users: dict[str, int] | None = None,
    trusted_user_ids: list[int] | None = None,
    transcriber: "Transcriber | None" = None,
    synthesizer: "Synthesizer | None" = None,
    team_name: str | None = None,
    active_persona: str | None = None,
    show_thinking: bool = False,
    group_chat_id: int | None = None,
    role: str | None = None,
    peer_bot_username: str = "",
    config_path: Path | None = None,
) -> None:
    effective_usernames = allowed_usernames or ([username] if username else [])
    if not effective_usernames:
        raise SystemExit(
            "No allowed username configured. Use --username or run 'configure --username'."
        )
    if session_id:
        save_session(
            name,
            session_id,
            config_path or DEFAULT_CONFIG,
            team_name=team_name,
            role=role,
        )
    bot = ProjectBot(
        name, path, token,
        allowed_usernames=effective_usernames,
        trusted_users=trusted_users,
        trusted_user_ids=trusted_user_ids or ([trusted_user_id] if trusted_user_id else []),
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        transcriber=transcriber,
        synthesizer=synthesizer,
        active_persona=active_persona,
        show_thinking=show_thinking,
        team_name=team_name,
        group_chat_id=group_chat_id,
        role=role,
        peer_bot_username=peer_bot_username,
        config_path=config_path,
    )
    bot.task_manager.backend.session_id = session_id or load_session(
        name,
        config_path or DEFAULT_CONFIG,
        team_name=team_name,
        role=role,
    )
    if model:
        bot.task_manager.backend.model = model
    if effort:
        bot.task_manager.backend.effort = effort
    bot.build()
    logger.info(
        "Bot '%s' started at %s (trusted_user_id=%s)", name, path, trusted_user_id
    )
    bot.run()


def run_bots(
    config: Config,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    config_path: Path | None = None,
    transcriber: "Transcriber | None" = None,
    synthesizer: "Synthesizer | None" = None,
) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        effective_usernames, effective_trusted_users = resolve_project_auth_scope(
            proj,
            config,
        )
        on_trust = None
        if config_path:
            _name = name
            _path = config_path
            on_trust = lambda uid, username: bind_project_trusted_user(_name, username, uid, _path)
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            on_trust=on_trust,
            allowed_usernames=effective_usernames,
            trusted_users=effective_trusted_users,
            transcriber=transcriber,
            synthesizer=synthesizer,
            active_persona=proj.active_persona,
            show_thinking=proj.show_thinking,
            config_path=config_path,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
