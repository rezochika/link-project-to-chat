"""Pure functions for deciding whether a group-chat message is directed at this bot.

No transport-specific dependencies — takes an IncomingMessage and returns bools.
`is_reply_to_bot` reads `msg.reply_to_sender` (populated by every transport
that has threading semantics); no `msg.native` lookup required.
"""

from __future__ import annotations

import re

from .transport import IncomingMessage

_MENTION_RE = re.compile(r"(?:^|[^A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]*)")


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
    """True if message mentions this bot.

    Prefers structured `IncomingMessage.mentions` (Discord/Slack/Web); falls back
    to regex text parsing only when `mentions` is empty (Telegram legacy path).
    """
    if msg.mentions:
        target = bot_username.lower()
        return any((m.handle or "").lower() == target for m in msg.mentions)
    return bot_username.lower() in extract_mentions(msg.text)


def mentions_bot_by_id(msg: IncomingMessage, transport_id: str, native_id: str) -> bool:
    """True if `msg.mentions` contains an identity with the given transport_id + native_id."""
    return any(
        m.transport_id == transport_id and m.native_id == native_id
        for m in msg.mentions
    )


def is_reply_to_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True when the message is a reply to an earlier message from this bot.

    Uses the portable `reply_to_sender` field — no transport-native lookup.
    """
    if msg.reply_to_sender is None:
        return False
    sender = (msg.reply_to_sender.handle or "").lower()
    return sender == bot_username.lower()


def is_directed_at_me(msg: IncomingMessage, my_username: str) -> bool:
    """Top-level decision: treat the message as addressed to this bot.

    An explicit @mention always wins (structured `mentions` preferred over
    regex text). A reply to this bot's prior message only counts when the user
    did NOT @mention anyone else — otherwise replying to bot A while pinging
    bot B would wake both A and B.
    """
    if mentions_bot(msg, my_username):
        return True
    if msg.mentions or extract_mentions(msg.text):
        return False
    return is_reply_to_bot(msg, my_username)
