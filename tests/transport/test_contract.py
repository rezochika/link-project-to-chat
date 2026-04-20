"""Parametrized Protocol contract test — every Transport must pass.

Initial parameter list: [FakeTransport]. TelegramTransport added in Task 24 once
we have a working test fixture for it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
    Transport,
)
from link_project_to_chat.transport.fake import FakeTransport


def _chat(transport_id: str) -> ChatRef:
    return ChatRef(transport_id=transport_id, native_id="1", kind=ChatKind.DM)


def _sender(transport_id: str) -> Identity:
    return Identity(
        transport_id=transport_id,
        native_id="1",
        display_name="Alice",
        handle="alice",
        is_bot=False,
    )


@pytest.fixture(params=[FakeTransport])
def transport(request) -> Transport:
    """Yield a fresh Transport implementation per test.

    New transports added to `params` when implemented.
    """
    cls = request.param
    t = cls()
    yield t


async def test_send_text_returns_usable_message_ref(transport):
    chat = _chat(transport.TRANSPORT_ID)
    ref = await transport.send_text(chat, "hello")
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
    # edit_text on the returned ref must not raise.
    await transport.edit_text(ref, "updated")


async def test_on_message_fires_for_injected_text(transport):
    # This test requires an inject_message method — all Transports used in
    # contract tests must expose one. FakeTransport has it natively; new
    # transports provide a test fixture that wires one in (see Task 24 for Telegram).
    if not hasattr(transport, "inject_message"):
        pytest.skip(f"{type(transport).__name__} does not support inject_message")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    captured: list[IncomingMessage] = []

    async def handler(msg):
        captured.append(msg)

    transport.on_message(handler)
    await transport.inject_message(chat, sender, "ping")

    assert len(captured) == 1
    assert captured[0].text == "ping"


async def test_on_command_fires_for_injected_command(transport):
    if not hasattr(transport, "inject_command"):
        pytest.skip(f"{type(transport).__name__} does not support inject_command")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    seen: list[str] = []

    async def handler(ci):
        seen.append(ci.name)

    transport.on_command("help", handler)
    await transport.inject_command(chat, sender, "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_on_button_fires_for_injected_click(transport):
    if not hasattr(transport, "inject_button_click"):
        pytest.skip(f"{type(transport).__name__} does not support inject_button_click")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    seen: list[str] = []

    async def handler(click):
        seen.append(click.value)

    transport.on_button(handler)
    ref = await transport.send_text(chat, "pick")
    await transport.inject_button_click(ref, sender, value="go")

    assert seen == ["go"]
