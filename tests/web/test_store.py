import os
import tempfile
from pathlib import Path

import pytest

from link_project_to_chat.web.store import WebStore


@pytest.fixture
async def store(tmp_path: Path) -> WebStore:
    s = WebStore(tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()


async def test_save_and_retrieve_message(store: WebStore):
    msg_id = await store.save_message(
        chat_id="chat1",
        sender_native_id="bot1",
        sender_display_name="Bot",
        sender_is_bot=True,
        text="Hello!",
        html=False,
    )
    assert isinstance(msg_id, int)
    messages = await store.get_messages("chat1")
    assert len(messages) == 1
    assert messages[0]["text"] == "Hello!"
    assert messages[0]["sender_is_bot"] is True


async def test_update_message(store: WebStore):
    msg_id = await store.save_message(
        chat_id="chat1",
        sender_native_id="bot1",
        sender_display_name="Bot",
        sender_is_bot=True,
        text="old",
        html=False,
    )
    await store.update_message(msg_id, "new", html=True)
    messages = await store.get_messages("chat1")
    assert messages[0]["text"] == "new"
    assert messages[0]["html"] is True


async def test_push_and_poll_event(store: WebStore):
    event_id = await store.push_event("chat1", "inbound_message", {"text": "hi"})
    events = await store.poll_events("chat1", after_id=event_id - 1)
    assert len(events) == 1
    assert events[0]["type"] == "inbound_message"
    assert events[0]["payload"]["text"] == "hi"


async def test_poll_events_after_id(store: WebStore):
    id1 = await store.push_event("chat1", "msg", {"n": 1})
    id2 = await store.push_event("chat1", "msg", {"n": 2})
    events = await store.poll_events("chat1", after_id=id1)
    assert len(events) == 1
    assert events[0]["payload"]["n"] == 2


async def test_messages_isolated_by_chat(store: WebStore):
    await store.save_message("chat1", "u1", "User", False, "for chat1", False)
    await store.save_message("chat2", "u2", "User", False, "for chat2", False)
    assert len(await store.get_messages("chat1")) == 1
    assert len(await store.get_messages("chat2")) == 1
