from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _fast_coalesce(monkeypatch):
    """Make the coalesce window instant so tests don't wait 3 real seconds.

    Each NewMessage routed through the relay now enters a coalesce buffer that
    normally waits `_COALESCE_WINDOW_SECONDS` before forwarding. Tests below
    use `_dispatch()` to await the flush; this fixture just shrinks the window
    to zero so the flush runs as soon as the event loop yields.
    """
    import link_project_to_chat.transport._telegram_relay as tr

    monkeypatch.setattr(tr, "_COALESCE_WINDOW_SECONDS", 0.0)


async def _dispatch(relay, event):
    """Route `event` through the relay and let any coalesce flush complete.

    The relay schedules the forward as a task; without waiting for that task,
    assertions on `send_message` would race with the timer.
    """
    await relay._on_new_message(event)
    while relay._coalesce_pending:
        timers = [
            p.timer for p in list(relay._coalesce_pending.values())
            if p.timer is not None and not p.timer.done()
        ]
        if not timers:
            break
        for t in timers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# --- pure helpers ---


def test_find_peer_mention_returns_peer_when_addressed():
    from link_project_to_chat.transport._telegram_relay import find_peer_mention

    text = "@acme_dev_bot please implement models.py"
    result = find_peer_mention(text, self_username="acme_mgr_bot", team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"})
    assert result == "acme_dev_bot"


def test_find_peer_mention_case_insensitive():
    from link_project_to_chat.transport._telegram_relay import find_peer_mention

    result = find_peer_mention(
        "Hey @ACME_Dev_Bot ready to go",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result == "acme_dev_bot"


def test_find_peer_mention_returns_none_when_self_mention():
    from link_project_to_chat.transport._telegram_relay import find_peer_mention

    result = find_peer_mention(
        "I, @acme_mgr_bot, have finished my review",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result is None


def test_find_peer_mention_returns_none_when_no_peer_mention():
    from link_project_to_chat.transport._telegram_relay import find_peer_mention

    result = find_peer_mention(
        "Here is my update",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result is None


# --- TeamRelay routing ---


def _fake_sender(username: str, is_bot: bool):
    s = MagicMock()
    s.username = username
    s.bot = is_bot
    return s


async def _mk_event(
    text: str,
    sender_username: str,
    sender_is_bot: bool,
    chat_id: int | None = -100_111,
    msg_id: int | None = None,
    reply_to: int | None = None,
):
    event = MagicMock()
    event.message = MagicMock()
    event.message.message = text
    event.message.chat_id = chat_id
    event.message.id = msg_id
    # Pin reply_to_msg_id explicitly; without this, MagicMock auto-creates a
    # fresh child mock per attribute access, which would make every coalesce
    # key unique and defeat the (sender, reply_to) buffering logic.
    event.message.reply_to_msg_id = reply_to
    event.get_sender = AsyncMock(return_value=_fake_sender(sender_username, sender_is_bot))
    return event


def _mk_client_with_ids(start_id: int = 1000):
    """Client stub whose send_message returns a mock with a fresh incremental .id."""
    client = MagicMock()
    client.add_event_handler = MagicMock(return_value="handler")
    counter = [start_id]

    async def _send(*args, **kwargs):
        sent = MagicMock()
        sent.id = counter[0]
        counter[0] += 1
        return sent

    client.send_message = AsyncMock(side_effect=_send)
    client.delete_messages = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_relay_ignores_user_messages():
    """A real user's message is not relayed — only bot-to-bot traffic needs the bridge."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = MagicMock()
    client.add_event_handler = MagicMock(return_value="handler")
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement X",
        sender_username="rezoc666",
        sender_is_bot=False,
    )
    await _dispatch(relay,event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_ignores_bot_messages_not_addressing_peer():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "PRD is drafted — standby for handoff",  # no @ mention
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await _dispatch(relay,event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_forwards_bot_to_bot_handoff():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement src/models.py per docs/architecture.md §2",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await _dispatch(relay,event)
    client.send_message.assert_awaited_once()
    args, _ = client.send_message.call_args
    chat_id, text = args
    assert chat_id == -100_111
    # The relay forwards the raw text — no "[auto-relay from ...]" prefix.
    assert text.startswith("@acme_dev_bot")
    assert "src/models.py" in text


@pytest.mark.asyncio
async def test_relay_ignores_message_from_unknown_bot():
    """A third-party bot's message shouldn't trigger a relay even if it @mentions a team bot."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot try this",
        sender_username="random_3rd_party_bot",
        sender_is_bot=True,
    )
    await _dispatch(relay,event)
    client.send_message.assert_not_called()


# --- loop guard: consecutive-rounds cap + halt/resume ---


@pytest.mark.asyncio
async def test_relay_halts_after_max_consecutive_rounds():
    """After `max_consecutive_bot_relays` forwards, the next bot-to-bot message is NOT forwarded."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )
    # Alternate bot A → bot B three times (cap=3): all three should forward.
    for i, sender in enumerate(["acme_mgr_bot", "acme_dev_bot", "acme_mgr_bot"]):
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        event = await _mk_event(f"@{peer} round {i}", sender_username=sender, sender_is_bot=True, msg_id=100 + i)
        await _dispatch(relay,event)
    # At this point cap is hit; forwards so far = 3, plus exactly one halt notice.
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    notices = [c for c in client.send_message.await_args_list if "paused" in c.args[1].lower()]
    assert len(forwards) == 3
    assert len(notices) == 1
    # A fourth bot-to-bot message must NOT produce another forward.
    event = await _mk_event("@acme_dev_bot round 4", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=104)
    await _dispatch(relay,event)
    forwards_after = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards_after) == 3  # still three; halted


@pytest.mark.asyncio
async def test_relay_halt_notice_is_sent_only_once():
    """Once halted, subsequent bot-to-bot messages must NOT emit another halt notice."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=2,
    )
    # Trip the cap
    for i in range(2):
        sender = "acme_mgr_bot" if i % 2 == 0 else "acme_dev_bot"
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        ev = await _mk_event(f"@{peer} x{i}", sender_username=sender, sender_is_bot=True, msg_id=200 + i)
        await _dispatch(relay,ev)
    # Another bot message after halt
    ev = await _mk_event("@acme_dev_bot still going", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=210)
    await _dispatch(relay,ev)
    notices = [c for c in client.send_message.await_args_list if "paused" in c.args[1].lower()]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_relay_resumes_when_user_posts_in_group():
    """A non-bot message in the group clears halt and resets the counter."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=2,
    )
    # Halt the relay
    for i in range(2):
        sender = "acme_mgr_bot" if i % 2 == 0 else "acme_dev_bot"
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        ev = await _mk_event(f"@{peer} x{i}", sender_username=sender, sender_is_bot=True, msg_id=300 + i)
        await _dispatch(relay,ev)
    assert relay._halted is True
    # Trusted user posts
    user_ev = await _mk_event("ping back online", sender_username="rezoc666", sender_is_bot=False, msg_id=310)
    await _dispatch(relay,user_ev)
    assert relay._halted is False
    assert relay._rounds == 0
    # Now a bot message should forward again
    ev = await _mk_event("@acme_dev_bot resumed", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=311)
    before = client.send_message.await_count
    await _dispatch(relay,ev)
    # A new send happened, and the counter started fresh.
    assert client.send_message.await_count == before + 1
    assert relay._rounds == 1


@pytest.mark.asyncio
async def test_relay_does_not_reset_on_its_own_echoed_posts():
    """The relay's own posts bounce back as NewMessage events (Telethon's own client).

    If we treated those as user activity, the counter would reset every forward
    and the cap would never trip. Verify: after a forward, feeding back the
    relay's own post does NOT reset the counter.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=500)
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=2,
    )
    # Forward one bot message (consumes send_message → sent.id=500)
    ev1 = await _mk_event("@acme_dev_bot one", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=400)
    await _dispatch(relay,ev1)
    # Simulate the same message bouncing back as a NewMessage from the trusted user
    # (this is what Telethon will deliver for the relay's own send).
    echo = await _mk_event("@acme_dev_bot one", sender_username="rezoc666", sender_is_bot=False, msg_id=500)
    await _dispatch(relay,echo)
    # Counter must not have reset.
    assert relay._rounds == 1


