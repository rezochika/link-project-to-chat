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
        self._rate_limits: dict[str, collections.deque] = {}
        # Mixed-key dict: int for legacy Telegram callers (user.id is int post
        # int-coercion in _auth_identity), str for non-numeric platforms
        # (Discord snowflakes, Slack channel ids) that fall through the
        # int-cast in _auth_identity. Migration of _auth itself to a
        # platform-neutral key is deferred to spec #0a.
        self._failed_auth_counts: dict[int | str, int] = {}

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
        """Return trusted user bindings scoped to the current allowlist.

        Values are coerced to int when possible; non-numeric ids
        (Discord/Slack) are passed through unchanged so the in-memory
        trust dict can still hold them. Real multi-platform persistence
        is spec #0a's problem.
        """
        allowed = [self._normalize_username(username) for username in self._get_allowed_usernames()]
        allowed_set = set(allowed)

        trusted_users = getattr(self, "_trusted_users", {}) or {}
        effective: dict[str, int] = {}
        for username, user_id in trusted_users.items():
            normalized = self._normalize_username(username)
            if normalized in allowed_set:
                effective[normalized] = self._coerce_trust_value(user_id)
        if effective:
            return effective

        if self._trusted_user_id is not None and self._allowed_username:
            normalized = self._normalize_username(self._allowed_username)
            if normalized in allowed_set:
                return {normalized: self._coerce_trust_value(self._trusted_user_id)}

        return {
            username: self._coerce_trust_value(user_id)
            for username, user_id in zip(allowed, self._trusted_user_ids)
        }

    @staticmethod
    def _coerce_trust_value(user_id) -> int:
        """Coerce a stored trust id to int, passing non-numeric values through.

        Non-numeric ids (Discord/Slack) stay as-is; type-check warnings about
        the dict[str, int] return are accepted because spec #0a will widen
        these signatures alongside persistence.
        """
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return user_id  # type: ignore[return-value]

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
        """Record a newly trusted user binding in the appropriate field.

        For non-numeric ids (Discord snowflakes, Slack ids) the int cast
        falls back to storing the raw value; the int-trusted-id fast path
        in ``_auth`` then silently mismatches by type and the user falls
        through to the username match — functionally correct, small perf
        hit. Real multi-platform persistence is spec #0a's problem.
        """
        normalized = self._normalize_username(username)
        try:
            stored_id: int | str = int(user_id)
        except (TypeError, ValueError):
            stored_id = user_id
        trusted_users = self._get_trusted_user_bindings()
        trusted_users[normalized] = stored_id  # type: ignore[assignment]
        self._trusted_users = trusted_users
        if self._is_multi_user_mode():
            self._trusted_user_ids = list(trusted_users.values())
        else:
            self._trusted_user_id = stored_id  # type: ignore[assignment]

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
        """Return True if *user* is authorised to use this bot.

        Flow: fail-closed if no usernames configured → brute-force lockout (5
        failures) → trusted-ID fast path → username match (locks in user.id so
        subsequent messages skip the username lookup). Multi-user mode uses
        _allowed_usernames (list); legacy single-user mode wraps _allowed_username
        in a list automatically via _get_allowed_usernames().
        """
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
            logger.info("Trusted user_id %s saved", user.id)
            return True
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
        return False

    def _auth_identity(self, identity) -> bool:
        """Authorize based on a transport Identity. Wraps _auth.

        Telegram-only legacy: _auth still consumes user.id as int because
        _trusted_users persistence is int-typed. We coerce here so the
        boundary is contained, but rate-limit/failed-auth dicts use the
        platform-neutral identity-key (transport_id:native_id) directly.
        """
        from types import SimpleNamespace
        # _auth still expects user.id to be int-comparable against _trusted_users.values().
        # Until persistence migrates, keep the cast scoped to this one site.
        try:
            uid = int(identity.native_id)
        except (TypeError, ValueError):
            uid = identity.native_id  # non-numeric: skip the int-trusted-id fast path; username match still works.
        user = SimpleNamespace(id=uid, username=identity.handle or "")
        return self._auth(user)

    @staticmethod
    def _identity_key(identity) -> str:
        """Stable string key for rate-limit / failed-auth bookkeeping.

        Includes transport_id so the same numeric id from different platforms
        doesn't collide (telegram:42 vs discord:42).
        """
        return f"{identity.transport_id}:{identity.native_id}"

    def _rate_limited(self, identity_key: str) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(identity_key, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
