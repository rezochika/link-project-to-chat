"""Telethon-based bot-to-bot relay for dual-agent teams.

Telegram's Bot API does not deliver messages between bots in groups. Without a
relay, the manager bot can @mention the dev bot all day and the dev bot will
never see it. This module watches a team's group chat via the trusted user's
Telethon session and — whenever a team bot posts a message that mentions
another team bot — reposts the same text as the trusted user so the peer bot
receives it through the normal user-message path.

The relay owns the loop guard: because relayed messages appear to come from the
trusted user, the per-bot round counter in ProjectBot sees `is_bot=False` and
never increments. The relay is the single choke point for bot-to-bot traffic,
so it counts consecutive forwards here. When the cap is reached, the relay
stops forwarding (silently drops bot messages) and posts a one-time "paused"
notice. Any non-bot message in the group — other than the relay's own echoes —
resets the counter and clears the halt.

The relay also deletes each forward after the peer bot answers (event-driven)
so the group chat is not cluttered with duplicates. A fallback timer deletes
forwards whose peer never responded (bot crashed, error, end of task).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any

try:
    from telethon import events
    from telethon.tl.types import MessageEntityMention, MessageEntityMentionName
except ImportError:
    events = None  # type: ignore[assignment]
    MessageEntityMention = None  # type: ignore[assignment, misc]
    MessageEntityMentionName = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)


_EDIT_DEBOUNCE_SECONDS = 6.0
# Halt the relay if `_MAX_CONSECUTIVE_BOT_RELAYS` forwards land within
# `_ROUND_WINDOW_SECONDS`. The raw-count halt used previously treated a
# long legitimate multi-round delegation the same as a fast ping-pong; a
# time-windowed count distinguishes the two because real work has tool
# calls between messages (10s+ minimum) while loops fire back-to-back.
_MAX_CONSECUTIVE_BOT_RELAYS = 10
_ROUND_WINDOW_SECONDS = 120.0
_FALLBACK_DELETE_SECONDS = 60.0
# Telegram splits bot messages longer than 4096 chars into separate parts,
# each delivered as its own NewMessage event. Without coalescing, each part
# becomes an independent forward and spawns its own task in the peer bot's
# queue (the garbled-parallel-output failure mode). Parts share the same
# sender and reply_to_message_id; we buffer by (sender, reply_to) for this
# many seconds before forwarding the combined text once.
_COALESCE_WINDOW_SECONDS = 3.0

# Shared with transport/telegram.py's _RELAY_PREFIX_RE — keep these in sync.
# Telegram bot usernames are constrained to [A-Za-z][A-Za-z0-9_]*.
_RELAY_HANDLE_PATTERN = r"[A-Za-z][A-Za-z0-9_]*"


# Messages whose body (after stripping the leading @peer mention) matches one
# of these patterns are pure acknowledgments and are never forwarded by the
# relay. Echoing acks back and forth is the root shape of a ping-pong loop:
# stopping them here prevents the loop from starting, regardless of whether
# Claude decides to hand the work off or the halt cap eventually trips.
_ACK_EMOJI_ONLY = re.compile(r"^\s*[\U0001F44D\U0001F44C\u2705\U0001F197\u2714\U0001F64F\U0001F64C]+\s*$")
_ACK_WORDS = re.compile(
    r"^(?:"
    r"ok|okay|yes|no|sure|yep|nope|ack|acknowledged|noted|understood|"
    r"agreed|agree|got\s?it|gotcha|roger|roger\s?that|will\s?do|"
    r"standing\s?by|waiting|ready|confirmed|received|copy|copy\s?that|"
    r"thanks|thank\s?you|np|no\s?problem|welcome|good|great|alright|done|fine"
    r")[\s.!?\U0001F44D\u2705]*$",
    re.IGNORECASE,
)


def _is_ack_only(body: str) -> bool:
    """True when `body` is a pure acknowledgment with no actionable content.

    Used by the relay to avoid forwarding ack-only bot messages: the classic
    ping-pong shape is "@peer ok" → "@peer agreed" → "@peer 👍" where each bot
    answers the peer's ack with another ack. Dropping these on the relay side
    is a hard circuit-breaker: the peer never receives it, so nothing triggers
    a response, so the loop never starts.
    """
    stripped = body.strip()
    if not stripped:
        return True
    if _ACK_EMOJI_ONLY.match(stripped):
        return True
    return bool(_ACK_WORDS.match(stripped))


def _body_without_mention(text: str, peer: str) -> str:
    """Strip the first `@peer` occurrence (case-insensitive) and return the rest.

    Used to tell "message has real content" apart from "message is still just the
    peer @mention on its own line." Matching is case-insensitive and also tolerates
    a bare @ with any casing.
    """
    if not text or not peer:
        return text.strip()
    lower = text.lower()
    needle = f"@{peer.lower()}"
    idx = lower.find(needle)
    if idx < 0:
        return text.strip()
    return (text[:idx] + text[idx + len(needle):]).strip()


def _normalize_mention_spacing(text: str, peer: str) -> str:
    """Ensure the first `@peer` is followed by a blank line.

    Bot messages rendered via Telegram HTML (<pre>, <code>, etc.) can end up
    with the @mention glued to the next token in the plain-text representation
    that Telethon reads back. When the relay re-posts that plain text, the
    concatenation turns `@peer` + `hash` into a single malformed handle. Force
    a `\\n\\n` separator so the re-sent message keeps the mention on its own
    line regardless of the source formatting.
    """
    if not text or not peer:
        return text
    lower = text.lower()
    needle = f"@{peer.lower()}"
    idx = lower.find(needle)
    if idx < 0:
        return text
    end = idx + len(needle)
    head = text[:end]
    tail = text[end:].lstrip()
    if not tail:
        return head
    return f"{head}\n\n{tail}"


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


class _CoalescePending:
    """Buffer for consecutive bot messages that share a reply_to target.

    Used by TeamRelay to merge split-message parts (and any other rapid-fire
    multi-part posts) into a single forward, so the peer bot sees one logical
    delegation rather than N independent tasks.
    """

    __slots__ = ("parts", "timer")

    def __init__(self) -> None:
        self.parts: list[tuple[int, str]] = []  # (msg_id, text)
        self.timer: asyncio.Task | None = None


class TeamRelay:
    """Watches one team's group chat and relays bot-to-bot handoffs."""

    def __init__(
        self,
        client: Any,
        team_name: str,
        group_chat_id: int,
        bot_usernames: set[str],
        *,
        max_consecutive_bot_relays: int = _MAX_CONSECUTIVE_BOT_RELAYS,
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
        # Loop guard: timestamps of recent bot-to-bot forwards. Halts when
        # `_max_rounds` of them land within `_ROUND_WINDOW_SECONDS`.
        self._max_rounds = max_consecutive_bot_relays
        self._round_times: deque[float] = deque()
        self._halted = False
        # Telegram message IDs the relay itself has sent (forwards + halt
        # notices). Needed because Telethon delivers our own sends back as
        # NewMessage events with `is_bot=False`, which would otherwise look
        # like user activity and reset the loop guard.
        self._own_relay_ids: set[int] = set()
        # Event-driven auto-delete: `_pending_deletes[sent_id] = peer_username`.
        # When that peer posts anything, we delete the relayed `sent_id`.
        # A fallback timer in `_pending_delete_timers` deletes after a timeout
        # if the peer never responds.
        self._pending_deletes: dict[int, str] = {}
        self._pending_delete_timers: dict[int, asyncio.Task] = {}
        # Coalesce buffer keyed by (sender_username, reply_to_msg_id). See
        # `_COALESCE_WINDOW_SECONDS` for rationale.
        self._coalesce_pending: dict[tuple[str, int | None], _CoalescePending] = {}

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
        for pending in list(self._coalesce_pending.values()):
            if pending.timer is not None and not pending.timer.done():
                pending.timer.cancel()
        self._coalesce_pending.clear()
        for task in list(self._pending_delete_timers.values()):
            task.cancel()
        self._pending_delete_timers.clear()
        self._pending_deletes.clear()
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
        # Ignore our own relay posts bouncing back as events.
        if msg_id is not None and msg_id in self._own_relay_ids:
            return
        if msg_id in self._relayed_ids:
            return  # already relayed this message once
        sender = await event.get_sender()
        if sender is None:
            return
        if not getattr(sender, "bot", False):
            # Non-bot activity in the group (not our echo) — user is engaged,
            # so clear the loop guard. Only fresh messages count; edits may
            # just be the user cleaning up a prior message.
            if not is_edit:
                self._round_times.clear()
                self._halted = False
            return
        sender_username = (getattr(sender, "username", "") or "").lower()
        if sender_username not in self._bot_usernames:
            return
        # A team bot posted — if we had a pending relay-delete waiting for
        # this exact peer to respond, fire it now (only on NewMessage to
        # avoid firing on every streaming edit of the response).
        if not is_edit:
            await self._delete_pending_for_peer(sender_username)
        text = msg.message or ""
        reply_to = getattr(msg, "reply_to_msg_id", None)

        # Continuation of a coalescing message: append regardless of whether
        # this chunk carries its own @peer mention. Telegram splits long bot
        # messages past 4096 chars into separate parts; usually only the first
        # part contains the @peer mention.
        if not is_edit:
            coalesce_key = (sender_username, reply_to)
            pending = self._coalesce_pending.get(coalesce_key)
            if pending is not None:
                if self._halted:
                    return
                pending.parts.append((msg_id, text))
                if pending.timer is not None and not pending.timer.done():
                    pending.timer.cancel()
                pending.timer = asyncio.create_task(self._coalesce_flush(coalesce_key))
                return

        peer = find_peer_mention(text, sender_username, self._bot_usernames)
        if peer is None:
            return
        if self._halted:
            return  # silently drop; user must post to resume

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
            # Start a new coalesce buffer; the timer flushes it once no more
            # parts arrive within `_COALESCE_WINDOW_SECONDS`. Single-part
            # messages still pay the delay but behave identically otherwise.
            coalesce_key = (sender_username, reply_to)
            new_pending = _CoalescePending()
            new_pending.parts.append((msg_id, text))
            new_pending.timer = asyncio.create_task(self._coalesce_flush(coalesce_key))
            self._coalesce_pending[coalesce_key] = new_pending

    async def _coalesce_flush(self, key: tuple[str, int | None]) -> None:
        """Forward the buffered parts for `key` as a single relay message.

        Called from the timer armed in `_handle_event`. If the timer was
        cancelled (another part arrived and restarted it), this coroutine
        exits silently; only the last timer's flush takes effect.
        """
        try:
            await asyncio.sleep(_COALESCE_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return
        pending = self._coalesce_pending.pop(key, None)
        if pending is None or not pending.parts:
            return
        sender_username, _ = key
        combined = "\n\n".join(text for _, text in pending.parts if text)
        first_msg_id = pending.parts[0][0]
        # Every input msg_id belongs to this one logical delegation; mark the
        # trailing parts as relayed so later edits or duplicate events can't
        # re-trigger. `_finalize_relay` marks `first_msg_id` itself.
        for mid, _ in pending.parts[1:]:
            if mid is not None:
                self._relayed_ids.add(mid)
        await self._finalize_relay(first_msg_id, sender_username, combined)

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
        peer = find_peer_mention(text, sender_username, self._bot_usernames)
        if peer is None:
            # The peer @mention disappeared during streaming (edge case).
            return
        # Skip relay while only the @mention has been written. Claude-driven
        # bots follow a system note that starts every reply with the peer
        # handle on its own line; during a tool-call pause mid-stream, the
        # debounce can fire while the body is still empty. Returning here
        # without marking `_relayed_ids` lets the next edit cycle try again
        # once real content arrives.
        if not _body_without_mention(text, peer):
            return
        await self._finalize_relay(msg_id, sender_username, text)

    async def _finalize_relay(self, msg_id: int | None, sender_username: str, text: str) -> None:
        if self._halted:
            return
        peer = find_peer_mention(text, sender_username, self._bot_usernames)
        if peer is not None:
            text = _normalize_mention_spacing(text, peer)
        body = _body_without_mention(text, peer) if peer else text
        if _is_ack_only(body):
            logger.info(
                "TeamRelay: dropping ack-only bot message from @%s (team=%s body=%r)",
                sender_username, self._team_name, body[:60],
            )
            if msg_id is not None:
                self._relayed_ids.add(msg_id)
            return
        sent_id = await self._relay(sender_username, text)
        if msg_id is not None:
            self._relayed_ids.add(msg_id)
        # Track this forward for event-driven auto-delete when `peer` responds.
        if sent_id is not None and peer is not None:
            self._pending_deletes[sent_id] = peer
            self._pending_delete_timers[sent_id] = asyncio.create_task(
                self._fallback_delete(sent_id)
            )
        self._record_round()
        if len(self._round_times) >= self._max_rounds and not self._halted:
            self._halted = True
            await self._send_halt_notice()

    def _record_round(self) -> None:
        """Append now() and drop entries older than the rolling window."""
        now = time.monotonic()
        self._round_times.append(now)
        cutoff = now - _ROUND_WINDOW_SECONDS
        while self._round_times and self._round_times[0] < cutoff:
            self._round_times.popleft()

    @property
    def _rounds(self) -> int:
        """Current round count within the rolling window (read-only view)."""
        return len(self._round_times)

    async def _relay(self, sender_username: str, text: str) -> int | None:
        try:
            sent = await self._client.send_message(self._group_chat_id, text)
            sent_id = getattr(sent, "id", None)
            if isinstance(sent_id, int):
                self._own_relay_ids.add(sent_id)
            logger.info(
                "Relayed bot-to-bot message: team=%s from=@%s",
                self._team_name, sender_username,
            )
            return sent_id if isinstance(sent_id, int) else None
        except Exception:
            logger.exception(
                "TeamRelay send failed (team=%s from=@%s)",
                self._team_name, sender_username,
            )
            return None

    async def _send_halt_notice(self) -> None:
        notice = (
            f"Bot-to-bot relay paused after {self._rounds} rounds within "
            f"{int(_ROUND_WINDOW_SECONDS)}s in team '{self._team_name}'. "
            f"Send any message to resume."
        )
        try:
            sent = await self._client.send_message(self._group_chat_id, notice)
            sent_id = getattr(sent, "id", None)
            if isinstance(sent_id, int):
                self._own_relay_ids.add(sent_id)
        except Exception:
            logger.exception(
                "TeamRelay halt notice send failed (team=%s)", self._team_name,
            )

    async def _delete_pending_for_peer(self, sender_username: str) -> None:
        """Delete any relay forwards that were waiting for `sender_username` to respond."""
        to_delete = [
            mid for mid, peer in self._pending_deletes.items()
            if peer == sender_username
        ]
        for mid in to_delete:
            self._pending_deletes.pop(mid, None)
            timer = self._pending_delete_timers.pop(mid, None)
            if timer is not None and not timer.done():
                timer.cancel()
            await self._delete_relay_message(mid)

    async def _delete_relay_message(self, msg_id: int) -> None:
        try:
            await self._client.delete_messages(self._group_chat_id, [msg_id])
            self._own_relay_ids.discard(msg_id)
        except Exception:
            logger.warning(
                "TeamRelay delete failed (mid=%s team=%s)",
                msg_id, self._team_name, exc_info=True,
            )

    async def _fallback_delete(self, msg_id: int) -> None:
        try:
            await asyncio.sleep(_FALLBACK_DELETE_SECONDS)
        except asyncio.CancelledError:
            return
        if msg_id not in self._pending_deletes:
            return  # peer already responded; the event-driven path handled it
        self._pending_deletes.pop(msg_id, None)
        self._pending_delete_timers.pop(msg_id, None)
        await self._delete_relay_message(msg_id)
