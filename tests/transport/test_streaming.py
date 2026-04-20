"""Unit tests for StreamingMessage — transport-agnostic streaming-edit helper."""
from __future__ import annotations

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef
from link_project_to_chat.transport.fake import FakeTransport
from link_project_to_chat.transport.streaming import StreamingMessage


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


async def test_open_sends_initial_text():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("starting...")
    assert len(t.sent_messages) == 1
    assert t.sent_messages[0].text == "starting..."


async def test_update_edits_existing_message():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("v1")
    await sm.update("v2")
    await sm.close()
    assert len(t.edited_messages) >= 1
    assert t.edited_messages[-1].text == "v2"


async def test_close_with_final_text_performs_final_edit():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0)
    await sm.open("working...")
    await sm.close(final_text="done")
    assert any(e.text == "done" for e in t.edited_messages)


async def test_throttle_defers_interim_updates():
    """Back-to-back updates inside the throttle window coalesce."""
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=10)  # huge window
    await sm.open("v0")
    await sm.update("v1")
    await sm.update("v2")
    await sm.update("v3")
    # None of v1/v2/v3 should be sent yet — only the initial open.
    assert len(t.edited_messages) == 0
    # close() must flush the final text.
    await sm.close()
    assert t.edited_messages[-1].text == "v3"


async def test_overflow_sends_new_message_and_continues_editing_tail():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), min_interval_s=0, max_chars=10)
    await sm.open("0123456789")  # exactly at cap
    await sm.update("0123456789ABCDE")  # overflow by 5 chars
    await sm.close()
    # At least 2 messages sent total: the original plus the overflow chunk.
    assert len(t.sent_messages) >= 2
