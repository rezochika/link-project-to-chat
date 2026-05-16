"""End-to-end: respond_in_groups=True wires through run_bot → ProjectBot
→ FakeTransport → routing gate → _on_text → backend submit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_solo_bot_in_group_e2e_addressed_message_submits_to_backend(tmp_path: Path):
    """Full pipeline: a ProjectBot constructed with respond_in_groups=True,
    receiving an @-mentioned group message from an authorized executor,
    submits the (mention-stripped) prompt to the agent. Drive-by messages
    in the same group don't reach the backend at all.
    """
    bot = ProjectBot(
        "p", tmp_path, "t",
        backend_name="claude",
        backend_state={},
        allowed_users=[AllowedUser(username="alice", role="executor",
                                    locked_identities=["fake:42"])],
        auth_source="project",
        respond_in_groups=True,
    )
    # Force bot_username (normally set in _after_ready by the transport).
    bot.bot_username = "MyBot"
    fake = FakeTransport()
    bot._transport = fake
    # Stub backend submission so we don't spawn a real agent.
    # submit_agent is sync — use MagicMock, NOT AsyncMock.
    bot.task_manager.submit_agent = MagicMock()

    bot_identity = Identity(
        transport_id="fake", native_id="bot-self",
        display_name="MyBot", handle="MyBot", is_bot=True,
    )

    # Addressed message → submit.
    chat = ChatRef(transport_id="fake", native_id="100", kind=ChatKind.ROOM)
    alice = Identity(
        transport_id="fake", native_id="42",
        display_name="alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    addressed = IncomingMessage(
        chat=chat, sender=alice, text="@MyBot what's the status?",
        files=[], reply_to=None, message=msg,
        mentions=[bot_identity],
    )
    await bot._on_text_from_transport(addressed)
    assert bot.task_manager.submit_agent.call_count == 1
    submit_kwargs = bot.task_manager.submit_agent.call_args.kwargs
    # The mention was stripped from the prompt.
    assert "@MyBot" not in submit_kwargs.get("prompt", "")
    assert "what's the status" in submit_kwargs.get("prompt", "")

    # Drive-by message → silent (no second submit).
    bot.task_manager.submit_agent.reset_mock()
    drive_by_msg = MessageRef(transport_id="fake", native_id="201", chat=chat)
    drive_by = IncomingMessage(
        chat=chat, sender=alice, text="random chatter",
        files=[], reply_to=None, message=drive_by_msg,
    )
    await bot._on_text_from_transport(drive_by)
    bot.task_manager.submit_agent.assert_not_called()
