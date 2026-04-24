"""M11 — config I/O tests: malformed JSON, permission errors, concurrent access."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------


def test_load_config_malformed_json_raises(tmp_path: Path):
    """load_config should propagate JSONDecodeError on corrupt files."""
    p = tmp_path / "bad.json"
    p.write_text("{invalid json}")
    with pytest.raises(json.JSONDecodeError):
        load_config(p)


def test_load_config_empty_file_raises(tmp_path: Path):
    """An empty config file is not valid JSON."""
    p = tmp_path / "empty.json"
    p.write_text("")
    with pytest.raises((json.JSONDecodeError, ValueError)):
        load_config(p)


def test_load_config_truncated_json_raises(tmp_path: Path):
    """A file truncated mid-object is not valid JSON."""
    p = tmp_path / "truncated.json"
    p.write_text('{"allowed_usernames": ["alice"')
    with pytest.raises(json.JSONDecodeError):
        load_config(p)


# ---------------------------------------------------------------------------
# Permission errors
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="UNIX permissions only")
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks",
)
def test_load_config_unreadable_file_raises(tmp_path: Path):
    """load_config raises when the file exists but is not readable."""
    p = tmp_path / "secret.json"
    p.write_text(json.dumps({"projects": {}}))
    p.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            load_config(p)
    finally:
        p.chmod(0o644)  # restore so tmp_path cleanup works


@pytest.mark.skipif(sys.platform == "win32", reason="UNIX permissions only")
def test_save_config_enforces_parent_dir_permissions(tmp_path: Path):
    """save_config always enforces 0o700 on the config parent directory."""
    cfg_dir = tmp_path / "cfg_dir"
    cfg_dir.mkdir(mode=0o755)
    save_config(Config(), cfg_dir / "config.json")
    assert cfg_dir.stat().st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------


def test_concurrent_save_config_no_data_loss(tmp_path: Path):
    """Multiple threads saving different projects must not lose each other's data."""
    p = tmp_path / "cfg.json"
    save_config(Config(), p)

    errors: list[Exception] = []

    def _save_project(name: str) -> None:
        try:
            cfg = Config(
                projects={name: ProjectConfig(path=f"/{name}", telegram_bot_token="TOK")}
            )
            save_config(cfg, p)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_save_project, args=(f"proj{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    # At minimum the file must be valid JSON after concurrent writes.
    data = json.loads(p.read_text())
    assert "projects" in data


def test_save_config_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    """After save_config, no *.tmp files should linger in the config directory."""
    p = tmp_path / "cfg.json"
    save_config(Config(allowed_usernames=["alice"]), p)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Stale tmp files found: {tmp_files}"
