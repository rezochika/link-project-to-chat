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


@pytest.mark.asyncio
async def test_add_bot_invokes_invite_to_channel():
    from link_project_to_chat.manager.telegram_group import add_bot
    from telethon.tl.functions.channels import InviteToChannelRequest

    bot_entity = MagicMock()
    channel_entity = MagicMock(name="channel")
    client = AsyncMock()
    # Differentiate get_entity (bot) from get_input_entity (channel)
    client.get_entity = AsyncMock(return_value=bot_entity)
    client.get_input_entity = AsyncMock(return_value=channel_entity)

    await add_bot(client, -1001, "acme_mgr_claude_bot")

    client.get_input_entity.assert_awaited_once_with(-1001)
    call_args = client.call_args_list
    invite_calls = [c for c in call_args if isinstance(c.args[0], InviteToChannelRequest)]
    assert invite_calls, "InviteToChannelRequest must be issued"
    request = invite_calls[0].args[0]
    assert request.channel is channel_entity
    assert request.users == [bot_entity]


@pytest.mark.asyncio
async def test_promote_admin_sets_correct_rights():
    from link_project_to_chat.manager.telegram_group import promote_admin
    from telethon.tl.functions.channels import EditAdminRequest

    bot_entity = MagicMock()
    channel_entity = MagicMock(name="channel")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=bot_entity)
    client.get_input_entity = AsyncMock(return_value=channel_entity)

    await promote_admin(client, -1001, "acme_mgr_claude_bot")

    client.get_input_entity.assert_awaited_once_with(-1001)
    call_args = client.call_args_list
    admin_calls = [c for c in call_args if isinstance(c.args[0], EditAdminRequest)]
    assert admin_calls, "EditAdminRequest must be issued"
    request = admin_calls[0].args[0]
    assert request.channel is channel_entity
    assert request.user_id is bot_entity
    assert request.admin_rights.post_messages is True
    assert request.admin_rights.delete_messages is True
    assert request.admin_rights.invite_users is True


@pytest.mark.asyncio
async def test_invite_user_uses_invite_to_channel():
    from link_project_to_chat.manager.telegram_group import invite_user
    from telethon.tl.functions.channels import InviteToChannelRequest

    user_entity = MagicMock()
    channel_entity = MagicMock(name="channel")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user_entity)
    client.get_input_entity = AsyncMock(return_value=channel_entity)

    await invite_user(client, -1001, "alice")

    client.get_input_entity.assert_awaited_once_with(-1001)
    call_args = client.call_args_list
    invite_calls = [c for c in call_args if isinstance(c.args[0], InviteToChannelRequest)]
    assert invite_calls, "InviteToChannelRequest must be issued"
    request = invite_calls[0].args[0]
    assert request.channel is channel_entity
    assert request.users == [user_entity]
