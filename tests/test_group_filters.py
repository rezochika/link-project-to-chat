from __future__ import annotations

from unittest.mock import MagicMock

from link_project_to_chat.group_filters import (
    is_directed_at_me,
    is_from_self,
    is_from_other_bot,
)


def _msg(
    text: str = "",
    from_username: str | None = None,
    from_is_bot: bool = False,
    reply_to_bot_username: str | None = None,
    entities=None,
) -> MagicMock:
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock()
    m.from_user.username = from_username
    m.from_user.is_bot = from_is_bot
    if reply_to_bot_username:
        m.reply_to_message = MagicMock()
        m.reply_to_message.from_user = MagicMock()
        m.reply_to_message.from_user.username = reply_to_bot_username
    else:
        m.reply_to_message = None
    m.parse_entities = MagicMock(return_value={})
    if entities is not None:
        m.parse_entities.return_value = entities
    return m


def test_directed_at_me_via_mention_entity():
    mention = MagicMock(type="mention")
    msg = _msg(text="@acme_dev_bot implement task 1", entities={mention: "@acme_dev_bot"})
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_via_reply_to_bot():
    msg = _msg(text="please redo this", reply_to_bot_username="acme_dev_bot")
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_not_directed_when_mention_is_other_bot():
    mention = MagicMock(type="mention")
    msg = _msg(text="@acme_manager_bot review", entities={mention: "@acme_manager_bot"})
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_not_directed_when_no_mention_no_reply():
    msg = _msg(text="just chatting")
    assert is_directed_at_me(msg, "acme_dev_bot") is False


def test_is_from_self_true_when_usernames_match():
    msg = _msg(from_username="acme_dev_bot", from_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is True


def test_is_from_self_false_when_different_username():
    msg = _msg(from_username="acme_manager_bot", from_is_bot=True)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_self_false_when_not_bot():
    msg = _msg(from_username="acme_dev_bot", from_is_bot=False)
    assert is_from_self(msg, "acme_dev_bot") is False


def test_is_from_other_bot_true():
    msg = _msg(from_username="acme_manager_bot", from_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is True


def test_is_from_other_bot_false_when_human():
    msg = _msg(from_username="revaz", from_is_bot=False)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False


def test_is_from_other_bot_false_when_self():
    msg = _msg(from_username="acme_dev_bot", from_is_bot=True)
    assert is_from_other_bot(msg, my_username="acme_dev_bot") is False


def test_mention_match_is_case_insensitive():
    mention = MagicMock(type="mention")
    msg = _msg(text="@Acme_Dev_Bot hi", entities={mention: "@Acme_Dev_Bot"})
    assert is_directed_at_me(msg, "acme_dev_bot") is True


def test_directed_at_me_when_human_mentions_bot():
    """A human user @mentioning the bot should still be detected as directed."""
    mention = MagicMock(type="mention")
    msg = _msg(
        text="@acme_dev_bot help me out",
        from_username="alice",
        from_is_bot=False,
        entities={mention: "@acme_dev_bot"},
    )
    assert is_directed_at_me(msg, "acme_dev_bot") is True
