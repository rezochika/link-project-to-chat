from __future__ import annotations

from link_project_to_chat.config import Config, GoogleChatConfig
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import ChatKind, ChatRef, Identity


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
