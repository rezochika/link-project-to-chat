"""Config.meta_dir — per-bot/per-plugin storage root override."""
from __future__ import annotations

import json
from pathlib import Path

from link_project_to_chat.config import (
    Config,
    DEFAULT_META_DIR,
    load_config,
    resolve_project_meta_dir,
    save_config,
)


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2))


def test_meta_dir_defaults_to_lptc_meta_subdir(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"projects": {}})
    loaded = load_config(cfg_file)
    assert loaded.meta_dir == DEFAULT_META_DIR


def test_meta_dir_explicit_override_loads(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"meta_dir": "/var/lib/lptc/data", "projects": {}})
    loaded = load_config(cfg_file)
    assert loaded.meta_dir == Path("/var/lib/lptc/data")


def test_meta_dir_tilde_expands(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"meta_dir": "~/custom-meta", "projects": {}})
    loaded = load_config(cfg_file)
    assert loaded.meta_dir == Path.home() / "custom-meta"


def test_meta_dir_non_string_warns_and_defaults(tmp_path: Path, caplog):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"meta_dir": 42, "projects": {}})
    with caplog.at_level("WARNING"):
        loaded = load_config(cfg_file)
    assert loaded.meta_dir == DEFAULT_META_DIR
    assert any("meta_dir" in r.message for r in caplog.records)


def test_meta_dir_default_value_omitted_on_disk(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "meta_dir" not in raw


def test_meta_dir_custom_value_written_to_disk(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.meta_dir = Path("/var/lib/lptc/data")
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["meta_dir"] == "/var/lib/lptc/data"


def test_resolve_project_meta_dir_creates_directory(tmp_path: Path):
    meta_root = tmp_path / "meta"
    resolved = resolve_project_meta_dir(meta_root, "myproject")
    assert resolved == meta_root / "myproject"
    assert resolved.is_dir()


def test_resolve_project_meta_dir_idempotent(tmp_path: Path):
    meta_root = tmp_path / "meta"
    r1 = resolve_project_meta_dir(meta_root, "myproject")
    r2 = resolve_project_meta_dir(meta_root, "myproject")
    assert r1 == r2
    assert r1.is_dir()


def test_init_plugins_uses_meta_dir_for_plugin_data_root(tmp_path: Path, monkeypatch):
    """When Config.meta_dir is set, plugin data_dir is rooted there."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import Config, ProjectConfig
    from link_project_to_chat.plugin import PluginContext

    cfg = Config()
    cfg.meta_dir = tmp_path / "custom-meta"
    cfg.projects["myproj"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
    )
    expected_root = tmp_path / "custom-meta" / "myproj"
    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "myproj"
    bot._config = cfg
    from link_project_to_chat.config import resolve_project_meta_dir
    resolved = resolve_project_meta_dir(bot._config.meta_dir, bot.name)
    assert resolved == expected_root
    assert resolved.is_dir()
