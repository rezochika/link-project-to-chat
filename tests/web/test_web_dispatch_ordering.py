import pytest

pytest.importorskip("fastapi")

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


async def test_messages_partial_includes_post_after_sse(tmp_path: Path):
    """After POST /message returns and SSE fires, /messages partial must include
    the just-posted user message — not just an empty stale list."""
    store = WebStore(tmp_path / "ord.db")
    await store.open()

    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}

    # Stand-in for WebTransport's dispatch loop: synchronously drain inbound
    # and persist as a "user message", then notify SSE.
    async def fake_dispatch_loop():
        while True:
            event = await inbound_queue.get()
            payload = event["payload"]
            await store.save_message(
                chat_id=event["chat_id"],
                sender_native_id=payload.get("sender_native_id", "browser_user"),
                sender_display_name=payload.get("sender_display_name", "You"),
                sender_is_bot=False,
                text=payload.get("text", ""),
                html=False,
            )
            # CRITICAL: notify AFTER save.
            for q in list(sse_queues.get(event["chat_id"], [])):
                await q.put({"chat_id": event["chat_id"]})

    app = create_app(store, inbound_queue, sse_queues)
    dispatch = asyncio.create_task(fake_dispatch_loop())

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/chat/default/message", data={"text": "hi", "username": "alice"})
            # Give the dispatch loop a moment.
            await asyncio.sleep(0.05)
            resp = await client.get("/chat/default/messages")
            assert resp.status_code == 200
            assert "hi" in resp.text  # the user's message is rendered
    finally:
        dispatch.cancel()
        try:
            await dispatch
        except asyncio.CancelledError:
            pass
        await store.close()
