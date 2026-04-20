"""Pure functions for deciding whether a group-chat message is directed at this bot.

No transport-specific dependencies — takes an IncomingMessage and returns bools.

One exception: `is_reply_to_bot` uses the `msg.native` escape hatch to read
reply_to_message.from_user.username, because MessageRef doesn't carry sender
info. Documented scope limit for spec #0a.
"""

from __future__ import annotations

import re

from .transport import IncomingMessage

_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")


def extract_mentions(text: str) -> list[str]:
    """Return lowercased `@handle` mentions from free text, without the leading '@'."""
    if not text:
        return []
    return [m.lower() for m in _MENTION_RE.findall(text)]


def is_from_self(msg: IncomingMessage, my_username: str) -> bool:
    """True when the message was sent by this bot itself (prevents self-reply loops)."""
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return sender == my_username.lower()


def is_from_other_bot(msg: IncomingMessage, my_username: str) -> bool:
    """True when the message was sent by a different bot account.

    Note: a relayed bot-to-bot message (msg.is_relayed_bot_to_bot=True) has
    sender=trusted user, so this check returns False for relays. Call sites
    that care about bot-to-bot semantics should also check
    `msg.is_relayed_bot_to_bot`.
    """
    if not msg.sender.is_bot:
        return False
    sender = (msg.sender.handle or "").lower()
    return bool(sender) and sender != my_username.lower()


def mentions_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message text mentions this bot via `@handle`."""
    target = bot_username.lower()
    return target in extract_mentions(msg.text)


def is_reply_to_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message is a reply to an earlier message from this bot.

    Uses the `native` escape hatch — MessageRef doesn't carry sender info.
    Future work (not #0a): MessageRef.sender: Identity | None.
    """
    reply = msg.reply_to
    if reply is None or msg.native is None:
        return False
    native_reply = getattr(msg.native, "reply_to_message", None)
    if native_reply is None:
        return False
    from_user = getattr(native_reply, "from_user", None)
    if from_user is None:
        return False
    sender = (getattr(from_user, "username", "") or "").lower()
    return sender == bot_username.lower()


def is_directed_at_me(msg: IncomingMessage, my_username: str) -> bool:
    """Top-level decision: treat the message as addressed to this bot.

    An explicit @mention always wins. A reply to this bot's prior message only
    counts when the user did NOT @mention anyone else — otherwise replying to
    bot A while pinging bot B would wake both A and B.
    """
    if mentions_bot(msg, my_username):
        return True
    if extract_mentions(msg.text):
        return False
    return is_reply_to_bot(msg, my_username)
