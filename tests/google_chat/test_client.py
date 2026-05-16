from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from link_project_to_chat.google_chat.client import GoogleChatClient


@dataclass
class _Call:
    url: str
    json: dict
    params: dict


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class FakeHttpx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    async def post(self, url: str, *, json: dict, params: dict | None = None) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}))
        return _FakeResponse({"name": f"{url}/messages/1"})

    async def patch(self, url: str, *, json: dict, params: dict | None = None) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}))
        return _FakeResponse({"name": url})


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
