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
    _trusted_users: dict[str, int] = {}  # canonical username -> user_id binding
    _trusted_user_id: int | None = None  # legacy single-user
    _trusted_user_ids: list[int] = []    # legacy multi-user input
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}
        self._failed_auth_counts: dict[int, int] = {}

    def _get_allowed_usernames(self) -> list[str]:
        """Return the effective list of allowed usernames."""
        if self._allowed_usernames:
            return self._allowed_usernames
        if self._allowed_username:
            return [self._allowed_username]
        return []

    @staticmethod
    def _normalize_username(username: str | None) -> str:
        return (username or "").lower().lstrip("@")

    def _get_trusted_user_bindings(self) -> dict[str, int]:
        """Return trusted user bindings scoped to the current allowlist."""
        allowed = [self._normalize_username(username) for username in self._get_allowed_usernames()]
        allowed_set = set(allowed)

        trusted_users = getattr(self, "_trusted_users", {}) or {}
        effective: dict[str, int] = {}
        for username, user_id in trusted_users.items():
            normalized = self._normalize_username(username)
            if normalized in allowed_set:
                effective[normalized] = int(user_id)
        if effective:
            return effective

        if self._trusted_user_id is not None and self._allowed_username:
            normalized = self._normalize_username(self._allowed_username)
            if normalized in allowed_set:
                return {normalized: int(self._trusted_user_id)}

        return {
            username: int(user_id)
            for username, user_id in zip(allowed, self._trusted_user_ids)
        }

    def _get_trusted_user_ids(self) -> list[int]:
        """Return the effective list of trusted user IDs."""
        return list(self._get_trusted_user_bindings().values())

    def _on_trust(self, user_id: int, username: str) -> None:
        """Called when a user_id is trusted for the first time. Override to persist."""

    def _is_multi_user_mode(self) -> bool:
        """Return True if this instance uses multi-user (list) fields vs legacy singular fields."""
        # Multi-user mode when _trusted_user_ids or _allowed_usernames is an instance attribute
        # (set explicitly in __init__), not just an inherited class-level default.
        return (
            '_trusted_user_ids' in self.__dict__
            or '_allowed_usernames' in self.__dict__
        )

    def _trust_user(self, user_id: int, username: str) -> None:
        """Record a newly trusted user binding in the appropriate field."""
        normalized = self._normalize_username(username)
        trusted_users = self._get_trusted_user_bindings()
        trusted_users[normalized] = int(user_id)
        self._trusted_users = trusted_users
        if self._is_multi_user_mode():
            self._trusted_user_ids = list(trusted_users.values())
        else:
            self._trusted_user_id = int(user_id)

    def _revoke_user(self, username: str) -> None:
        """Remove any stored trusted binding for a username."""
        normalized = self._normalize_username(username)
        trusted_users = self._get_trusted_user_bindings()
        trusted_users.pop(normalized, None)
        self._trusted_users = trusted_users
        if self._is_multi_user_mode():
            self._trusted_user_ids = list(trusted_users.values())
        else:
            self._trusted_user_id = next(iter(trusted_users.values()), None)

    def _auth(self, user) -> bool:
        allowed = [self._normalize_username(username) for username in self._get_allowed_usernames()]
        if not allowed:
            return False  # fail-closed
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return False
        trusted = self._get_trusted_user_bindings()
        if trusted and user.id in trusted.values():
            return True
        # Allow any allowed username to get trusted (even if other IDs exist)
        username = self._normalize_username(user.username)
        if username in allowed:
            self._trust_user(user.id, username)
            self._on_trust(user.id, username)
            logger.info("Trusted user_id %d saved", user.id)
            return True
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
        return False

    def _auth_identity(self, identity) -> bool:
        """Authorize based on a transport Identity. Wraps _auth."""
        from types import SimpleNamespace
        user = SimpleNamespace(
            id=int(identity.native_id),
            username=identity.handle or "",
        )
        return self._auth(user)

    def _rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(user_id, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
