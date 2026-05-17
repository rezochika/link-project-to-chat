from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("google.auth")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.credentials import build_google_chat_http_client


@pytest.mark.asyncio
async def test_build_google_chat_http_client_uses_injected_credentials_factory(tmp_path: Path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    cfg = GoogleChatConfig(service_account_file=str(sa), allowed_audiences=["aud"])

    captured = {}

    def fake_credentials_factory(path, scopes):
        captured["path"] = path
        captured["scopes"] = scopes

        class _Creds:
            def refresh(self, request):
                self.token = "fake-token"

            token = "fake-token"

        return _Creds()

    client = build_google_chat_http_client(cfg, credentials_factory=fake_credentials_factory)
    try:
        assert captured["path"] == str(sa)
        assert "https://www.googleapis.com/auth/chat.bot" in captured["scopes"]
        assert str(client.base_url).rstrip("/") == "https://chat.googleapis.com"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_google_auth_injects_header_on_async_client_request_path():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    captured = {}

    class _Creds:
        token = None
        valid = False

        def refresh(self, request):
            self.token = "fresh-token"
            self.valid = True

    async def handler(request):
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(200)

    async with httpx.AsyncClient(
        auth=_GoogleAuth(_Creds()),
        transport=httpx.MockTransport(handler),
        base_url="https://chat.googleapis.com",
    ) as client:
        await client.get("/v1/spaces")

    assert captured["authorization"] == "Bearer fresh-token"


@pytest.mark.asyncio
async def test_google_auth_refresh_does_not_block_event_loop_on_async_client_request_path():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    class _Creds:
        token = None
        valid = False

        def refresh(self, request):
            time.sleep(0.2)
            self.token = "fresh-token"
            self.valid = True

    async def handler(request):
        return httpx.Response(200)

    async def ticker():
        started = time.perf_counter()
        await asyncio.sleep(0.05)
        return time.perf_counter() - started

    async with httpx.AsyncClient(
        auth=_GoogleAuth(_Creds()),
        transport=httpx.MockTransport(handler),
        base_url="https://chat.googleapis.com",
    ) as client:
        ticker_task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        await client.get("/v1/spaces")
        ticker_elapsed = await ticker_task

    assert ticker_elapsed < 0.15


def test_google_auth_refreshes_when_credentials_not_valid():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    refresh_calls = []

    class _Creds:
        token = None
        valid = False

        def refresh(self, request):
            refresh_calls.append(request)
            self.token = "fresh-token"
            self.valid = True

    auth = _GoogleAuth(_Creds())

    class _FakeRequest:
        headers: dict = {}

    request = _FakeRequest()
    request.headers = {}
    next(auth.auth_flow(request))

    assert refresh_calls, "_GoogleAuth must call credentials.refresh() when token is missing or expired"
    assert request.headers["authorization"] == "Bearer fresh-token"


def test_google_auth_skips_refresh_when_credentials_already_valid():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    refresh_calls = []

    class _Creds:
        token = "hot-token"
        valid = True

        def refresh(self, request):
            refresh_calls.append(request)

    auth = _GoogleAuth(_Creds())

    class _FakeRequest:
        headers: dict = {}

    request = _FakeRequest()
    request.headers = {}
    next(auth.auth_flow(request))

    assert refresh_calls == []
    assert request.headers["authorization"] == "Bearer hot-token"
