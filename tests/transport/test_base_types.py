"""Smoke tests for transport primitive types.

These don't exercise behavior (dataclasses have none); they verify the shape
so later refactors can't silently drop or rename a field.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    Button,
    ButtonClick,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
    Transport,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="test", native_id="123", kind=ChatKind.DM)


def _sender(is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="test",
        native_id="42",
        display_name="Alice",
        handle="alice",
        is_bot=is_bot,
    )


def test_chat_ref_fields():
    c = _chat()
    assert c.transport_id == "test"
    assert c.native_id == "123"
    assert c.kind is ChatKind.DM


def test_chat_kind_has_dm_and_room():
    assert {ChatKind.DM, ChatKind.ROOM} == set(ChatKind)


def test_identity_fields():
    i = _sender(is_bot=True)
    assert i.is_bot is True
    assert i.handle == "alice"


def test_message_ref_carries_chat():
    m = MessageRef(transport_id="test", native_id="m1", chat=_chat())
    assert m.chat.kind is ChatKind.DM


def test_button_defaults_to_default_style():
    b = Button(label="Go", value="go")
    assert b.style is ButtonStyle.DEFAULT


def test_buttons_is_rows_of_buttons():
    bs = Buttons(rows=[[Button(label="A", value="a")], [Button(label="B", value="b")]])
    assert len(bs.rows) == 2
    assert bs.rows[0][0].label == "A"


def test_button_click_carries_value():
    click = ButtonClick(chat=_chat(), message=MessageRef("test", "m", _chat()), sender=_sender(), value="go")
    assert click.value == "go"


def test_incoming_file_carries_path():
    f = IncomingFile(path=Path("/tmp/x"), original_name="x", mime_type="text/plain", size_bytes=10)
    assert f.path == Path("/tmp/x")


def test_incoming_message_has_empty_files_by_default():
    m = IncomingMessage(
        chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None,
        message=MessageRef("test", "m", _chat()), native=None,
    )
    assert m.files == []


def test_command_invocation_has_args_list():
    ci = CommandInvocation(
        chat=_chat(),
        sender=_sender(),
        name="run",
        args=["ls", "-la"],
        raw_text="/run ls -la",
        message=MessageRef("test", "m", _chat()),
    )
    assert ci.args == ["ls", "-la"]


def test_transport_is_importable():
    # Protocol runtime-check is a compile-time concern for mypy/pyright;
    # here we simply assert the symbol is importable without error.
    assert Transport is not None


def test_incoming_message_has_is_relayed_bot_to_bot_field():
    m = IncomingMessage(
        chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None,
        message=MessageRef("test", "m", _chat()), native=None,
    )
    # default is False
    assert m.is_relayed_bot_to_bot is False


def test_incoming_message_accepts_is_relayed_bot_to_bot_true():
    m = IncomingMessage(
        chat=_chat(), sender=_sender(), text="hi", files=[], reply_to=None,
        message=MessageRef("test", "m", _chat()), native=None,
        is_relayed_bot_to_bot=True,
    )
    assert m.is_relayed_bot_to_bot is True
