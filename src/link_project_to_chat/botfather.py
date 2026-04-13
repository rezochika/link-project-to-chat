from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import User
except ImportError:
    TelegramClient = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\d{7,15}:[A-Za-z0-9_-]{20,50}")
_BOTFATHER = "BotFather"


def sanitize_bot_username(name: str) -> str:
    """Convert a project name to a valid bot username (must end with 'bot')."""
    clean = re.sub(r"[^a-z0-9_]", "_", name.lower().replace("-", "_"))
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "project"
    return f"{clean}_claude_bot"


def extract_token(text: str) -> str | None:
    """Extract a bot token from BotFather's response text."""
    match = _TOKEN_RE.search(text)
    return match.group(0) if match else None


class BotFatherClient:
    def __init__(self, api_id: int, api_hash: str, session_path: Path):
        if TelegramClient is None:
            raise ImportError(
                "telethon is required for BotFather automation. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._client: TelegramClient | None = None

    async def _ensure_client(self) -> TelegramClient:
        if self._client is None:
            self._client = TelegramClient(
                str(self._session_path), self._api_id, self._api_hash
            )
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    @property
    def is_authenticated(self) -> bool:
        return self._session_path.exists()

    async def authenticate(self, phone: str, code_callback, password_callback=None) -> None:
        client = await self._ensure_client()
        await client.start(phone=phone, code_callback=code_callback, password=password_callback)
        self._session_path.chmod(0o600)

    async def create_bot(self, display_name: str, username: str) -> str:
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            raise Exception("Not authenticated. Run /setup first.")
        entity = await client.get_entity(_BOTFATHER)
        await client.send_message(entity, "/newbot")
        await asyncio.sleep(1.5)
        await client.send_message(entity, display_name)
        await asyncio.sleep(1.5)
        max_retries = 3
        for attempt in range(max_retries + 1):
            trial_username = username if attempt == 0 else f"{username.rstrip('bot')}_{attempt + 1}_bot"
            if not trial_username.endswith("bot"):
                trial_username += "_bot"
            await client.send_message(entity, trial_username)
            await asyncio.sleep(2)
            messages = await client.get_messages(entity, limit=1)
            if not messages:
                continue
            response_text = messages[0].text or ""
            token = extract_token(response_text)
            if token:
                logger.info("Created bot @%s", trial_username)
                return token
            if "not available" in response_text.lower() or "already" in response_text.lower():
                logger.info("Username @%s taken, retrying...", trial_username)
                if attempt < max_retries:
                    await asyncio.sleep(3 * (attempt + 1))
                    await client.send_message(entity, "/newbot")
                    await asyncio.sleep(1.5)
                    await client.send_message(entity, display_name)
                    await asyncio.sleep(1.5)
                continue
            raise Exception(f"Unexpected BotFather response: {response_text[:200]}")
        raise Exception(f"Failed to create bot after {max_retries} retries. All username variants of '{username}' were taken.")

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected():
            await self._client.disconnect()
