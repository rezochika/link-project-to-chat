from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram.ext import Application

import telegram.error
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .auth import Authenticator
from .claude_client import EFFORT_LEVELS, MODELS, PERMISSION_MODES
from .config import (
    Config,
    clear_session,
    load_sessions,
    save_project_trusted_user_id,
    save_session,
    save_trusted_user_id,
)
from .constants import (
    COMMAND_OUTPUT_LIMIT,
    FILE_SIZE_LIMIT,
    IMAGE_EXTENSIONS,
    TELEGRAM_MESSAGE_LIMIT,
    TYPING_INDICATOR_INTERVAL,
)
from .formatting import md_to_telegram, split_html, strip_html
from .health import HealthServer
from .history import History
from .rate_limiter import RateLimiter
from .stream import StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType
from .ui import (
    CMD_HELP,
    COMMANDS,
    effort_markup,
    format_status,
    model_markup,
    parse_task_id,
    permissions_markup,
    reset_markup,
    sanitize_error,
    sanitize_filename,
    task_info_markup,
    task_log_text,
    tasks_markup,
)

logger = logging.getLogger(__name__)


class ProjectBot:
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_username: str,
        trusted_user_id: int | None = None,
        on_trust: Callable[[int], None] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        task_manager: TaskManager | None = None,
        authenticator: Authenticator | None = None,
        rate_limiter: RateLimiter | None = None,
        health_port: int | None = None,
        webhook_url: str | None = None,
        webhook_port: int = 8443,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self.webhook_url = webhook_url
        self.webhook_port = webhook_port
        self._started_at = time.monotonic()
        self._app: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._typing_tasks: dict[int, asyncio.Task[None]] = {}
        self._stream_text: dict[int, str] = {}
        self._health_server: HealthServer | None = (
            HealthServer(health_port, self._health_status) if health_port is not None else None
        )
        self._authenticator = authenticator or Authenticator(
            allowed_username=allowed_username,
            trusted_user_id=trusted_user_id,
            on_trust=on_trust or self._default_on_trust,
        )
        self._rate_limiter = rate_limiter or RateLimiter()
        self._history = History()
        self.task_manager = task_manager or TaskManager(
            project_path=self.path,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

    def _health_status(self) -> dict[str, Any]:
        st = self.task_manager.claude.status
        return {
            "uptime": time.monotonic() - self._started_at,
            "tasks_running": self.task_manager.running_count,
            "session_id": st.get("session_id"),
        }

    @property
    def _allowed_username(self) -> str:
        return self._authenticator.allowed_username

    @property
    def _trusted_user_id(self) -> int | None:
        return self._authenticator.trusted_user_id

    def _default_on_trust(self, user_id: int) -> None:
        save_trusted_user_id(user_id)

    async def _on_task_started(self, task: Task) -> None:
        assert self._app is not None
        chat = await self._app.bot.get_chat(task.chat_id)
        self._typing_tasks[task.id] = asyncio.create_task(self._keep_typing(chat))

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, ThinkingDelta):
            await self._send_to_chat(task.chat_id, f"🔸 {event.text}", reply_to=task.message_id)
        elif isinstance(event, TextDelta):
            self._stream_text.setdefault(task.id, "")
            self._stream_text[task.id] += event.text
        elif isinstance(event, ToolUse) and event.path and self._is_image(event.path):
            await self._send_image(
                task.chat_id, event.path, reply_to=task.message_id
            )

    async def _send_with_retry(
        self,
        send_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> Any:
        for attempt in range(max_retries):
            try:
                return await send_fn(*args, **kwargs)
            except telegram.error.RetryAfter as e:
                delay = e.retry_after
                await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay))
            except telegram.error.TimedOut:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1)
        return None

    async def _send_html(self, chat_id: int, html: str, reply_to: int | None = None) -> None:
        assert self._app is not None
        for chunk in split_html(html):
            try:
                await self._send_with_retry(
                    self._app.bot.send_message,
                    chat_id, chunk, parse_mode="HTML", reply_to_message_id=reply_to
                )
            except Exception:
                logger.warning("HTML send failed, falling back to plain", exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    await self._app.bot.send_message(
                        chat_id,
                        plain[:TELEGRAM_MESSAGE_LIMIT] if len(plain) > TELEGRAM_MESSAGE_LIMIT else plain,
                        reply_to_message_id=reply_to,
                    )

    async def _send_to_chat(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        html = md_to_telegram(text or "[No output]").replace("\x00", "")
        await self._send_html(chat_id, html, reply_to)

    async def _send_raw(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        escaped = (text or "[No output]").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._send_html(chat_id, f"<pre>{escaped}</pre>", reply_to)

    async def _finalize_claude_task(self, task: Task) -> None:
        self._stream_text.pop(task.id, None)

        if task._compact:
            if task.status == TaskStatus.DONE:
                text = "Session compacted."
            else:
                text = f"Compact failed: {sanitize_error(task.error)}"
            await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            return

        if task.status == TaskStatus.DONE:
            self._history.add("assistant", task.result or "", task_id=task.id)
            await self._send_to_chat(task.chat_id, f"🔹 {task.result}", reply_to=task.message_id)
        else:
            await self._send_to_chat(task.chat_id, f"Error: {sanitize_error(task.error)}", reply_to=task.message_id)

    async def _finalize_command_task(self, task: Task) -> None:
        output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
        if len(output) > COMMAND_OUTPUT_LIMIT:
            output = output[:COMMAND_OUTPUT_LIMIT] + "\n... (truncated, use /log)"
        if task.status == TaskStatus.DONE:
            await self._send_raw(task.chat_id, f"{output}\n[exit 0]")
        else:
            await self._send_raw(task.chat_id, f"[exit {task.exit_code}]\n\n{output}")

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.status == TaskStatus.CANCELLED:
            self._stream_text.pop(task.id, None)
            return

        if task.type == TaskType.CLAUDE:
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            await self._finalize_claude_task(task)
        else:
            await self._finalize_command_task(task)

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        await msg.reply_text(
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with Claude.\n{CMD_HELP}"
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        user = update.effective_user
        if not self._authenticator.authenticate(user):
            await msg.reply_text("Unauthorized.")
            return
        assert user is not None
        if self._rate_limiter.is_limited(user.id):
            await msg.reply_text("Rate limited. Try again shortly.")
            return

        for prev in self.task_manager.find_by_message(msg.message_id):
            self.task_manager.cancel(prev.id)
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        prompt = msg.text or ""
        if msg.reply_to_message and msg.reply_to_message.text:
            prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"
        task = self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )
        self._history.add("user", prompt, task_id=task.id)

    async def _on_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        if not ctx.args:
            await msg.reply_text("Usage: /run <command>")
            return
        command = " ".join(ctx.args)
        self.task_manager.run_command(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            command=command,
        )

    def _tasks_markup(self, chat_id: int) -> InlineKeyboardMarkup | None:
        all_tasks = self.task_manager.list_tasks(chat_id=chat_id, limit=100)
        return tasks_markup(all_tasks)

    async def _render_tasks(self, chat_id: int, edit_query) -> None:  # type: ignore[no-untyped-def]
        markup = self._tasks_markup(chat_id)
        await edit_query.edit_message_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    async def _on_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await update.effective_message.reply_text("Unauthorized.")
            return
        markup = self._tasks_markup(update.effective_chat.id)
        await update.effective_message.reply_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    def _model_markup(self) -> InlineKeyboardMarkup:
        return model_markup()

    def _current_model(self) -> str:
        return self.task_manager.claude.model_display or self.task_manager.claude.model

    async def _on_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        await msg.reply_text(
            f"Current: {self._current_model()}",
            reply_markup=self._model_markup(),
        )

    def _effort_markup(self) -> InlineKeyboardMarkup:
        return effort_markup()

    def _current_effort(self) -> str:
        return self.task_manager.claude.effort

    async def _on_effort(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        await msg.reply_text(
            f"Current: {self._current_effort()}",
            reply_markup=self._effort_markup(),
        )

    def _permissions_markup(self) -> InlineKeyboardMarkup:
        return permissions_markup()

    def _current_permission(self) -> str:
        claude = self.task_manager.claude
        if claude.skip_permissions:
            return "dangerously-skip-permissions"
        return claude.permission_mode or "default"

    async def _on_permissions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        await msg.reply_text(
            f"Current: {self._current_permission()}",
            reply_markup=self._permissions_markup(),
        )

    async def _on_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        if not self.task_manager.claude.session_id:
            await msg.reply_text("No active session.")
            return
        self.task_manager.submit_compact(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
        )

    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await update.effective_message.reply_text("Unauthorized.")
            return
        await update.effective_message.reply_text(CMD_HELP)

    async def _on_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return
        entries = self._history.recent(10)
        if not entries:
            await msg.reply_text("No conversation history yet.")
            return
        lines = []
        for entry in entries:
            dt = datetime.datetime.fromtimestamp(
                entry.timestamp - time.monotonic() + time.time()
            )
            hhmm = dt.strftime("%H:%M")
            icon = "👤 User" if entry.role == "user" else "🤖 Assistant"
            lines.append(f"🕐 {hhmm}  {icon}: {entry.text}")
        await msg.reply_text("\n".join(lines))

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await update.effective_message.reply_text("Unauthorized.")
            return
        await update.effective_message.reply_text(
            "Are you sure? This will clear the Claude session.",
            reply_markup=reset_markup(),
        )

    async def _on_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if query.message and query.message.chat and query.message.chat.type != "private":
            await query.answer("Only available in private chats.")
            return
        if not self._authenticator.authenticate(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()

        if query.data.startswith("model_set_"):
            name = query.data[len("model_set_"):]
            if name in MODELS:
                self.task_manager.claude.model = name
                self.task_manager.claude.model_display = None
            await query.edit_message_text(
                f"Model: {self._current_model()}",
                reply_markup=self._model_markup(),
            )
        elif query.data.startswith("effort_set_"):
            level = query.data[len("effort_set_"):]
            if level in EFFORT_LEVELS:
                self.task_manager.claude.effort = level
            await query.edit_message_text(
                f"Effort: {self._current_effort()}",
                reply_markup=self._effort_markup(),
            )
        elif query.data.startswith("permissions_set_"):
            mode = query.data[len("permissions_set_"):]
            if mode == "dangerously-skip-permissions":
                self.task_manager.claude.skip_permissions = True
                self.task_manager.claude.permission_mode = None
            elif mode in PERMISSION_MODES:
                self.task_manager.claude.skip_permissions = False
                self.task_manager.claude.permission_mode = mode if mode != "default" else None
            await query.edit_message_text(
                f"Permissions: {self._current_permission()}",
                reply_markup=self._permissions_markup(),
            )
        elif query.data == "reset_confirm":
            self.task_manager.cancel_all()
            self.task_manager.claude.session_id = None
            clear_session(self.name)
            await query.edit_message_text("Session reset.")
        elif query.data == "reset_cancel":
            await query.edit_message_text("Reset cancelled.")
        elif query.data.startswith("task_info_"):
            task_id = parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            text, markup = task_info_markup(task)
            await query.edit_message_text(text, reply_markup=markup)
        elif query.data == "tasks_back":
            if query.message:
                await self._render_tasks(query.message.chat.id, edit_query=query)
        elif query.data.startswith("task_cancel_"):
            task_id = parse_task_id(query.data)
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await query.edit_message_text(f"#{task_id} cancelled.")
            else:
                await query.edit_message_text(f"#{task_id} not found or already finished.")
        elif query.data.startswith("task_log_"):
            task_id = parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            text, markup = task_log_text(task)
            await query.edit_message_text(text, reply_markup=markup)

    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return

        st = self.task_manager.claude.status
        text = format_status(
            name=self.name,
            path=str(self.path),
            model=self.task_manager.claude.model_display or self.task_manager.claude.model,
            started_at=self._started_at,
            session_id=st["session_id"],
            is_running=st["running"],
            running_count=self.task_manager.running_count,
            waiting_count=self.task_manager.waiting_count,
        )
        await msg.reply_text(text)

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        user = update.effective_user
        if not self._authenticator.authenticate(user):
            await msg.reply_text("Unauthorized.")
            return
        assert user is not None
        if self._rate_limiter.is_limited(user.id):
            await msg.reply_text("Rate limited. Try again shortly.")
            return

        uploads_dir = self.path / "uploads"
        uploads_dir.mkdir(exist_ok=True)

        if msg.photo:
            photo = msg.photo[-1]
            file = await photo.get_file()
            filename = f"photo_{int(time.monotonic() * 1000)}.jpg"
        elif msg.document:
            file = await msg.document.get_file()
            raw_name = msg.document.file_name or f"file_{int(time.monotonic() * 1000)}"
            filename = sanitize_filename(raw_name)
        else:
            await msg.reply_text("Unsupported file type.")
            return

        dest = uploads_dir / filename
        # Guard against path traversal after sanitization
        if not dest.resolve().is_relative_to(uploads_dir.resolve()):
            logger.warning("Path traversal attempt blocked: %s", filename)
            await msg.reply_text("Invalid filename.")
            return
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
        prompt = f"[User uploaded uploads/{filename}]"
        if caption:
            prompt += f"\n\n{caption}"

        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )

    async def _on_unsupported(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._authenticator.authenticate(update.effective_user):
            await msg.reply_text("Unauthorized.")
            return

        if msg.voice or msg.video_note:
            text = "Voice messages aren't supported yet. Please type your message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        await msg.reply_text(text)

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath

        return PurePosixPath(path).suffix.lower() in IMAGE_EXTENSIONS

    async def _send_image(
        self, chat_id: int, file_path: str, reply_to: int | None = None
    ) -> None:
        path = (
            self.path / file_path if not file_path.startswith("/") else Path(file_path)
        )
        if not path.exists():
            logger.warning("Image file not found: %s", path)
            return
        try:
            size = path.stat().st_size
            suffix = path.suffix.lower()
            with path.open("rb") as f:
                data = f.read()
            assert self._app is not None
            if suffix == ".svg" or size > FILE_SIZE_LIMIT:
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
    async def _keep_typing(chat: Any) -> None:
        try:
            while True:
                try:
                    await chat.send_action(ChatAction.TYPING)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)
                await asyncio.sleep(TYPING_INDICATOR_INTERVAL)
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

    async def _post_stop(self, app: Any) -> None:
        await self.task_manager.shutdown()
        if self._health_server:
            self._health_server.stop()

    async def _post_init(self, app: Any) -> None:
        if self._health_server:
            self._health_server.start()
        result = await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("delete_webhook result=%s (drop_pending_updates=True)", result)
        await app.bot.set_my_commands(COMMANDS)
        if self._trusted_user_id:
            try:
                await app.bot.send_message(
                    self._trusted_user_id,
                    f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                )
            except Exception:
                logger.error("Failed to send startup message", exc_info=True)

    def build(self) -> Application[Any, Any, Any, Any, Any, Any]:
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_stop(self._post_stop)
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
            "history": self._on_history,
            "help": self._on_help,
        }
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

        unsupported_filter = private & (
            filters.VOICE
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
            | filters.AUDIO
        )
        app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))

        app.add_error_handler(self._on_error)
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app


def run_bot(
    name: str,
    path: Path,
    token: str,
    username: str,
    session_id: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    trusted_user_id: int | None = None,
    on_trust: Callable[[int], None] | None = None,
    health_port: int | None = None,
    webhook_url: str | None = None,
    webhook_port: int = 8443,
) -> None:
    if not username:
        raise SystemExit(
            "No allowed username configured. Use --username or run 'configure --username'."
        )
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(
        name, path, token, username,
        trusted_user_id=trusted_user_id,
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        health_port=health_port,
        webhook_url=webhook_url,
        webhook_port=webhook_port,
    )
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    if model:
        bot.task_manager.claude.model = model
    app = bot.build()
    logger.info(
        "Bot '%s' started at %s (trusted_user_id=%s)", name, path, trusted_user_id
    )
    if bot.webhook_url:
        app.run_webhook(
            listen="0.0.0.0",
            port=bot.webhook_port,
            url_path=bot.token,
            webhook_url=f"{bot.webhook_url}/{bot.token}",
        )
    else:
        app.run_polling(drop_pending_updates=True)


def run_bots(
    config: Config,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    config_path: Path | None = None,
) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        effective_username = proj.allowed_username or config.allowed_username
        if proj.allowed_username:
            effective_trusted_id = proj.trusted_user_id
        else:
            effective_trusted_id = proj.trusted_user_id if proj.trusted_user_id is not None else config.trusted_user_id
        on_trust = None
        if config_path:
            _name = name
            _path = config_path

            def on_trust(uid: int) -> None:
                save_project_trusted_user_id(_name, uid, _path)
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            effective_username,
            model=model or proj.model,
            skip_permissions=skip_permissions or proj.dangerously_skip_permissions,
            permission_mode=permission_mode or proj.permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            trusted_user_id=effective_trusted_id,
            on_trust=on_trust,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