# --- event-driven auto-delete of relay forwards ---


@pytest.mark.asyncio
async def test_relay_deletes_forward_when_peer_bot_responds():
    """When the peer bot posts its response, the earlier relay forward is deleted."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=700)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    # Bot A forwards a handoff to bot B (relay sends as user → sent.id=700)
    ev_a = await _mk_event("@acme_dev_bot please X", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=600)
    await _dispatch(relay,ev_a)
    client.delete_messages.assert_not_called()  # not yet — peer hasn't responded
    # Bot B responds (no relay needed for this test, but forces the delete path)
    ev_b = await _mk_event("@acme_mgr_bot done", sender_username="acme_dev_bot", sender_is_bot=True, msg_id=601)
    await _dispatch(relay,ev_b)
    # The relay forward (sent.id=700) must now be deleted.
    client.delete_messages.assert_awaited()
    call = client.delete_messages.await_args_list[0]
    chat_id, ids = call.args
    assert chat_id == -100_111
    assert 700 in list(ids)


# --- ack-only suppression + default cap ---


def test_is_ack_only_detects_pure_acks():
    from link_project_to_chat.transport._telegram_relay import _is_ack_only

    assert _is_ack_only("")
    assert _is_ack_only("   ")
    assert _is_ack_only("ok")
    assert _is_ack_only("OK.")
    assert _is_ack_only("Okay!")
    assert _is_ack_only("agreed")
    assert _is_ack_only("Agreed.")
    assert _is_ack_only("👍")
    assert _is_ack_only("👍👍")
    assert _is_ack_only("understood")
    assert _is_ack_only("Understood!")
    assert _is_ack_only("standing by")
    assert _is_ack_only("Standing by.")
    assert _is_ack_only("got it")
    assert _is_ack_only("noted")
    assert _is_ack_only("Roger that.")
    assert _is_ack_only("confirmed")


def test_is_ack_only_lets_substance_through():
    from link_project_to_chat.transport._telegram_relay import _is_ack_only

    assert not _is_ack_only("Please implement src/models.py")
    assert not _is_ack_only("Done — PR #123 opened")
    assert not _is_ack_only("Yes, let's proceed with option B")
    assert not _is_ack_only("OK, but can you clarify the scope first?")
    assert not _is_ack_only("I see the bug — it's in foo.py line 42")


@pytest.mark.asyncio
async def test_relay_skips_ack_only_bot_messages():
    """Ack-only messages must not be relayed — that's how ping-pong loops start."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    for i, text in enumerate([
        "@acme_dev_bot ok",
        "@acme_dev_bot agreed.",
        "@acme_dev_bot 👍",
        "@acme_dev_bot standing by",
        "@acme_dev_bot Understood!",
    ]):
        ev = await _mk_event(text, sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=1100 + i)
        await _dispatch(relay,ev)
    # Not a single forward should have happened.
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@acme_dev_bot")]
    assert forwards == []
    # And the counter must not have moved — these aren't consuming the budget.
    assert relay._rounds == 0


