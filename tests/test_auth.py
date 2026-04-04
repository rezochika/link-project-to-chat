from __future__ import annotations

from unittest.mock import MagicMock

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


def test_fail_closed_when_no_username():
    bot = _Bot(username="")
    assert bot._auth(_make_user(1, "alice")) is False


def test_first_contact_locks_user_id():
    bot = _Bot()
    user = _make_user(10, "Alice")  # case-insensitive
    assert bot._auth(user) is True
    assert bot._trusted_user_id == 10


def test_trusted_by_id_allowed():
    bot = _Bot(trusted_id=42)
    assert bot._auth(_make_user(42, "alice")) is True


def test_wrong_id_denied_and_counted():
    bot = _Bot(trusted_id=42)
    bad = _make_user(99, "hacker")
    assert bot._auth(bad) is False
    assert bot._failed_auth_counts[99] == 1


def test_wrong_username_denied():
    bot = _Bot()
    assert bot._auth(_make_user(5, "mallory")) is False
    assert bot._failed_auth_counts[5] == 1


def test_brute_force_blocked_after_5():
    bot = _Bot(trusted_id=42)
    bad = _make_user(7, "x")
    for _ in range(5):
        bot._auth(bad)
    # 6th attempt still blocked even if the counts equal exactly 5
    assert bot._auth(bad) is False


def test_on_trust_called():
    trusted_ids = []

    class _PersistBot(_Bot):
        def _on_trust(self, user_id: int) -> None:
            trusted_ids.append(user_id)

    bot = _PersistBot()
    bot._auth(_make_user(99, "alice"))
    assert trusted_ids == [99]


def test_rate_limited_after_limit():
    bot = _Bot()
    for _ in range(bot._MAX_MESSAGES_PER_MINUTE):
        assert bot._rate_limited(1) is False
    assert bot._rate_limited(1) is True


def test_rate_limit_independent_per_user():
    bot = _Bot()
    for _ in range(bot._MAX_MESSAGES_PER_MINUTE):
        bot._rate_limited(1)
    # User 2 should not be affected
    assert bot._rate_limited(2) is False
