"""Unit tests for telegram-native ↔ transport-primitive mapping helpers.

The full TelegramTransport wiring is tested via the contract test in test_contract.py.
These tests isolate the pure mapping functions so they can be debugged in isolation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from link_project_to_chat.transport import ChatKind
from link_project_to_chat.transport.telegram import (
    chat_ref_from_telegram,
    identity_from_telegram_user,
    message_ref_from_telegram,
)


def test_private_chat_maps_to_dm():
    fake_chat = SimpleNamespace(id=12345, type="private")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.native_id == "12345"
    assert ref.kind is ChatKind.DM
    assert ref.transport_id == "telegram"


def test_group_chat_maps_to_room():
    fake_chat = SimpleNamespace(id=-100123, type="group")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.kind is ChatKind.ROOM


def test_supergroup_chat_maps_to_room():
    fake_chat = SimpleNamespace(id=-100123, type="supergroup")
    ref = chat_ref_from_telegram(fake_chat)
    assert ref.kind is ChatKind.ROOM


def test_identity_from_user():
    fake_user = SimpleNamespace(
        id=42, full_name="Alice Bee", username="alice", is_bot=False
    )
    i = identity_from_telegram_user(fake_user)
    assert i.native_id == "42"
    assert i.display_name == "Alice Bee"
    assert i.handle == "alice"
    assert i.is_bot is False


def test_identity_with_no_username():
    fake_user = SimpleNamespace(id=7, full_name="Bot McBotface", username=None, is_bot=True)
    i = identity_from_telegram_user(fake_user)
    assert i.handle is None
    assert i.is_bot is True


def test_message_ref_from_telegram():
    fake_chat = SimpleNamespace(id=12345, type="private")
    fake_msg = SimpleNamespace(message_id=99, chat=fake_chat)
    m = message_ref_from_telegram(fake_msg)
    assert m.native_id == "99"
    assert m.chat.native_id == "12345"
    assert m.chat.kind is ChatKind.DM