@pytest.mark.asyncio
async def test_relay_still_forwards_substantive_messages():
    """The ack filter must not swallow real handoffs."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    ev = await _mk_event(
        "@acme_dev_bot Please implement src/models.py per docs/spec.md §2",
        sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=1200,
    )
    await _dispatch(relay,ev)
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@acme_dev_bot")]
    assert len(forwards) == 1
    assert relay._rounds == 1


def test_default_loop_guard_constants():
    """Tightened from 10/120s to 6/180s after the 2026-04-27 quota incident
    showed sustained loops at 5-7 forwards per 120s — under the old cap. The
    wider window plus lower count catches slower loops without stopping
    legitimate multi-round delegations (where tool calls space rounds 10s+
    apart).
    """
    from link_project_to_chat.transport._telegram_relay import (
        _MAX_CONSECUTIVE_BOT_RELAYS,
        _ROUND_WINDOW_SECONDS,
    )

    assert _MAX_CONSECUTIVE_BOT_RELAYS == 6
    assert _ROUND_WINDOW_SECONDS == 180.0


def test_default_same_author_streak_cap():
    """Same-author streak guard: 3 consecutive forwards from the same sender
    halt regardless of the time-window count. Catches the 'manager re-issues
    same dispatch 3x without dev replying' shape from the 2026-04-27 incident,
    which never tripped the count/window cap because legitimate dev tool-call
    pauses kept the rate low."""
    from link_project_to_chat.transport._telegram_relay import (
        _MAX_SAME_AUTHOR_STREAK,
    )

    assert _MAX_SAME_AUTHOR_STREAK == 3


@pytest.mark.asyncio
async def test_relay_halts_on_same_author_streak():
    """Three forwards in a row from the same sender halt the relay even if
    the count/window cap hasn't tripped. This is the loop shape the
    2026-04-27 lptc team incident showed: manager re-dispatched the same
    batch 3+ times before dev's relay-visible response landed."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        # Use a high count cap so the streak guard is the only thing that can halt.
        max_consecutive_bot_relays=999,
    )
    for i in range(3):
        ev = await _mk_event(
            f"@acme_dev_bot dispatch {i}",
            sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=2000 + i,
        )
        await _dispatch(relay, ev)
    assert relay._halted is True
    notices = [c for c in client.send_message.await_args_list if "paused" in c.args[1].lower()]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_relay_alternating_senders_does_not_trigger_streak_halt():
    """Legitimate alternating delegation (mgr→dev→mgr→dev→...) must not halt
    on the streak guard, no matter how many rounds happen — this is what real
    work looks like."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=999,
        max_autonomous_turns=20,
    )
    senders = ["acme_mgr_bot", "acme_dev_bot"] * 5  # 10 alternating rounds
    for i, sender in enumerate(senders):
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        ev = await _mk_event(
            f"@{peer} round {i}",
            sender_username=sender, sender_is_bot=True, msg_id=3000 + i,
        )
        await _dispatch(relay, ev)
    assert relay._halted is False
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards) == 10


@pytest.mark.asyncio
async def test_relay_streak_resets_when_other_author_forwards():
    """Two same-author forwards followed by the peer is NOT a streak — the
    streak counter must reset on author change, not just on user activity."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=999,
    )
    # mgr → mgr → dev → mgr → mgr — at no point does any author hit a streak of 3
    plan = [
        ("acme_mgr_bot", "acme_dev_bot"),
        ("acme_mgr_bot", "acme_dev_bot"),
        ("acme_dev_bot", "acme_mgr_bot"),
        ("acme_mgr_bot", "acme_dev_bot"),
        ("acme_mgr_bot", "acme_dev_bot"),
    ]
    for i, (sender, peer) in enumerate(plan):
        ev = await _mk_event(
            f"@{peer} step {i}",
            sender_username=sender, sender_is_bot=True, msg_id=4000 + i,
        )
        await _dispatch(relay, ev)
    assert relay._halted is False


