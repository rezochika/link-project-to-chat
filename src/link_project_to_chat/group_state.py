"""Per-room state for dual-agent teams, keyed by ChatRef.

Lives for the process lifetime. Halts and round counters do not persist across
restarts — a process restart is itself a reset.
"""

from __future__ import annotations

from dataclasses import dataclass

from .transport import ChatRef


@dataclass
class GroupState:
    halted: bool = False
    bot_to_bot_rounds: int = 0


class GroupStateRegistry:
    def __init__(self, max_bot_rounds: int = 20) -> None:
        self._states: dict[tuple[str, str], GroupState] = {}
        self._max = max_bot_rounds

    @property
    def max_bot_rounds(self) -> int:
        return self._max

    @staticmethod
    def _key(chat: ChatRef) -> tuple[str, str]:
        return (chat.transport_id, chat.native_id)

    def get(self, chat: ChatRef) -> GroupState:
        return self._states.setdefault(self._key(chat), GroupState())

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
