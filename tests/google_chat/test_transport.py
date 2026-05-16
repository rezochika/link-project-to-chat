from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import Config, GoogleChatConfig
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import ChatKind, ChatRef, Identity


FIXTURES = Path(__file__).parent / "fixtures"


def test_google_chat_transport_has_expected_identity():
    cfg = GoogleChatConfig(service_account_file="/tmp/key.json", allowed_audiences=["https://x.test/google-chat/events"])
    transport = GoogleChatTransport(config=cfg)

    assert transport.transport_id == "google_chat"
    assert transport.self_identity == Identity(
        transport_id="google_chat",
        native_id="google_chat:app",
        display_name="Google Chat App",
        handle=None,
        is_bot=True,
    )


def test_google_chat_chat_refs_use_google_transport_id():
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    assert chat.transport_id == "google_chat"


@pytest.mark.asyncio
async def test_message_event_normalizes_to_incoming_message():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "message_text.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].chat.kind is ChatKind.ROOM
    assert seen[0].text == "hello"
    assert seen[0].message.native["thread_name"] == "spaces/AAA/threads/T1"


@pytest.mark.asyncio
async def test_command_event_uses_configured_root_command_id():
    transport = GoogleChatTransport(config=GoogleChatConfig(root_command_id=7, allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_command("help", lambda cmd: seen.append(cmd))
    payload = json.loads((FIXTURES / "app_command_help.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].name == "help"
    assert seen[0].args == []
