from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# --- pure helpers ---


def test_find_peer_mention_returns_peer_when_addressed():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    text = "@acme_dev_bot please implement models.py"
    result = find_peer_mention(text, self_username="acme_mgr_bot", team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"})
    assert result == "acme_dev_bot"


def test_find_peer_mention_case_insensitive():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    result = find_peer_mention(
        "Hey @ACME_Dev_Bot ready to go",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result == "acme_dev_bot"


def test_find_peer_mention_returns_none_when_self_mention():
    from link_project_to_chat.manager.team_relay import find_peer_mention

    result = find_peer_mention(
        "I, @acme_mgr_bot, have finished my review",
        self_username="acme_mgr_bot",
        team_bot_usernames={"acme_mgr_bot", "acme_dev_bot"},
    )
    assert result is None


def test_find_peer_mention_returns_none_when_no_peer_mention():
    from link_project_to_chat.manager.team_relay import find_peer_mention

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


async def _mk_event(text: str, sender_username: str, sender_is_bot: bool, chat_id: int | None = -100_111, msg_id: int | None = None):
    event = MagicMock()
    event.message = MagicMock()
    event.message.message = text
    event.message.chat_id = chat_id
    event.message.id = msg_id
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
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.add_event_handler = MagicMock(return_value="handler")
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement X",
        sender_username="rezoc666",
        sender_is_bot=False,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_ignores_bot_messages_not_addressing_peer():
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "PRD is drafted — standby for handoff",  # no @ mention
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_forwards_bot_to_bot_handoff():
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot please implement src/models.py per docs/architecture.md §2",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
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
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = MagicMock()
    client.send_message = AsyncMock()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    event = await _mk_event(
        "@acme_dev_bot try this",
        sender_username="random_3rd_party_bot",
        sender_is_bot=True,
    )
    await relay._on_new_message(event)
    client.send_message.assert_not_called()


# --- loop guard: consecutive-rounds cap + halt/resume ---


@pytest.mark.asyncio
async def test_relay_halts_after_max_consecutive_rounds():
    """After `max_consecutive_bot_relays` forwards, the next bot-to-bot message is NOT forwarded."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )
    # Alternate bot A → bot B three times (cap=3): all three should forward.
    for i, sender in enumerate(["acme_mgr_bot", "acme_dev_bot", "acme_mgr_bot"]):
        peer = "acme_dev_bot" if sender == "acme_mgr_bot" else "acme_mgr_bot"
        event = await _mk_event(f"@{peer} round {i}", sender_username=sender, sender_is_bot=True, msg_id=100 + i)
        await relay._on_new_message(event)
    # At this point cap is hit; forwards so far = 3, plus exactly one halt notice.
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    notices = [c for c in client.send_message.await_args_list if "paused" in c.args[1].lower()]
    assert len(forwards) == 3
    assert len(notices) == 1
    # A fourth bot-to-bot message must NOT produce another forward.
    event = await _mk_event("@acme_dev_bot round 4", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=104)
    await relay._on_new_message(event)
    forwards_after = [c for c in client.send_message.await_args_list if c.args[1].startswith("@")]
    assert len(forwards_after) == 3  # still three; halted


@pytest.mark.asyncio
async def test_relay_halt_notice_is_sent_only_once():
    """Once halted, subsequent bot-to-bot messages must NOT emit another halt notice."""
    from link_project_to_chat.manager.team_relay import TeamRelay

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
        await relay._on_new_message(ev)
    # Another bot message after halt
    ev = await _mk_event("@acme_dev_bot still going", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=210)
    await relay._on_new_message(ev)
    notices = [c for c in client.send_message.await_args_list if "paused" in c.args[1].lower()]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_relay_resumes_when_user_posts_in_group():
    """A non-bot message in the group clears halt and resets the counter."""
    from link_project_to_chat.manager.team_relay import TeamRelay

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
        await relay._on_new_message(ev)
    assert relay._halted is True
    # Trusted user posts
    user_ev = await _mk_event("ping back online", sender_username="rezoc666", sender_is_bot=False, msg_id=310)
    await relay._on_new_message(user_ev)
    assert relay._halted is False
    assert relay._rounds == 0
    # Now a bot message should forward again
    ev = await _mk_event("@acme_dev_bot resumed", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=311)
    before = client.send_message.await_count
    await relay._on_new_message(ev)
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
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = _mk_client_with_ids(start_id=500)
    relay = TeamRelay(
        client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=2,
    )
    # Forward one bot message (consumes send_message → sent.id=500)
    ev1 = await _mk_event("@acme_dev_bot one", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=400)
    await relay._on_new_message(ev1)
    # Simulate the same message bouncing back as a NewMessage from the trusted user
    # (this is what Telethon will deliver for the relay's own send).
    echo = await _mk_event("@acme_dev_bot one", sender_username="rezoc666", sender_is_bot=False, msg_id=500)
    await relay._on_new_message(echo)
    # Counter must not have reset.
    assert relay._rounds == 1


# --- event-driven auto-delete of relay forwards ---


@pytest.mark.asyncio
async def test_relay_deletes_forward_when_peer_bot_responds():
    """When the peer bot posts its response, the earlier relay forward is deleted."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = _mk_client_with_ids(start_id=700)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    # Bot A forwards a handoff to bot B (relay sends as user → sent.id=700)
    ev_a = await _mk_event("@acme_dev_bot please X", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=600)
    await relay._on_new_message(ev_a)
    client.delete_messages.assert_not_called()  # not yet — peer hasn't responded
    # Bot B responds (no relay needed for this test, but forces the delete path)
    ev_b = await _mk_event("@acme_mgr_bot done", sender_username="acme_dev_bot", sender_is_bot=True, msg_id=601)
    await relay._on_new_message(ev_b)
    # The relay forward (sent.id=700) must now be deleted.
    client.delete_messages.assert_awaited()
    call = client.delete_messages.await_args_list[0]
    chat_id, ids = call.args
    assert chat_id == -100_111
    assert 700 in list(ids)


# --- ack-only suppression + default cap ---


def test_is_ack_only_detects_pure_acks():
    from link_project_to_chat.manager.team_relay import _is_ack_only

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
    from link_project_to_chat.manager.team_relay import _is_ack_only

    assert not _is_ack_only("Please implement src/models.py")
    assert not _is_ack_only("Done — PR #123 opened")
    assert not _is_ack_only("Yes, let's proceed with option B")
    assert not _is_ack_only("OK, but can you clarify the scope first?")
    assert not _is_ack_only("I see the bug — it's in foo.py line 42")


@pytest.mark.asyncio
async def test_relay_skips_ack_only_bot_messages():
    """Ack-only messages must not be relayed — that's how ping-pong loops start."""
    from link_project_to_chat.manager.team_relay import TeamRelay

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
        await relay._on_new_message(ev)
    # Not a single forward should have happened.
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@acme_dev_bot")]
    assert forwards == []
    # And the counter must not have moved — these aren't consuming the budget.
    assert relay._rounds == 0


@pytest.mark.asyncio
async def test_relay_still_forwards_substantive_messages():
    """The ack filter must not swallow real handoffs."""
    from link_project_to_chat.manager.team_relay import TeamRelay

    client = _mk_client_with_ids()
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    ev = await _mk_event(
        "@acme_dev_bot Please implement src/models.py per docs/spec.md §2",
        sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=1200,
    )
    await relay._on_new_message(ev)
    forwards = [c for c in client.send_message.await_args_list if c.args[1].startswith("@acme_dev_bot")]
    assert len(forwards) == 1
    assert relay._rounds == 1


def test_default_max_consecutive_bot_relays_is_5():
    """Prior default of 10 was too lenient — 5 catches ping-pongs earlier."""
    from link_project_to_chat.manager.team_relay import _MAX_CONSECUTIVE_BOT_RELAYS

    assert _MAX_CONSECUTIVE_BOT_RELAYS == 5


@pytest.mark.asyncio
async def test_relay_fallback_deletes_forward_after_timeout(monkeypatch):
    """If the peer never responds, a fallback timer deletes the relay forward."""
    import link_project_to_chat.manager.team_relay as tr
    from link_project_to_chat.manager.team_relay import TeamRelay

    # Shrink the fallback window to keep the test fast.
    monkeypatch.setattr(tr, "_FALLBACK_DELETE_SECONDS", 0.01)

    client = _mk_client_with_ids(start_id=900)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})
    ev = await _mk_event("@acme_dev_bot ping", sender_username="acme_mgr_bot", sender_is_bot=True, msg_id=800)
    await relay._on_new_message(ev)
    # Let the fallback timer fire.
    import asyncio as _asyncio
    await _asyncio.sleep(0.05)
    client.delete_messages.assert_awaited()
    call = client.delete_messages.await_args_list[0]
    assert 900 in list(call.args[1])
