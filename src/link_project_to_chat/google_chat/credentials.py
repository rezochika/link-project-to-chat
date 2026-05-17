from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from link_project_to_chat.config import GoogleChatConfig

GOOGLE_CHAT_BASE_URL = "https://chat.googleapis.com"
GOOGLE_CHAT_SCOPES = ("https://www.googleapis.com/auth/chat.bot",)


def _default_credentials_factory(path: str, scopes: tuple[str, ...]) -> Any:
    from google.oauth2 import service_account  # noqa: PLC0415

    return service_account.Credentials.from_service_account_file(path, scopes=list(scopes))


class _GoogleAuth(httpx.Auth):
    """httpx auth that refreshes the service-account token on demand."""

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials

    def _ensure_fresh(self) -> None:
        from google.auth.transport.requests import Request  # noqa: PLC0415

        if not getattr(self._credentials, "valid", False):
            self._credentials.refresh(Request())

    def auth_flow(self, request):
        self._ensure_fresh()
        token = getattr(self._credentials, "token", None)
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        yield request

    async def async_auth_flow(self, request):
        await asyncio.to_thread(self._ensure_fresh)
        token = getattr(self._credentials, "token", None)
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        yield request


def build_google_chat_http_client(
    cfg: GoogleChatConfig,
    *,
    credentials_factory: Callable[[str, tuple[str, ...]], Any] | None = None,
) -> httpx.AsyncClient:
    factory = credentials_factory or _default_credentials_factory
    credentials = factory(cfg.service_account_file, GOOGLE_CHAT_SCOPES)
    return httpx.AsyncClient(
        base_url=GOOGLE_CHAT_BASE_URL,
        auth=_GoogleAuth(credentials),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
