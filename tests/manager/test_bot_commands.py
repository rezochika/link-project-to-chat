from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager


def _make_update(args: list[str] | None = None, user_id: int = 1, username: str = "testuser", text: str = ""):
    user = MagicMock()
    user.id = user_id
    user.username = username
    message = AsyncMock()
    message.reply_text = AsyncMock()
    message.text = text
    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    update.message = message
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.user_data = {}
    return update, ctx


def _make_callback(data: str, user_id: int = 1, username: str = "testuser"):
    user = MagicMock()
    user.id = user_id
    user.username = username
    query = AsyncMock()
    query.data = data
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    ctx = MagicMock()
    return update, ctx, query


@pytest.fixture
def bot_env(tmp_path: Path):
    proj_cfg = tmp_path / "projects.json"
    proj_cfg.write_text(json.dumps({"projects": {}}))
    pm = ProcessManager(project_config_path=proj_cfg)
    bot = ManagerBot("TOKEN", pm, allowed_username="testuser", trusted_user_id=1, project_config_path=proj_cfg)
    return bot, pm, proj_cfg


async def _run_add_dialogue(bot, tmp_path, name="myproj", token="/skip", username="/skip", model="/skip"):
    proj_path = tmp_path / name
    proj_path.mkdir(exist_ok=True)

    update, ctx = _make_update()
    result = await bot._on_add_project(update, ctx)
    assert result == bot.ADD_NAME

    for step_text, expected_state in [
        (name, bot.ADD_PATH),
        (str(proj_path), bot.ADD_TOKEN),
        (token, bot.ADD_USERNAME),
        (username, bot.ADD_MODEL),
    ]:
        u, _ = _make_update(text=step_text)
        step_ctx = MagicMock()
        step_ctx.user_data = ctx.user_data
        handler = {
            bot.ADD_PATH: bot._add_name,
            bot.ADD_TOKEN: bot._add_path,
            bot.ADD_USERNAME: bot._add_token,
            bot.ADD_MODEL: bot._add_username,
        }[expected_state]
        result = await handler(u, step_ctx)
        assert result == expected_state

    u, _ = _make_update(text=model)
    final_ctx = MagicMock()
    final_ctx.user_data = ctx.user_data
    result = await bot._add_model(u, final_ctx)
    return result, u, str(proj_path)


@pytest.mark.asyncio
async def test_addproject_success(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler
    bot, pm, proj_cfg = bot_env
    result, last_update, proj_path = await _run_add_dialogue(bot, tmp_path)
    assert result == ConversationHandler.END
    assert "Added" in last_update.effective_message.reply_text.call_args[0][0]
    assert "myproj" in json.loads(proj_cfg.read_text())["projects"]


@pytest.mark.asyncio
async def test_addproject_with_all_options(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler
    bot, pm, proj_cfg = bot_env
    result, _, _ = await _run_add_dialogue(bot, tmp_path, name="fullproj", token="MYTOKEN", username="myuser", model="opus")
    assert result == ConversationHandler.END
    proj = json.loads(proj_cfg.read_text())["projects"]["fullproj"]
    assert proj["telegram_bot_token"] == "MYTOKEN"
    assert proj["username"] == "myuser"
    assert proj["model"] == "opus"


@pytest.mark.asyncio
async def test_addproject_already_exists(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    existing = tmp_path / "existing"
    existing.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(existing)}}}))
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u, _ = _make_update(text="myproj")
    step_ctx = MagicMock()
    step_ctx.user_data = ctx.user_data
    result = await bot._add_name(u, step_ctx)
    assert result == bot.ADD_NAME
    assert "already exists" in u.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_addproject_invalid_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u1, _ = _make_update(text="newproj")
    c1 = MagicMock(); c1.user_data = ctx.user_data
    await bot._add_name(u1, c1)
    u2, _ = _make_update(text="/nonexistent/xyz")
    c2 = MagicMock(); c2.user_data = ctx.user_data
    result = await bot._add_path(u2, c2)
    assert result == bot.ADD_PATH
    assert "not exist" in u2.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_editproject_rename(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"oldname": {"path": str(tmp_path)}}}))
    update, ctx = _make_update(["oldname", "name", "newname"])
    await bot._on_edit_project(update, ctx)
    assert "Renamed" in update.effective_message.reply_text.call_args[0][0]
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "newname" in projects and "oldname" not in projects


@pytest.mark.asyncio
async def test_editproject_change_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    new_path = tmp_path / "new"; new_path.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    update, ctx = _make_update(["myproj", "path", str(new_path)])
    await bot._on_edit_project(update, ctx)
    assert "Updated" in update.effective_message.reply_text.call_args[0][0]
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"]["path"] == str(new_path)


