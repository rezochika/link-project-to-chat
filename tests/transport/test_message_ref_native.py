from __future__ import annotations

from link_project_to_chat.transport.base import ChatKind, ChatRef, MessageRef


def test_message_ref_native_is_optional():
    chat = ChatRef("fake", "chat-1", ChatKind.DM)
    msg = MessageRef("fake", "msg-1", chat)
    assert msg.native is None


def test_message_ref_native_does_not_affect_equality_hash_or_repr():
    chat = ChatRef("fake", "chat-1", ChatKind.DM)
    left = MessageRef("fake", "msg-1", chat, native={"thread": "a"})
    right = MessageRef("fake", "msg-1", chat, native={"thread": "b"})

    assert left == right
    assert hash(left) == hash(right)
    assert "native=" not in repr(left)
    assert "thread" not in repr(left)
