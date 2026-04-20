"""Telethon-based bot-to-bot relay for dual-agent teams.

Telegram's Bot API does not deliver messages between bots in groups. Without a
relay, the manager bot can @mention the dev bot all day and the dev bot will
never see it. This module watches a team's group chat via the trusted user's
Telethon session and — whenever a team bot posts a message that mentions
another team bot — reposts the same text as the trusted user so the peer bot
receives it through the normal user-message path.

The relay does NOT try to preserve the bot-to-bot round counter that lives on
each ProjectBot. Since relayed messages appear to come from the trusted user,
each relay resets the peer's counter. This is documented as a v1 tradeoff —
the user can still `/halt` a runaway loop manually.
"""
from __future__ import annotations

import logging
from typing import Any

try:
    from telethon import events
    from telethon.tl.types import MessageEntityMention, MessageEntityMentionName
except ImportError:
    events = None  # type: ignore[assignment]
    MessageEntityMention = None  # type: ignore[assignment, misc]
    MessageEntityMentionName = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)


_RELAY_PREFIX = "[auto-relay from @"


def is_relayed_text(text: str) -> bool:
    """Return True if this text was produced by TeamRelay, so downstream bots
    can avoid re-relaying it (a loop-breaker).
    """
    return text.startswith(_RELAY_PREFIX)


def find_peer_mention(text: str, self_username: str, team_bot_usernames: set[str]) -> str | None:
    """Return the first peer bot's @username mentioned in `text`, else None.

    `team_bot_usernames` is the full set of bots on the team (lowercased, no @).
    `self_username` is excluded from the match.
    """
    if not text:
        return None
    lower = text.lower()
    for peer in team_bot_usernames:
        if peer == self_username:
            continue
        if f"@{peer}" in lower:
            return peer
    return None


class TeamRelay:
    """Watches one team's group chat and relays bot-to-bot handoffs."""

    def __init__(
        self,
        client: Any,
        team_name: str,
        group_chat_id: int,
        bot_usernames: set[str],
    ) -> None:
        if events is None:
            raise ImportError(
                "telethon is required for TeamRelay. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._client = client
        self._team_name = team_name
        self._group_chat_id = group_chat_id
        self._bot_usernames = {u.lower().lstrip("@") for u in bot_usernames if u}
        self._handler = None

    async def start(self) -> None:
        """Register the NewMessage handler on the shared Telethon client."""
        if self._handler is not None:
            return  # already running
        self._handler = self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=self._group_chat_id),
        )
        logger.info(
            "TeamRelay started: team=%s chat_id=%s bots=%s",
            self._team_name, self._group_chat_id, sorted(self._bot_usernames),
        )

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                self._client.remove_event_handler(self._on_new_message)
            except Exception:
                logger.warning("Removing TeamRelay handler failed", exc_info=True)
            self._handler = None

    async def _on_new_message(self, event: Any) -> None:
        """Route one new group message: relay if bot-to-bot, otherwise skip."""
        try:
            msg = event.message
            text = msg.message or ""
            # Guard against infinite relay loops.
            if is_relayed_text(text):
                return
            sender = await event.get_sender()
            if sender is None or not getattr(sender, "bot", False):
                return  # only relay bot messages (user messages don't need relay)
            sender_username = (getattr(sender, "username", "") or "").lower()
            if sender_username not in self._bot_usernames:
                return  # not one of our team bots
            peer = find_peer_mention(text, sender_username, self._bot_usernames)
            if peer is None:
                return  # not addressed to a peer
            await self._relay(sender_username, text)
        except Exception:
            logger.exception(
                "TeamRelay handler failed (team=%s chat_id=%s)",
                self._team_name, self._group_chat_id,
            )

    async def _relay(self, sender_username: str, text: str) -> None:
        relayed_text = f"{_RELAY_PREFIX}{sender_username}]\n\n{text}"
        try:
            await self._client.send_message(self._group_chat_id, relayed_text)
            logger.info(
                "Relayed bot-to-bot message: team=%s from=@%s",
                self._team_name, sender_username,
            )
        except Exception:
            logger.exception(
                "TeamRelay send failed (team=%s from=@%s)",
                self._team_name, sender_username,
            )