@pytest.mark.asyncio
async def test_relay_rounds_outside_window_are_pruned(monkeypatch):
    """A round older than `_ROUND_WINDOW_SECONDS` must not count toward the
    halt cap — real multi-round coordination spaces rounds by tool calls,
    and we want it to flow through."""
    import link_project_to_chat.transport._telegram_relay as tr
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    # Tiny window so the test can age rounds out quickly.
    monkeypatch.setattr(tr, "_ROUND_WINDOW_SECONDS", 0.05)

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )

    # 2 rounds inside the window — not yet halted.
    for i in range(2):
        sender = "acme_mgr_bot" if i % 2 == 0 else "acme_dev_bot"
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        ev = await _mk_event(f"@{peer} x{i}", sender_username=sender, sender_is_bot=True, msg_id=1300 + i)
        await _dispatch(relay, ev)
    assert relay._halted is False
    assert relay._rounds == 2

    # Let the first two rounds age out.
    await asyncio.sleep(0.08)

    # 2 more rounds — prior two are now outside the window and must not count.
    for i in range(2, 4):
        sender = "acme_mgr_bot" if i % 2 == 0 else "acme_dev_bot"
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        ev = await _mk_event(f"@{peer} x{i}", sender_username=sender, sender_is_bot=True, msg_id=1300 + i)
        await _dispatch(relay, ev)
    # Window now contains just the two fresh rounds — below the cap of 3.
    assert relay._halted is False
    assert relay._rounds == 2


