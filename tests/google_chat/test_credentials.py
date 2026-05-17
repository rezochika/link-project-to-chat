from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx")
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
