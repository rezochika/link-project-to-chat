"""Transport-agnostic streaming-edit helper.

Owns one editable message and throttles updates. Called by the bot when
streaming Claude's output; the throttling + chunking logic lives here so
every Transport behaves identically.
"""
from __future__ import annotations

import time

from .base import ChatRef, MessageRef, Transport


class StreamingMessage:
    def __init__(
        self,
        transport: Transport,
        chat: ChatRef,
        *,
        min_interval_s: float = 2.0,
        max_chars: int = 4000,
    ) -> None:
        self._transport = transport
        self._chat = chat
        self._min_interval_s = min_interval_s
        self._max_chars = max_chars
        self._current_ref: MessageRef | None = None
        self._current_text = ""
        self._pending_text: str | None = None
        self._last_edit_ts = 0.0
        self._closed = False

    async def open(self, initial_text: str) -> None:
        self._current_text = initial_text[: self._max_chars]
        self._current_ref = await self._transport.send_text(self._chat, self._current_text)
        self._last_edit_ts = time.monotonic()

    async def update(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("StreamingMessage is closed")
        if self._current_ref is None:
            raise RuntimeError("open() must be called before update()")
        self._pending_text = text
        now = time.monotonic()
        if now - self._last_edit_ts < self._min_interval_s:
            return
        await self._flush()

    async def close(self, final_text: str | None = None) -> None:
        if self._closed:
            return
        if final_text is not None:
            self._pending_text = final_text
        if self._pending_text is not None:
            await self._flush()
        self._closed = True

    async def _flush(self) -> None:
        if self._pending_text is None or self._current_ref is None:
            return
        text = self._pending_text
        self._pending_text = None

        if len(text) <= self._max_chars:
            await self._transport.edit_text(self._current_ref, text)
            self._current_text = text
            self._last_edit_ts = time.monotonic()
            return

        # Overflow: chunk the prefix into new messages; keep the tail on the
        # current message so the stream continues to edit-in-place.
        keep_last = text[-self._max_chars :]
        prefix = text[: -self._max_chars]
        while len(prefix) > self._max_chars:
            head, prefix = prefix[: self._max_chars], prefix[self._max_chars :]
            await self._transport.edit_text(self._current_ref, head)
            self._current_ref = await self._transport.send_text(self._chat, "...")
        if prefix:
            await self._transport.edit_text(self._current_ref, prefix)
            self._current_ref = await self._transport.send_text(self._chat, keep_last)
        else:
            await self._transport.edit_text(self._current_ref, keep_last)
        self._current_text = keep_last
        self._last_edit_ts = time.monotonic()
