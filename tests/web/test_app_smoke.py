import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


@pytest.fixture
async def app_client(tmp_path: Path):
    store = WebStore(tmp_path / "smoke.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    app = create_app(store, inbound_queue, sse_queues)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, inbound_queue
    await store.close()


async def test_chat_page_returns_200(app_client):
    client, _ = app_client
    resp = await client.get("/chat/default")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_messages_partial_returns_200(app_client):
    client, _ = app_client
    resp = await client.get("/chat/default/messages")
    assert resp.status_code == 200


async def test_post_message_enqueues_event(app_client):
    client, inbound_queue = app_client
    resp = await client.post("/chat/default/message", data={"text": "hello bot"})
    assert resp.status_code in (200, 204)
    assert not inbound_queue.empty()
    event = inbound_queue.get_nowait()
    assert event["event_type"] == "inbound_message"
    assert event["payload"]["text"] == "hello bot"


async def test_root_redirects_to_default_chat(app_client):
    client, _ = app_client
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/chat/" in resp.headers["location"]
