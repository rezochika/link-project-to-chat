from __future__ import annotations

import collections
import logging
import time

logger = logging.getLogger(__name__)


class AuthMixin:
    """Username-based auth with user_id locking, brute-force protection, and rate limiting.

    Supports both multi-user (list) and legacy single-user fields.
    Set _allowed_usernames (list) for multi-user mode.
    Set _allowed_username (str) for legacy single-user mode (auto-wrapped to list).
    """

    _allowed_username: str = ""          # legacy single-user
    _allowed_usernames: list[str] = []   # multi-user
    _trusted_user_id: int | None = None  # legacy single-user
    _trusted_user_ids: list[int] = []    # multi-user
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}
        self._failed_auth_counts: dict[int, int] = {}
        # Track how many trusted IDs existed at startup (before any auth calls).
        # When > 0, the trusted list is considered "sealed" and new usernames cannot
        # be added dynamically — only pre-seeded IDs are accepted.
        if '_trusted_user_ids' in self.__dict__:
            self._initial_trusted_count = len(self._trusted_user_ids)
        else:
            self._initial_trusted_count = 0

    def _get_allowed_usernames(self) -> list[str]:
        """Return the effective list of allowed usernames."""
        if self._allowed_usernames:
            return self._allowed_usernames
        if self._allowed_username:
            return [self._allowed_username]
        return []

    def _get_trusted_user_ids(self) -> list[int]:
        """Return the effective list of trusted user IDs."""
        if self._trusted_user_ids:
            return list(self._trusted_user_ids)
        if self._trusted_user_id is not None:
            return [self._trusted_user_id]
        return []

    def _on_trust(self, user_id: int) -> None:
        """Called when a user_id is trusted for the first time. Override to persist."""

    def _is_multi_user_mode(self) -> bool:
        """Return True if this instance uses multi-user (list) fields vs legacy singular fields."""
        # Multi-user mode when _trusted_user_ids or _allowed_usernames is an instance attribute
        # (set explicitly in __init__), not just an inherited class-level default.
        return (
            '_trusted_user_ids' in self.__dict__
            or '_allowed_usernames' in self.__dict__
        )

    def _trust_user(self, user_id: int) -> None:
        """Record a newly trusted user_id in the appropriate field."""
        if self._is_multi_user_mode():
            self._trusted_user_ids.append(user_id)
        else:
            self._trusted_user_id = user_id

    def _auth(self, user) -> bool:
        allowed = self._get_allowed_usernames()
        if not allowed:
            return False  # fail-closed
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return False
        trusted = self._get_trusted_user_ids()
        if trusted:
            if user.id in trusted:
                return True
            # When the trusted list was pre-seeded at startup, it is sealed —
            # only those exact IDs are accepted; new usernames cannot join.
            if self._initial_trusted_count > 0:
                self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
                return False
            # Trusted list grew dynamically: allow new allowed usernames to join.
            username = (user.username or "").lower()
            if username in allowed:
                self._trust_user(user.id)
                self._on_trust(user.id)
                logger.info("Trusted user_id %d saved", user.id)
                return True
            self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
            return False
        # No trusted IDs yet — trust on first contact
        username = (user.username or "").lower()
        if username in allowed:
            self._trust_user(user.id)
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
