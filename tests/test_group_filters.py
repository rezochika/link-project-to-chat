from __future__ import annotations

from types import SimpleNamespace

from link_project_to_chat.group_filters import (
    extract_mentions,
    is_directed_at_me,
    is_from_other_bot,
    is_from_self,
    is_reply_to_bot,
    mentions_bot,
)
from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="telegram", native_id="-100123", kind=ChatKind.ROOM)


def _sender(handle: str | None = None, is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="telegram",
        native_id="1",
        display_name="X",
        handle=handle,
        is_bot=is_bot,
    )


def _msg(
    text: str = "",
    sender_handle: str | None = None,
    sender_is_bot: bool = False,
    reply_to_bot_username: str | None = None,
) -> IncomingMessage:
    reply_to: MessageRef | None = None
    reply_to_sender: Identity | None = None
    if reply_to_bot_username:
        reply_to = MessageRef(transport_id="telegram", native_id="0", chat=_chat())
        reply_to_sender = Identity(
            transport_id="telegram", native_id="0",
            display_name=reply_to_bot_username,
            handle=reply_to_bot_username, is_bot=True,
        )
    return IncomingMessage(
        chat=_chat(),
        sender=_sender(handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=None,
        reply_to_sender=reply_to_sender,
    )


# extract_mentions


def test_extract_mentions_empty_text():
    assert extract_mentions("") == []


def test_extract_mentions_single():
    assert extract_mentions("@acme_dev_bot do X") == ["acme_dev_bot"]


def test_extract_mentions_multiple():
    out = extract_mentions("@bot_a and @bot_b please")
    assert out == ["bot_a", "bot_b"]


def test_extract_mentions_case_folding():
    assert extract_mentions("@Acme_Dev_Bot hi") == ["acme_dev_bot"]


def test_extract_mentions_ignores_non_mention_text():
    assert extract_mentions("no mentions here") == []


def test_extract_mentions_strips_punctuation_boundaries():
    assert extract_mentions("hey @bot_a, can you") == ["bot_a"]


def test_extract_mentions_ignores_email_addresses():
    """Regression: v1 used Telegram entity parsing, which treats email @ as non-mention.
    Our pure-regex version must match that behavior by requiring a non-word left boundary."""
    assert extract_mentions("contact me at alice@acme_dev_bot.com") == []


def test_extract_mentions_ignores_embedded_at_after_word_char():
    """Regression: `foo@handle` (no space before @) is not a mention."""
    assert extract_mentions("run foo@acme_dev_bot") == []


def test_extract_mentions_still_matches_at_start_of_string():
    """Boundary: the left-boundary must allow start-of-string, not require a preceding non-word char."""
    assert extract_mentions("@acme_dev_bot hi") == ["acme_dev_bot"]


def test_extract_mentions_still_matches_after_newline_or_punctuation():
    """Newlines and punctuation count as non-word — mention on the second line should match."""
    assert extract_mentions("hello\n@acme_dev_bot") == ["acme_dev_bot"]
    assert extract_mentions("(@acme_dev_bot)") == ["acme_dev_bot"]


# is_directed_at_me


def test_directed_at_me_via_mention():
    msg = _msg(text="@acme_dev_bot implement task 1")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_via_reply_to_bot():
    msg = _msg(text="please redo this", reply_to_bot_username="acme_dev_bot")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_not_directed_when_mention_is_other_bot():
    msg = _msg(text="@acme_manager_bot review")
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_not_directed_when_no_mention_no_reply():
    msg = _msg(text="just chatting")
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_reply_to_me_is_suppressed_when_user_mentions_other_bot():
    """Regression: if the user replies to bot A's message but only @mentions
    bot B, bot A must not respond (previously both woke up)."""
    msg = _msg(
        text="@acme_manager_bot",
        reply_to_bot_username="acme_dev_bot",
    )
    assert is_directed_at_me(msg, "acme_dev_bot") is False
    assert is_directed_at_me(msg, "acme_manager_bot") is True


def test_reply_to_me_still_fires_without_any_mention():
    msg = _msg(text="yes please redo it", reply_to_bot_username="acme_dev_bot")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_mention_match_is_case_insensitive():
    msg = _msg(text="@Acme_Dev_Bot hi")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_when_human_mentions_bot():
    msg = _msg(text="@acme_dev_bot help me out", sender_handle="alice", sender_is_bot=False)
    assert is_directed_at_me(msg, "acme_dev_bot") is True


# is_from_self / is_from_other_bot


def test_is_from_self_true_when_usernames_match():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is True


def test_is_from_self_false_when_different_username():
    msg = _msg(sender_handle="acme_manager_bot", sender_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_self_false_when_not_bot():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=False)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_other_bot_true():
    msg = _msg(sender_handle="acme_manager_bot", sender_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is True


def test_is_from_other_bot_false_when_human():
    msg = _msg(sender_handle="revaz", sender_is_bot=False)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False


def test_is_from_other_bot_false_when_self():
    msg = _msg(sender_handle="acme_dev_bot", sender_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False
