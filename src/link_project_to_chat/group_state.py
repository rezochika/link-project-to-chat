"""Per-room state for dual-agent teams, keyed by ChatRef.

Lives for the process lifetime. Halts and round counters do not persist across
restarts — a process restart is itself a reset.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from time import time

from .transport import ChatRef

_MAX_REGISTRY_ENTRIES = 500


@dataclass
class GroupState:
    halted: bool = False
    bot_to_bot_rounds: int = 0
    last_user_activity_ts: float = field(default_factory=time)


class GroupStateRegistry:
    def __init__(self, max_bot_rounds: int = 20) -> None:
        # OrderedDict + LRU eviction caps memory growth for long-running manager
        # processes that see many transient group chats. Keys are (transport_id,
        # native_id) tuples so a single registry can serve multiple transports.
        self._states: collections.OrderedDict[tuple[str, str], GroupState] = (
            collections.OrderedDict()
        )
        self._max = max_bot_rounds

    @property
    def max_bot_rounds(self) -> int:
        return self._max

    @staticmethod
    def _key(chat: ChatRef) -> tuple[str, str]:
        return (chat.transport_id, chat.native_id)

    def get(self, chat: ChatRef) -> GroupState:
        key = self._key(chat)
        if key not in self._states:
            if len(self._states) >= _MAX_REGISTRY_ENTRIES:
                self._states.popitem(last=False)  # evict least-recently-used
            self._states[key] = GroupState()
        else:
            self._states.move_to_end(key)
        return self._states[key]

    def note_user_message(self, chat: ChatRef) -> None:
        s = self.get(chat)
        s.bot_to_bot_rounds = 0
        s.last_user_activity_ts = time()

    def note_bot_to_bot(self, chat: ChatRef) -> None:
        """Increment the bot-to-bot round counter. Halts the group if cap reached."""
        s = self.get(chat)
        s.bot_to_bot_rounds += 1
        if s.bot_to_bot_rounds >= self._max:
            s.halted = True

    def halt(self, chat: ChatRef) -> None:
        self.get(chat).halted = True

    def resume(self, chat: ChatRef) -> None:
        s = self.get(chat)
        s.halted = False
        s.bot_to_bot_rounds = 0