@pytest.mark.asyncio
async def test_editproject_rename_conflict(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}}}))
    update, ctx = _make_update(["a", "name", "b"])
    await bot._on_edit_project(update, ctx)
    assert "already exists" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_editproject_invalid_field(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    update, ctx = _make_update(["myproj", "color", "blue"])
    await bot._on_edit_project(update, ctx)
    assert "Unknown field" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_callback_proj_info(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    update, ctx, query = _make_callback("proj_info_myproj")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "myproj" in text


@pytest.mark.asyncio
async def test_callback_proj_start(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: ["sleep", "60"]
    update, ctx, query = _make_callback("proj_start_myproj")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()
    assert pm.status("myproj") == "running"
    pm.stop("myproj")


@pytest.mark.asyncio
async def test_callback_proj_stop(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: ["sleep", "60"]
    pm.start("myproj")
    assert pm.status("myproj") == "running"
    update, ctx, query = _make_callback("proj_stop_myproj")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_remove(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: ["sleep", "60"]
    pm.start("myproj")
    update, ctx, query = _make_callback("proj_remove_myproj")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()
    assert "myproj" not in json.loads(proj_cfg.read_text())["projects"]
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_back(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx, query = _make_callback("proj_back")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_callback_unauthorized(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx, query = _make_callback("proj_back", user_id=999, username="hacker")
    await bot._on_callback(update, ctx)
    query.answer.assert_called_with("Unauthorized.")
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_projects_header_shows_count(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    update, ctx = _make_update()
    await bot._on_projects(update, ctx)
    text = update.effective_message.reply_text.call_args[0][0]
    assert "0/1" in text


@pytest.mark.asyncio
async def test_callback_proj_edit_shows_fields(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    update, ctx, query = _make_callback("proj_edit_myproj")
    await bot._on_callback(update, ctx)
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "myproj" in text
    markup = query.edit_message_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("proj_efld_path_myproj" == d for d in button_datas)
    assert any("proj_efld_model_myproj" == d for d in button_datas)
    assert any("proj_info_myproj" == d for d in button_datas)  # Back button


@pytest.mark.asyncio
async def test_edit_field_prompt_and_save(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))

    # Clicking the "model" field button shows a model picker (not pending_edit)
    update, ctx, query = _make_callback("proj_efld_model_myproj")
    ctx.user_data = {}
    await bot._on_callback(update, ctx)
    assert "pending_edit" not in ctx.user_data
    query.edit_message_text.assert_called_once()
    call_text = query.edit_message_text.call_args[0][0]
    assert "Select model" in call_text

    # Clicking a model option saves it
    update2, ctx2, query2 = _make_callback("proj_model_opus_myproj")
    ctx2.user_data = {}
    await bot._on_callback(update2, ctx2)
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"].get("model") == "opus"


@pytest.mark.asyncio
async def test_edit_field_rename_via_button(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))

    update, ctx, query = _make_callback("proj_efld_name_myproj")
    ctx.user_data = {}
    await bot._on_callback(update, ctx)

    save_update, save_ctx = _make_update(text="renamed")
    save_ctx.user_data = ctx.user_data
    await bot._edit_field_save(save_update, save_ctx)
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "renamed" in projects and "myproj" not in projects


@pytest.mark.asyncio
async def test_edit_cancel(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update()
    ctx.user_data = {"pending_edit": {"name": "myproj", "field": "model"}}
    await bot._edit_cancel(update, ctx)
    assert "pending_edit" not in ctx.user_data


@pytest.mark.asyncio
async def test_button_click_cancels_pending_edit(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))

    # Start a non-model edit (e.g. "name") — this still uses pending_edit
    update, ctx, query = _make_callback("proj_efld_name_myproj")
    ctx.user_data = {}
    await bot._on_callback(update, ctx)
    assert "pending_edit" in ctx.user_data

    # Click back — clears pending_edit
    update2, ctx2, query2 = _make_callback("proj_back")
    ctx2.user_data = ctx.user_data
    await bot._on_callback(update2, ctx2)
    assert "pending_edit" not in ctx2.user_data


@pytest.mark.asyncio
async def test_edit_field_save_noop_without_pending(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update(text="some text")
    ctx.user_data = {}
    await bot._edit_field_save(update, ctx)  # should not raise or reply
    update.effective_message.reply_text.assert_not_called()


def _write_team(proj_cfg: Path, team: str, bots: dict, group_chat_id: int = -1001) -> None:
    raw = json.loads(proj_cfg.read_text())
    raw.setdefault("teams", {})[team] = {
        "path": str(proj_cfg.parent),
        "group_chat_id": group_chat_id,
        "bots": bots,
    }
    proj_cfg.write_text(json.dumps(raw))


@pytest.mark.asyncio
async def test_on_teams_lists_configured_teams(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    update, ctx = _make_update()
    await bot._on_teams(update, ctx)
    update.effective_message.reply_text.assert_called_once()
    markup = update.effective_message.reply_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("team_info_acme:manager" == d for d in button_datas)
    assert any("team_info_acme:dev" == d for d in button_datas)


@pytest.mark.asyncio
async def test_on_teams_empty_no_markup(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update()
    await bot._on_teams(update, ctx)
    text = update.effective_message.reply_text.call_args[0][0]
    assert "No teams" in text


@pytest.mark.asyncio
async def test_callback_team_info_shows_start_when_stopped(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    update, ctx, query = _make_callback("team_info_acme:manager")
    await bot._on_callback(update, ctx)
    markup = query.edit_message_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("team_start_acme:manager" == d for d in button_datas)
    assert not any("team_stop_acme:manager" == d for d in button_datas)


@pytest.mark.asyncio
async def test_callback_team_start_invokes_start_team(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    pm.start_team = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="running")
    update, ctx, query = _make_callback("team_start_acme:manager")
    await bot._on_callback(update, ctx)
    pm.start_team.assert_called_once_with("acme", "manager")
    query.edit_message_text.assert_called_once()
    markup = query.edit_message_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("team_stop_acme:manager" == d for d in button_datas)


@pytest.mark.asyncio
async def test_callback_team_stop_invokes_stop_with_team_key(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    pm.stop = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="stopped")
    update, ctx, query = _make_callback("team_stop_acme:manager")
    await bot._on_callback(update, ctx)
    pm.stop.assert_called_once_with("team:acme:manager")
    query.edit_message_text.assert_called_once()
    markup = query.edit_message_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("team_start_acme:manager" == d for d in button_datas)


@pytest.mark.asyncio
async def test_callback_team_back_relists_teams(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    update, ctx, query = _make_callback("team_back")
    await bot._on_callback(update, ctx)
    markup = query.edit_message_text.call_args[1]["reply_markup"]
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("team_info_acme:manager" == d for d in button_datas)
