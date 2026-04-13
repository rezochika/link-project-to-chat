"""In-memory conversation history ring buffer."""
from __future__ import annotations

import collections
import dataclasses
import time


@dataclasses.dataclass
class HistoryEntry:
    role: str  # "user" or "assistant"
    text: str  # truncated to first 200 chars for display
    timestamp: float = dataclasses.field(default_factory=time.monotonic)
    task_id: int | None = None


class History:
    """Ring buffer of conversation exchanges."""

    def __init__(self, maxlen: int = 50) -> None:
        self._entries: collections.deque[HistoryEntry] = collections.deque(maxlen=maxlen)

    def add(self, role: str, text: str, task_id: int | None = None) -> None:
        self._entries.append(HistoryEntry(
            role=role,
            text=text[:200],
            task_id=task_id,
        ))

    def recent(self, n: int = 10) -> list[HistoryEntry]:
        entries = list(self._entries)
        return entries[-n:] if n < len(entries) else entries

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
