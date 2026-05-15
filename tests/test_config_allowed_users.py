from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    AllowedUser,
    Config,
    ProjectConfig,
    load_config,
    save_config,
)


def test_allowed_user_defaults_to_viewer():
    u = AllowedUser(username="alice")
    assert u.role == "viewer"
    assert u.locked_identities == []


def test_project_config_has_plugins_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.plugins == []


def test_project_config_has_allowed_users_default_empty():
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert p.allowed_users == []


# NOTE: A `test_legacy_fields_are_not_dataclass_attributes` would belong
# here in principle, but legacy fields STAY on ProjectConfig and Config
# through Tasks 3-4 as transitional read-only inputs (existing callers in
# bot.py / cli.py / manager/bot.py still read them until Task 5's audit
# rewrites them). That test is added in **Task 5 Step 12** after the
# dataclass field removal lands. Don't add it here - it would fail by
# design at the end of Task 3.


def test_save_load_roundtrip(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["myp"] = ProjectConfig(
        path="/tmp/p",
        telegram_bot_token="t",
        allowed_users=[
            AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
            AllowedUser(username="bob", role="viewer"),
        ],
        plugins=[{"name": "in-app-web-server"}, {"name": "diff", "option": 1}],
    )
    save_config(cfg, cfg_file)
    loaded = load_config(cfg_file)
    p = loaded.projects["myp"]
    assert {(u.username, u.role, tuple(u.locked_identities)) for u in p.allowed_users} == {
        ("alice", "executor", ("telegram:12345",)),
        ("bob", "viewer", ()),
    }
    assert p.plugins == [{"name": "in-app-web-server"}, {"name": "diff", "option": 1}]


def test_load_repairs_legacy_web_identity_misclassified_as_telegram(tmp_path: Path):
    """Earlier migration could save browser_user under the Telegram prefix.
    That identity is a Web native id, so loading the modern shape should repair
    it before Telegram startup tries to parse it as an int chat id.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "allowed_users": [
            {
                "username": "alice",
                "role": "executor",
                "locked_identities": ["telegram:browser_user"],
            },
        ],
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {
                        "username": "bob",
                        "role": "executor",
                        "locked_identities": ["telegram:web-session:abc"],
                    },
                ],
            }
        },
    }))

    loaded = load_config(cfg_file)

    assert loaded.allowed_users[0].locked_identities == ["web:browser_user"]
    assert loaded.projects["p"].allowed_users[0].locked_identities == [
        "web:web-session:abc",
    ]


def test_unknown_role_falls_back_to_viewer(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [{"username": "x", "role": "admin"}],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    assert p.allowed_users == [AllowedUser(username="x", role="viewer", locked_identities=[])]


def test_malformed_plugin_entry_skipped(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "plugins": [{"name": "good"}, {"not_name": "bad"}, "string-not-dict"],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].plugins == [{"name": "good"}]


def test_malformed_allowed_user_entry_skipped_with_warning(tmp_path, caplog):
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "good", "role": "viewer"},
                    {"not_username": "missing"},
                    "string-not-dict",
                ],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.projects["p"].allowed_users == [
        AllowedUser(username="good", role="viewer", locked_identities=[]),
    ]


def test_empty_allowed_users_after_load_logs_warning(tmp_path, caplog):
    """Per-load empty allowlist emits WARNING; CRITICAL aggregation is done by CLI start."""
    cfg_file = tmp_path / "config.json"
    raw = {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [],
            }
        }
    }
    cfg_file.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        load_config(cfg_file)
    assert any(
        "no users authorized" in r.message.lower() and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_migration_pending_flag_unset_on_clean_config(tmp_path):
    """A config without any legacy fields loads with migration_pending=False."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor"},
                ],
            }
        }
    }))
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is False


