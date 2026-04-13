"""Tests for multi-user role-based access control."""

from __future__ import annotations

from unittest.mock import MagicMock

from link_project_to_chat.auth import Authenticator
from link_project_to_chat.roles import COMMAND_ROLES, ROLE_HIERARCHY, Role, has_permission


def _make_user(user_id: int, username: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.username = username
    return u


# ── Role enum ──────────────────────────────────────────────────────────────

class TestRoleEnum:
    def test_values(self) -> None:
        assert Role.ADMIN == "admin"
        assert Role.DEVELOPER == "developer"
        assert Role.VIEWER == "viewer"

    def test_hierarchy_order(self) -> None:
        assert ROLE_HIERARCHY[Role.ADMIN] > ROLE_HIERARCHY[Role.DEVELOPER]
        assert ROLE_HIERARCHY[Role.DEVELOPER] > ROLE_HIERARCHY[Role.VIEWER]


# ── has_permission ─────────────────────────────────────────────────────────

class TestHasPermission:
    def test_admin_has_all(self) -> None:
        assert has_permission(Role.ADMIN, Role.ADMIN) is True
        assert has_permission(Role.ADMIN, Role.DEVELOPER) is True
        assert has_permission(Role.ADMIN, Role.VIEWER) is True

    def test_developer_has_dev_and_viewer(self) -> None:
        assert has_permission(Role.DEVELOPER, Role.DEVELOPER) is True
        assert has_permission(Role.DEVELOPER, Role.VIEWER) is True
        assert has_permission(Role.DEVELOPER, Role.ADMIN) is False

    def test_viewer_has_only_viewer(self) -> None:
        assert has_permission(Role.VIEWER, Role.VIEWER) is True
        assert has_permission(Role.VIEWER, Role.DEVELOPER) is False
        assert has_permission(Role.VIEWER, Role.ADMIN) is False


# ── COMMAND_ROLES mapping ──────────────────────────────────────────────────

class TestCommandRoles:
    def test_key_commands_mapped(self) -> None:
        for cmd in ("start", "help", "run", "tasks", "reset", "compact", "permissions"):
            assert cmd in COMMAND_ROLES

    def test_viewer_commands(self) -> None:
        assert COMMAND_ROLES["start"] == Role.VIEWER
        assert COMMAND_ROLES["help"] == Role.VIEWER
        assert COMMAND_ROLES["status"] == Role.VIEWER
        assert COMMAND_ROLES["history"] == Role.VIEWER

    def test_developer_commands(self) -> None:
        assert COMMAND_ROLES["run"] == Role.DEVELOPER
        assert COMMAND_ROLES["tasks"] == Role.DEVELOPER
        assert COMMAND_ROLES["model"] == Role.DEVELOPER
        assert COMMAND_ROLES["effort"] == Role.DEVELOPER

    def test_admin_commands(self) -> None:
        assert COMMAND_ROLES["reset"] == Role.ADMIN
        assert COMMAND_ROLES["compact"] == Role.ADMIN
        assert COMMAND_ROLES["permissions"] == Role.ADMIN


# ── Multi-user Authenticator ──────────────────────────────────────────────

class TestMultiUserAuth:
    def test_authenticates_multiple_users(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[
                {"username": "alice", "role": "admin"},
                {"username": "bob", "role": "developer"},
            ],
        )
        assert auth.authenticate(_make_user(1, "alice")) is True
        assert auth.authenticate(_make_user(2, "bob")) is True

    def test_rejects_unknown_user(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "alice", "role": "admin"}],
        )
        assert auth.authenticate(_make_user(99, "mallory")) is False

    def test_assigns_correct_roles(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[
                {"username": "alice", "role": "admin"},
                {"username": "bob", "role": "developer"},
                {"username": "carol", "role": "viewer"},
            ],
        )
        auth.authenticate(_make_user(1, "alice"))
        auth.authenticate(_make_user(2, "bob"))
        auth.authenticate(_make_user(3, "carol"))
        assert auth.get_role(1) == Role.ADMIN
        assert auth.get_role(2) == Role.DEVELOPER
        assert auth.get_role(3) == Role.VIEWER

    def test_case_insensitive_username(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "Alice", "role": "admin"}],
        )
        assert auth.authenticate(_make_user(1, "ALICE")) is True
        assert auth.get_role(1) == Role.ADMIN

    def test_at_prefix_stripped(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "@alice", "role": "developer"}],
        )
        assert auth.authenticate(_make_user(1, "alice")) is True

    def test_brute_force_blocked(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "alice", "role": "admin"}],
            max_failed_attempts=3,
        )
        bad = _make_user(99, "mallory")
        for _ in range(3):
            auth.authenticate(bad)
        assert auth.authenticate(bad) is False

    def test_already_trusted_skips_lookup(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "alice", "role": "admin"}],
        )
        auth.authenticate(_make_user(1, "alice"))
        # Second call should succeed even without username
        assert auth.authenticate(_make_user(1, None)) is True

    def test_invalid_role_defaults_to_viewer(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "alice", "role": "superadmin"}],
        )
        auth.authenticate(_make_user(1, "alice"))
        assert auth.get_role(1) == Role.VIEWER

    def test_on_trust_called_for_each_user(self) -> None:
        trusted: list[int] = []
        auth = Authenticator(
            allowed_username="",
            allowed_users=[
                {"username": "alice", "role": "admin"},
                {"username": "bob", "role": "viewer"},
            ],
            on_trust=trusted.append,
        )
        auth.authenticate(_make_user(1, "alice"))
        auth.authenticate(_make_user(2, "bob"))
        assert trusted == [1, 2]


# ── Backward compatibility ────────────────────────────────────────────────

class TestBackwardCompat:
    def test_single_username_creates_admin(self) -> None:
        auth = Authenticator(allowed_username="alice")
        auth.authenticate(_make_user(10, "alice"))
        assert auth.get_role(10) == Role.ADMIN

    def test_single_username_no_role_before_auth(self) -> None:
        auth = Authenticator(allowed_username="alice")
        assert auth.get_role(10) is None

    def test_allowed_users_takes_precedence(self) -> None:
        """When both allowed_username and allowed_users are set, multi-user wins."""
        auth = Authenticator(
            allowed_username="legacy_user",
            allowed_users=[{"username": "alice", "role": "developer"}],
        )
        # legacy_user is NOT in allowed_users, so should be rejected
        assert auth.authenticate(_make_user(1, "legacy_user")) is False
        # alice IS in allowed_users
        assert auth.authenticate(_make_user(2, "alice")) is True
        assert auth.get_role(2) == Role.DEVELOPER


# ── get_role ───────────────────────────────────────────────────────────────

class TestGetRole:
    def test_returns_none_for_unknown_user(self) -> None:
        auth = Authenticator(
            allowed_username="",
            allowed_users=[{"username": "alice", "role": "admin"}],
        )
        assert auth.get_role(999) is None

    def test_returns_none_for_single_user_unauthenticated(self) -> None:
        auth = Authenticator(allowed_username="alice")
        assert auth.get_role(42) is None
