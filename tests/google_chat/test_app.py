from __future__ import annotations

import asyncio

import pytest
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