def test_locked_config_rmw_round_trip_smoke(tmp_path: Path):
    """Smoke test: API exists and a basic RMW cycle works.

    This does NOT prove the lock is held across the load/save - it only
    proves the context manager round-trips cleanly. The real concurrency
    test (`test_locked_config_rmw_actually_serializes_writers` below) uses
    multiprocessing to force contention.
    """
    from link_project_to_chat.config import locked_config_rmw, save_config_within_lock

    cfg_file = tmp_path / "config.json"
    save_config(Config(), cfg_file)

    with locked_config_rmw(cfg_file) as cfg:
        cfg.allowed_users = [AllowedUser(username="alice", role="executor")]
        save_config_within_lock(cfg, cfg_file)

    reloaded = load_config(cfg_file)
    assert reloaded.allowed_users == [AllowedUser(username="alice", role="executor", locked_identities=[])]


# Module-scope worker - multiprocessing's default start method on macOS and
# Windows is "spawn", which requires the target callable to be importable
# (= pickled by qualified name). Nested functions inside a test body can't
# be pickled under spawn. Keep this at module scope.
def _rmw_contention_worker(cfg_file_path: str, new_username: str) -> None:
    """Append a new AllowedUser inside ``locked_config_rmw``.

    This test only cares that the RMW lock serializes writers; without
    cross-phase locking, one process would load before the other writes
    and clobber it.
    """
    from pathlib import Path as _Path
    import time
    from link_project_to_chat.config import (
        AllowedUser as _AllowedUser,
        locked_config_rmw,
        save_config_within_lock,
    )
    with locked_config_rmw(_Path(cfg_file_path)) as disk:
        # Tiny sleep widens the contention window - without the cross-phase
        # lock, this all but guarantees one writer clobbers the other.
        time.sleep(0.05)
        if not any(u.username == new_username for u in disk.allowed_users):
            disk.allowed_users.append(_AllowedUser(username=new_username, role="executor"))
        save_config_within_lock(disk, _Path(cfg_file_path))


# Removed in Task 5 Step 12: the three ``test_legacy_*`` tests exercised
# the legacy ``allowed_usernames`` mutation path that no longer exists
# (the dataclass field is gone). The new-shape path is covered by
# ``test_save_load_preserves_*`` later in this file.


def test_locked_config_rmw_actually_serializes_writers(tmp_path: Path):
    """Real contention test: two writers, each appending a different
    username, must converge to BOTH usernames on disk.

    If `locked_config_rmw` only locked the write phase (like the rejected
    earlier design), one writer would load the pre-write state, the other
    would also load it, both would compute different merged states, and the
    last-to-save would clobber the first. With the lock held across the
    whole load->modify->save cycle, the second writer sees the first writer's
    result and unions on top.

    Uses multiprocessing to force separate file-lock holders (a single
    process can't really test fcntl.flock contention against itself).
    Forces the 'spawn' context explicitly so the test behaves the same on
    Linux (default 'fork') and macOS/Windows (default 'spawn'); requires
    the worker to be at module scope so it can be pickled.
    """
    import multiprocessing as mp
    from link_project_to_chat.config import (
        AllowedUser, Config, load_config, save_config,
    )

    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [AllowedUser(username="alice", role="executor")]
    save_config(cfg, cfg_file)

    ctx = mp.get_context("spawn")  # explicit; identical behavior across OSes
    p1 = ctx.Process(target=_rmw_contention_worker, args=(str(cfg_file), "bob"))
    p2 = ctx.Process(target=_rmw_contention_worker, args=(str(cfg_file), "carol"))
    p1.start(); p2.start()
    p1.join(); p2.join()
    assert p1.exitcode == 0 and p2.exitcode == 0

    final = load_config(cfg_file)
    # Both writers' usernames must be present - neither clobbered the other.
    by_name = {u.username for u in final.allowed_users}
    assert by_name == {"alice", "bob", "carol"}


def test_save_load_preserves_viewer_role_through_roundtrip(tmp_path: Path):
    """Regression for C1: viewer roles must survive save->reload->save.
    Pre-fix, the save-time synthesis always assigned executor, silently
    promoting viewers."""
    import json
    from link_project_to_chat.config import AllowedUser, Config, load_config, save_config

    cfg_file = tmp_path / "config.json"
    cfg = Config(allowed_users=[
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
        AllowedUser(username="bob", role="viewer", locked_identities=["telegram:67890"]),
    ])
    save_config(cfg, cfg_file)
    once = load_config(cfg_file)
    save_config(once, cfg_file)
    twice = load_config(cfg_file)

    by_user = {u.username: u for u in twice.allowed_users}
    assert by_user["alice"].role == "executor"
    assert by_user["bob"].role == "viewer", "viewer role silently promoted to executor"


