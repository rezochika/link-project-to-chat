"""Telethon group operations for the /create_team flow."""
from __future__ import annotations

import asyncio
import logging

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChatAdminRights

logger = logging.getLogger(__name__)

_FLOOD_WAIT_RETRY_THRESHOLD_SECONDS = 30


async def _call_with_flood_retry(client, request):
    """Invoke a Telethon TL request, retrying once on short FloodWaits."""
    try:
        return await client(request)
    except FloodWaitError as e:
        if e.seconds > _FLOOD_WAIT_RETRY_THRESHOLD_SECONDS:
            raise
        logger.info("FloodWait %ds, sleeping and retrying once", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await client(request)


async def create_supergroup(client, title: str) -> int:
    """Create a Telegram supergroup. Returns the Bot-API-style chat_id (-100...)."""
    resp = await _call_with_flood_retry(
        client,
        CreateChannelRequest(title=title, about="", megagroup=True),
    )
    raw_id = resp.chats[0].id
    return int(f"-100{raw_id}")
