from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
async def test_create_supergroup_returns_negative_chat_id():
    from link_project_to_chat.manager.telegram_group import create_supergroup

    # Mock Telethon response: channels.CreateChannelRequest returns an Updates object
    # whose .chats[0].id is a large positive int; caller must prepend -100 to get
    # the full -100... form used by the Bot API.
    mock_chat = MagicMock()
    mock_chat.id = 1234567890
    mock_response = MagicMock()
    mock_response.chats = [mock_chat]

    client = AsyncMock()
    client.return_value = mock_response  # calling client(request) returns the response

    chat_id = await create_supergroup(client, "acme team")
    assert chat_id == -1001234567890
