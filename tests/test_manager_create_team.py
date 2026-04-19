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


from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_show_repo_page_supports_user_data_key(tmp_path, monkeypatch):
    """_show_repo_page must read/write to the key passed in, not hardcoded 'create'."""
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    ctx = MagicMock()
    ctx.user_data = {"create_team": {"config_path": str(cfg_path)}}
    query = AsyncMock()
    query.edit_message_text = AsyncMock()

    # Monkeypatch GitHubClient.list_repos to avoid network
    async def fake_list_repos(self, *a, **kw):
        repo = MagicMock()
        repo.name = "acme"
        repo.full_name = "me/acme"
        repo.description = "example"
        repo.private = False
        repo.html_url = "https://github.com/me/acme"
        repo.clone_url = "https://github.com/me/acme.git"
        return [repo], False

    monkeypatch.setattr(
        "link_project_to_chat.github_client.GitHubClient.list_repos", fake_list_repos
    )

    await mb._show_repo_page(query, ctx, page=1, user_data_key="create_team")
    # Assert the repos landed in ctx.user_data["create_team"], not ["create"]
    assert "repos" in ctx.user_data["create_team"]
    assert "me/acme" in ctx.user_data["create_team"]["repos"]


@pytest.mark.asyncio
async def test_create_bot_with_retry_tries_suffixes(monkeypatch):
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    attempts = []

    async def fake_create_bot(display_name: str, username: str) -> str:
        attempts.append(username)
        if len(attempts) < 3:
            raise ValueError("username taken")
        return "FAKE_TOKEN"

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    token, username = await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")
    assert token == "FAKE_TOKEN"
    assert username == "acme_mgr_2_claude_bot"
    assert attempts == ["acme_mgr_claude_bot", "acme_mgr_1_claude_bot", "acme_mgr_2_claude_bot"]


@pytest.mark.asyncio
async def test_create_bot_with_retry_fails_after_5_tries():
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    async def fake_create_bot(display_name: str, username: str) -> str:
        raise ValueError("username taken")

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    with pytest.raises(RuntimeError, match="5 attempts"):
        await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")
