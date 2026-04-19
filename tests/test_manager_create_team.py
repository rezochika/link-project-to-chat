from __future__ import annotations

from pathlib import Path


def test_persona_keyboard_lists_discovered_personas(tmp_path):
    from link_project_to_chat.manager.bot import _build_persona_keyboard

    # Create fake personas (path layout matches what load_personas() expects)
    personas_dir = tmp_path / ".claude" / "personas"
    personas_dir.mkdir(parents=True)
    (personas_dir / "developer.md").write_text("# Developer")
    (personas_dir / "tester.md").write_text("# Tester")

    kb = _build_persona_keyboard(tmp_path, callback_prefix="team_persona_mgr")
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = {btn.text for btn in buttons}
    # Assert at LEAST our two test personas appear (load_personas may also discover globals)
    assert "developer" in labels
    assert "tester" in labels
    # Callbacks are prefixed
    for btn in buttons:
        assert btn.callback_data.startswith("team_persona_mgr:")


import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    save_config,
)


def test_preflight_rejects_existing_team_name(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        teams={
            "acme": TeamConfig(path="/a", group_chat_id=-1, bots={})
        },
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")  # fake session file

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "already configured" in err


def test_preflight_rejects_legacy_project_name_collision(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        projects={"acme_mgr": ProjectConfig(path="/a", telegram_bot_token="t")},
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "project names are taken" in err or "acme_mgr" in err


def test_preflight_rejects_missing_telethon_config(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(github_pat="ghp_x")  # telegram_api_id/hash missing
    save_config(config, cfg_path)

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "/setup" in err


def test_preflight_passes_when_all_good(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x")
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is None
