"""Pure functions for deciding whether a group-chat message is directed at this bot.

No telegram-bot framework side effects — takes a Message-like object and returns bools.
"""

from __future__ import annotations


def is_from_self(msg, my_username: str) -> bool:
    """True when the message was sent by this bot itself (prevents self-reply loops)."""
    if not msg.from_user:
        return False
    if not msg.from_user.is_bot:
        return False
    sender = (msg.from_user.username or "").lower()
    return sender == my_username.lower()


def is_from_other_bot(msg, my_username: str) -> bool:
    """True when the message was sent by a different bot account."""
    if not msg.from_user or not msg.from_user.is_bot:
        return False
    sender = (msg.from_user.username or "").lower()
    return bool(sender) and sender != my_username.lower()


def mentions_bot(msg, bot_username: str) -> bool:
    """True when the message's text contains an @mention entity matching bot_username."""
    target = "@" + bot_username.lower()
    if not msg.text:
        return False
    try:
        entities = msg.parse_entities(["mention"])
    except Exception:
        return False
    for entity, text in entities.items():
        if getattr(entity, "type", None) == "mention" and text.lower() == target:
            return True
    return False


def is_reply_to_bot(msg, bot_username: str) -> bool:
    """True when the message is a reply to an earlier message from this bot."""
    reply = getattr(msg, "reply_to_message", None)
    if not reply or not reply.from_user:
        return False
    sender = (reply.from_user.username or "").lower()
    return sender == bot_username.lower()


def is_directed_at_me(msg, my_username: str) -> bool:
    """Top-level decision: treat the message as addressed to this bot."""
    return mentions_bot(msg, my_username) or is_reply_to_bot(msg, my_username)
