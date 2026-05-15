"""M10 — auth tests: concurrent attempts, exact rate-limit boundary.

Originally these tests covered legacy ``_allowed_username`` vs
``_allowed_usernames`` precedence. Task 5 removed the legacy fields; the
precedence tests are gone with them. The concurrent-attempt and rate-limit
boundary cases stay, rewritten on top of ``_allowed_users``.
"""
from __future__ import annotations

import collections
import time

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


def _identity(username: str | None, native_id: int | str) -> Identity:
    return Identity(
        transport_id="telegram",
        native_id=str(native_id),
        display_name=username or "",
        handle=username or "",
        is_bot=False,
    )


class _Bot(AuthMixin):
    def __init__(self, allowed_users: list[AllowedUser] | None = None):
        self._allowed_users = list(allowed_users or [])
        self._init_auth()


# ---------------------------------------------------------------------------
# Concurrent attempt safety
# ---------------------------------------------------------------------------


def test_interleaved_bad_attempts_accumulate_per_user():
    """Failures from different users don't bleed into each other's counters."""
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    for _ in range(3):
        bot._auth_identity(_identity("mallory", 10))
    for _ in range(2):
        bot._auth_identity(_identity("eve", 20))
    assert bot._failed_auth_counts["telegram:10"] == 3
    assert bot._failed_auth_counts["telegram:20"] == 2


def test_lockout_survives_interleaved_good_and_bad():
    """Lockout of user A is not cleared by successful auth of user B."""
    bot = _Bot(allowed_users=[
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor"),
    ])
    bad = _identity("mallory", 10)
    good = _identity("alice", 20)
    for _ in range(5):
        bot._auth_identity(bad)
    bot._auth_identity(good)  # alice logs in successfully
    # mallory is still locked out
    assert bot._auth_identity(bad) is False


def test_brute_force_exact_threshold():
    """5 failures lock out; the 5th attempt itself still gets rejected."""
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:1"],
    )
    bot = _Bot(allowed_users=[au])
    bad = _identity("x", 7)
    results = [bot._auth_identity(bad) for _ in range(6)]
    assert results == [False, False, False, False, False, False]
    assert bot._failed_auth_counts["telegram:7"] == 5  # counter stops at lockout threshold


# ---------------------------------------------------------------------------
# Rate-limit boundary at exactly 30 msg/min
# ---------------------------------------------------------------------------


def test_rate_limit_exactly_30_allowed():
    """The 30th message is accepted; the 31st is rejected."""
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    key = "telegram:1"
    for i in range(30):
        assert bot._rate_limited(key) is False, f"message {i + 1} should not be rate-limited"
    assert bot._rate_limited(key) is True


def test_rate_limit_window_expires():
    """After the 60s window passes, the counter resets and traffic is allowed again."""
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    key = "telegram:1"
    now = time.monotonic()
    # Backfill 30 timestamps that are just over 60s old — they should be evicted.
    bucket = bot._rate_limits.setdefault(key, collections.deque())
    for _ in range(30):
        bucket.append(now - 61)
    # Window has expired; next message should be allowed.
    assert bot._rate_limited(key) is False
