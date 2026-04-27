"""SQLite-backed store for WebTransport messages and event queue."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite


class WebStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                sender_native_id TEXT NOT NULL,
                sender_display_name TEXT NOT NULL,
                sender_is_bot INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                html INTEGER NOT NULL DEFAULT 0,
                buttons_json TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_chat ON events (chat_id, id);
        """)
        async with self._db.execute("PRAGMA table_info(messages)") as cursor:
            cols = {row["name"] for row in await cursor.fetchall()}
        if "buttons_json" not in cols:
            await self._db.execute("ALTER TABLE messages ADD COLUMN buttons_json TEXT")
        await self._db.commit()

    async def save_message(
        self,
        chat_id: str,
        sender_native_id: str,
        sender_display_name: str,
        sender_is_bot: bool,
        text: str,
        html: bool,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> int:
        assert self._db is not None
        async with self._db.execute(
            """INSERT INTO messages
               (chat_id, sender_native_id, sender_display_name, sender_is_bot, text, html, buttons_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, sender_native_id, sender_display_name, 1 if sender_is_bot else 0,
             text, 1 if html else 0, json.dumps(buttons) if buttons else None, time.time()),
        ) as cursor:
            msg_id = cursor.lastrowid
        await self._db.commit()
        return msg_id  # type: ignore[return-value]

    async def update_message(
        self,
        msg_id: int,
        text: str,
        html: bool,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE messages SET text = ?, html = ?, buttons_json = ? WHERE id = ?",
            (text, 1 if html else 0, json.dumps(buttons) if buttons else None, msg_id),
        )
        await self._db.commit()

    async def get_messages(self, chat_id: str, limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "chat_id": r["chat_id"],
                "sender_native_id": r["sender_native_id"],
                "sender_display_name": r["sender_display_name"],
                "sender_is_bot": bool(r["sender_is_bot"]),
                "text": r["text"],
                "html": bool(r["html"]),
                "buttons": json.loads(r["buttons_json"]) if r["buttons_json"] else None,
                "created_at": r["created_at"],
            }
            for r in reversed(rows)
        ]

    async def push_event(self, chat_id: str, event_type: str, payload: dict[str, Any]) -> int:
        assert self._db is not None
        async with self._db.execute(
            "INSERT INTO events (chat_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, event_type, json.dumps(payload), time.time()),
        ) as cursor:
            event_id = cursor.lastrowid
        await self._db.commit()
        return event_id  # type: ignore[return-value]

    async def poll_events(self, chat_id: str, after_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, event_type, payload_json FROM events WHERE chat_id = ? AND id > ? ORDER BY id",
            (chat_id, after_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"id": r["id"], "type": r["event_type"], "payload": json.loads(r["payload_json"])}
            for r in rows
        ]
