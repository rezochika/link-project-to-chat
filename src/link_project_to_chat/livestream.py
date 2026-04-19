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

    async def append(self, delta: str) -> None:
        if self._finalized:
            logger.debug("append after finalize ignored (mid=%s)", self.message_id)
            return
        if not delta:
            return
        self._buffer += delta
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        try:
            # Sleep until we're allowed to edit again.
            wait = self._effective_throttle - (time.monotonic() - self._last_edit_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._edit_current()
            # If new deltas landed during the edit, schedule another flush to drain them.
            if self._prefix + self._buffer != self._last_rendered and not self._finalized:
                self._pending = asyncio.create_task(self._flush_soon())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LiveMessage flush failed (mid=%s)", self.message_id)

    async def _edit_current(self) -> None:
        async with self._lock:
            if self._finalized or self.message_id is None:
                return
            text = self._prefix + self._buffer
            if text == self._last_rendered:
                return
            if not text.strip():
                return
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self._last_rendered = text
            self._last_edit_ts = time.monotonic()
