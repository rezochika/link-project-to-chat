from __future__ import annotations

from link_project_to_chat.group_state import GroupStateRegistry


def test_cap_message_fires_only_once():
    """Once halted, subsequent bot-to-bot messages should be ignored silently."""
    reg = GroupStateRegistry(max_bot_rounds=2)
    reg.note_bot_to_bot(-1)
    reg.note_bot_to_bot(-1)  # halts
    assert reg.get(-1).halted is True
    reg.note_bot_to_bot(-1)
    assert reg.get(-1).halted is True


def test_resume_via_user_message_clears_halt():
    """A user message after halt should clear it and reset the counter (uses resume())."""
    reg = GroupStateRegistry(max_bot_rounds=3)
    for _ in range(3):
        reg.note_bot_to_bot(-1)
    assert reg.get(-1).halted is True
    reg.resume(-1)
    s = reg.get(-1)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0
