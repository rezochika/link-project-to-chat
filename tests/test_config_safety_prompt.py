"""ProjectConfig.safety_prompt — three-state field (None | string | "").

None  → bot resolves to DEFAULT_SAFETY_SYSTEM_PROMPT (safety on, default text)
""    → bot leaves backend.safety_system_prompt = "" (safety off)
"..."  → bot sets backend.safety_system_prompt = "..." (safety on, custom text)
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


def test_safety_prompt_defaults_to_none(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"projects": {"p": {"path": str(tmp_path), "telegram_bot_token": "t"}}})
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].safety_prompt is None


def test_safety_prompt_custom_string_loads(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "safety_prompt": "custom safety override",
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].safety_prompt == "custom safety override"


def test_safety_prompt_empty_string_loads_as_disable_marker(tmp_path: Path):
    """Empty string is the explicit-disable signal; must survive load."""
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "safety_prompt": "",
            }
        }
    })
    loaded = load_config(cfg_file)
    assert loaded.projects["p"].safety_prompt == ""


def test_safety_prompt_round_trip_default_omits_key(tmp_path: Path):
    """When safety_prompt is None (default), it shouldn't appear on disk."""
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(path=str(tmp_path), telegram_bot_token="t")
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "safety_prompt" not in raw["projects"]["p"]


def test_safety_prompt_round_trip_custom_writes_key(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        safety_prompt="custom",
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["safety_prompt"] == "custom"


def test_safety_prompt_round_trip_empty_string_writes_key(tmp_path: Path):
    """Empty string is meaningful — it's the explicit-disable signal.
    Must be written to disk (not stripped as if it were None)."""
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        safety_prompt="",
    )
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["safety_prompt"] == ""


def test_safety_prompt_non_string_warns_and_treats_as_none(tmp_path: Path, caplog):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {
        "projects": {
            "p": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "safety_prompt": 42,
            }
        }
    })
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.projects["p"].safety_prompt is None
    assert any("safety_prompt" in r.message for r in caplog.records)
