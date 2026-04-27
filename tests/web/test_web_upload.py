import pytest

pytest.importorskip("fastapi")

import asyncio
import io
import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


@pytest.fixture
async def app_client(tmp_path: Path):
    store = WebStore(tmp_path / "u.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    app = create_app(store, inbound_queue, sse_queues)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, inbound_queue, tmp_path
    await store.close()


async def test_post_message_with_file_attaches(app_client):
    client, inbound_queue, _ = app_client
    page = await client.get("/chat/default")
    token = _csrf_token(page.text)
    files = {"file": ("hello.txt", io.BytesIO(b"hi there"), "text/plain")}
    data = {"text": "see attached", "username": "alice", "csrf_token": token}
    resp = await client.post("/chat/default/message", data=data, files=files)
    assert resp.status_code in (200, 204)
    event = inbound_queue.get_nowait()
    assert event["payload"]["text"] == "see attached"
    assert "files" in event["payload"]
    assert len(event["payload"]["files"]) == 1
    saved_path = Path(event["payload"]["files"][0]["path"])
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"hi there"
