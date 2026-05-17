from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from link_project_to_chat.google_chat.client import GoogleChatClient


@dataclass
class _Call:
    url: str
    json: dict | None
    params: dict
    files: dict | None = None


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeHttpx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.stream_chunks: list[bytes] = []
        self.next_post_json: dict | None = None

    async def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
    ) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}, files=files))
        return _FakeResponse(self.next_post_json or {"name": f"{url}/messages/1"})

    async def patch(self, url: str, *, json: dict, params: dict | None = None) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}))
        return _FakeResponse({"name": url})

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        self.stream_calls.append((method, url))
        return _FakeStreamResponse(self.stream_chunks)


@pytest.fixture
def fake_httpx() -> FakeHttpx:
    return FakeHttpx()


@pytest.mark.asyncio
async def test_create_message_sends_request_id(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.create_message("spaces/AAA", {"text": "hello"}, request_id="req-1")

    assert fake_httpx.calls[0].params["requestId"] == "req-1"


@pytest.mark.asyncio
async def test_update_message_requires_update_mask(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.update_message("spaces/AAA/messages/1", {"text": "new"}, update_mask="text")

    assert fake_httpx.calls[0].params["updateMask"] == "text"
    assert fake_httpx.calls[0].params.get("allowMissing") is False


@pytest.mark.asyncio
async def test_download_attachment_writes_bytes_under_size_cap(fake_httpx, tmp_path: Path):
    fake_httpx.stream_chunks = [b"abc", b"def"]
    client = GoogleChatClient(http=fake_httpx)
    destination = tmp_path / "report.txt"

    await client.download_attachment(
        "spaces/AAA/messages/3/attachments/A1",
        destination,
        max_bytes=6,
    )

    assert fake_httpx.stream_calls == [
        ("GET", "/v1/media/spaces/AAA/messages/3/attachments/A1?alt=media"),
    ]
    assert destination.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_upload_attachment_posts_multipart_with_resource_name(fake_httpx, tmp_path: Path):
    src = tmp_path / "report.txt"
    src.write_bytes(b"fake file bytes")
    fake_httpx.next_post_json = {
        "attachmentDataRef": {"resourceName": "spaces/AAA/attachments/X1"},
    }
    client = GoogleChatClient(http=fake_httpx)

    result = await client.upload_attachment("spaces/AAA", src, mime_type="text/plain")

    assert result["attachmentDataRef"]["resourceName"] == "spaces/AAA/attachments/X1"
    assert fake_httpx.calls[0].url == "/upload/v1/spaces/AAA/attachments:upload"
    assert fake_httpx.calls[0].params["uploadType"] == "multipart"


@pytest.mark.asyncio
async def test_upload_attachment_uses_display_name_in_metadata(fake_httpx, tmp_path: Path):
    src = tmp_path / "tmp-random-name"
    src.write_bytes(b"fake file bytes")
    client = GoogleChatClient(http=fake_httpx)

    await client.upload_attachment(
        "spaces/AAA",
        src,
        mime_type="text/plain",
        display_name="report.txt",
    )

    metadata = fake_httpx.calls[0].files["metadata"]
    assert json.loads(metadata[1]) == {"filename": "report.txt"}


@pytest.mark.asyncio
async def test_upload_attachment_rejects_oversize_files(fake_httpx, tmp_path: Path):
    src = tmp_path / "large.txt"
    src.write_bytes(b"123456")
    client = GoogleChatClient(http=fake_httpx)

    with pytest.raises(ValueError):
        await client.upload_attachment("spaces/AAA", src, mime_type="text/plain", max_bytes=5)

    assert fake_httpx.calls == []
