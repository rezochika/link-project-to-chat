from __future__ import annotations

from link_project_to_chat.group_state import GroupState, GroupStateRegistry
from link_project_to_chat.transport import ChatKind, ChatRef


def _chat(native_id: str = "-100123") -> ChatRef:
    return ChatRef(transport_id="telegram", native_id=native_id, kind=ChatKind.ROOM)


def test_new_group_defaults():
    reg = GroupStateRegistry(max_bot_rounds=20)
    s = reg.get(_chat())
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_user_message_resets_round_counter():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    s = reg.get(chat)
    s.bot_to_bot_rounds = 5
    reg.note_user_message(chat)
    assert reg.get(chat).bot_to_bot_rounds == 0


def test_bot_to_bot_increment():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    reg.note_bot_to_bot(chat)
    reg.note_bot_to_bot(chat)
    assert reg.get(chat).bot_to_bot_rounds == 2


def test_cap_halts_at_max_rounds():
    reg = GroupStateRegistry(max_bot_rounds=3)
    chat = _chat()
    for _ in range(3):
        reg.note_bot_to_bot(chat)
    s = reg.get(chat)
    assert s.halted is True
    assert s.bot_to_bot_rounds == 3


def test_halt_and_resume():
    reg = GroupStateRegistry(max_bot_rounds=20)
    chat = _chat()
    reg.halt(chat)
    assert reg.get(chat).halted is True
    reg.resume(chat)
    s = reg.get(chat)
    assert s.halted is False
    assert s.bot_to_bot_rounds == 0


def test_independent_groups_do_not_interfere():
    reg = GroupStateRegistry(max_bot_rounds=20)
    a = _chat("-1")
    b = _chat("-2")
    reg.halt(a)
    assert reg.get(a).halted is True
    assert reg.get(b).halted is False


def test_different_transport_ids_do_not_interfere():
    """A ChatRef with the same native_id but a different transport_id is a different group."""
    reg = GroupStateRegistry(max_bot_rounds=20)
    tg = ChatRef(transport_id="telegram", native_id="-100123", kind=ChatKind.ROOM)
    dc = ChatRef(transport_id="discord", native_id="-100123", kind=ChatKind.ROOM)
    reg.halt(tg)
    assert reg.get(tg).halted is True
    assert reg.get(dc).halted is False
