"""Auth tests (rewritten in Task 5 around AllowedUser).

The legacy single-user / multi-user / trusted-id helpers are gone. The new
auth model reads exclusively from ``self._allowed_users`` (list[AllowedUser])
and locks identities via ``AllowedUser.locked_identities`` on first contact.
These tests preserve the original intent (fail-closed, brute-force lockout,
rate-limit independence) in the new shape, with role="executor" as the
migration default (everyone allowed gets full access).
"""
from __future__ import annotations

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


def _identity(username: str | None, native_id: int | str = 1) -> Identity:
    """Build a Telegram-shaped Identity for tests."""
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


def test_fail_closed_when_no_users():
    bot = _Bot(allowed_users=[])
    assert bot._auth_identity(_identity("alice", 1)) is False


def test_first_contact_locks_identity():
    au = AllowedUser(username="alice", role="executor")
    bot = _Bot(allowed_users=[au])
    # Case-insensitive: "Alice" handle resolves to "alice" entry.
    assert bot._auth_identity(_identity("Alice", 10)) is True
    assert au.locked_identities == ["telegram:10"]


def test_trusted_by_locked_identity_allowed():
    """A user with a pre-locked identity matches even with a different handle."""
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    bot = _Bot(allowed_users=[au])
    assert bot._auth_identity(_identity("renamed", 42)) is True


def test_wrong_id_denied_and_counted():
    """A user not in the allow-list (different handle, no locked identity)
    is denied and their failure count increments."""
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    bot = _Bot(allowed_users=[au])
    bad = _identity("hacker", 99)
    assert bot._auth_identity(bad) is False
    assert bot._failed_auth_counts["telegram:99"] == 1


def test_wrong_username_denied():
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    assert bot._auth_identity(_identity("mallory", 5)) is False
    assert bot._failed_auth_counts["telegram:5"] == 1


def test_brute_force_blocked_after_5():
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    bot = _Bot(allowed_users=[au])
    bad = _identity("x", 7)
    for _ in range(5):
        bot._auth_identity(bad)
    # 6th attempt still blocked even if the counts equal exactly 5
    assert bot._auth_identity(bad) is False


def test_rate_limited_after_limit():
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    for _ in range(bot._MAX_MESSAGES_PER_MINUTE):
        assert bot._rate_limited("telegram:1") is False
    assert bot._rate_limited("telegram:1") is True


def test_rate_limit_independent_per_user():
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    for _ in range(bot._MAX_MESSAGES_PER_MINUTE):
        bot._rate_limited("telegram:1")
    # User 2 should not be affected (different identity_key).
    assert bot._rate_limited("telegram:2") is False


def test_multi_user_fail_closed_empty_list():
    bot = _Bot(allowed_users=[])
    assert bot._auth_identity(_identity("alice", 1)) is False


def test_multi_user_first_contact_trusts():
    au_alice = AllowedUser(username="alice", role="executor")
    au_bob = AllowedUser(username="bob", role="executor")
    bot = _Bot(allowed_users=[au_alice, au_bob])
    assert bot._auth_identity(_identity("Alice", 10)) is True
    assert au_alice.locked_identities == ["telegram:10"]


def test_multi_user_second_user_trusts():
    au_alice = AllowedUser(username="alice", role="executor")
    au_bob = AllowedUser(username="bob", role="executor")
    bot = _Bot(allowed_users=[au_alice, au_bob])
    assert bot._auth_identity(_identity("alice", 10)) is True
    assert bot._auth_identity(_identity("bob", 20)) is True
    assert au_alice.locked_identities == ["telegram:10"]
    assert au_bob.locked_identities == ["telegram:20"]


def test_multi_user_locked_by_id():
    """A user listed with locked_identities matches by id even when handle differs."""
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    bot = _Bot(allowed_users=[au])
    assert bot._auth_identity(_identity("renamed", 42)) is True


def test_multi_user_wrong_username_denied():
    bot = _Bot(allowed_users=[AllowedUser(username="alice", role="executor")])
    assert bot._auth_identity(_identity("mallory", 5)) is False


def test_multi_user_unknown_identity_denied():
    """An unknown username is denied even if other users have locked identities."""
    au = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    bot = _Bot(allowed_users=[au])
    assert bot._auth_identity(_identity("mallory", 99)) is False
    assert bot._failed_auth_counts["telegram:99"] == 1


def test_multi_user_new_allowed_user_joins_existing_trusted():
    """An allowed username can first-contact and lock even when other users
    already have locked identities."""
    au_alice = AllowedUser(
        username="alice", role="executor", locked_identities=["telegram:42"],
    )
    au_bob = AllowedUser(username="bob", role="executor")
    bot = _Bot(allowed_users=[au_alice, au_bob])
    assert bot._auth_identity(_identity("bob", 99)) is True
    assert au_alice.locked_identities == ["telegram:42"]
    assert au_bob.locked_identities == ["telegram:99"]
