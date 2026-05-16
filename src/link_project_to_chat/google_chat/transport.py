from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.transport.base import Identity

if TYPE_CHECKING:
    from .auth import VerifiedGoogleChatRequest
    from .client import GoogleChatClient

logger = logging.getLogger(__name__)


class GoogleChatTransport:
    transport_id = "google_chat"
    # 8 000 is the conservative *character* budget surfaced to callers
    # via the `max_text_length` capability. The hard *byte* ceiling is
    # `config.max_message_bytes` (default 32 000), enforced at send time
    # by `_check_message_bytes()`. 8 000 characters stays under 32 000
    # bytes even for 4-byte UTF-8 graphemes (emoji / non-BMP), so the
    # character cap can never produce an over-byte payload.
    max_text_length = 8000

    def __init__(
        self,
        *,
        config: GoogleChatConfig,
        client: "GoogleChatClient | None" = None,
    ) -> None:
        self.config = config
        # Tests pass a fake here; production wiring constructs the real
        # `GoogleChatClient` in `start()` once Task 9 lands.
        self.client = client
        self.self_identity = Identity(
            transport_id="google_chat",
            native_id="google_chat:app",
            display_name="Google Chat App",
            handle=None,
            is_bot=True,
        )
        self._pending_events: asyncio.Queue = asyncio.Queue()
        self._fast_ack_timeouts: int = 0

    @property
    def pending_event_count(self) -> int:
        return self._pending_events.qsize()

    def verify_request(self, headers) -> "VerifiedGoogleChatRequest":
        from .auth import verify_google_chat_request  # noqa: PLC0415

        return verify_google_chat_request(
            headers=headers,
            mode=self.config.auth_audience_type,
            audiences=self.config.allowed_audiences,
        )

    async def enqueue_verified_event(
        self,
        payload: dict,
        verified: "VerifiedGoogleChatRequest",
        *,
        headers: dict,
    ) -> None:
        self._pending_events.put_nowait({"payload": payload, "verified": verified, "headers": headers})

    def note_fast_ack_timeout(self) -> None:
        self._fast_ack_timeouts += 1
        logger.warning("Google Chat fast-ack budget exceeded; event dropped (total=%d)", self._fast_ack_timeouts)
