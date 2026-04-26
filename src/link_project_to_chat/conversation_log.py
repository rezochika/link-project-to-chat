"""Per-chat conversation history, persisted in SQLite.

Decouples conversational context from any single backend's session: every
plain-text user turn and final agent reply text gets logged here, keyed by
``(transport_id, chat_native_id)``. The bot prepends the last N turns to the
next user prompt so a backend swap (e.g. ``/backend codex``) doesn't drop
prior context.

Slash-command output and tool invocations (`/run`, button clicks, file
uploads) are NOT logged here — they're bot-internal mechanics, not
conversational turns.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from .transport import ChatRef

logger = logging.getLogger(__name__)

USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"

# Per-turn rendering cap. The history block is prepended to every subsequent
# prompt; a runaway 50KB user paste would otherwise inject ~500KB into every
# turn and blow context windows. Truncation only affects the rendered prepend
# block, not what we store on disk — so /reset (which clears the table) and
# any future history-export feature still see the full text.
HISTORY_TURN_CHAR_CAP = 4000
_TRUNCATION_SUFFIX = "…[truncated]"


class ConversationLog:
    """Append-only SQLite log keyed by (transport_id, chat_native_id).

    Synchronous on purpose: writes are tiny and infrequent compared to the
    streaming traffic the bot is already juggling, and avoiding aiosqlite
    keeps the dependency footprint identical to the existing codebase.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                self._db_path.parent.chmod(0o700)
            except OSError:
                pass
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transport_id TEXT NOT NULL,
                    chat_native_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    backend TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_log_chat
                    ON conversation_log (transport_id, chat_native_id, created_at);
                """
            )
        if sys.platform != "win32":
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass

    def append(
        self,
        chat: ChatRef,
        role: str,
        text: str,
        backend: str | None = None,
    ) -> None:
        """Append a turn. Empty text is ignored (no point persisting nothing)."""
        if role not in (USER_ROLE, ASSISTANT_ROLE):
            raise ValueError(f"role must be {USER_ROLE!r} or {ASSISTANT_ROLE!r}")
        if not text or not text.strip():
            return
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO conversation_log
                       (transport_id, chat_native_id, role, text, created_at, backend)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat.transport_id, chat.native_id, role, text, time.time(), backend),
                )
        except sqlite3.Error:
            logger.exception(
                "Failed to append conversation log for chat %s/%s",
                chat.transport_id, chat.native_id,
            )

    def recent(self, chat: ChatRef, limit: int = 10) -> list[tuple[str, str]]:
        """Return the last ``limit`` (role, text) pairs in chronological order."""
        if limit <= 0:
            return []
        try:
            with self._connect() as db:
                cur = db.execute(
                    """SELECT role, text FROM conversation_log
                       WHERE transport_id = ? AND chat_native_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (chat.transport_id, chat.native_id, limit),
                )
                rows = cur.fetchall()
        except sqlite3.Error:
            logger.exception(
                "Failed to read conversation log for chat %s/%s",
                chat.transport_id, chat.native_id,
            )
            return []
        return [(role, text) for role, text in reversed(rows)]

    def clear(self, chat: ChatRef) -> int:
        """Delete every row for the chat. Returns the number of deleted rows."""
        try:
            with self._connect() as db:
                cur = db.execute(
                    """DELETE FROM conversation_log
                       WHERE transport_id = ? AND chat_native_id = ?""",
                    (chat.transport_id, chat.native_id),
                )
                return cur.rowcount or 0
        except sqlite3.Error:
            logger.exception(
                "Failed to clear conversation log for chat %s/%s",
                chat.transport_id, chat.native_id,
            )
            return 0


def default_db_path(project_name: str) -> Path:
    """One DB per project bot, all chats for that bot in one file."""
    return (
        Path.home()
        / ".link-project-to-chat"
        / "conversations"
        / f"{project_name}.db"
    )


def format_history_block(turns: list[tuple[str, str]]) -> str:
    """Render the recent-history prepend block for the next user prompt.

    Returns an empty string when ``turns`` is empty so callers can append
    unconditionally without checking. The block is wrapped in plain-text
    markers so the agent can recognise it as quoted history rather than a
    fresh instruction.
    """
    if not turns:
        return ""
    lines = [f"[Recent conversation history — last {len(turns)} turns in this chat]"]
    for role, text in turns:
        if len(text) > HISTORY_TURN_CHAR_CAP:
            keep = HISTORY_TURN_CHAR_CAP - len(_TRUNCATION_SUFFIX)
            text = text[:keep] + _TRUNCATION_SUFFIX
        lines.append(f"{role}: {text}")
    lines.append("")
    lines.append("[Current message]")
    lines.append("")
    return "\n".join(lines)
