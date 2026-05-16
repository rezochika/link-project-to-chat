"""Unit tests for ProjectBot._strip_self_mention.

The helper removes @<bot_username> (case-insensitive, word-bounded) from
the IncomingMessage's text — used by the respond_in_groups routing gate
to clean the prompt before it reaches the agent.
"""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)


def _make_bot(bot_username: str = "MyBot") -> ProjectBot:
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = bot_username
    return bot


def _make_incoming(text: str) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
    )


def test_strip_removes_simple_mention():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("@MyBot do X"))
    assert out.text == " do X"


def test_strip_is_case_insensitive():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("hello @MYBOT please"))
    assert out.text == "hello  please"


def test_strip_word_bounded_does_not_clobber_longer_handle():
    """@MyBotIsCool should NOT be stripped — it's a different handle."""
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("ping @MyBotIsCool here"))
    assert out.text == "ping @MyBotIsCool here"


def test_strip_word_bounded_does_not_clobber_email_at():
    """user@MyBot.example.com (an email-like string) must not be stripped."""
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("contact user@MyBot.example"))
    # The @MyBot inside the email has no leading non-word char before it,
    # but the negative lookbehind on @ catches it. The . after MyBot is the
    # word-boundary that allows the negative lookahead to match. Concretely,
    # this would strip user@MyBot if we weren't careful. The implementation
    # MUST use a negative lookbehind on @ ([^A-Za-z0-9_@] OR start-of-string).
    assert out.text == "contact user@MyBot.example"


def test_strip_leaves_other_mentions_intact():
    bot = _make_bot("MyBot")
    out = bot._strip_self_mention(_make_incoming("@MyBot and @SomeoneElse"))
    assert out.text == " and @SomeoneElse"


def test_strip_with_empty_bot_username_returns_unchanged():
    """Defensive: before _after_ready fires, bot_username may be empty."""
    bot = _make_bot("")
    incoming = _make_incoming("hi @MyBot")
    out = bot._strip_self_mention(incoming)
    assert out is incoming or out.text == "hi @MyBot"


def test_strip_returns_immutable_replacement():
    """_strip_self_mention returns a NEW IncomingMessage (dataclasses.replace).
    Original is untouched (IncomingMessage is frozen)."""
    bot = _make_bot("MyBot")
    incoming = _make_incoming("@MyBot ping")
    out = bot._strip_self_mention(incoming)
    assert incoming.text == "@MyBot ping"
    assert out.text == " ping"
    assert out is not incoming