def test_save_load_preserves_non_telegram_identities_through_roundtrip(tmp_path: Path):
    """Regression for C1: web/discord/slack identities must survive
    save->reload->save. Pre-fix, the legacy mirror only emitted telegram:
    identities, so web bindings were dropped."""
    import json
    from link_project_to_chat.config import AllowedUser, Config, load_config, save_config

    cfg_file = tmp_path / "config.json"
    cfg = Config(allowed_users=[
        AllowedUser(username="alice", role="executor",
                    locked_identities=["web:web-session:abc-def"]),
    ])
    save_config(cfg, cfg_file)
    once = load_config(cfg_file)
    save_config(once, cfg_file)
    twice = load_config(cfg_file)

    alice = twice.allowed_users[0]
    assert alice.username == "alice"
    assert alice.locked_identities == ["web:web-session:abc-def"]


def test_save_load_preserves_multi_transport_identities_through_roundtrip(tmp_path: Path):
    """Regression for C1: a user with locks on multiple transports
    must keep all locks through save->reload->save."""
    import json
    from link_project_to_chat.config import AllowedUser, Config, load_config, save_config

    cfg_file = tmp_path / "config.json"
    cfg = Config(allowed_users=[
        AllowedUser(username="alice", role="executor",
                    locked_identities=["telegram:12345", "web:web-session:abc"]),
    ])
    save_config(cfg, cfg_file)
    once = load_config(cfg_file)
    save_config(once, cfg_file)
    twice = load_config(cfg_file)

    alice = twice.allowed_users[0]
    assert set(alice.locked_identities) == {"telegram:12345", "web:web-session:abc"}


def test_save_load_preserves_locked_identity_addition_through_roundtrip(tmp_path: Path):
    """Regression for C1: Task 5's _persist_auth_if_dirty appends a new
    identity to disk.allowed_users[au].locked_identities. This test
    simulates that append + save pattern and verifies the new identity
    survives."""
    import json
    from link_project_to_chat.config import (
        AllowedUser, Config, load_config, save_config, locked_config_rmw,
    )

    cfg_file = tmp_path / "config.json"
    save_config(Config(allowed_users=[
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
    ]), cfg_file)

    # Simulate _persist_auth_if_dirty: load, mutate locked_identities, save.
    loaded = load_config(cfg_file)
    alice = loaded.allowed_users[0]
    alice.locked_identities.append("web:web-session:new-session")
    save_config(loaded, cfg_file)

    reloaded = load_config(cfg_file)
    reloaded_alice = reloaded.allowed_users[0]
    assert set(reloaded_alice.locked_identities) == {
        "telegram:12345", "web:web-session:new-session",
    }


# Removed in Task 5 Step 12:
# ``test_project_cross_scope_synthesis_fallback_pins_behavior`` pinned the
# behavior where a project with ``trusted_user_ids`` (legacy) and no
# ``allowed_usernames`` of its own inherits the global allow-list as the
# synthesis basis. With the legacy fields gone from the dataclass, the
# scenario is unreachable from in-memory construction. The loader still
# accepts legacy on-disk fields and migrates them into ``allowed_users``;
# that path is covered by ``test_config.py::test_load_config_migrates_single_username``.


def test_legacy_fields_are_not_dataclass_attributes():
    """After Task 5 Step 12, the legacy fields are GONE from the dataclass.
    They can only be read by the loader's _migrate_legacy_auth, which sees
    the raw JSON dict — never the typed ProjectConfig / Config.
    Adding this test in Task 3 would fail by design, since legacy fields
    stay on the dataclass through Tasks 3–4 as transitional read-only inputs.
    """
    p = ProjectConfig(path="/tmp", telegram_bot_token="x")
    assert not hasattr(p, "allowed_usernames")
    assert not hasattr(p, "trusted_users")
    assert not hasattr(p, "trusted_user_ids")

    c = Config()
    assert not hasattr(c, "allowed_usernames")
    assert not hasattr(c, "trusted_users")
    assert not hasattr(c, "trusted_user_ids")
