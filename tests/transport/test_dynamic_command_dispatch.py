"""Regression test: late on_command() calls must produce dispatchable handlers.

Plugins register their commands inside `_after_ready`, which fires AFTER
`attach_telegram_routing`. Before the fix, TelegramTransport.on_command only
updated `_command_handlers[name]` — PTB never got a CommandHandler for that
name and updates were dropped at the filter level.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_fake_transport_late_on_command_is_dispatchable():
    """FakeTransport already iterates _command_handlers per message — this is
    a baseline assertion that the late registration is honored."""
    transport = FakeTransport()
    handler = AsyncMock()
    # Late registration (after notional "routing" — Fake has no routing step,
    # but the contract is: on_command(name, h) makes /name dispatchable from
    # whatever point it's called).
    transport.on_command("late_cmd", handler)
    assert "late_cmd" in transport._command_handlers


def test_telegram_transport_late_on_command_registers_ptb_handler():
    """TelegramTransport.on_command called AFTER attach_telegram_routing must
    register a PTB CommandHandler so updates actually reach _dispatch_command.

    This is the fix for Issue #1 — without it, plugin commands silently fail.
    """
    pytest.importorskip("telegram")

    from telegram.ext import CommandHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(group_mode=False, command_names=["help", "tasks"])

    async def late_handler(ci):
        return None

    # The number of CommandHandlers before late registration:
    before = sum(
        1
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
    )
    transport.on_command("late_cmd", late_handler)
    after = sum(
        1
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
    )
    # The new command produced a NEW PTB CommandHandler.
    assert after == before + 1, (
        f"Expected PTB CommandHandler count to grow by 1 after late on_command, "
        f"got {before} → {after}"
    )
    # And the dispatch dict reflects it.
    assert "late_cmd" in transport._command_handlers


def test_telegram_transport_filter_widened_when_respond_in_groups_true():
    """When respond_in_groups=True, the MessageHandler filter accepts both
    private DMs AND groups. Matches the GitLab fork's behavior for solo
    bots; isolated from team mode (group_mode=True still narrows to GROUPS).
    """
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=True,
    )
    # Inspect the filter expression on the registered MessageHandler.
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    # ChatType.PRIVATE | ChatType.GROUPS — verify both present.
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()


def test_telegram_transport_filter_private_only_when_respond_in_groups_false():
    """Default behavior unchanged: solo bots see only PRIVATE messages."""
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=False,
    )
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    # GROUPS should NOT be in the filter for solo+respond_in_groups=False.
    assert "ChatType.GROUPS" not in filter_repr


def test_telegram_transport_filter_groups_only_when_team_mode():
    """Team-mode behavior unchanged: group_mode=True narrows to GROUPS,
    regardless of respond_in_groups (which is a solo-only concern)."""
    pytest.importorskip("telegram")

    from telegram.ext import MessageHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=True,
        command_names=["help"],
        respond_in_groups=True,  # ignored when group_mode=True
    )
    handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, MessageHandler)
    )
    filter_repr = repr(handler.filters)
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()
    # PRIVATE should NOT be in the team-mode filter.
    assert "ChatType.PRIVATE" not in filter_repr


def test_telegram_transport_late_on_command_picks_widened_filter():
    """When routing was attached with respond_in_groups=True, late on_command
    registrations also pick up the wider filter (so plugin commands work in
    both DMs and groups, not just one)."""
    pytest.importorskip("telegram")

    from telegram.ext import CommandHandler

    from link_project_to_chat.transport.telegram import TelegramTransport

    transport = TelegramTransport.build("123:fake-token", menu=[])
    transport.attach_telegram_routing(
        group_mode=False,
        command_names=["help"],
        respond_in_groups=True,
    )

    async def late_handler(ci):
        return None

    transport.on_command("late_cmd", late_handler)
    late_ptb_handler = next(
        h
        for group in transport.app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler) and "late_cmd" in h.commands
    )
    filter_repr = repr(late_ptb_handler.filters)
    assert "ChatType.PRIVATE" in filter_repr or "private" in filter_repr.lower()
    assert "ChatType.GROUPS" in filter_repr or "group" in filter_repr.lower()
