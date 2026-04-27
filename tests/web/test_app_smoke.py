import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")

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
    assert 'name="csrf_token"' in resp.text


async def test_messages_partial_returns_200(app_client):
    client, _ = app_client
    resp = await client.get("/chat/default/messages")
    assert resp.status_code == 200


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


async def test_post_message_requires_csrf(app_client):
    client, inbound_queue = app_client
    resp = await client.post("/chat/default/message", data={"text": "hello bot"})
    assert resp.status_code == 403
    assert inbound_queue.empty()


async def test_post_message_enqueues_event(app_client):
    client, inbound_queue = app_client
    page = await client.get("/chat/default")
    token = _csrf_token(page.text)

    resp = await client.post(
        "/chat/default/message",
        data={"text": "hello bot", "csrf_token": token},
    )
    assert resp.status_code in (200, 204)
    assert not inbound_queue.empty()
    event = inbound_queue.get_nowait()
    assert event["event_type"] == "inbound_message"
    assert event["payload"]["text"] == "hello bot"
    assert event["payload"]["sender_native_id"].startswith("web-session:")
    assert event["payload"]["sender_handle"] is None


async def test_root_redirects_to_default_chat(app_client):
    client, _ = app_client
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/chat/" in resp.headers["location"]


async def test_post_with_username_flows_to_inbound_queue(app_client):
    client, inbound_queue = app_client
    page = await client.get("/chat/default")
    token = _csrf_token(page.text)

    resp = await client.post(
        "/chat/default/message",
        data={"text": "hi", "username": "alice", "csrf_token": token},
    )
    assert resp.status_code in (200, 204)
    event = inbound_queue.get_nowait()
    assert event["payload"]["sender_display_name"] == "alice"
    assert event["payload"]["sender_handle"] is None


async def test_message_partial_escapes_stored_rich_html(tmp_path: Path):
    store = WebStore(tmp_path / "rich.db")
    await store.open()
    try:
        await store.save_message(
            chat_id="default",
            sender_native_id="bot",
            sender_display_name="Bot",
            sender_is_bot=True,
            text="<img src=x onerror=alert(1)>",
            html=True,
        )
        inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
        sse_queues: dict[str, list[asyncio.Queue]] = {}
        app = create_app(store, inbound_queue, sse_queues)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/chat/default/messages")
    finally:
        await store.close()

    assert "<img src=x onerror=alert(1)>" not in resp.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in resp.text
