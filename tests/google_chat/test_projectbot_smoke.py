from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import httpx


@pytest.mark.asyncio
async def test_projectbot_google_chat_end_to_end(tmp_path: Path):
    """Regression for the Google Chat ProjectBot lifecycle integration path."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import Config, GoogleChatConfig
    from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest

    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    cfg = Config(
        google_chat=GoogleChatConfig(
            service_account_file=str(sa),
            app_id="app-1",
            allowed_audiences=["https://x.test/google-chat/events"],
            root_command_id=1,
            host="127.0.0.1",
            port=0,
        ),
    )

    bot = ProjectBot(
        name="x",
        path=tmp_path,
        token="",
        config=cfg,
        transport_kind="google_chat",
    )

    # build() must not raise: this covers ProjectBot -> transport on_ready wiring.
    bot.build()
    transport = bot._transport
    assert transport.transport_id == "google_chat"

    # ProjectBot registers auth as the transport authorizer. Empty allowed_users
    # fail closed, so this lifecycle smoke bypasses auth after build().
    transport.set_authorizer(lambda identity: True)

    sent_messages: list[dict] = []

    class _FakeCreds:
        token = "fake"
        valid = True

        def refresh(self, request):
            pass

    transport._credentials_factory = lambda path, scopes: _FakeCreds()

    class _FakeClient:
        async def create_message(
            self,
            space,
            body,
            *,
            thread_name=None,
            request_id=None,
            message_reply_option=None,
        ):
            sent_messages.append({"space": space, "body": body, "thread_name": thread_name})
            return {"name": f"{space}/messages/1"}

        async def update_message(self, name, body, *, update_mask, allow_missing=False):
            return {}

    transport.client = _FakeClient()

    incoming: list[str] = []

    async def on_msg(msg):
        incoming.append(msg.text)
        await transport.send_text(msg.chat, f"echo: {msg.text}")

    transport.on_message(on_msg)

    transport.verify_request = lambda headers: VerifiedGoogleChatRequest(
        issuer="https://accounts.google.com",
        audience="https://x.test/google-chat/events",
        subject="chat",
        email="chat@system.gserviceaccount.com",
        expires_at=1770000000,
        auth_mode="endpoint_url",
    )

    await transport.start()
    try:
        url = f"http://127.0.0.1:{transport.bound_port}{cfg.google_chat.endpoint_path}"
        event = {
            "type": "MESSAGE",
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/in-1", "text": "hello"},
            "user": {"name": "users/111", "displayName": "R"},
        }
        async with httpx.AsyncClient() as http:
            response = await http.post(url, headers={"authorization": "Bearer fake"}, json=event)
        assert response.status_code == 200

        for _ in range(100):
            if incoming and sent_messages:
                break
            await asyncio.sleep(0.01)

        assert incoming == ["hello"], f"inbound handler did not receive event; got {incoming!r}"
        assert any(message["body"]["text"] == "echo: hello" for message in sent_messages), (
            f"outbound send_text did not reach client; sent={sent_messages!r}"
        )
    finally:
        await transport.stop()
