"""Smoke tests for FakeTransport — ensures the test double works before anything else relies on it."""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


def _alice() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


async def test_send_text_is_captured():
    t = FakeTransport()
    ref = await t.send_text(_chat(), "hello")
    assert len(t.sent_messages) == 1
    assert t.sent_messages[0].text == "hello"
    assert ref.chat == _chat()


async def test_edit_text_is_captured():
    t = FakeTransport()
    ref = await t.send_text(_chat(), "hello")
    await t.edit_text(ref, "updated")
    assert len(t.edited_messages) == 1
    assert t.edited_messages[0].text == "updated"
    assert t.edited_messages[0].message == ref


async def test_send_file_is_captured(tmp_path: Path):
    t = FakeTransport()
    p = tmp_path / "x.txt"
    p.write_text("hi")
    ref = await t.send_file(_chat(), p, caption="see")
    assert len(t.sent_files) == 1
    assert t.sent_files[0].path == p
    assert t.sent_files[0].caption == "see"
    assert ref.chat == _chat()


async def test_inject_message_fires_on_message_handler():
    t = FakeTransport()
    captured: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        captured.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _alice(), "hi")

    assert len(captured) == 1
    assert captured[0].text == "hi"
    assert captured[0].sender == _alice()


async def test_inject_command_fires_on_command_handler():
    t = FakeTransport()
    seen: list[str] = []

    async def handler(ci):
        seen.append(ci.name)

    t.on_command("help", handler)
    await t.inject_command(_chat(), _alice(), "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_inject_button_click_fires_handler():
    t = FakeTransport()
    seen: list[str] = []

    async def handler(click):
        seen.append(click.value)

    t.on_button(handler)
    ref = await t.send_text(_chat(), "pick one")
    await t.inject_button_click(ref, _alice(), value="go")

    assert seen == ["go"]


async def test_unknown_command_is_noop():
    """Injecting a command with no registered handler doesn't raise — just no-op."""
    t = FakeTransport()
    # No handler registered for 'help'.
    await t.inject_command(_chat(), _alice(), "help", args=[], raw_text="/help")
    # No assertion beyond "didn't raise" — this is the contract.


async def test_start_and_stop_are_idempotent():
    t = FakeTransport()
    await t.start()
    await t.start()  # second start must not raise
    await t.stop()
    await t.stop()  # second stop must not raise