@pytest.mark.asyncio
async def test_relay_fallback_deletes_forward_after_timeout(monkeypatch):
    """If the peer never responds, a fallback timer deletes the relay forward."""
    import link_project_to_chat.transport._telegram_relay as tr
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    # Shrink the fallback window to keep the test fast.
    monkeypatch.setattr(tr, "_FALLBACK_DELETE_SECONDS", 0.01)

    client = _mk_client_with_ids(start_id=900)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    ev = await _mk_event("@acme_dev_bot ping", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=800)
    await _dispatch(relay,ev)
    # Let the fallback timer fire.
    await asyncio.sleep(0.05)
    client.delete_messages.assert_awaited()
    call = client.delete_messages.await_args_list[0]
    assert 900 in list(call.args[1])


@pytest.mark.asyncio
async def test_relay_suppresses_recent_duplicate_forward():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=30_000)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    text = "@acme_dev_bot\n\nRequest changes: README quick start is broken."
    await _dispatch(relay, await _mk_event(text, "acme_mgr_bot", True, msg_id=30_100))
    await _dispatch(relay, await _mk_event("  " + text + "  \n", "acme_mgr_bot", True, msg_id=30_101))

    forwards = [
        call for call in client.send_message.await_args_list
        if call.args[1].startswith("@acme_dev_bot")
    ]
    assert len(forwards) == 1
    assert 30_101 in relay._relayed_ids


@pytest.mark.asyncio
async def test_relay_halts_before_exceeding_autonomous_turn_budget():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=31_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=999,
        max_autonomous_turns=2,
    )

    for i in range(3):
        await _dispatch(
            relay,
            await _mk_event(
                f"@acme_dev_bot batch {i}",
                "acme_mgr_bot",
                True,
                msg_id=31_100 + i,
            ),
        )

    forwards = [
        call for call in client.send_message.await_args_list
        if call.args[1].startswith("@acme_dev_bot")
    ]
    notices = [
        call for call in client.send_message.await_args_list
        if "autonomous turn budget" in call.args[1]
    ]
    assert len(forwards) == 2
    assert len(notices) == 1
    assert relay._halted is True


@pytest.mark.asyncio
async def test_relay_observes_authenticated_user_message_after_own_echo_guard():
    from link_project_to_chat.team_safety import TeamAuthority
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=32_000)
    authority = TeamAuthority(team_name="acme")
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        team_authority=authority,
        authenticated_user_id=42,
    )

    relay._own_relay_ids.add(32_100)
    own_echo = await _mk_event("--auth push", "trusted_user", False, msg_id=32_100)
    own_echo.get_sender.return_value.id = 42
    await _dispatch(relay, own_echo)
    assert authority.is_authorized("push") is False

    user_msg = await _mk_event("--auth push", "trusted_user", False, msg_id=32_101)
    user_msg.get_sender.return_value.id = 42
    relay._consecutive_bot_turns = 2
    await _dispatch(relay, user_msg)
    assert authority.is_authorized("push") is True
    assert relay._consecutive_bot_turns == 0


@pytest.mark.asyncio
async def test_relay_peer_response_clears_same_author_streak():
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=33_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=999,
        max_autonomous_turns=20,
    )

    for i in range(2):
        await _dispatch(
            relay,
            await _mk_event(f"@acme_dev_bot review {i}", "acme_mgr_bot", True, msg_id=33_100 + i),
        )
    assert relay._rounds == 2

    await _dispatch(
        relay,
        await _mk_event("Patched both items.", "acme_dev_bot", True, msg_id=33_200),
    )
    assert relay._rounds == 2
    assert relay._is_same_author_streak() is False

    await _dispatch(
        relay,
        await _mk_event("@acme_dev_bot confirm HEAD", "acme_mgr_bot", True, msg_id=33_300),
    )
    assert relay._rounds == 3
    assert relay._halted is False


# --- coalesce of split/multi-part bot messages ---


