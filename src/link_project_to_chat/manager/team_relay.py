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

import asyncio
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


_RELAY_PREFIX = "[auto-relay from "
_EDIT_DEBOUNCE_SECONDS = 3.0


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
        self._edit_handler = None
        # Message IDs we have already relayed, so edits to the same message do
        # not trigger a second relay.
        self._relayed_ids: set[int] = set()
        # Pending debounced relay tasks, keyed by message_id. The livestream
        # emits many edits per reply; we only relay once the stream goes quiet.
        self._debounce_tasks: dict[int, asyncio.Task] = {}

    async def start(self) -> None:
        """Register NewMessage + MessageEdited handlers on the shared Telethon client.

        We deliberately do NOT pass ``chats=`` to the event filters. Telethon
        resolves ``chats`` against its entity cache at handler-registration
        time; if the supergroup entity is not yet cached in the session, the
        filter silently matches nothing. Manual chat-ID filtering inside the
        handlers is equivalent and always works.

        We listen to MessageEdited in addition to NewMessage because the bots'
        livestream sends an empty placeholder first and then streams the reply
        content via edit_message_text — the @peer mention only appears in the
        edits, not the original send.
        """
        if self._handler is not None:
            return  # already running
        self._handler = self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(),
        )
        self._edit_handler = self._client.add_event_handler(
            self._on_message_edited,
            events.MessageEdited(),
        )
        logger.info(
            "TeamRelay started: team=%s chat_id=%s bots=%s",
            self._team_name, self._group_chat_id, sorted(self._bot_usernames),
        )

    async def stop(self) -> None:
        for task in list(self._debounce_tasks.values()):
            task.cancel()
        self._debounce_tasks.clear()
        if self._handler is not None:
            try:
                self._client.remove_event_handler(self._on_new_message)
            except Exception:
                logger.warning("Removing TeamRelay handler failed", exc_info=True)
            self._handler = None
        if self._edit_handler is not None:
            try:
                self._client.remove_event_handler(self._on_message_edited)
            except Exception:
                logger.warning("Removing TeamRelay edit handler failed", exc_info=True)
            self._edit_handler = None

    async def _on_new_message(self, event: Any) -> None:
        """Route one new group message: relay if bot-to-bot, otherwise skip."""
        try:
            await self._handle_event(event, is_edit=False)
        except Exception:
            logger.exception(
                "TeamRelay handler failed (team=%s chat_id=%s)",
                self._team_name, self._group_chat_id,
            )

    async def _on_message_edited(self, event: Any) -> None:
        """Route one edited group message: relay once per message (debounced)."""
        try:
            await self._handle_event(event, is_edit=True)
        except Exception:
            logger.exception(
                "TeamRelay edit handler failed (team=%s chat_id=%s)",
                self._team_name, self._group_chat_id,
            )

    async def _handle_event(self, event: Any, *, is_edit: bool) -> None:
        msg = event.message
        if getattr(msg, "chat_id", None) != self._group_chat_id:
            return
        msg_id = getattr(msg, "id", None)
        if msg_id in self._relayed_ids:
            return  # already relayed this message once
        text = msg.message or ""
        if is_relayed_text(text):
            return
        sender = await event.get_sender()
        if sender is None or not getattr(sender, "bot", False):
            return
        sender_username = (getattr(sender, "username", "") or "").lower()
        if sender_username not in self._bot_usernames:
            return
        peer = find_peer_mention(text, sender_username, self._bot_usernames)
        if peer is None:
            return

        if is_edit:
            # Edits come in rapid-fire during streaming. Debounce: cancel any
            # previous pending relay for this message and schedule a fresh one.
            existing = self._debounce_tasks.get(msg_id)
            if existing is not None and not existing.done():
                existing.cancel()
            self._debounce_tasks[msg_id] = asyncio.create_task(
                self._debounced_relay(msg_id, sender_username)
            )
        else:
            await self._finalize_relay(msg_id, sender_username, text)

    async def _debounced_relay(self, msg_id: int, sender_username: str) -> None:
        try:
            await asyncio.sleep(_EDIT_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            # Re-fetch the message to relay the latest text, not the snapshot
            # that was current when this debounce started.
            current = await self._client.get_messages(self._group_chat_id, ids=msg_id)
            text = getattr(current, "message", "") or ""
        except Exception:
            logger.exception(
                "TeamRelay get_messages failed during debounce (mid=%s)", msg_id,
            )
            self._debounce_tasks.pop(msg_id, None)
            return
        self._debounce_tasks.pop(msg_id, None)
        if msg_id in self._relayed_ids:
            return
        if is_relayed_text(text):
            return
        if find_peer_mention(text, sender_username, self._bot_usernames) is None:
            # The peer @mention disappeared during streaming (edge case).
            return
        await self._finalize_relay(msg_id, sender_username, text)

    async def _finalize_relay(self, msg_id: int | None, sender_username: str, text: str) -> None:
        await self._relay(sender_username, text)
        if msg_id is not None:
            self._relayed_ids.add(msg_id)

    async def _relay(self, sender_username: str, text: str) -> None:
        # NOTE: the prefix deliberately writes the sender's username WITHOUT a
        # leading "@". Telegram parses any `@handle` in plain message text as a
        # mention entity, and the peer bots' routing treats any mention of
        # themselves as "addressed to me" — so an `@sender` in the prefix used
        # to feed the sender back its own message, triggering a self-reply loop.
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
