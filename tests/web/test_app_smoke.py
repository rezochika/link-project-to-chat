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


async def test_chat_page_requires_web_auth_token_when_configured(tmp_path: Path):
    store = WebStore(tmp_path / "auth.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    try:
        app = create_app(store, inbound_queue, sse_queues, auth_token="secret-token")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.get("/chat/default")
            assert denied.status_code == 401

            # Bootstrap exchange: /auth swaps the URL token for the cookie.
            bootstrap = await client.get(
                "/auth?token=secret-token", follow_redirects=False
            )
            assert bootstrap.status_code in (302, 303, 307)
            assert "token" not in (bootstrap.headers.get("location") or "")

            allowed = await client.get("/chat/default")
            assert allowed.status_code == 200
            assert 'name="csrf_token"' in allowed.text

            followup = await client.get("/chat/default/messages")
            assert followup.status_code == 200
    finally:
        await store.close()


async def test_per_user_web_auth_token_sets_server_handle(tmp_path: Path):
    store = WebStore(tmp_path / "auth-users.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    try:
        app = create_app(
            store,
            inbound_queue,
            sse_queues,
            authenticated_handles={
                "tok-alice": "alice",
                "tok-bob": "bob",
            },
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.get("/auth?token=bad", follow_redirects=False)
            assert denied.status_code == 401

            bootstrap = await client.get(
                "/auth?token=tok-bob", follow_redirects=False
            )
            assert bootstrap.status_code in (302, 303, 307)

            page = await client.get("/chat/default")
            assert page.status_code == 200
            token = _csrf_token(page.text)

            resp = await client.post(
                "/chat/default/message",
                data={"text": "hello", "csrf_token": token},
            )
            assert resp.status_code in (200, 204)
            event = inbound_queue.get_nowait()
            assert event["payload"]["sender_native_id"] == "web-user:bob"
            assert event["payload"]["sender_handle"] == "bob"
            assert event["payload"]["authenticated_handle"] == "bob"
    finally:
        await store.close()


async def test_chat_route_rejects_url_token_directly(tmp_path: Path):
    """Tokens passed via query string to non-/auth routes must be ignored."""
    store = WebStore(tmp_path / "url-rej.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    try:
        app = create_app(store, inbound_queue, sse_queues, auth_token="secret-token")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.get("/chat/default?token=secret-token")
            assert denied.status_code == 401

            denied_partial = await client.get(
                "/chat/default/messages?token=secret-token"
            )
            assert denied_partial.status_code == 401

            denied_sse = await client.get("/chat/default/sse?token=secret-token")
            assert denied_sse.status_code == 401
    finally:
        await store.close()


async def test_auth_bootstrap_redirects_to_next_param_only_if_local(tmp_path: Path):
    """The `next` redirect target must be a local path; reject open-redirect attempts."""
    store = WebStore(tmp_path / "redir.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    try:
        app = create_app(store, inbound_queue, sse_queues, auth_token="secret-token")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Local path is honored.
            local = await client.get(
                "/auth?token=secret-token&next=/chat/special",
                follow_redirects=False,
            )
            assert local.status_code in (302, 303, 307)
            assert local.headers.get("location") == "/chat/special"

            # Off-host redirect is rejected (falls back to default).
            offsite = await client.get(
                "/auth?token=secret-token&next=https://evil.example/x",
                follow_redirects=False,
            )
            assert offsite.status_code in (302, 303, 307)
            assert offsite.headers.get("location", "").startswith("/chat/")

            # Protocol-relative is rejected.
            proto_rel = await client.get(
                "/auth?token=secret-token&next=//evil.example/x",
                follow_redirects=False,
            )
            assert proto_rel.status_code in (302, 303, 307)
            assert proto_rel.headers.get("location", "").startswith("/chat/")
    finally:
        await store.close()


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
