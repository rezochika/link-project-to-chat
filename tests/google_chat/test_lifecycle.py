from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("httpx")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest
from link_project_to_chat.google_chat.client import GoogleChatClient
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.google_chat.validators import GoogleChatStartupError


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
