from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.app import create_google_chat_app
from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest
from link_project_to_chat.google_chat.transport import GoogleChatTransport


@pytest.mark.asyncio
async def test_route_rejects_missing_token_before_queue():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    app = create_google_chat_app(transport)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/google-chat/events", json={"type": "MESSAGE"})

    assert response.status_code == 401
    assert transport.pending_event_count == 0


@pytest.mark.asyncio
async def test_route_fast_acks_valid_event():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))

    def verifier(headers):
        return VerifiedGoogleChatRequest(
            issuer="https://accounts.google.com",
            audience="https://x.test/google-chat/events",
            subject="chat",
            email="chat@system.gserviceaccount.com",
            expires_at=1770000000,
            auth_mode="endpoint_url",
        )

    app = create_google_chat_app(transport, request_verifier=verifier)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/google-chat/events", headers={"authorization": "Bearer ok"}, json={"type": "MESSAGE"})

    assert response.status_code == 200
    assert response.json() == {}
    assert transport.pending_event_count == 1


@pytest.mark.asyncio
async def test_route_runs_slow_sync_verification_off_event_loop():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    verifier_started = threading.Event()
    verifier_can_finish = threading.Event()

    def verifier(headers):
        verifier_started.set()
        verifier_can_finish.wait(timeout=1.0)
        return VerifiedGoogleChatRequest(
            issuer="https://accounts.google.com",
            audience="https://x.test/google-chat/events",
            subject="chat",
            email="chat@system.gserviceaccount.com",
            expires_at=1770000000,
            auth_mode="endpoint_url",
        )

    app = create_google_chat_app(transport, request_verifier=verifier)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request_task = asyncio.create_task(
            client.post("/google-chat/events", headers={"authorization": "Bearer ok"}, json={"type": "MESSAGE"})
        )
        try:
            assert await asyncio.to_thread(verifier_started.wait, 1.0)

            loop_ran = asyncio.Event()

            async def set_loop_ran():
                loop_ran.set()

            sentinel_task = asyncio.create_task(set_loop_ran())
            await asyncio.wait_for(loop_ran.wait(), timeout=1.0)
            await sentinel_task

            assert not request_task.done()
            verifier_can_finish.set()
            response = await request_task
        finally:
            verifier_can_finish.set()
            if not request_task.done():
                request_task.cancel()
                try:
                    await request_task
                except asyncio.CancelledError:
                    pass

    assert response.status_code == 200
    assert transport.pending_event_count == 1
