from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.transport.fake import FakeTransport


@pytest.mark.asyncio
async def test_fake_transport_on_stop_callback_fires():
    transport = FakeTransport()
    cb = AsyncMock()
    transport.on_stop(cb)
    await transport.start()
    await transport.stop()
    cb.assert_awaited_once()
