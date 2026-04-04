from __future__ import annotations

import collections
import logging
import time

logger = logging.getLogger(__name__)


class AuthMixin:
    """Username-based auth with user_id locking, brute-force protection, and rate limiting."""

    _allowed_username: str = ""
    _trusted_user_id: int | None = None
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}
        self._failed_auth_counts: dict[int, int] = {}

    def _on_trust(self, user_id: int) -> None:
        """Called when a user_id is trusted for the first time. Override to persist."""

    def _auth(self, user) -> bool:
        if not self._allowed_username:
            return False  # fail-closed
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return False
        if self._trusted_user_id is not None:
            if user.id != self._trusted_user_id:
                self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
                return False
            return True
        if (user.username or "").lower() == self._allowed_username:
            self._trusted_user_id = user.id
            self._on_trust(user.id)
            logger.info("Trusted user_id %d saved", user.id)
            return True
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
        return False

    def _rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(user_id, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
