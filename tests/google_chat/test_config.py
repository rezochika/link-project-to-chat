from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import ConfigError, GoogleChatConfig, load_config, save_config


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def test_missing_google_chat_block_loads_default(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"projects": {}})

    cfg = load_config(cfg_file)

    assert cfg.google_chat == GoogleChatConfig()


def test_google_chat_config_round_trips_non_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(
        cfg_file,
        {
            "google_chat": {
                "service_account_file": "/secure/key.json",
                "app_id": "app-1",
                "project_number": "123",
                "auth_audience_type": "project_number",
                "allowed_audiences": ["123"],
                "endpoint_path": "/chat",
                "public_url": "https://chat.example.test",
                "host": "0.0.0.0",
                "port": 8099,
                "root_command_name": "lp2c",
                "root_command_id": 7,
                "callback_token_ttl_seconds": 60,
                "pending_prompt_ttl_seconds": 120,
                "max_message_bytes": 32000,
            }
        },
    )

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert raw["google_chat"]["project_number"] == "123"
    assert raw["google_chat"]["root_command_id"] == 7


def test_default_google_chat_config_is_omitted_on_save(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": {}})

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert "google_chat" not in raw


def test_non_dict_google_chat_is_config_error(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": "bad"})

    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_invalid_allowed_audiences_is_config_error(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": {"allowed_audiences": "https://bad"}})

    with pytest.raises(ConfigError):
        load_config(cfg_file)
