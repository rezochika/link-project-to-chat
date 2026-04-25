import asyncio
from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)
from link_project_to_chat.web.transport import WebTransport


def _bot_identity() -> Identity:
    return Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)


def _chat() -> ChatRef:
    return ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)


@pytest.fixture
async def transport(tmp_path: Path) -> WebTransport:
    t = WebTransport(db_path=tmp_path / "web.db", bot_identity=_bot_identity(), port=18080)
    await t.start()
    yield t
    await t.stop()


async def test_transport_id_is_web(transport: WebTransport):
    assert transport.TRANSPORT_ID == "web"


async def test_send_text_returns_message_ref(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "hello")
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
    assert ref.transport_id == "web"


async def test_edit_text_does_not_raise(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "first")
    await transport.edit_text(ref, "updated")


async def test_inbound_message_dispatched(transport: WebTransport):
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)
    await transport.inject_message(_chat(), _browser_sender(), "ping")

    assert len(received) == 1
    assert received[0].text == "ping"


async def test_inbound_command_dispatched(transport: WebTransport):
    seen: list[str] = []

    async def handler(ci) -> None:
        seen.append(ci.name)

    transport.on_command("help", handler)
    await transport.inject_command(_chat(), _browser_sender(), "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_prompt_lifecycle(transport: WebTransport):
    chat = _chat()
    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)
    ref = await transport.open_prompt(chat, spec)
    assert isinstance(ref, PromptRef)

    submissions: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        submissions.append(sub)

    transport.on_prompt_submit(on_submit)
    await transport.inject_prompt_submit(ref, _browser_sender(), text="Alice")

    assert len(submissions) == 1
    assert submissions[0].text == "Alice"

    await transport.close_prompt(ref, final_text="Done")


def _browser_sender() -> Identity:
    return Identity(
        transport_id="web", native_id="browser_user",
        display_name="You", handle=None, is_bot=False,
    )
