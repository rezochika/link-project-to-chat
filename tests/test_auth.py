from __future__ import annotations

from unittest.mock import MagicMock

from link_project_to_chat.auth import Authenticator
from link_project_to_chat.rate_limiter import RateLimiter


def _make_user(user_id: int, username: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.username = username
    return u


def test_fail_closed_when_no_username():
    auth = Authenticator(allowed_username="")
    assert auth.authenticate(_make_user(1, "alice")) is False


def test_first_contact_locks_user_id():
    auth = Authenticator(allowed_username="alice")
    user = _make_user(10, "Alice")  # case-insensitive
    assert auth.authenticate(user) is True
    assert auth.trusted_user_id == 10


def test_trusted_by_id_allowed():
    auth = Authenticator(allowed_username="alice", trusted_user_id=42)
    assert auth.authenticate(_make_user(42, "alice")) is True


def test_wrong_id_denied_and_counted():
    auth = Authenticator(allowed_username="alice", trusted_user_id=42)
    bad = _make_user(99, "hacker")
    assert auth.authenticate(bad) is False
    assert auth._failed_counts[99] == 1


def test_wrong_username_denied():
    auth = Authenticator(allowed_username="alice")
    assert auth.authenticate(_make_user(5, "mallory")) is False
    assert auth._failed_counts[5] == 1


def test_brute_force_blocked_after_5():
    auth = Authenticator(allowed_username="alice", trusted_user_id=42)
    bad = _make_user(7, "x")
    for _ in range(5):
        auth.authenticate(bad)
    # 6th attempt still blocked even if the counts equal exactly 5
    assert auth.authenticate(bad) is False


def test_on_trust_called():
    trusted_ids: list[int] = []
    auth = Authenticator(allowed_username="alice", on_trust=trusted_ids.append)
    auth.authenticate(_make_user(99, "alice"))
    assert trusted_ids == [99]


def test_rate_limited_after_limit():
    limiter = RateLimiter(max_per_minute=30)
    for _ in range(limiter.max_per_minute):
        assert limiter.is_limited(1) is False
    assert limiter.is_limited(1) is True


def test_rate_limit_independent_per_user():
    limiter = RateLimiter(max_per_minute=30)
    for _ in range(limiter.max_per_minute):
        limiter.is_limited(1)
    # User 2 should not be affected
    assert limiter.is_limited(2) is False
