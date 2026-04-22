"""M10 — auth tests: concurrent attempts, exact rate-limit boundary, field precedence."""
from __future__ import annotations

import collections
import time
from unittest.mock import MagicMock

import pytest

from link_project_to_chat._auth import AuthMixin


def _make_user(user_id: int, username: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.username = username
    return u


class _Bot(AuthMixin):
    def __init__(self, username: str = "alice", trusted_id: int | None = None):
        self._allowed_username = username
        self._trusted_user_id = trusted_id
        self._init_auth()


class _MultiBot(AuthMixin):
    def __init__(self, usernames: list[str] | None = None, trusted_ids: list[int] | None = None):
        self._allowed_usernames = usernames or []
        self._trusted_user_ids = trusted_ids or []
        self._init_auth()


# ---------------------------------------------------------------------------
# Concurrent attempt safety
# ---------------------------------------------------------------------------


def test_interleaved_bad_attempts_accumulate_per_user():
    """Failures from different users don't bleed into each other's counters."""
    bot = _MultiBot(usernames=["alice"])
    for _ in range(3):
        bot._auth(_make_user(10, "mallory"))
    for _ in range(2):
        bot._auth(_make_user(20, "eve"))
    assert bot._failed_auth_counts[10] == 3
    assert bot._failed_auth_counts[20] == 2


def test_lockout_survives_interleaved_good_and_bad():
    """Lockout of user A is not cleared by successful auth of user B."""
    bot = _MultiBot(usernames=["alice", "bob"])
    bad = _make_user(10, "mallory")
    good = _make_user(20, "alice")
    for _ in range(5):
        bot._auth(bad)
    bot._auth(good)  # bob logs in successfully
    # mallory is still locked out
    assert bot._auth(bad) is False


def test_brute_force_exact_threshold():
    """5 failures lock out; the 5th attempt itself still gets rejected."""
    bot = _Bot()
    bad = _make_user(7, "x")
    results = [bot._auth(bad) for _ in range(6)]
    assert results == [False, False, False, False, False, False]
    assert bot._failed_auth_counts[7] == 5  # counter stops at lockout threshold


# ---------------------------------------------------------------------------
# Rate-limit boundary at exactly 30 msg/min
# ---------------------------------------------------------------------------


def test_rate_limit_exactly_30_allowed():
    """The 30th message is accepted; the 31st is rejected."""
    bot = _Bot(trusted_id=1)
    for i in range(30):
        assert bot._rate_limited(1) is False, f"message {i+1} should not be rate-limited"
    assert bot._rate_limited(1) is True


def test_rate_limit_window_expires():
    """After the 60s window passes, the counter resets and traffic is allowed again."""
    bot = _Bot(trusted_id=1)
    now = time.monotonic()
    # Backfill 30 timestamps that are just over 60s old — they should be evicted.
    bucket = bot._rate_limits.setdefault(1, collections.deque())
    for _ in range(30):
        bucket.append(now - 61)
    # Window has expired; next message should be allowed.
    assert bot._rate_limited(1) is False


# ---------------------------------------------------------------------------
# Multi-user mode field precedence
# ---------------------------------------------------------------------------


def test_multi_user_list_takes_precedence_over_singular():
    """_allowed_usernames (list) wins when both singular and list fields are set."""
    bot = _MultiBot(usernames=["bob"])
    bot._allowed_username = "alice"  # also set the legacy field
    # "alice" should NOT be allowed because list mode wins
    assert bot._auth(_make_user(1, "alice")) is False
    assert bot._auth(_make_user(2, "bob")) is True


def test_trusted_ids_list_takes_precedence_over_singular():
    """_trusted_user_ids (list) wins; singular _trusted_user_id is ignored."""
    bot = _MultiBot(usernames=["alice"], trusted_ids=[99])
    bot._trusted_user_id = 42  # also set the legacy field
    assert bot._auth(_make_user(99, "x")) is True   # list id accepted
    assert bot._auth(_make_user(42, "x")) is False  # singular id ignored


def test_multi_user_mode_detected_correctly():
    """_is_multi_user_mode() returns True when list fields are instance attrs."""
    multi = _MultiBot(usernames=["alice"])
    single = _Bot()
    assert multi._is_multi_user_mode() is True
    assert single._is_multi_user_mode() is False
