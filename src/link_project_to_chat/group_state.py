"""In-memory per-group state for dual-agent teams.

Lives for the process lifetime. Halts and round counters do not persist across
restarts — that's intentional: a process restart is itself a reset.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from time import time

_MAX_REGISTRY_ENTRIES = 500


@dataclass
class GroupState:
    halted: bool = False
    bot_to_bot_rounds: int = 0
    last_user_activity_ts: float = field(default_factory=time)


class GroupStateRegistry:
    def __init__(self, max_bot_rounds: int = 20) -> None:
        self._states: collections.OrderedDict[int, GroupState] = collections.OrderedDict()
        self._max = max_bot_rounds

    @property
    def max_bot_rounds(self) -> int:
        return self._max

    def get(self, chat_id: int) -> GroupState:
        if chat_id not in self._states:
            if len(self._states) >= _MAX_REGISTRY_ENTRIES:
                self._states.popitem(last=False)  # evict LRU
            self._states[chat_id] = GroupState()
        else:
            self._states.move_to_end(chat_id)
        return self._states[chat_id]

    def note_user_message(self, chat_id: int) -> None:
        s = self.get(chat_id)
        s.bot_to_bot_rounds = 0
        s.last_user_activity_ts = time()

    def note_bot_to_bot(self, chat_id: int) -> None:
        """Increment the bot-to-bot round counter. Halts the group if cap reached."""
        s = self.get(chat_id)
        s.bot_to_bot_rounds += 1
        if s.bot_to_bot_rounds >= self._max:
            s.halted = True

    def halt(self, chat_id: int) -> None:
        self.get(chat_id).halted = True

    def resume(self, chat_id: int) -> None:
        s = self.get(chat_id)
        s.halted = False
        s.bot_to_bot_rounds = 0
