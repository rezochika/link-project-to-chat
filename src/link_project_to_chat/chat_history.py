"""In-memory chat history per ROOM. Process-local; not persisted.

Keyed on transport-portable ChatRef so the same class works for
Telegram groups, future Web rooms, Slack channels, Google Chat rooms.

Zero telegram imports. Zero backend imports. Verified by
tests/test_transport_lockout.py.
"""
from __future__ import annotations

import collections
from typing import Iterable

from .transport.base import ChatKind, ChatRef

_DEFAULT_MAXLEN = 200                   # Per-chat ring buffer; matches GitLab fork.
_DEFAULT_MAX_SERIALIZED_CHARS = 4000    # Defensive cap on injected text.
_TRUNCATION_PREFIX = "[…older messages truncated…]\n"


class ChatHistory:
    """Bounded in-memory history of group/room messages, per ChatRef.

    Used by ProjectBot to inject "[Recent discussion]" context into the
    backend prompt on each LLM call in a ROOM-kind chat. The buffer is
    process-local and never persisted; restarts clear it.
    """

    def __init__(
        self,
        maxlen: int = _DEFAULT_MAXLEN,
        max_serialized_chars: int = _DEFAULT_MAX_SERIALIZED_CHARS,
    ) -> None:
        self._history: dict[ChatRef, collections.deque[dict]] = {}
        self._last_llm_msg_id: dict[ChatRef, str] = {}
        self._maxlen = maxlen
        self._max_chars = max_serialized_chars

    def record(
        self,
        chat: ChatRef,
        msg_id: str,
        sender: str,
        text: str,
        *,
        is_own_bot: bool = False,
    ) -> None:
        """Append a message to the per-chat buffer.

        Skips when chat.kind != ROOM (DMs are tracked by conversation_log).
        Skips when is_own_bot=True (bot's own outbound messages don't count
        as chatter).
        Skips empty or whitespace-only text.

        Idempotent on (chat, msg_id) — re-recording the same message is a
        no-op. This guards against dispatch chains in ProjectBot that record
        once in the group-gate and again in the file/voice handler.
        """
        if chat.kind != ChatKind.ROOM:
            return
        if is_own_bot:
            return
        if not text or not text.strip():
            return
        dq = self._history.setdefault(chat, collections.deque(maxlen=self._maxlen))
        if any(entry["msg_id"] == msg_id for entry in dq):
            return  # idempotent: same msg already recorded for this chat
        dq.append({"msg_id": msg_id, "sender": sender or "unknown", "text": text})

    def since_last_llm(self, chat: ChatRef, before_msg_id: str) -> str:
        """Serialize messages between last_llm_msg_id (exclusive) and
        before_msg_id (exclusive). Returns "" when nothing to inject.

        If the mark msg_id has been evicted from the ring, walks from the
        buffer head. Truncates oldest if serialized length exceeds
        max_serialized_chars.
        """
        dq = self._history.get(chat)
        if not dq:
            return ""
        last_llm = self._last_llm_msg_id.get(chat)
        msgs: list[dict] = []
        passed_last = last_llm is None
        # First pass: try to find the mark.
        if not passed_last:
            mark_seen = any(entry["msg_id"] == last_llm for entry in dq)
            if not mark_seen:
                # Mark was evicted — fall back to walking from head.
                passed_last = True
        for entry in dq:
            if not passed_last:
                if entry["msg_id"] == last_llm:
                    passed_last = True
                continue
            if entry["msg_id"] == before_msg_id:
                break
            msgs.append(entry)
        if not msgs:
            return ""
        return self._serialize(msgs)

    def mark_llm_call(self, chat: ChatRef, msg_id: str) -> None:
        """Record that an LLM call started for msg_id. Subsequent
        since_last_llm calls won't include messages at or before this
        msg_id."""
        self._last_llm_msg_id[chat] = msg_id

    def last_msg_id(self, chat: ChatRef) -> str | None:
        """Most recently recorded msg_id in chat, or None if empty/absent.

        Used by ProjectBot._resolve_recent_discussion to determine the LLM-call
        boundary — see that helper's docstring for rationale.
        """
        dq = self._history.get(chat)
        return dq[-1]["msg_id"] if dq else None

    def _serialize(self, msgs: Iterable[dict]) -> str:
        lines = [f"{m['sender']}: {m['text']}" for m in msgs]
        body = "\n".join(lines)
        if len(body) <= self._max_chars:
            return f"[Recent discussion]\n{body}\n\n"
        # Drop oldest until we fit; prepend truncation marker.
        while lines and len("\n".join(lines)) > self._max_chars:
            lines.pop(0)
        body = "\n".join(lines)
        return f"[Recent discussion]\n{_TRUNCATION_PREFIX}{body}\n\n"
