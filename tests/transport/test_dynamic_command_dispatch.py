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
