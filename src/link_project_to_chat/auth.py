"""Authentication as a standalone injectable class.

Separated from rate limiting and extracted from mixin pattern for DI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .protocols import TelegramUser
from .roles import Role

logger = logging.getLogger(__name__)


class Authenticator:
    """Username-based auth with user_id locking and brute-force protection.

    Supports multi-user mode via ``allowed_users`` (list of
    ``{"username": ..., "role": ...}`` dicts).  When ``allowed_users`` is
    provided it takes precedence over the legacy ``allowed_username`` scalar.
    If only ``allowed_username`` is set, the single user is treated as admin
    for backward compatibility.
    """

    def __init__(
        self,
        allowed_username: str,
        trusted_user_id: int | None = None,
        on_trust: Callable[[int], None] | None = None,
        max_failed_attempts: int = 5,
        allowed_users: list[dict[str, str]] | None = None,
    ) -> None:
        self._allowed_username = allowed_username.lower().lstrip("@") if allowed_username else ""
        self._trusted_user_id = trusted_user_id
        self._on_trust = on_trust
        self._max_failed_attempts = max_failed_attempts
        self._failed_counts: dict[int, int] = {}

        # Multi-user support: map normalised username -> Role
        self._user_roles_by_name: dict[str, Role] = {}
        # Resolved user_id -> Role after authentication
        self._user_roles: dict[int, Role] = {}

        if allowed_users is not None:
            for entry in allowed_users:
                uname = entry.get("username", "").lower().lstrip("@")
                role_str = entry.get("role", "viewer")
                try:
                    role = Role(role_str)
                except ValueError:
                    role = Role.VIEWER
                if uname:
                    self._user_roles_by_name[uname] = role
            self._multi_user = True
        else:
            self._multi_user = False

    @property
    def allowed_username(self) -> str:
        return self._allowed_username

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

        if self._multi_user:
            return self._authenticate_multi(user)

        # Legacy single-user path (backward compatible)
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
            # Assign admin role for single-user backward compat
            self._user_roles[user.id] = Role.ADMIN
            if self._on_trust:
                self._on_trust(user.id)
            logger.info("Trusted user_id %d saved", user.id)
            return True
        self._failed_counts[user.id] = self._failed_counts.get(user.id, 0) + 1
        return False

    def _authenticate_multi(self, user: TelegramUser) -> bool:
        """Authenticate against the allowed_users list."""
        if self._failed_counts.get(user.id, 0) >= self._max_failed_attempts:
            return False

        # Already trusted
        if user.id in self._user_roles:
            return True

        username = (user.username or "").lower()
        if username in self._user_roles_by_name:
            role = self._user_roles_by_name[username]
            self._user_roles[user.id] = role
            # Set first trusted user id for backward compat (startup message etc.)
            if self._trusted_user_id is None:
                self._trusted_user_id = user.id
            if self._on_trust:
                self._on_trust(user.id)
            logger.info("Multi-user: trusted user_id %d as %s", user.id, role)
            return True

        self._failed_counts[user.id] = self._failed_counts.get(user.id, 0) + 1
        return False

    def get_role(self, user_id: int) -> Role | None:
        """Return the role for a trusted user, or None if unknown."""
        return self._user_roles.get(user_id)
