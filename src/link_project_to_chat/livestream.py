from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_THROTTLE = 1.2  # seconds between edits per message
_DEFAULT_MAX_CHARS = 3800  # Telegram hard cap is 4096; leave room for prefix + ellipsis
_MAX_THROTTLE = 5.0  # cap when backing off from 429


class LiveMessage:
    """A single Telegram message that is edited in place as deltas arrive."""

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        *,
        reply_to_message_id: int | None = None,
        prefix: str = "",
        throttle: float = _DEFAULT_THROTTLE,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._bot = bot
        self.chat_id = chat_id
        self._reply_to = reply_to_message_id
        self._prefix = prefix
        self._throttle = throttle
        self._effective_throttle = throttle
        self._max_chars = max_chars
        self._buffer: str = ""
        self._last_rendered: str = ""
        self._last_edit_ts: float = 0.0
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._finalized = False
        self.message_id: int | None = None

    async def start(self, initial: str = "…") -> None:
        msg = await self._bot.send_message(
            self.chat_id, self._prefix + initial, reply_to_message_id=self._reply_to
        )
        self.message_id = msg.message_id
        self._last_rendered = self._prefix + initial
        self._last_edit_ts = time.monotonic()
