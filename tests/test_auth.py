from __future__ import annotations

from unittest.mock import MagicMock

from link_project_to_chat._auth import AuthMixin


def _make_user(user_id: int, username: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.username = username
    return u


class _Bot(AuthMixin):
    def __init__(
        self,
        username: str = "alice",
        trusted_id: int | None = None,
        trusted_users: dict[str, int] | None = None,
    ):
        self._allowed_username = username
        self._trusted_users = trusted_users or {}
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
    assert bot._get_trusted_user_bindings() == {"alice": 10}


def test_trusted_by_id_allowed():
    bot = _Bot(trusted_users={"alice": 42})
    assert bot._auth(_make_user(42, "renamed")) is True


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
    trusted = []

    class _PersistBot(_Bot):
        def _on_trust(self, user_id: int, username: str) -> None:
            trusted.append((user_id, username))

    bot = _PersistBot()
    bot._auth(_make_user(99, "alice"))
    assert trusted == [(99, "alice")]


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


class _MultiBot(AuthMixin):
    def __init__(
        self,
        usernames: list[str] = None,
        trusted_ids: list[int] = None,
        trusted_users: dict[str, int] | None = None,
    ):
        self._allowed_usernames = usernames or []
        self._trusted_users = trusted_users or {}
        self._trusted_user_ids = trusted_ids or []
        self._init_auth()


def test_multi_user_fail_closed_empty_list():
    bot = _MultiBot(usernames=[])
    assert bot._auth(_make_user(1, "alice")) is False


def test_multi_user_first_contact_trusts():
    bot = _MultiBot(usernames=["alice", "bob"])
    user = _make_user(10, "Alice")
    assert bot._auth(user) is True
    assert bot._get_trusted_user_bindings()["alice"] == 10


def test_multi_user_second_user_trusts():
    bot = _MultiBot(usernames=["alice", "bob"])
    assert bot._auth(_make_user(10, "alice")) is True
    assert bot._auth(_make_user(20, "bob")) is True
    assert bot._get_trusted_user_bindings() == {"alice": 10, "bob": 20}


def test_multi_user_trusted_by_id():
    bot = _MultiBot(usernames=["alice"], trusted_users={"alice": 42})
    assert bot._auth(_make_user(42, "renamed")) is True


def test_multi_user_wrong_username_denied():
    bot = _MultiBot(usernames=["alice"])
    assert bot._auth(_make_user(5, "mallory")) is False


def test_multi_user_trusted_id_requires_allowed_binding():
    bot = _MultiBot(usernames=["alice"], trusted_users={"mallory": 42})
    assert bot._auth(_make_user(42, "mallory")) is False


def test_multi_user_untrusted_id_denied():
    """An unknown username is denied even if other users are trusted."""
    bot = _MultiBot(usernames=["alice"], trusted_ids=[42])
    assert bot._auth(_make_user(99, "mallory")) is False
    assert bot._failed_auth_counts[99] == 1


def test_multi_user_new_allowed_user_joins_existing_trusted():
    """An allowed username can join even when other trusted IDs already exist."""
    bot = _MultiBot(usernames=["alice", "bob"], trusted_users={"alice": 42})
    assert bot._auth(_make_user(99, "bob")) is True
    assert bot._get_trusted_user_bindings() == {"alice": 42, "bob": 99}


def test_multi_user_remove_username_revokes_trusted_id():
    bot = _MultiBot(usernames=["alice"], trusted_users={"alice": 42})
    bot._allowed_usernames = []
    assert bot._auth(_make_user(42, "alice")) is False


def test_multi_user_legacy_trusted_ids_bind_in_allowlist_order():
    bot = _MultiBot(usernames=["alice", "bob"], trusted_ids=[42, 99])
    assert bot._get_trusted_user_bindings() == {"alice": 42, "bob": 99}
