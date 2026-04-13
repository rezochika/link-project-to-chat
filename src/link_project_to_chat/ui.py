"""Pure UI helper functions for Telegram markup and status formatting.

All functions are stateless and testable at Level 1 (no I/O, no side effects).
"""

from __future__ import annotations

import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .claude_client import EFFORT_LEVELS, MODELS, PERMISSION_MODES
from .constants import (
    ERROR_TRUNCATION,
    FILENAME_MAX_LENGTH,
    MAX_FINISHED_TASKS_SHOWN,
    TASK_INPUT_TRUNCATION,
    TASK_LOG_LIMIT,
    TASK_NAME_TRUNCATION,
)
from .task_manager import Task, TaskStatus, TaskType

_TASK_ICONS: dict[TaskStatus, str] = {
    TaskStatus.WAITING: "~",
    TaskStatus.RUNNING: ">",
    TaskStatus.DONE: "+",
    TaskStatus.FAILED: "!",
    TaskStatus.CANCELLED: "x",
}

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List all tasks"),
    ("model", "Set Claude model (haiku/sonnet/opus)"),
    ("effort", "Set thinking depth (low/medium/high/max)"),
    ("permissions", "Set permission mode"),
    ("compact", "Compress session context"),
    ("status", "Bot status"),
    ("reset", "Clear Claude session"),
    ("help", "Show available commands"),
]

CMD_HELP = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)

_PERMISSION_OPTIONS = (
    *PERMISSION_MODES,
    "dangerously-skip-permissions",
)


def parse_task_id(data: str) -> int:
    """Extract task ID from callback_data like 'task_info_42'."""
    return int(data.split("_")[-1])


def sanitize_error(error: str | None, max_length: int = ERROR_TRUNCATION) -> str:
    """Sanitize error messages before sending to users.

    Strips file paths and limits length to prevent leaking internal details.
    """
    if not error:
        return "An unexpected error occurred."
    sanitized = re.sub(r"/[\w/.-]+(?:\.py|\.json|\.toml)", "<path>", error)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "... (truncated)"
    return sanitized


def tasks_markup(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for task list.

    Shows active tasks + up to 5 most recent finished tasks.
    """
    active = [t for t in tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
    finished = [t for t in tasks if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)][:MAX_FINISHED_TASKS_SHOWN]
    visible = active + finished
    if not visible:
        return None
    buttons = []
    for t in visible:
        icon = _TASK_ICONS.get(t.status, "?")
        elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
        label = t.name if t.type == TaskType.COMMAND else t.input[:TASK_NAME_TRUNCATION]
        btn = InlineKeyboardButton(
            f"{icon} #{t.id}{elapsed} {label}", callback_data=f"task_info_{t.id}"
        )
        buttons.append([btn])
    return InlineKeyboardMarkup(buttons)


def task_info_markup(task: Task) -> tuple[str, InlineKeyboardMarkup]:
    """Build task detail view text + keyboard."""
    elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
    text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:TASK_INPUT_TRUNCATION]}"
    rows: list[list[InlineKeyboardButton]] = []
    if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
        rows.append([InlineKeyboardButton("Cancel", callback_data=f"task_cancel_{task.id}")])
    if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
        rows.append([InlineKeyboardButton("Log", callback_data=f"task_log_{task.id}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="tasks_back")])
    return text, InlineKeyboardMarkup(rows)


def task_log_text(task: Task, max_length: int = TASK_LOG_LIMIT) -> tuple[str, InlineKeyboardMarkup]:
    """Build task log view text + back button."""
    output = task.result or task.error or "(no output)"
    if len(output) > max_length:
        total = len(task.result or "")
        output = output[:max_length] + f"\n... (truncated, {total} chars total)"
    rows = [[InlineKeyboardButton("« Back", callback_data=f"task_info_{task.id}")]]
    return f"#{task.id} log:\n{output}", InlineKeyboardMarkup(rows)


def model_markup() -> InlineKeyboardMarkup:
    """Build model selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(m, callback_data=f"model_set_{m}")]
        for m in MODELS
    ])


def effort_markup() -> InlineKeyboardMarkup:
    """Build effort level selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(e, callback_data=f"effort_set_{e}")]
        for e in EFFORT_LEVELS
    ])


def permissions_markup() -> InlineKeyboardMarkup:
    """Build permission mode selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(o, callback_data=f"permissions_set_{o}")]
        for o in _PERMISSION_OPTIONS
    ])


def reset_markup() -> InlineKeyboardMarkup:
    """Build session reset confirmation keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, reset", callback_data="reset_confirm"),
                InlineKeyboardButton("Cancel", callback_data="reset_cancel"),
            ]
        ]
    )


def format_status(
    name: str,
    path: str,
    model: str,
    started_at: float,
    session_id: str | None,
    is_running: bool,
    running_count: int,
    waiting_count: int,
) -> str:
    """Format the /status response text."""
    uptime = time.monotonic() - started_at
    h, rem = divmod(int(uptime), 3600)
    m, s = divmod(rem, 60)
    return "\n".join([
        f"Project: {name}",
        f"Path: {path}",
        f"Model: {model}",
        f"Uptime: {h}h {m}m {s}s",
        f"Session: {session_id or 'none'}",
        f"Claude: {'RUNNING' if is_running else 'idle'}",
        f"Running tasks: {running_count}",
        f"Waiting: {waiting_count}",
    ])


def sanitize_filename(raw_name: str, max_length: int = FILENAME_MAX_LENGTH) -> str:
    """Sanitize a filename for safe filesystem storage."""
    return "".join(
        c
        for c in raw_name.replace("/", "_").replace("\\", "_")
        if c.isalnum() or c in "._- "
    )[:max_length]
