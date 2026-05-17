from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import Config, GoogleChatConfig
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import (
    Button,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    Identity,
    MessageRef,
    PromptKind,
    PromptSpec,
)


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


@pytest.mark.asyncio
async def test_app_command_runs_authorizer_and_drops_unauthorized():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            root_command_id=7,
            allowed_audiences=["https://x.test/google-chat/events"],
        ),
    )
    transport.set_authorizer(lambda identity: False)

    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)

    assert fired == []


@pytest.mark.asyncio
async def test_app_command_dropped_when_root_command_id_unset():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)

    assert fired == []


class _FakeClient:
    def __init__(self):
        self.calls = []
        self._counter = 0

    async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
        self._counter += 1
        self.calls.append({"space": space, "body": body, "thread_name": thread_name, "request_id": request_id})
        return {"name": f"{space}/messages/{self._counter}"}

    async def update_message(self, message_name, body, *, update_mask, allow_missing=False):
        self.calls.append({"message_name": message_name, "body": body, "update_mask": update_mask})
        return {"name": message_name}


@pytest.mark.asyncio
async def test_send_text_preserves_thread_name_in_reply_to_native():
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=fake,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    reply_to = MessageRef(
        "google_chat",
        "spaces/AAA/messages/0",
        chat,
        native={"thread_name": "spaces/AAA/threads/T1"},
    )

    result = await transport.send_text(chat, "hello", reply_to=reply_to)

    assert fake.calls[0]["thread_name"] == "spaces/AAA/threads/T1"
    assert result.native["thread_name"] == "spaces/AAA/threads/T1"


@pytest.mark.asyncio
async def test_send_text_with_buttons_includes_cards_v2(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    captured = {}

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            captured["body"] = body
            return {"name": f"{space}/messages/1"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    buttons = Buttons(rows=[[Button("Run", "run", ButtonStyle.PRIMARY)]])
    await transport.send_text(chat, "hi", buttons=buttons)

    assert captured["body"]["text"] == "hi"
    assert captured["body"]["cardsV2"][0]["cardId"] == "lp2c-buttons"
    assert "cardsV2" not in captured["body"]["cardsV2"][0]


@pytest.mark.asyncio
async def test_text_prompt_reply_fallback_accepts_expected_sender_only():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    sender = Identity("google_chat", "users/1", "R", "r@example.test", False)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission))

    prompt = await transport.open_prompt(chat, PromptSpec(key="name", title="Name", body="Your name", kind=PromptKind.TEXT))
    await transport.inject_prompt_reply(prompt, sender=sender, text="R")

    assert seen[0].text == "R"
    assert seen[0].option is None


@pytest.mark.asyncio
async def test_unsupported_drive_attachment_sets_unsupported_media():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = {
        "type": "MESSAGE",
        "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        "message": {"name": "spaces/AAA/messages/4", "attachment": [{"driveDataRef": {"driveFileId": "1"}}]},
        "user": {"name": "users/111", "displayName": "R"},
    }

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is True


@pytest.mark.asyncio
async def test_uploaded_content_attachment_also_sets_unsupported_media():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is True


@pytest.mark.asyncio
async def test_on_ready_callbacks_fire_with_self_identity():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired_with = []
    transport.on_ready(lambda identity: fired_with.append(identity))

    await transport._fire_on_ready()

    assert fired_with == [transport.self_identity]


@pytest.mark.asyncio
async def test_start_fires_on_ready_callbacks_with_self_identity(tmp_path):
    service_account = tmp_path / "key.json"
    service_account.write_text("{}", encoding="utf-8")
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            service_account_file=str(service_account),
            allowed_audiences=["https://x.test/google-chat/events"],
            root_command_id=1,
        ),
        client=_FakeClient(),
    )
    fired_with = []
    transport.on_ready(lambda identity: fired_with.append(identity))

    await transport.start()

    assert fired_with == [transport.self_identity]


@pytest.mark.asyncio
async def test_send_typing_is_noop_and_does_not_raise():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    await transport.send_typing(chat)
