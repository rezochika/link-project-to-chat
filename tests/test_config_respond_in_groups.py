"""Per-project respond_in_groups field — load/save/default behavior.

The flag is default-off and only emitted on disk when True. Loader tolerates
missing keys (→ False) and non-bool values (→ False with WARNING).
"""
from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    load_config,
    save_config,
)


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2))


def test_respond_in_groups_defaults_false(tmp_path: Path):
    """A project with no respond_in_groups key loads as False."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is False


def test_respond_in_groups_round_trip_true(tmp_path: Path):
    """Setting True survives load/save/load."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is True
    save_config(loaded, cfg_file)
    reloaded = load_config(cfg_file)
    assert reloaded.projects["p"].respond_in_groups is True


def test_respond_in_groups_omitted_on_disk_when_false(tmp_path: Path):
    """save_config does not write the key when the field is False —
    keeps configs tidy and avoids spurious diffs for the default case."""
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        respond_in_groups=False,
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "respond_in_groups" not in raw["projects"]["p"]


def test_respond_in_groups_emitted_on_disk_when_true(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        respond_in_groups=True,
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["respond_in_groups"] is True


def test_respond_in_groups_non_bool_input_coerces_to_false(tmp_path: Path, caplog):
    """A string "yes" or a list value silently coerces to False with a WARNING.
    Real bools (True/False) and Python truthy ints (0/1) pass through bool().
    """
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": "yes",  # not a bool
            }
        }
    })
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.projects["p"].respond_in_groups is False
    assert any(
        "respond_in_groups" in r.message.lower()
        for r in caplog.records
    )