@pytest.mark.asyncio
async def test_relay_coalesces_split_bot_message_into_one_forward():
    """Telegram splits >4096-char bot messages into parts sharing reply_to.

    Only the first part usually carries the @peer mention; continuations are
    raw text. Without coalescing, each part becomes its own forward and spawns
    a separate task in the peer bot. Verify: two parts → exactly one forward.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    ev1 = await _mk_event(
        "@acme_dev_bot implement P1-1 per docs/plan.md. Start with tests...",
        sender_username="acme_mgr_bot", sender_is_bot=True,
        msg_id=10_001, reply_to=42,
    )
    await relay._on_new_message(ev1)  # don't drain yet — second part is coming
    ev2 = await _mk_event(
        "...and make sure to cover auth, then CSP in P1-3.",
        sender_username="acme_mgr_bot", sender_is_bot=True,
        msg_id=10_002, reply_to=42,
    )
    await _dispatch(relay, ev2)

    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards) == 1
    assert "implement P1-1" in forwards[0].args[1]
    assert "CSP in P1-3" in forwards[0].args[1]
    # Both input msg_ids must be recorded as relayed so late edits don't re-fire.
    assert 10_001 in relay._relayed_ids
    assert 10_002 in relay._relayed_ids


@pytest.mark.asyncio
async def test_relay_coalesces_split_when_reply_to_differs():
    """Streaming-edit splits and middleware can land sibling parts of the SAME
    logical bot reply with different reply_to values (some None, some pointing
    to the original prompt, some to an intermediate placeholder). The old
    (sender, reply_to) coalesce key dropped these into separate buckets and
    only the first part ever forwarded — see the 2026-04-27 incident where
    continuations like 'd.py (3 cases)' arrived after the @-mention head and
    were never forwarded to the peer.

    Sender-only keying means same-sender parts within the coalesce window
    combine into a single forward regardless of their reply_to."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    ev1 = await _mk_event(
        "@acme_dev_bot Batch 1 dispatch — files A, B, C",
        sender_username="acme_mgr_bot", sender_is_bot=True,
        msg_id=20_001, reply_to=42,
    )
    await relay._on_new_message(ev1)  # don't drain — second part is in flight
    ev2 = await _mk_event(
        "...continued: also touch test_xyz.py and update CHANGELOG.",
        sender_username="acme_mgr_bot", sender_is_bot=True,
        msg_id=20_002, reply_to=None,  # different from ev1's reply_to=42
    )
    await _dispatch(relay, ev2)

    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards) == 1, (
        f"split parts with different reply_to should coalesce; got {len(forwards)} forwards"
    )
    assert "Batch 1 dispatch" in forwards[0].args[1]
    assert "test_xyz.py" in forwards[0].args[1]
    # Both input msg_ids must be marked relayed so late edits don't re-fire.
    assert 20_001 in relay._relayed_ids
    assert 20_002 in relay._relayed_ids


@pytest.mark.asyncio
async def test_relay_does_not_coalesce_unrelated_reply_targets():
    """Two bot messages that reply to DIFFERENT user messages are separate
    delegations and must each produce their own forward."""
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    ev1 = await _mk_event(
        "@acme_dev_bot do task A", sender_username="acme_mgr_bot",
        sender_is_bot=True, msg_id=11_001, reply_to=100,
    )
    await _dispatch(relay, ev1)
    ev2 = await _mk_event(
        "@acme_dev_bot do task B", sender_username="acme_mgr_bot",
        sender_is_bot=True, msg_id=11_002, reply_to=101,
    )
    await _dispatch(relay, ev2)

    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards) == 2


@pytest.mark.asyncio
async def test_relay_ignores_continuation_without_prior_peer_mention():
    """A continuation-shaped message (no @peer, not a recognized split) is dropped.

    Without a prior part that opened a coalesce buffer, the relay has no way to
    know this text is a follow-on to anything — so it behaves like any other
    bot message without a peer @mention: ignored.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    ev = await _mk_event(
        "just trailing text with no peer mention",
        sender_username="acme_mgr_bot", sender_is_bot=True,
        msg_id=12_001, reply_to=200,
    )
    await _dispatch(relay, ev)
    client.send_message.assert_not_called()
