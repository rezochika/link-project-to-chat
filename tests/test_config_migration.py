import json
from pathlib import Path

from link_project_to_chat.config import load_config, save_config


def test_legacy_project_fields_migrate_into_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_model": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "model": "opus",
                        "effort": "high",
                        "permissions": "plan",
                        "session_id": "sess-1",
                        "show_thinking": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    project = config.projects["demo"]

    assert project.backend == "claude"
    assert project.backend_state["claude"]["model"] == "opus"
    assert project.backend_state["claude"]["session_id"] == "sess-1"
    assert config.default_backend == "claude"
    assert config.default_model_claude == "sonnet"


def test_legacy_team_bot_fields_migrate_into_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "alpha": {
                        "path": str(tmp_path),
                        "group_chat_id": -100,
                        "bots": {
                            "primary": {
                                "telegram_bot_token": "tok",
                                "model": "opus",
                                "effort": "high",
                                "permissions": "plan",
                                "session_id": "sess-1",
                                "show_thinking": True,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    team = config.teams["alpha"].bots["primary"]

    assert team.backend == "claude"
    assert team.backend_state["claude"]["model"] == "opus"
    assert team.backend_state["claude"]["session_id"] == "sess-1"


def test_new_shape_round_trip_preserves_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_backend": "claude",
                "default_model_claude": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {
                            "claude": {
                                "model": "opus",
                                "session_id": "sess-1",
                                "permissions": "plan",
                                "show_thinking": True,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    save_config(config, path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend"] == "claude"
    state = raw["projects"]["demo"]["backend_state"]["claude"]
    assert state["model"] == "opus"
    assert state["session_id"] == "sess-1"
    assert state["permissions"] == "plan"
    assert state["show_thinking"] is True
    # Legacy top-level mirror was dropped in v1.0.0; only the canonical
    # nested shape is written now.
    for legacy_key in ("model", "session_id", "permissions", "show_thinking", "effort"):
        assert legacy_key not in raw["projects"]["demo"], (
            f"legacy mirror key {legacy_key!r} should not be re-emitted on save"
        )
    assert raw["default_model_claude"] == "sonnet"
    assert "default_model" not in raw


def test_save_session_writes_backend_state_only_no_legacy_mirror(tmp_path: Path):
    """save_session writes session_id under backend_state["claude"] and strips
    any legacy top-level mirror that may have lingered from a pre-v1.0 config."""
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {}},
                        # Simulate a lingering legacy top-level mirror from a
                        # pre-v1.0 on-disk config — the save path must strip it.
                        "session_id": "old-legacy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-1", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "sess-1"
    assert "session_id" not in raw["projects"]["demo"], (
        "legacy top-level session_id mirror should be stripped on save"
    )


def test_save_session_uses_active_non_claude_backend_without_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "codex",
                        "backend_state": {"codex": {}, "claude": {"session_id": "old-claude"}},
                        "session_id": "old-claude",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-codex", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["codex"]["session_id"] == "sess-codex"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "old-claude"
    # The legacy top-level session_id mirror was kept for one release; v1.0.0
    # dropped it. Codex saves never emitted the Claude mirror — confirm the
    # leftover legacy key from the seed JSON is stripped on save.
    assert "session_id" not in raw["projects"]["demo"]


def test_load_session_prefers_backend_state(tmp_path: Path):
    from link_project_to_chat.config import load_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "new-shape"}},
                        "session_id": "legacy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_session("demo", path) == "new-shape"


def test_clear_session_removes_backend_state_and_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import clear_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "sess-1"}},
                        "session_id": "sess-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    clear_session("demo", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert "session_id" not in raw["projects"]["demo"]["backend_state"]["claude"]
    assert "session_id" not in raw["projects"]["demo"]


# ---------------------------------------------------------------------------
# Team safety: loader leniency + writer safeguard against partial team entries
# ---------------------------------------------------------------------------


def test_load_config_skips_team_missing_path(tmp_path: Path, caplog):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "acme": {
                        "bots": {
                            "manager": {
                                "permissions": "acceptEdits",
                                "backend_state": {"claude": {"permissions": "acceptEdits"}},
                            }
                        }
                    },
                    "valid": {
                        "path": str(tmp_path),
                        "group_chat_id": -100123,
                        "bots": {"manager": {"telegram_bot_token": "t2"}},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        config = load_config(path)

    assert "acme" not in config.teams
    assert "valid" in config.teams
    assert any("acme" in r.message for r in caplog.records)


def test_load_config_skips_team_missing_group_chat_id(tmp_path: Path, caplog):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "incomplete": {
                        "path": "/tmp/incomplete",
                        "bots": {"manager": {"telegram_bot_token": "t"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        config = load_config(path)

    assert "incomplete" not in config.teams


def test_load_config_cleans_up_malformed_team_on_disk(tmp_path: Path):
    """Loader rewrites the file to drop incomplete team entries (matches the
    project-side cleanup precedent so the warning stops repeating)."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "acme": {
                        "bots": {"manager": {"permissions": "acceptEdits"}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    load_config(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "acme" not in raw.get("teams", {})


def test_patch_team_bot_backend_state_refuses_unknown_team(tmp_path: Path, caplog):
    from link_project_to_chat.config import patch_team_bot_backend_state

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"teams": {}}), encoding="utf-8")

    with caplog.at_level("WARNING"):
        patch_team_bot_backend_state(
            "acme", "manager", "claude", {"permissions": "acceptEdits"}, path
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "acme" not in raw.get("teams", {})
    assert any("acme" in r.message for r in caplog.records)


def test_patch_team_bot_backend_state_writes_when_team_configured(tmp_path: Path):
    from link_project_to_chat.config import patch_team_bot_backend_state

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "alpha": {
                        "path": "/tmp/alpha",
                        "group_chat_id": -100123,
                        "bots": {"manager": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    patch_team_bot_backend_state(
        "alpha", "manager", "claude", {"permissions": "plan"}, path
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert (
        raw["teams"]["alpha"]["bots"]["manager"]["backend_state"]["claude"]["permissions"]
        == "plan"
    )


def test_patch_team_bot_backend_refuses_unknown_team(tmp_path: Path):
    from link_project_to_chat.config import patch_team_bot_backend

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"teams": {}}), encoding="utf-8")

    patch_team_bot_backend("acme", "manager", "codex", path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "acme" not in raw.get("teams", {})


def test_save_session_for_unknown_team_is_noop(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"teams": {}}), encoding="utf-8")

    save_session("acme", "sess-x", path, team_name="acme", role="manager")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "acme" not in raw.get("teams", {})


# ---------------------------------------------------------------------------
# Auth-migration golden-file tests (Task 3)
#
# Six golden-file fixtures matching the spec's testing section:
# (a) `allowed_usernames` only;
# (b) dict-shape `trusted_users` (current on-disk format) covering a subset of
#     `allowed_usernames`;
# (c) dict-shape `trusted_users` covering all of `allowed_usernames`;
# (d) legacy list-shape `trusted_users` aligned with `trusted_user_ids`;
# (e) global `Config.allowed_usernames` migrating while a project's per-project
#     list is empty;
# (f) orphan trust - `trusted_users` contains a username not in
#     `allowed_usernames`.
# ---------------------------------------------------------------------------

from link_project_to_chat.config import AllowedUser  # noqa: E402


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2))


def test_migration_a_allowed_usernames_only(tmp_path: Path):
    """Shape (a): only allowed_usernames; no trust info at all."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    p = loaded.projects["p"]
    assert p.allowed_users == [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor"),
    ]
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in written["projects"]["p"]
    assert "allowed_users" in written["projects"]["p"]


def test_migration_b_trusted_users_dict_subset(tmp_path: Path):
    """Shape (b): current on-disk format - trusted_users is dict, subset of allowed_usernames."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob", "carol"],
                "trusted_users": {"alice": 12345},  # dict shape (post-A1)
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].role == "executor" and by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].role == "executor" and by_user["bob"].locked_identities == []
    assert by_user["carol"].role == "executor" and by_user["carol"].locked_identities == []


def test_migration_c_trusted_users_dict_full(tmp_path: Path):
    """Shape (c): every allowed user is in the trusted dict."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": {"alice": 12345, "bob": 67890},
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_d_legacy_list_with_ids_aligned(tmp_path: Path):
    """Shape (d): pre-A1 - trusted_users is a list aligned with trusted_user_ids by index."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": ["alice", "bob"],
                "trusted_user_ids": [12345, 67890],
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_e_global_config_migration(tmp_path: Path):
    """Shape (e): global Config.allowed_usernames migrates while per-project is empty."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "allowed_usernames": ["admin"],
        "trusted_users": {"admin": 99999},
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                # No legacy fields at project scope.
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.migration_pending is True
    # Global allow-list got migrated.
    assert loaded.allowed_users == [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:99999"]),
    ]
    # Project has empty allowed_users (and that's fine - it'll fall back to
    # Config.allowed_users in some paths, or warn at startup).
    assert loaded.projects["p"].allowed_users == []
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in written
    assert "trusted_users" not in written
    assert written["allowed_users"] == [
        {"username": "admin", "role": "executor", "locked_identities": ["telegram:99999"]},
    ]


def test_migration_f_orphan_trust(tmp_path: Path):
    """Shape (f): trusted_users contains a username not in allowed_usernames.

    Should still produce an AllowedUser entry - preserves access. No data loss.
    """
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"bob": 67890},  # bob NOT in allowed_usernames
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert "alice" in by_user
    assert "bob" in by_user
    assert by_user["bob"].locked_identities == ["telegram:67890"]


def test_migration_g_web_session_id_normalized(tmp_path: Path):
    """Pre-v1.0 Web stored trusted_users["alice"] = "web-session:abc-def".
    The legacy value contains ":" but lacks the "web:" transport prefix
    that the new identity-keyed auth comparison requires. Migration must
    normalize "web-session:abc" -> "web:web-session:abc" so the locked
    identity matches _identity_key(web_identity) at runtime."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "web-session:abc-def"},
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["web:web-session:abc-def"]


def test_migration_g_legacy_browser_user_id_normalized(tmp_path: Path):
    """Older WebTransport test/helper paths used the bare browser_user id.
    It is a Web native id, not a Telegram chat id, so migration must keep it
    under the web transport prefix.
    """
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "browser_user"},
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["web:browser_user"]


def test_migration_h_unknown_prefix_falls_back_to_telegram(tmp_path: Path):
    """Bare strings that don't match a known transport prefix migrate as
    telegram (the legacy default - pre-multi-transport configs)."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": "12345"},  # bare numeric string
            }
        }
    })
    loaded = load_config(cfg_file)
    p = loaded.projects["p"]
    by_user = {u.username: u for u in p.allowed_users}
    assert by_user["alice"].locked_identities == ["telegram:12345"]


def test_legacy_list_length_mismatch_drops_ids(tmp_path, caplog):
    """Mismatched legacy list shapes drop the IDs and log WARNING."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": ["alice"],  # list shape
                "trusted_user_ids": [],      # length mismatch
            }
        }
    })
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert "length mismatch" in caplog.text.lower()
    p = loaded.projects["p"]
    assert p.allowed_users == [AllowedUser(username="alice", role="executor", locked_identities=[])]


def test_save_strips_legacy_keys(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    })
    loaded = load_config(cfg_file)
    save_config(loaded, cfg_file)
    written = json.loads(cfg_file.read_text())
    p = written["projects"]["p"]
    assert "allowed_usernames" not in p
    assert "trusted_users" not in p
    assert "trusted_user_ids" not in p
    assert p["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]


def test_load_save_load_is_stable(tmp_path: Path):
    """Second load after save has migration_pending=False (idempotent)."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": "/tmp/p",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    })
    once = load_config(cfg_file)
    assert once.migration_pending is True
    save_config(once, cfg_file)
    twice = load_config(cfg_file)
    assert twice.migration_pending is False
    save_config(twice, cfg_file)
    final = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in final["projects"]["p"]
    assert final["projects"]["p"]["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]
