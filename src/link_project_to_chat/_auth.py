from __future__ import annotations

import collections
import logging
import time

logger = logging.getLogger(__name__)


class AuthMixin:
    """Identity-based auth backed by AllowedUser (sole source of truth).

    Set ``self._allowed_users: list[AllowedUser]`` in __init__. Empty list →
    fail-closed (every request denied).

    The legacy ``_allowed_usernames`` / ``_trusted_user_ids`` / ``_trusted_users``
    instance state was removed in Task 5 Step 3. ID-locking moves from a
    separate ``trusted_user_ids`` list to ``AllowedUser.locked_identities``
    (a list of ``"transport_id:native_id"`` strings), appended on first
    contact via ``_identity_key(identity)``. The list shape supports the same
    username across multiple transports (telegram:, web:, future
    discord:/slack:).
    """

    _allowed_users: list = []  # list[AllowedUser]; set by ProjectBot.__init__
    _auth_source: str = "project"  # "project" | "global"; set by ProjectBot.__init__
    _MAX_MESSAGES_PER_MINUTE: int = 30
    _MAX_FAILED_AUTH: int = 5
    _RELOAD_DEBOUNCE_SECONDS: float = 5.0

    def _init_auth(self) -> None:
        # Both dicts keyed on `_identity_key(identity)` = "transport_id:native_id"
        # so Discord/Slack/Telegram identities never collide.
        self._rate_limits: dict[str, collections.deque] = {}
        self._failed_auth_counts: dict[str, int] = {}
        # First-contact lock APPENDS to locked_identities in-memory; this flag
        # tells the bot's message-handling tail to call save_config once.
        self._auth_dirty: bool = False

    @staticmethod
    def _normalize_username(handle) -> str:
        if not handle:
            return ""
        return str(handle).strip().lstrip("@").lower()

    @staticmethod
    def _identity_key(identity) -> str:
        """Stable string key for rate-limit / failed-auth bookkeeping."""
        return f"{identity.transport_id}:{identity.native_id}"

    def _get_user_role(self, identity) -> str | None:
        """Return 'executor', 'viewer', or None.

        Order of checks:
          1. Identity-lock fast path: ``_identity_key(identity)`` is in any
             ``AllowedUser.locked_identities`` list. Security-critical —
             prevents username-spoof attacks. Works for every transport since
             the keys are ``transport_id:native_id`` strings.
          2. Username fallback: case- and @-insensitive match against an
             entry. Appends ``_identity_key(identity)`` to that AllowedUser's
             ``locked_identities`` (no replacement, just append if absent)
             and sets ``self._auth_dirty = True`` so the message-handling
             tail persists via save_config. Multi-transport users naturally
             accumulate identities here: a user authed first on Telegram
             gets ``["telegram:12345"]``, then later on Web appends
             ``"web:web-session:abc"`` so the list becomes
             ``["telegram:12345", "web:web-session:abc"]`` and both
             transports authenticate them.

        Same-transport spoof guard: if the user already has any locked
        identity from the same transport_id, the username fallback is
        REFUSED for any other native_id. Otherwise an attacker could rename
        themselves to "alice" and steal her telegram lock. Without this
        guard, the previous draft had a hole.
        """
        if not self._allowed_users:
            return None
        ident_key = self._identity_key(identity)
        transport_prefix = f"{identity.transport_id}:"
        # 1. Identity-lock fast path.
        for au in self._allowed_users:
            if ident_key in au.locked_identities:
                return au.role
        # 2. Username fallback — ONLY when no identity from THIS transport is
        # already locked for that user. If the user has a different identity
        # from the same transport locked (e.g., locked_identities=["fake:12345"]
        # and the incoming is "fake:11111"), the fast path missed AND there's
        # already a transport lock — this is a same-transport spoof attempt.
        # Deny without appending.
        uname = self._normalize_username(getattr(identity, "handle", ""))
        if not uname:
            return None
        for au in self._allowed_users:
            if self._normalize_username(au.username) != uname:
                continue
            native_id = str(getattr(identity, "native_id", ""))
            if identity.transport_id == "web" and native_id.startswith("web-user:"):
                asserted = self._normalize_username(native_id[len("web-user:"):])
                if asserted == uname:
                    if ident_key not in au.locked_identities:
                        au.locked_identities.append(ident_key)
                        self._auth_dirty = True
                    return au.role
            # Same-transport spoof guard. We only username-fallback when the
            # user has NO identity from this transport yet.
            if any(x.startswith(transport_prefix) for x in au.locked_identities):
                logger.warning(
                    "Same-transport spoof rejected: %s already has a %s lock; "
                    "ignoring incoming %s",
                    au.username, identity.transport_id, ident_key,
                )
                return None
            # First contact from this transport — append.
            au.locked_identities.append(ident_key)
            self._auth_dirty = True
            logger.info(
                "Locked identity %s for %s on first contact",
                ident_key, au.username,
            )
            return au.role
        return None

    def _auth_identity(self, identity) -> bool:
        """True iff identity resolves to any role. Fail-closed on empty."""
        if not self._allowed_users:
            return False
        key = self._identity_key(identity)
        if self._failed_auth_counts.get(key, 0) >= self._MAX_FAILED_AUTH:
            return False
        role = self._get_user_role(identity)
        if role is None:
            self._failed_auth_counts[key] = self._failed_auth_counts.get(key, 0) + 1
            return False
        return True

    def _require_executor(self, identity) -> bool:
        """True iff role is 'executor'."""
        return self._get_user_role(identity) == "executor"

    def _rate_limited(self, identity_key: str) -> bool:
        """Identity-keyed rate limiter. Caller passes _identity_key(identity)."""
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(identity_key, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False

    def _reload_allowed_users_if_stale(self) -> None:
        """Refresh self._allowed_users from disk, debounced to 5 s.

        Skips when:
          - self._auth_dirty is True (unsaved changes; would clobber)
          - <5 s have elapsed since last reload (debounce)

        On reload, merges by username:
          - disk role wins
          - locked_identities = union(disk, memory)
          - users in memory but not disk → dropped (operator removed them)
          - users on disk but not memory → appended (new users)

        Failures (missing file, parse error) log + keep current state +
        update the timestamp so we retry in 5 s.
        """
        if not hasattr(self, "_project_config_path"):
            return  # Subclass doesn't participate in hot-reload.
        if self._auth_dirty:
            return
        now = time.monotonic()
        if now - self._last_allowed_users_reload < self._RELOAD_DEBOUNCE_SECONDS:
            return
        self._last_allowed_users_reload = now
        try:
            from .config import load_config, resolve_project_allowed_users
            config = load_config(self._project_config_path)
            if self._project_name is not None:
                project = config.projects.get(self._project_name)
                if project is not None:
                    disk_users, _src = resolve_project_allowed_users(project, config)
                else:
                    disk_users = []
            else:
                # Manager-scope: global allowed_users.
                disk_users = list(config.allowed_users)
        except Exception:
            logger.warning(
                "hot-reload of allowed_users failed", exc_info=True,
            )
            return

        # Merge by username, preserving locked_identities union.
        from .config import AllowedUser
        disk_by_username = {u.username: u for u in disk_users}
        merged: list[AllowedUser] = []
        for mem_u in self._allowed_users:
            if mem_u.username in disk_by_username:
                d = disk_by_username.pop(mem_u.username)
                merged.append(AllowedUser(
                    username=d.username,
                    role=d.role,
                    locked_identities=list(
                        set(d.locked_identities) | set(mem_u.locked_identities)
                    ),
                ))
            # else: user removed on disk → drop
        # New users on disk that weren't in memory.
        merged.extend(disk_by_username.values())
        self._allowed_users = merged
