from __future__ import annotations

from link_project_to_chat.group_state import GroupState, GroupStateRegistry


def test_new_group_defaults():
    reg = GroupStateRegistry(max_bot_rounds=20)
    s = reg.get(-100123)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_user_message_resets_round_counter():
    reg = GroupStateRegistry(max_bot_rounds=20)
    s = reg.get(-100123)
    s.bot_to_bot_rounds = 5
    reg.note_user_message(-100123)
    assert reg.get(-100123).bot_to_bot_rounds == 0


def test_bot_to_bot_increment():
    reg = GroupStateRegistry(max_bot_rounds=20)
    reg.note_bot_to_bot(-100123)
    reg.note_bot_to_bot(-100123)
    assert reg.get(-100123).bot_to_bot_rounds == 2


def test_cap_halts_at_max_rounds():
    reg = GroupStateRegistry(max_bot_rounds=3)
    for _ in range(3):
        reg.note_bot_to_bot(-100123)
    s = reg.get(-100123)
    assert s.halted is True
    assert s.bot_to_bot_rounds == 3


def test_halt_and_resume():
    reg = GroupStateRegistry(max_bot_rounds=20)
    reg.halt(-100123)
    assert reg.get(-100123).halted is True
    reg.resume(-100123)
    s = reg.get(-100123)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_independent_groups_do_not_interfere():
    reg = GroupStateRegistry(max_bot_rounds=20)
    reg.halt(-1)
    assert reg.get(-1).halted is True
    assert reg.get(-2).halted is False
