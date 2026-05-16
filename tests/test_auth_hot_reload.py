"""AuthMixin._reload_allowed_users_if_stale — debounced disk poll with merge."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser


class _Stub(AuthMixin):
    """Test subclass that provides the reload primitives a concrete bot would.

    Real ProjectBot wires:
      - self._project_config_path: Path to config.json
      - self._project_name: str (None for manager)
      - self._last_allowed_users_reload: float (monotonic)
      - self._allowed_users: list[AllowedUser]
      - self._auth_dirty: bool
    """
    def __init__(self, cfg_path: Path, project_name: str | None = None):
        self._project_config_path = cfg_path
        self._project_name = project_name
        self._last_allowed_users_reload = 0.0
        self._allowed_users: list[AllowedUser] = []
        self._auth_dirty = False


def _write_cfg(path: Path, project_users=None, global_users=None) -> None:
    import json
    raw = {"projects": {}, "allowed_users": global_users or []}
    if project_users is not None:
        raw["projects"]["p"] = {
            "path": "/tmp",
            "telegram_bot_token": "t",
            "allowed_users": project_users,
        }
    path.write_text(json.dumps(raw))


def test_reload_pulls_new_user_from_disk(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "alice" for u in stub._allowed_users)


def test_reload_debounced_within_five_seconds(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    # Add bob to disk; reload again immediately — must NOT pick him up.
    _write_cfg(cfg, project_users=[
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "viewer"},
    ])
    stub._reload_allowed_users_if_stale()
    assert not any(u.username == "bob" for u in stub._allowed_users)


def test_reload_picks_up_after_debounce(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    # Backdate the timestamp to simulate 5+ seconds passing.
    stub._last_allowed_users_reload = time.monotonic() - 10.0
    _write_cfg(cfg, project_users=[
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "viewer"},
    ])
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "bob" for u in stub._allowed_users)


def test_reload_skipped_when_auth_dirty(tmp_path: Path):
    """Don't clobber in-flight first-contact locks."""
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    # Simulate in-flight first-contact: alice gets a locked identity in memory
    # but it's not yet persisted.
    stub._allowed_users[0].locked_identities.append("telegram:99")
    stub._auth_dirty = True
    # Backdate the timestamp so the debounce isn't what's blocking the reload.
    stub._last_allowed_users_reload = time.monotonic() - 10.0
    # Manager writes new user to disk meanwhile.
    _write_cfg(cfg, project_users=[
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "viewer"},
    ])
    stub._reload_allowed_users_if_stale()
    # bob NOT picked up: we don't clobber unsaved changes.
    assert not any(u.username == "bob" for u in stub._allowed_users)
    # In-flight lock preserved.
    assert "telegram:99" in stub._allowed_users[0].locked_identities


def test_reload_merges_locked_identities_as_union(tmp_path: Path):
    """Disk role wins; locked_identities is the union of disk + memory.
    Preserves in-flight first-contact locks that haven't been persisted."""
    cfg = tmp_path / "config.json"
    # Disk: alice with one locked id.
    _write_cfg(cfg, project_users=[{
        "username": "alice", "role": "executor",
        "locked_identities": ["telegram:1"],
    }])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    # Memory gains a NEW locked id (e.g., bot just authenticated alice from web).
    stub._allowed_users[0].locked_identities.append("web:browser_user")
    # auth_dirty is False (some other reload path didn't flip it).
    stub._auth_dirty = False
    # Manager writes role change to disk.
    _write_cfg(cfg, project_users=[{
        "username": "alice", "role": "viewer",
        "locked_identities": ["telegram:1"],
    }])
    stub._last_allowed_users_reload = time.monotonic() - 10.0
    stub._reload_allowed_users_if_stale()
    # Disk role wins.
    assert stub._allowed_users[0].role == "viewer"
    # locked_identities is union: disk's telegram:1 + memory's web:browser_user.
    locks = set(stub._allowed_users[0].locked_identities)
    assert "telegram:1" in locks
    assert "web:browser_user" in locks


def test_reload_handles_removed_user(tmp_path: Path):
    """Manager runs /remove_user → bot drops the user from memory."""
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[
        {"username": "alice", "role": "executor"},
        {"username": "bob", "role": "viewer"},
    ])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "bob" for u in stub._allowed_users)
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub._last_allowed_users_reload = time.monotonic() - 10.0
    stub._reload_allowed_users_if_stale()
    assert not any(u.username == "bob" for u in stub._allowed_users)


def test_reload_corrupt_file_keeps_state(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, project_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    # Corrupt the file.
    cfg.write_text("{ broken json")
    stub._last_allowed_users_reload = time.monotonic() - 10.0
    stub._reload_allowed_users_if_stale()
    # Alice is still there.
    assert any(u.username == "alice" for u in stub._allowed_users)


def test_manager_scope_reads_global_allowed_users(tmp_path: Path):
    cfg = tmp_path / "config.json"
    _write_cfg(cfg, global_users=[{"username": "alice", "role": "executor"}])
    stub = _Stub(cfg, project_name=None)  # manager-scope (no project_name)
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "alice" for u in stub._allowed_users)


def test_project_scope_falls_back_to_global_when_project_users_empty(tmp_path: Path):
    cfg = tmp_path / "config.json"
    import json
    cfg.write_text(json.dumps({
        "allowed_users": [{"username": "global_alice", "role": "executor"}],
        "projects": {"p": {"path": "/tmp", "telegram_bot_token": "t"}},
    }))
    stub = _Stub(cfg, project_name="p")
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "global_alice" for u in stub._allowed_users)


def test_reload_missing_file_keeps_state(tmp_path: Path):
    """Pointing at a non-existent config preserves the in-memory ctor seed.

    Production wiring sets ``_project_config_path = config_path or
    DEFAULT_CONFIG`` so the field is never None, but the file itself may not
    exist yet (one-shot ``--path/--token`` start, tests with a tmp_path
    config that wasn't written to disk). The reload must NOT drop the
    in-memory users in that case.
    """
    cfg = tmp_path / "missing.json"
    assert not cfg.exists()
    stub = _Stub(cfg, project_name="p")
    stub._allowed_users = [AllowedUser(username="alice", role="executor")]
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "alice" for u in stub._allowed_users)


def test_reload_empty_disk_keeps_in_memory(tmp_path: Path):
    """Disk resolves to zero users + memory has users → keep memory.

    The operator hasn't expressed intent to lock the bot out (no global, no
    project entries). Clobbering would drop the ctor-synthesized
    ``allowed_username`` set that the bot was started with.
    """
    cfg = tmp_path / "config.json"
    # File exists but resolves to zero users at every scope.
    cfg.write_text('{"projects": {"p": {"path": "/tmp", "telegram_bot_token": "t"}}}')
    stub = _Stub(cfg, project_name="p")
    stub._allowed_users = [AllowedUser(username="alice", role="executor")]
    stub._reload_allowed_users_if_stale()
    assert any(u.username == "alice" for u in stub._allowed_users)
