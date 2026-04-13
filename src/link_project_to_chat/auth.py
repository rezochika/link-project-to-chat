"""Authentication as a standalone injectable class.

Separated from rate limiting and extracted from mixin pattern for DI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .protocols import TelegramUser

logger = logging.getLogger(__name__)


class Authenticator:
    """Username-based auth with user_id locking and brute-force protection."""

    def __init__(
        self,
        allowed_username: str,
        trusted_user_id: int | None = None,
        on_trust: Callable[[int], None] | None = None,
        max_failed_attempts: int = 5,
    ) -> None:
        self._allowed_username = allowed_username.lower().lstrip("@") if allowed_username else ""
        self._trusted_user_id = trusted_user_id
        self._on_trust = on_trust
        self._max_failed_attempts = max_failed_attempts
        self._failed_counts: dict[int, int] = {}

    @property
    def trusted_user_id(self) -> int | None:
        return self._trusted_user_id

    @trusted_user_id.setter
    def trusted_user_id(self, value: int | None) -> None:
        self._trusted_user_id = value

    def authenticate(self, user: TelegramUser | None) -> bool:
        """Check if a user is authorized. Returns True if allowed."""
        if user is None:
            return False
        if not self._allowed_username:
            return False  # fail-closed
        if self._failed_counts.get(user.id, 0) >= self._max_failed_attempts:
            return False
        if self._trusted_user_id is not None:
            if user.id != self._trusted_user_id:
                self._failed_counts[user.id] = self._failed_counts.get(user.id, 0) + 1
                return False
            return True
        if (user.username or "").lower() == self._allowed_username:
            self._trusted_user_id = user.id
            if self._on_trust:
                self._on_trust(user.id)
            logger.info("Trusted user_id %d saved", user.id)
            return True
        self._failed_counts[user.id] = self._failed_counts.get(user.id, 0) + 1
        return False
