from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcriber import Synthesizer, Transcriber

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import (
    Config,
    DEFAULT_CONFIG,
    clear_session,
    load_sessions,
    patch_project,
    resolve_permissions,
    save_session,
    add_trusted_user_id,
    add_project_trusted_user_id,
)
from ._auth import AuthMixin
from .formatting import md_to_telegram, split_html, strip_html
from .claude_client import EFFORT_LEVELS, MODELS, PERMISSION_MODES, is_usage_cap_error
from .stream import AskQuestion, Question, StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType

logger = logging.getLogger(__name__)

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List all tasks"),
    ("model", "Set Claude model (haiku/sonnet/opus)"),
    ("effort", "Set thinking depth (low/medium/high/max)"),
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
        on_trust: Callable[[int], None] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        allowed_usernames: list[str] | None = None,
        trusted_user_ids: list[int] | None = None,
        transcriber: "Transcriber | None" = None,
        synthesizer: "Synthesizer | None" = None,
        active_persona: str | None = None,
        team_name: str | None = None,
        group_chat_id: int | None = None,
        role: str | None = None,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        if allowed_usernames is not None:
            self._allowed_usernames = allowed_usernames
        else:
            self._allowed_username = allowed_username
        if trusted_user_ids is not None:
            self._trusted_user_ids = trusted_user_ids
        else:
            self._trusted_user_id = trusted_user_id
        self._on_trust_fn = on_trust
        self._started_at = time.monotonic()
        self._app = None
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._stream_text: dict[int, str] = {}
        self._thinking_buf: dict[int, str] = {}   # task_id → accumulated thinking
        self._thinking_store: dict[int, str] = {}  # result_msg_id → thinking text
        self._init_auth()
        self._active_skill: str | None = None
        self._active_persona = active_persona
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._voice_tasks: set[int] = set()
        self.task_manager = TaskManager(
            project_path=self.path,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            on_waiting_input=self._on_waiting_input,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )
        self.team_name = team_name
        self.group_mode = team_name is not None
        # Team-mode fields — stored now, wired into behavior in later tasks (B3/B4).
        self.group_chat_id = group_chat_id
        self.role = role
        self.bot_username: str = ""  # populated in _post_init via get_me()
        from .group_state import GroupStateRegistry
        self._group_state = GroupStateRegistry(max_bot_rounds=20)
        self._probe_tasks: set[asyncio.Task] = set()

    def _on_trust(self, user_id: int) -> None:
        if self._on_trust_fn:
            self._on_trust_fn(user_id)
        else:
            add_trusted_user_id(user_id)

    async def _on_task_started(self, task: Task) -> None:
        # Only show typing indicator for Claude tasks, not /run commands
        if task.type == TaskType.COMMAND:
            return
        chat = await self._app.bot.get_chat(task.chat_id)
        self._typing_tasks[task.id] = asyncio.create_task(self._keep_typing(chat))

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, ThinkingDelta):
            self._thinking_buf.setdefault(task.id, "")
            if self._thinking_buf[task.id]:
                self._thinking_buf[task.id] += "\n\n"
            self._thinking_buf[task.id] += event.text
        elif isinstance(event, TextDelta):
            self._stream_text.setdefault(task.id, "")
            self._stream_text[task.id] += event.text
        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(
                    task.chat_id, event.path, reply_to=task.message_id
                )

    async def _send_html(self, chat_id: int, html: str, reply_to: int | None = None, reply_markup=None) -> int | None:
        """Send HTML message(s), attaching reply_markup to the last chunk. Returns last message ID."""
        chunks = split_html(html)
        last_id = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            km = reply_markup if is_last else None
            try:
                msg = await self._app.bot.send_message(
                    chat_id, chunk, parse_mode="HTML", reply_to_message_id=reply_to, reply_markup=km
                )
                last_id = msg.message_id
            except Exception:
                logger.warning("HTML send failed, falling back to plain", exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    msg = await self._app.bot.send_message(
                        chat_id,
                        plain[:4096] if len(plain) > 4096 else plain,
                        reply_to_message_id=reply_to,
                        reply_markup=km,
                    )
                    last_id = msg.message_id
        return last_id

    async def _send_to_chat(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        html = md_to_telegram(text or "[No output]").replace("\x00", "")
        await self._send_html(chat_id, html, reply_to)

    async def _send_raw(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        escaped = (text or "[No output]").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._send_html(chat_id, f"<pre>{escaped}</pre>", reply_to)

    async def _finalize_claude_task(self, task: Task) -> None:
        self._stream_text.pop(task.id, None)
        thinking = self._thinking_buf.pop(task.id, None)
        is_voice = task.id in self._voice_tasks
        self._voice_tasks.discard(task.id)

        if task._compact:
            text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
            await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            return

        if task.status == TaskStatus.DONE:
            if thinking:
                self._thinking_store[task.id] = thinking
            await self._send_to_chat(task.chat_id, task.result, reply_to=task.message_id)
            if is_voice and self._synthesizer and task.result:
                await self._send_voice_response(task.chat_id, task.result, reply_to=task.message_id)
        else:
            if is_usage_cap_error(task.error) and self.group_mode:
                self._group_state.halt(task.chat_id)
                await self._send_to_chat(
                    task.chat_id,
                    "Hit Max usage cap. Pausing until reset. Will retry every 30 min.",
                    reply_to=task.message_id,
                )
                self._schedule_cap_probe(task.chat_id)
                return
            await self._send_to_chat(task.chat_id, f"Error: {task.error}", reply_to=task.message_id)

    def _schedule_cap_probe(self, chat_id: int, interval_s: int = 1800) -> None:
        """Probe Claude every `interval_s` seconds; on success, resume the group."""
        async def _probe() -> None:
            from .claude_client import ClaudeClient
            while self._group_state.get(chat_id).halted:
                await asyncio.sleep(interval_s)
                if not self._group_state.get(chat_id).halted:
                    return  # user manually resumed
                try:
                    probe = ClaudeClient(project_path=self.path)
                    result = await probe.chat("ping")
                    if not result.startswith("Error:") and not is_usage_cap_error(result):
                        self._group_state.resume(chat_id)
                        await self._send_to_chat(chat_id, "Usage cap cleared. Resumed.")
                        return
                except Exception:
                    logger.warning("cap probe failed", exc_info=True)
        task = asyncio.create_task(_probe())
        self._probe_tasks.add(task)
        task.add_done_callback(self._probe_tasks.discard)

    async def _send_voice_response(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "tts"
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Strip markdown formatting for cleaner speech
        plain = strip_html(md_to_telegram(text))
        # OpenAI TTS has a 4096 char limit per request
        if len(plain) > 4096:
            plain = plain[:4093] + "..."
        out_path = voice_dir / f"tts_{uuid.uuid4().hex}.opus"
        try:
            await self._synthesizer.synthesize(plain, out_path)
            with out_path.open("rb") as f:
                await self._app.bot.send_voice(chat_id, f, reply_to_message_id=reply_to)
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
            await self._send_raw(task.chat_id, f"{output}\n[exit 0]")
        else:
            await self._send_raw(task.chat_id, f"[exit {task.exit_code}]\n\n{output}")

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.CLAUDE:
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            await self._finalize_claude_task(task)
        else:
            await self._finalize_command_task(task)

    def _question_markup(self, task_id: int, q_idx: int, question: Question) -> InlineKeyboardMarkup:
        buttons = []
        for opt_idx, opt in enumerate(question.options):
            label = opt.label[:64] or f"Option {opt_idx + 1}"
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"ask_{task_id}_{q_idx}_{opt_idx}")
            ])
        return InlineKeyboardMarkup(buttons)

    async def _on_waiting_input(self, task: Task) -> None:
        """Render pending questions as Telegram messages with option buttons."""
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        # Flush any accompanying text Claude produced alongside the question
        accompanying = task.result or self._stream_text.pop(task.id, "")
        if accompanying and accompanying.strip():
            await self._send_to_chat(task.chat_id, accompanying, reply_to=task.message_id)
        self._stream_text.pop(task.id, None)

        def _esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        for q_idx, question in enumerate(task.pending_questions):
            header = question.header.strip() if question.header else ""
            body = question.question.strip() or "(question)"
            lines = []
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
            await self._send_html(
                task.chat_id,
                "\n".join(lines),
                reply_to=task.message_id,
                reply_markup=self._question_markup(task.id, q_idx, question),
            )

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        await update.effective_message.reply_text(
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with Claude.\n{_CMD_HELP}"
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if self.group_mode:
            # Auto-capture: if chat_id not yet bound (sentinel 0 or None), and sender is the trusted user,
            # write this group's chat_id into the team config and update in-memory state.
            if self.group_chat_id in (0, None):
                if self._auth(update.effective_user) and self.team_name:
                    from .config import patch_team
                    new_chat_id = msg.chat_id
                    patch_team(self.team_name, {"group_chat_id": new_chat_id})
                    self.group_chat_id = new_chat_id
                    # Fall through so this same message still gets processed normally.
            elif msg.chat_id != self.group_chat_id:
                return  # wrong group — silent ignore
            from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot
            if is_from_self(msg, self.bot_username):
                return  # self-silence
            if not is_directed_at_me(msg, self.bot_username):
                return  # not addressed to this bot
            chat_id = msg.chat_id
            if is_from_other_bot(msg, self.bot_username):
                # Bot-to-bot message: check halt before acting.
                if self._group_state.get(chat_id).halted:
                    return
                self._group_state.note_bot_to_bot(chat_id)
                if self._group_state.get(chat_id).halted:
                    # Cap tripped by this very message.
                    await msg.reply_text(
                        f"Auto-paused after {self._group_state.max_bot_rounds} bot-to-bot rounds. "
                        "Send any message to resume."
                    )
                    return
            else:
                # Human (trusted user) message — reset the round counter and clear any halt.
                self._group_state.resume(chat_id)
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        pending_skill = ctx.user_data.pop("pending_skill_name", None) if ctx.user_data else None
        pending_scope = ctx.user_data.pop("pending_skill_scope", None) if ctx.user_data else None
        if pending_skill and pending_scope:
            from .skills import save_skill
            save_skill(pending_skill, msg.text, self.path, scope=pending_scope)
            icon = "🌐" if pending_scope == "global" else "📁"
            return await msg.reply_text(f"{icon} Skill '{pending_skill}' created ({pending_scope}). Use /use {pending_skill} to activate.")
        if pending_skill:
            ctx.user_data["pending_skill_name"] = pending_skill
            return

        pending_persona = ctx.user_data.pop("pending_persona_name", None) if ctx.user_data else None
        pending_persona_scope = ctx.user_data.pop("pending_persona_scope", None) if ctx.user_data else None
        if pending_persona and pending_persona_scope:
            from .skills import save_persona
            save_persona(pending_persona, msg.text, self.path, scope=pending_persona_scope)
            icon = "🌐" if pending_persona_scope == "global" else "📁"
            return await msg.reply_text(f"{icon} Persona '{pending_persona}' created ({pending_persona_scope}). Use /persona {pending_persona} to activate.")
        if pending_persona:
            ctx.user_data["pending_persona_name"] = pending_persona
            return

        # If Claude is currently waiting on a question in this chat, route
        # this message as the answer instead of starting a new turn.
        waiting = self.task_manager.waiting_input_task(update.effective_chat.id)
        if waiting:
            self.task_manager.submit_answer(waiting.id, msg.text)
            return

        for prev in self.task_manager.find_by_message(msg.message_id):
            self.task_manager.cancel(prev.id)
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        prompt = msg.text
        if msg.reply_to_message and msg.reply_to_message.text:
            prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"
        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)
        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )

    async def _on_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /run <command>")
        command = " ".join(ctx.args)
        self.task_manager.run_command(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
            command=command,
        )

    _TASK_ICONS = {
        TaskStatus.WAITING: "~",
        TaskStatus.RUNNING: ">",
        TaskStatus.DONE: "+",
        TaskStatus.FAILED: "!",
        TaskStatus.CANCELLED: "x",
    }

    def _tasks_markup(self, chat_id: int) -> InlineKeyboardMarkup | None:
        all_tasks = self.task_manager.list_tasks(chat_id=chat_id, limit=100)
        active = [t for t in all_tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
        finished = [t for t in all_tasks if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)][:5]
        tasks = active + finished
        if not tasks:
            return None
        buttons = []
        for t in tasks:
            icon = self._TASK_ICONS.get(t.status, "?")
            elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:40]
            buttons.append([InlineKeyboardButton(f"{icon} #{t.id}{elapsed} {label}", callback_data=f"task_info_{t.id}")])
        return InlineKeyboardMarkup(buttons)

    async def _render_tasks(self, chat_id: int, edit_query) -> None:
        markup = self._tasks_markup(chat_id)
        await edit_query.edit_message_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    async def _on_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        markup = self._tasks_markup(update.effective_chat.id)
        await update.effective_message.reply_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    MODEL_OPTIONS = [
        ("opus[1m]", "Opus 4.7 1M", "Most capable, 1M context"),
        ("opus", "Opus 4.7", "Most capable"),
        ("sonnet[1m]", "Sonnet 4.6 1M", "Everyday tasks, 1M context"),
        ("sonnet", "Sonnet 4.6", "Best for everyday tasks"),
        ("haiku", "Haiku 4.5", "Fastest for quick answers"),
    ]

    def _model_markup(self) -> InlineKeyboardMarkup:
        current = self.task_manager.claude.model
        rows = []
        for model_id, label, _ in self.MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"model_set_{model_id}")])
        return InlineKeyboardMarkup(rows)

    def _current_model(self) -> str:
        raw = self.task_manager.claude.model
        for model_id, label, desc in self.MODEL_OPTIONS:
            if model_id == raw:
                return f"{label} — {desc}"
        return self.task_manager.claude.model_display or raw

    async def _on_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        await update.effective_message.reply_text(
            f"Select model\nCurrent: {self._current_model()}",
            reply_markup=self._model_markup(),
        )

    def _effort_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(e, callback_data=f"effort_set_{e}")]
            for e in EFFORT_LEVELS
        ])

    def _current_effort(self) -> str:
        return self.task_manager.claude.effort

    async def _on_effort(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        await update.effective_message.reply_text(
            f"Current: {self._current_effort()}",
            reply_markup=self._effort_markup(),
        )

    _PERMISSION_OPTIONS = (
        *PERMISSION_MODES,
        "dangerously-skip-permissions",
    )

    def _permissions_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(o, callback_data=f"permissions_set_{o}")]
            for o in self._PERMISSION_OPTIONS
        ])

    def _current_permission(self) -> str:
        claude = self.task_manager.claude
        if claude.skip_permissions:
            return "dangerously-skip-permissions"
        return claude.permission_mode or "default"

    async def _on_permissions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        await update.effective_message.reply_text(
            f"Current: {self._current_permission()}",
            reply_markup=self._permissions_markup(),
        )

    async def _on_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not self.task_manager.claude.session_id:
            return await update.effective_message.reply_text("No active session.")
        self.task_manager.submit_compact(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
        )

    async def _on_version(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        from . import __version__
        await update.effective_message.reply_text(f"link-project-to-chat v{__version__}")

    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        await update.effective_message.reply_text(_CMD_HELP)

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, reset", callback_data="reset_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="reset_cancel"),
                ]
            ]
        )
        await update.effective_message.reply_text(
            "Are you sure? This will clear the Claude session.",
            reply_markup=keyboard,
        )

    def _picker_markup(self, items: dict, prefix: str) -> InlineKeyboardMarkup | None:
        if not items:
            return None
        buttons = [
            InlineKeyboardButton(name, callback_data=f"{prefix}_{name}")
            for name in sorted(items)
        ]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        return InlineKeyboardMarkup(rows)

    async def _on_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if ctx.args:
            name = ctx.args[0].lower()
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                return await update.effective_message.reply_text(f"Skill '{name}' not found.")
            self.task_manager.claude.append_system_prompt = skill.content
            self._active_skill = name
            return await update.effective_message.reply_text(f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate.")
        from .skills import load_skills
        skills = load_skills(self.path)
        if not skills:
            return await update.effective_message.reply_text("No skills available.\nCreate one with /create_skill <name>")
        lines = ["Available skills:"]
        for name, skill in sorted(skills.items()):
            icon = "\U0001f4c1" if skill.source == "project" else "\U0001f310" if skill.source == "global" else "\U0001f916"
            lines.append(f"  {icon} {name} ({skill.source})")
        if self._active_skill:
            lines.append(f"\nActive: {self._active_skill}")
        markup = self._picker_markup(skills, "pick_skill")
        await update.effective_message.reply_text("\n".join(lines), reply_markup=markup)

    async def _on_stop_skill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not self._active_skill:
            return await update.effective_message.reply_text("No active skill.")
        old = self._active_skill
        self._active_skill = None
        self.task_manager.claude.append_system_prompt = None
        await update.effective_message.reply_text(f"Skill '{old}' deactivated.")

    async def _on_create_skill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /create_skill <name>")
        name = ctx.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "skill")
        except ValueError as e:
            return await update.effective_message.reply_text(str(e))
        ctx.user_data["pending_skill_name"] = name
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 Project", callback_data=f"skill_scope_project_{name}"),
                InlineKeyboardButton("🌐 Global", callback_data=f"skill_scope_global_{name}"),
            ]
        ])
        await update.effective_message.reply_text(
            f"Where should skill '{name}' be saved?", reply_markup=keyboard,
        )

    async def _on_delete_skill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /delete_skill <name>")
        name = ctx.args[0].lower()
        from .skills import load_skill
        skill = load_skill(name, self.path)
        if not skill:
            return await update.effective_message.reply_text(f"Skill '{name}' not found.")
        icon = "🌐" if skill.source == "global" else "📁"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Delete", callback_data=f"skill_delete_confirm_{skill.source}_{name}"),
                InlineKeyboardButton("Cancel", callback_data="skill_delete_cancel"),
            ]
        ])
        await update.effective_message.reply_text(f"Delete {icon} {skill.source} skill '{name}'?", reply_markup=keyboard)

    # --- Persona handlers ---

    async def _on_persona(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            if self._active_persona:
                return await update.effective_message.reply_text(f"Active persona: {self._active_persona}\nUse /stop_persona to deactivate.")
            from .skills import load_personas
            markup = self._picker_markup(load_personas(self.path), "pick_persona")
            if not markup:
                return await update.effective_message.reply_text("No personas available.\nCreate one with /create_persona <name>")
            return await update.effective_message.reply_text("Pick a persona to activate:", reply_markup=markup)
        name = ctx.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            return await update.effective_message.reply_text(f"Persona '{name}' not found.")
        self._active_persona = name
        from .config import patch_project
        patch_project(self.name, {"active_persona": name})
        await update.effective_message.reply_text(f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate.")

    async def _on_stop_persona(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not self._active_persona:
            return await update.effective_message.reply_text("No active persona.")
        old = self._active_persona
        self._active_persona = None
        from .config import patch_project
        patch_project(self.name, {"active_persona": None})
        await update.effective_message.reply_text(f"Persona '{old}' deactivated.")

    async def _on_halt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.group_mode:
            return await update.effective_message.reply_text("/halt is only available in group mode.")
        if self.group_chat_id is not None and update.effective_chat.id != self.group_chat_id:
            return  # silently ignore — wrong group
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        self._group_state.halt(update.effective_chat.id)
        await update.effective_message.reply_text("Halted. Use /resume to continue.")

    async def _on_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.group_mode:
            return await update.effective_message.reply_text("/resume is only available in group mode.")
        if self.group_chat_id is not None and update.effective_chat.id != self.group_chat_id:
            return  # silently ignore — wrong group
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        self._group_state.resume(update.effective_chat.id)
        await update.effective_message.reply_text("Resumed.")

    async def _on_create_persona(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /create_persona <name>")
        name = ctx.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "persona")
        except ValueError as e:
            return await update.effective_message.reply_text(str(e))
        ctx.user_data["pending_persona_name"] = name
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 Project", callback_data=f"persona_scope_project_{name}"),
                InlineKeyboardButton("🌐 Global", callback_data=f"persona_scope_global_{name}"),
            ]
        ])
        await update.effective_message.reply_text(
            f"Where should persona '{name}' be saved?", reply_markup=keyboard,
        )

    async def _on_delete_persona(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /delete_persona <name>")
        name = ctx.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            return await update.effective_message.reply_text(f"Persona '{name}' not found.")
        icon = "🌐" if persona.source == "global" else "📁"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Delete", callback_data=f"persona_delete_confirm_{persona.source}_{name}"),
                InlineKeyboardButton("Cancel", callback_data="persona_delete_cancel"),
            ]
        ])
        await update.effective_message.reply_text(f"Delete {icon} {persona.source} persona '{name}'?", reply_markup=keyboard)

    async def _on_voice_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if self._transcriber:
            backend = type(self._transcriber).__name__
            await update.effective_message.reply_text(f"Voice: enabled ({backend})")
        else:
            await update.effective_message.reply_text(
                "Voice: disabled\n"
                "Configure with: link-project-to-chat setup"
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

    async def _on_lang(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if not self._transcriber:
            return await update.effective_message.reply_text("Voice is not configured.")
        if ctx.args:
            code = ctx.args[0].lower()
            if code == "auto":
                code = ""
            self._transcriber._language = code
            label = code or "auto-detect"
            return await update.effective_message.reply_text(f"Voice language set to: {label}")
        current = getattr(self._transcriber, "_language", "") or "auto-detect"
        buttons = [
            InlineKeyboardButton(
                f"{'● ' if (code or '') == getattr(self._transcriber, '_language', '') else ''}{label}",
                callback_data=f"lang_set_{code}",
            )
            for code, label in self.LANGUAGES
        ]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        await update.effective_message.reply_text(
            f"Current language: {current}\nSelect voice language:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _on_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if query.message and query.message.chat.type != "private":
            await query.answer("Only available in private chats.")
            return
        if not self._auth(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()

        if query.data.startswith("model_set_"):
            name = query.data[len("model_set_"):]
            valid = {m[0] for m in self.MODEL_OPTIONS}
            if name in valid:
                self.task_manager.claude.model = name
                self.task_manager.claude.model_display = None
                patch_project(self.name, {"model": name})
            await query.edit_message_text(
                f"Select model\nCurrent: {self._current_model()}",
                reply_markup=self._model_markup(),
            )
        elif query.data.startswith("effort_set_"):
            level = query.data[len("effort_set_"):]
            if level in EFFORT_LEVELS:
                self.task_manager.claude.effort = level
                patch_project(self.name, {"effort": level})
            await query.edit_message_text(
                f"Effort: {self._current_effort()}",
                reply_markup=self._effort_markup(),
            )
        elif query.data.startswith("permissions_set_"):
            mode = query.data[len("permissions_set_"):]
            if mode == "dangerously-skip-permissions" or mode in PERMISSION_MODES:
                skip, pm = resolve_permissions(mode)
                self.task_manager.claude.skip_permissions = skip
                self.task_manager.claude.permission_mode = pm
                patch_project(self.name, {"permissions": mode if mode != "default" else None})
            await query.edit_message_text(
                f"Permissions: {self._current_permission()}",
                reply_markup=self._permissions_markup(),
            )
        elif query.data.startswith("lang_set_"):
            code = query.data[len("lang_set_"):]
            if code == "auto":
                code = ""
            if self._transcriber:
                self._transcriber._language = code
            label = code or "auto-detect"
            buttons = [
                InlineKeyboardButton(
                    f"{'● ' if (c or '') == code else ''}{l}",
                    callback_data=f"lang_set_{c}",
                )
                for c, l in self.LANGUAGES
            ]
            rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
            await query.edit_message_text(
                f"Voice language set to: {label}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif query.data == "reset_confirm":
            self.task_manager.cancel_all()
            self.task_manager.claude.session_id = None
            self._active_skill = None
            self._active_persona = None
            self.task_manager.claude.append_system_prompt = None
            clear_session(self.name)
            await query.edit_message_text("Session reset.")
        elif query.data == "reset_cancel":
            await query.edit_message_text("Reset cancelled.")
        # Skill callbacks
        elif query.data.startswith("skill_delete_confirm_"):
            rest = query.data[len("skill_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_skill as _delete_skill
            _delete_skill(name, self.path, scope=scope)
            if self._active_skill == name:
                self._active_skill = None
                self.task_manager.claude.append_system_prompt = None
            await query.edit_message_text(f"Skill '{name}' deleted.")
        elif query.data == "skill_delete_cancel":
            await query.edit_message_text("Cancelled.")
        elif query.data.startswith("skill_scope_"):
            rest = query.data[len("skill_scope_"):]
            scope, _, name = rest.partition("_")
            ctx.user_data["pending_skill_name"] = name
            ctx.user_data["pending_skill_scope"] = scope
            await query.edit_message_text(f"Send the content for skill '{name}':")
        elif query.data.startswith("pick_skill_"):
            name = query.data[len("pick_skill_"):]
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                return await query.edit_message_text(f"Skill '{name}' not found.")
            self.task_manager.claude.append_system_prompt = skill.content
            self._active_skill = name
            await query.edit_message_text(f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate.")
        # Persona callbacks
        elif query.data.startswith("persona_delete_confirm_"):
            rest = query.data[len("persona_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_persona as _delete_persona
            _delete_persona(name, self.path, scope=scope)
            if self._active_persona == name:
                self._active_persona = None
            await query.edit_message_text(f"Persona '{name}' deleted.")
        elif query.data == "persona_delete_cancel":
            await query.edit_message_text("Cancelled.")
        elif query.data.startswith("persona_scope_"):
            rest = query.data[len("persona_scope_"):]
            scope, _, name = rest.partition("_")
            ctx.user_data["pending_persona_name"] = name
            ctx.user_data["pending_persona_scope"] = scope
            await query.edit_message_text(f"Send the content for persona '{name}':")
        elif query.data.startswith("pick_persona_"):
            name = query.data[len("pick_persona_"):]
            from .skills import load_persona
            persona = load_persona(name, self.path)
            if not persona:
                return await query.edit_message_text(f"Persona '{name}' not found.")
            self._active_persona = name
            await query.edit_message_text(f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate.")
        elif query.data.startswith("ask_"):
            # Format: ask_{task_id}_{q_idx}_{opt_idx}
            parts = query.data.split("_")
            if len(parts) != 4:
                await query.answer("Invalid option.")
                return
            try:
                task_id = int(parts[1])
                q_idx = int(parts[2])
                opt_idx = int(parts[3])
            except ValueError:
                await query.answer("Invalid option.")
                return
            task = self.task_manager.get(task_id)
            if not task or task.status != TaskStatus.WAITING_INPUT:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.answer("No longer waiting on input.")
                return
            if q_idx >= len(task.pending_questions):
                await query.answer("Question no longer active.")
                return
            question = task.pending_questions[q_idx]
            if opt_idx >= len(question.options):
                await query.answer("Option no longer available.")
                return
            label = question.options[opt_idx].label
            if self.task_manager.submit_answer(task_id, label):
                await query.edit_message_reply_markup(reply_markup=None)
                # Append selection to the message so the user sees what they chose
                try:
                    original = query.message.text_html or query.message.text or ""
                    escaped = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    await query.edit_message_text(
                        f"{original}\n\n<i>Selected:</i> {escaped}",
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.debug("could not annotate selected option", exc_info=True)
            else:
                await query.answer("Could not send answer.")
        elif query.data.startswith("task_info_"):
            task_id = _parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
            text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:200]}"
            rows = []
            if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                rows.append([InlineKeyboardButton("Cancel", callback_data=f"task_cancel_{task_id}")])
            if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                rows.append([InlineKeyboardButton("Log", callback_data=f"task_log_{task_id}")])
            if task_id in self._thinking_store:
                rows.append([InlineKeyboardButton("Thinking", callback_data=f"show_thinking_{task_id}")])
            rows.append([InlineKeyboardButton("« Back", callback_data="tasks_back")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        elif query.data == "tasks_back":
            await self._render_tasks(query.message.chat_id, edit_query=query)
        elif query.data.startswith("task_cancel_"):
            task_id = _parse_task_id(query.data)
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await query.edit_message_text(f"#{task_id} cancelled.")
            else:
                await query.edit_message_text(f"#{task_id} not found or already finished.")
        elif query.data.startswith("task_log_"):
            task_id = _parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            # For running tasks, read from live log buffer; for finished tasks, use result
            if task._log:
                output = "\n".join(task._log)
            else:
                output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            rows = [[InlineKeyboardButton("« Back", callback_data=f"task_info_{task_id}")]]
            await query.edit_message_text(f"#{task_id} log:\n{output}", reply_markup=InlineKeyboardMarkup(rows))
        elif query.data.startswith("show_thinking_"):
            try:
                task_id = int(query.data[len("show_thinking_"):])
            except ValueError:
                await query.answer("Invalid thinking reference.")
                return
            thinking = self._thinking_store.get(task_id)
            if not thinking:
                await query.answer("Thinking not available.")
                return
            await self._send_to_chat(query.message.chat_id, f"💭 {thinking}")

    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")

        uptime = time.monotonic() - self._started_at
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)

        st = self.task_manager.claude.status
        lines = [
            f"Project: {self.name}",
            f"Path: {self.path}",
            f"Model: {self.task_manager.claude.model_display or self.task_manager.claude.model}",
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st['session_id'] or 'none'}",
            f"Claude: {'RUNNING' if st['running'] else 'idle'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
            f"Skill: {self._active_skill or 'none'}",
            f"Persona: {self._active_persona or 'none'}",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        uploads_dir = Path("/tmp/link-project-to-chat") / self.name / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        if msg.photo:
            photo = msg.photo[-1]
            file = await photo.get_file()
            filename = f"photo_{int(time.monotonic() * 1000)}.jpg"
        elif msg.document:
            file = await msg.document.get_file()
            raw_name = msg.document.file_name or f"file_{int(time.monotonic() * 1000)}"
            filename = "".join(
                c
                for c in raw_name.replace("/", "_").replace("\\", "_")
                if c.isalnum() or c in "._- "
            )[:200]
        else:
            return await msg.reply_text("Unsupported file type.")

        dest = uploads_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 2
            while dest.exists():
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        await file.download_to_drive(str(dest))

        caption = msg.caption or ""
        prompt = f"[User uploaded {dest}]"
        if caption:
            prompt += f"\n\n{caption}"

        waiting = self.task_manager.waiting_input_task(update.effective_chat.id)
        if waiting:
            self.task_manager.submit_answer(waiting.id, prompt)
            return

        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages and audio files."""
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        if not self._transcriber:
            return await msg.reply_text(
                "Voice messages aren't configured. "
                "Set up STT with: link-project-to-chat setup --stt-backend whisper-api"
            )

        voice = msg.voice or msg.audio
        if not voice:
            return await msg.reply_text("Could not read voice message.")

        # Telegram Bot API caps file downloads at 20 MB.
        MAX_VOICE_BYTES = 20 * 1024 * 1024
        if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
            size_mb = voice.file_size // (1024 * 1024)
            return await msg.reply_text(
                f"Audio too large ({size_mb} MB). Telegram Bot API limit is 20 MB."
            )

        status_msg = await msg.reply_text("🎤 Transcribing...")

        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        ogg_path = voice_dir / f"voice_{uuid.uuid4().hex}.ogg"

        try:
            file = await voice.get_file()
            await file.download_to_drive(str(ogg_path))

            text = await self._transcriber.transcribe(ogg_path)

            if not text or not text.strip():
                await status_msg.edit_text("Could not transcribe the voice message (empty result).")
                return

            # Show the transcript (truncated for status display)
            display = text if len(text) <= 200 else text[:200] + "..."
            await status_msg.edit_text(f'🎤 "{display}"')

            # If Claude is waiting on a question, route transcript as the answer.
            waiting = self.task_manager.waiting_input_task(update.effective_chat.id)
            if waiting:
                self.task_manager.submit_answer(waiting.id, text)
                return

            # Build prompt with optional reply context
            prompt = text
            if msg.reply_to_message and msg.reply_to_message.text:
                prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"

            # Apply active persona
            if self._active_persona:
                from .skills import load_persona, format_persona_prompt
                persona = load_persona(self._active_persona, self.path)
                if persona:
                    prompt = format_persona_prompt(persona, prompt)

            task = self.task_manager.submit_claude(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                prompt=prompt,
            )
            if self._synthesizer:
                self._voice_tasks.add(task.id)

        except Exception as e:
            logger.exception("Voice transcription failed")
            # Sanitize: only the first line, hard-truncated, to avoid leaking
            # API keys or full request payloads in SDK error messages.
            error_summary = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            await status_msg.edit_text(f"Transcription failed: {error_summary}")
        finally:
            if ogg_path.exists():
                try:
                    ogg_path.unlink()
                except OSError:
                    pass

    async def _on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        if msg.video_note:
            text = "Video notes aren't supported yet. Please type your message or send a voice message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        await msg.reply_text(text)

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath

        return PurePosixPath(path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _send_image(
        self, chat_id: int, file_path: str, reply_to: int | None = None
    ) -> None:
        path = (
            self.path / file_path if not file_path.startswith("/") else Path(file_path)
        )
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            logger.warning("Invalid image path: %s", file_path)
            return
        if not str(resolved).startswith(str(self.path.resolve())):
            logger.warning("Image path traversal blocked: %s", file_path)
            return
        if not path.exists():
            logger.warning("Image file not found: %s", path)
            return
        try:
            size = path.stat().st_size
            suffix = path.suffix.lower()
            with path.open("rb") as f:
                data = f.read()
            if suffix == ".svg" or size > 10 * 1024 * 1024:
                await self._app.bot.send_document(
                    chat_id,
                    data,
                    filename=path.name,
                    reply_to_message_id=reply_to,
                )
            else:
                await self._app.bot.send_photo(
                    chat_id,
                    data,
                    caption=path.name,
                    reply_to_message_id=reply_to,
                )
        except Exception:
            logger.warning("Failed to send image %s", path, exc_info=True)

    @staticmethod
    async def _keep_typing(chat) -> None:
        try:
            while True:
                try:
                    await chat.send_action(ChatAction.TYPING)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = str(ctx.error)
        if "Conflict" in err:
            logger.warning(
                "Conflict error (another instance?): %s | update=%s", err, update
            )
        else:
            logger.error("Update error: %s | update=%s", ctx.error, update)

    async def _post_init(self, app) -> None:
        result = await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("delete_webhook result=%s (drop_pending_updates=True)", result)
        try:
            me = await app.bot.get_me()
            self.bot_username = (me.username or "").lower()
        except Exception:
            logger.error("get_me() failed at startup", exc_info=True)
            if self.group_mode:
                raise RuntimeError(
                    "team mode requires a reachable Telegram API at startup "
                    "to fetch bot username; aborting."
                )
        await app.bot.set_my_commands(COMMANDS)
        for uid in self._get_trusted_user_ids():
            try:
                await app.bot.send_message(
                    uid,
                    f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                )
            except Exception:
                logger.error("Failed to send startup message to %d", uid, exc_info=True)

    def build(self):
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
        self._app = app
        handlers = {
            "start": self._on_start,
            "run": self._on_run,
            "tasks": self._on_tasks,
            "model": self._on_model,
            "effort": self._on_effort,
            "permissions": self._on_permissions,
            "compact": self._on_compact,
            "reset": self._on_reset,
            "status": self._on_status,
            "version": self._on_version,
            "help": self._on_help,
            "skills": self._on_skills,
            "stop_skill": self._on_stop_skill,
            "create_skill": self._on_create_skill,
            "delete_skill": self._on_delete_skill,
            "persona": self._on_persona,
            "stop_persona": self._on_stop_persona,
            "create_persona": self._on_create_persona,
            "delete_persona": self._on_delete_persona,
            "voice": self._on_voice_status,
            "lang": self._on_lang,
            "halt": self._on_halt,
            "resume": self._on_resume,
        }
        if self.group_mode:
            # Group mode: accept commands and text from groups/supergroups only.
            chat_filter = filters.ChatType.GROUPS
            for name, handler in handlers.items():
                app.add_handler(CommandHandler(name, handler, filters=chat_filter))
            text_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
            app.add_handler(MessageHandler(text_filter, self._on_text))
            # Voice, files, and other media are disabled in group mode for v1.
        else:
            private = filters.ChatType.PRIVATE
            for name, handler in handlers.items():
                app.add_handler(CommandHandler(name, handler, filters=private))
            text_filter = (
                private
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
            app.add_handler(MessageHandler(text_filter, self._on_text))
            file_filter = private & (filters.Document.ALL | filters.PHOTO)
            app.add_handler(MessageHandler(file_filter, self._on_file))
            voice_filter = private & (filters.VOICE | filters.AUDIO)
            app.add_handler(MessageHandler(voice_filter, self._on_voice))
            unsupported_filter = private & (
                filters.VIDEO_NOTE
                | filters.Sticker.ALL
                | filters.VIDEO
                | filters.LOCATION
                | filters.CONTACT
            )
            app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))

        app.add_error_handler(self._on_error)
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app


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
    on_trust: Callable[[int], None] | None = None,
    allowed_usernames: list[str] | None = None,
    trusted_user_ids: list[int] | None = None,
    transcriber: "Transcriber | None" = None,
    synthesizer: "Synthesizer | None" = None,
    team_name: str | None = None,
    active_persona: str | None = None,
    group_chat_id: int | None = None,
    role: str | None = None,
) -> None:
    effective_usernames = allowed_usernames or ([username] if username else [])
    if not effective_usernames:
        raise SystemExit(
            "No allowed username configured. Use --username or run 'configure --username'."
        )
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(
        name, path, token,
        allowed_usernames=effective_usernames,
        trusted_user_ids=trusted_user_ids or ([trusted_user_id] if trusted_user_id else []),
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        transcriber=transcriber,
        synthesizer=synthesizer,
        active_persona=active_persona,
        team_name=team_name,
        group_chat_id=group_chat_id,
        role=role,
    )
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    if model:
        bot.task_manager.claude.model = model
    if effort:
        bot.task_manager.claude.effort = effort
    app = bot.build()
    logger.info(
        "Bot '%s' started at %s (trusted_user_id=%s)", name, path, trusted_user_id
    )
    app.run_polling()


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
        effective_usernames = proj.allowed_usernames or config.allowed_usernames
        effective_trusted_ids = proj.trusted_user_ids or config.trusted_user_ids
        on_trust = None
        if config_path:
            _name = name
            _path = config_path
            on_trust = lambda uid: add_project_trusted_user_id(_name, uid, _path)
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
            trusted_user_ids=effective_trusted_ids,
            transcriber=transcriber,
            synthesizer=synthesizer,
            active_persona=proj.active_persona,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
