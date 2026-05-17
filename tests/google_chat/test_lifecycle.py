from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

pytest.importorskip("httpx")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest
from link_project_to_chat.google_chat.client import GoogleChatClient
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.google_chat.validators import GoogleChatStartupError
from link_project_to_chat.transport.base import ChatKind, ChatRef


def _runnable_cfg(tmp_path: Path) -> GoogleChatConfig:
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    return GoogleChatConfig(
        service_account_file=str(sa),
        app_id="app-1",
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
        port=0,
    )


def _fake_credentials_factory(path, scopes):
    class _C:
        token = "fake"
        valid = True

        def refresh(self, request):
            pass

    return _C()


class _FinalSendClient:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
        if self.closed:
            raise RuntimeError("client already closed")
        self.calls.append(
            {
                "space": space,
                "body": body,
                "thread_name": thread_name,
                "request_id": request_id,
            }
        )
        return {"name": f"{space}/messages/final"}


class _FinalSendHttp:
    def __init__(self, client: _FinalSendClient):
        self._client = client

    async def aclose(self):
        self._client.closed = True


@pytest.mark.asyncio
async def test_start_constructs_google_chat_client_when_none_injected(tmp_path):
    cfg = _runnable_cfg(tmp_path)

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=_fake_credentials_factory,
        serve=False,
    )

    await transport.start()
    try:
        assert isinstance(transport.client, GoogleChatClient)
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_start_validates_default_config_before_on_ready():
    transport = GoogleChatTransport(config=GoogleChatConfig(), serve=False)
    fired = []
    transport.on_ready(lambda identity: fired.append(identity))

    with pytest.raises(GoogleChatStartupError):
        await transport.start()

    assert fired == []


@pytest.mark.asyncio
async def test_stop_clears_owned_client_and_later_start_rebuilds(tmp_path):
    transport = GoogleChatTransport(
        config=_runnable_cfg(tmp_path),
        credentials_factory=_fake_credentials_factory,
        serve=False,
    )

    await transport.start()
    first_client = transport.client
    assert isinstance(first_client, GoogleChatClient)

    await transport.stop()
    assert transport.client is None

    await transport.start()
    try:
        assert isinstance(transport.client, GoogleChatClient)
        assert transport.client is not first_client
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_repeated_start_preserves_owned_client_cleanup(tmp_path):
    transport = GoogleChatTransport(
        config=_runnable_cfg(tmp_path),
        credentials_factory=_fake_credentials_factory,
        serve=False,
    )

    await transport.start()
    first_client = transport.client
    assert isinstance(first_client, GoogleChatClient)

    await transport.start()
    assert transport.client is first_client

    await transport.stop()
    assert transport.client is None

    await transport.start()
    try:
        assert isinstance(transport.client, GoogleChatClient)
        assert transport.client is not first_client
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_queue_consumer_drains_events_to_dispatch(tmp_path):
    cfg = _runnable_cfg(tmp_path)

    def fake_credentials_factory(path, scopes):
        class _C:
            token = "fake"
            valid = True

            def refresh(self, request):
                pass

        return _C()

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=fake_credentials_factory,
        serve=False,
    )

    seen = []
    transport.on_message(lambda msg: seen.append(msg.text))

    await transport.start()
    try:
        verified = VerifiedGoogleChatRequest(
            issuer="https://accounts.google.com",
            audience="https://x.test/google-chat/events",
            subject="chat",
            email="chat@system.gserviceaccount.com",
            expires_at=1770000000,
            auth_mode="endpoint_url",
        )
        payload = {
            "type": "MESSAGE",
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
            "user": {"name": "users/111", "displayName": "R"},
        }
        await transport.enqueue_verified_event(payload, verified, headers={})

        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == ["hi"]
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_start_serves_http_endpoint(tmp_path):
    cfg = _runnable_cfg(tmp_path)
    cfg.host = "127.0.0.1"
    cfg.port = 0

    def fake_credentials_factory(path, scopes):
        class _C:
            token = "fake"
            valid = True

            def refresh(self, request):
                pass

        return _C()

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=fake_credentials_factory,
        serve=True,
    )

    await transport.start()
    try:
        url = f"http://127.0.0.1:{transport.bound_port}{cfg.endpoint_path}"
        async with httpx.AsyncClient() as http:
            response = await http.post(url, json={"type": "MESSAGE"})
        assert response.status_code == 401
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_start_port_conflict_cleans_up_partial_startup(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    cfg = _runnable_cfg(tmp_path)
    cfg.host = "127.0.0.1"
    cfg.port = port

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=_fake_credentials_factory,
        serve=True,
    )
    ready = []
    transport.on_ready(lambda identity: ready.append(identity))

    try:
        with pytest.raises(RuntimeError, match=f"{cfg.host}:{cfg.port}"):
            await transport.start()

        assert ready == []
        assert transport._consumer_task is None
        assert transport.client is None
        assert transport._http is None
        assert transport._owns_client is False
        assert transport._server_task is None
        assert transport._uvicorn_server is None
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_stop_allows_on_stop_callback_to_send_before_client_close(tmp_path):
    cfg = _runnable_cfg(tmp_path)
    cfg.host = "127.0.0.1"
    cfg.port = 0
    client = _FinalSendClient()
    transport = GoogleChatTransport(config=cfg, client=client, serve=True)
    transport._http = _FinalSendHttp(client)
    transport._owns_client = True
    callback_seen = []

    async def final_send():
        callback_seen.append(transport._uvicorn_server is None)
        await transport.send_text(
            ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM),
            "final shutdown message",
        )

    transport.on_stop(final_send)

    await transport.start()
    await transport.stop()

    assert callback_seen == [True]
    assert len(client.calls) == 1
    assert client.calls[0]["space"] == "spaces/AAA"
    assert client.calls[0]["body"] == {"text": "final shutdown message"}
    assert client.calls[0]["thread_name"] is None
    assert client.calls[0]["request_id"].startswith("lp2c-")
    assert client.closed is True
    assert transport.client is None
