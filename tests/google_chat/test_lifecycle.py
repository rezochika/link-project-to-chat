from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.client import GoogleChatClient
from link_project_to_chat.google_chat.transport import GoogleChatTransport


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


@pytest.mark.asyncio
async def test_start_constructs_google_chat_client_when_none_injected(tmp_path):
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

    await transport.start()
    try:
        assert isinstance(transport.client, GoogleChatClient)
    finally:
        await transport.stop()
