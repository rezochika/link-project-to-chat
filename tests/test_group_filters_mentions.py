"""Task 5 of Web UI plan: structured-mention preference in group_filters.

These tests pin the contract that `mentions_bot` and `is_directed_at_me` prefer
the structured `IncomingMessage.mentions` list when populated (Discord/Slack/Web)
and only fall back to text regex parsing when it's empty (Telegram legacy path).
`mentions_bot_by_id` is a new helper for transport-id + native-id matching.
"""

from __future__ import annotations

from link_project_to_chat.group_filters import (
    is_directed_at_me,
    mentions_bot,
    mentions_bot_by_id,
)
from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)


def _room() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="r1", kind=ChatKind.ROOM)


def _user() -> Identity:
    return Identity(
        transport_id="fake",
        native_id="u1",
        display_name="User",
        handle=None,
        is_bot=False,
    )


def _bot(handle: str = "mybot", native_id: str = "b1") -> Identity:
    return Identity(
        transport_id="fake",
        native_id=native_id,
        display_name="Bot",
        handle=handle,
        is_bot=True,
    )


def _msg(text: str, mentions: list[Identity] | None = None) -> IncomingMessage:
    chat = _room()
    return IncomingMessage(
        chat=chat,
        sender=_user(),
        text=text,
        files=[],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="m1", chat=chat),
        mentions=mentions or [],
    )


def test_mentions_bot_uses_structured_mention_when_present():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot(msg, "mybot") is True


def test_mentions_bot_ignores_other_bot_in_structured_mentions():
    msg = _msg("@mybot hey", mentions=[_bot("otherbot", "b2")])
    assert mentions_bot(msg, "mybot") is False


def test_mentions_bot_falls_back_to_text_when_no_mentions():
    msg = _msg("@mybot hey")
    assert mentions_bot(msg, "mybot") is True


def test_mentions_bot_text_fallback_negative():
    msg = _msg("hey there")
    assert mentions_bot(msg, "mybot") is False


def test_mentions_bot_by_id_positive():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot_by_id(msg, "fake", "b1") is True


def test_mentions_bot_by_id_negative():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot_by_id(msg, "fake", "b2") is False


def test_is_directed_at_me_via_structured_mention():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert is_directed_at_me(msg, "mybot") is True
