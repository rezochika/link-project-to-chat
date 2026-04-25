from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager
from link_project_to_chat.transport import (
    ButtonClick,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_update(args: list[str] | None = None, user_id: int = 1, username: str = "testuser", text: str = ""):
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.full_name = username
    user.is_bot = False
    chat = MagicMock()
    chat.id = user_id
    chat.type = "private"
    message = AsyncMock()
    message.reply_text = AsyncMock()
    message.text = text
    message.chat = chat
    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat
    update.message = message
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.user_data = {}
    return update, ctx


def _make_invocation(
    name: str,
    *,
    args: list[str] | None = None,
    user_id: int = 1,
    username: str = "testuser",
) -> CommandInvocation:
    chat = ChatRef(transport_id="fake", native_id=str(user_id), kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake",
        native_id=str(user_id),
        display_name=username,
        handle=username,
        is_bot=False,
    )
    return CommandInvocation(
        chat=chat,
        sender=sender,
        name=name,
        args=list(args or []),
        raw_text=f"/{name}",
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
    )


def _swap_fake_transport(bot: ManagerBot) -> FakeTransport:
    """Replace the bot's transport with a FakeTransport for assertions."""
    fake = FakeTransport()
    bot._transport = fake
    return fake


def _sleep_cmd() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def _make_button_click(
    value: str,
    *,
    user_id: int = 1,
    username: str = "testuser",
    user_data: dict | None = None,
) -> tuple[ButtonClick, dict]:
    """Build a ButtonClick suitable for _on_button_from_transport.

    Returns (click, user_data) where user_data is a real dict that mirrors
    what PTB's per-user storage provides via click.native[1].user_data.
    The caller can mutate it to seed pending_edit / setup_awaiting before the
    handler runs and read it after to assert state mutations.
    """
    chat = ChatRef(transport_id="fake", native_id=str(user_id), kind=ChatKind.DM)
    msg = MessageRef(transport_id="fake", native_id="1", chat=chat)
    sender = Identity(
        transport_id="fake",
        native_id=str(user_id),
        display_name=username,
        handle=username,
        is_bot=False,
    )
    state = user_data if user_data is not None else {}
    ctx = MagicMock()
    ctx.user_data = state
    update = MagicMock()
    click = ButtonClick(
        chat=chat, message=msg, sender=sender, value=value, native=(update, ctx),
    )
    return click, state


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
    fake = _swap_fake_transport(bot)

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
    return result, fake, str(proj_path)


@pytest.mark.asyncio
async def test_addproject_success(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler
    bot, pm, proj_cfg = bot_env
    result, fake, proj_path = await _run_add_dialogue(bot, tmp_path)
    assert result == ConversationHandler.END
    assert "Added" in fake.sent_messages[-1].text
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
async def test_finalize_create_stores_manager_cleanup_metadata(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler

    bot, _pm, proj_cfg = bot_env
    _swap_fake_transport(bot)
    ctx = MagicMock()
    ctx.user_data = {
        "create": {
            "name": "myproj",
            "repo": {"html_url": "https://github.com/acme/myproj"},
            "clone_path": str(tmp_path / "repos" / "myproj"),
            "bot_token": "TOKEN",
            "bot_username": "myproj_bot",
        }
    }

    result = await bot._finalize_create(_make_invocation("create_project").chat, ctx)

    assert result == ConversationHandler.END
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    assert proj["managed_by_manager"] is True
    assert proj["managed_repo_path"] == str(tmp_path / "repos" / "myproj")
    assert proj["managed_bot_username"] == "myproj_bot"


@pytest.mark.asyncio
async def test_addproject_already_exists(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    existing = tmp_path / "existing"
    existing.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(existing)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u, _ = _make_update(text="myproj")
    step_ctx = MagicMock()
    step_ctx.user_data = ctx.user_data
    result = await bot._add_name(u, step_ctx)
    assert result == bot.ADD_NAME
    assert "already exists" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_addproject_invalid_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u1, _ = _make_update(text="newproj")
    c1 = MagicMock(); c1.user_data = ctx.user_data
    await bot._add_name(u1, c1)
    u2, _ = _make_update(text="/nonexistent/xyz")
    c2 = MagicMock(); c2.user_data = ctx.user_data
    result = await bot._add_path(u2, c2)
    assert result == bot.ADD_PATH
    assert "not exist" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_editproject_rename(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"oldname": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["oldname", "name", "newname"])
    await bot._on_edit_project(update, ctx)
    assert "Renamed" in fake.sent_messages[-1].text
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "newname" in projects and "oldname" not in projects


@pytest.mark.asyncio
async def test_editproject_change_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    new_path = tmp_path / "new"; new_path.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "path", str(new_path)])
    await bot._on_edit_project(update, ctx)
    assert "Updated" in fake.sent_messages[-1].text
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"]["path"] == str(new_path)


@pytest.mark.asyncio
async def test_editproject_rename_conflict(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["a", "name", "b"])
    await bot._on_edit_project(update, ctx)
    assert "already exists" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_editproject_invalid_field(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "color", "blue"])
    await bot._on_edit_project(update, ctx)
    assert "Unknown field" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_info(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_info_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert "myproj" in fake.edited_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_start(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_start_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert pm.status("myproj") == "running"
    pm.stop("myproj")


@pytest.mark.asyncio
async def test_callback_proj_stop(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    pm.start("myproj")
    assert pm.status("myproj") == "running"
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_stop_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_remove(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    pm.start("myproj")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_remove_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert "myproj" not in json.loads(proj_cfg.read_text())["projects"]
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_remove_managed_project_runs_cleanup(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": str(tmp_path),
                        "managed_by_manager": True,
                        "managed_repo_path": str(tmp_path),
                        "managed_bot_username": "myproj_bot",
                    }
                }
            }
        )
    )
    cleanup = AsyncMock(return_value=(["deleted repo"], []))
    bot._cleanup_managed_project_resources = cleanup
    fake = _swap_fake_transport(bot)

    click, _ = _make_button_click("proj_remove_myproj")
    await bot._on_button_from_transport(click)

    cleanup.assert_awaited_once()
    assert "cleaned up manager-owned resources" in fake.edited_messages[-1].text


@pytest.mark.asyncio
async def test_create_team_execute_missing_dependencies_returns_install_hint(bot_env, monkeypatch):
    from telegram.ext import ConversationHandler

    bot, _pm, _proj_cfg = bot_env
    fake = _swap_fake_transport(bot)

    def _boom():
        raise ImportError("missing")

    monkeypatch.setattr("link_project_to_chat.manager.bot._load_team_create_dependencies", _boom)

    update, ctx = _make_update()
    ctx.user_data = {"create_team": {}}
    result = await bot._create_team_execute(update, ctx)

    assert result == ConversationHandler.END
    assert "Missing dependencies" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_delete_team_execute_missing_dependencies_returns_install_hint(bot_env, monkeypatch):
    bot, _pm, _proj_cfg = bot_env
    fake = _swap_fake_transport(bot)

    def _boom():
        raise ImportError("missing")

    monkeypatch.setattr("link_project_to_chat.manager.bot._load_team_delete_dependencies", _boom)

    await bot._delete_team_execute(_make_invocation("delete_team").chat, "acme")

    assert "Missing dependencies" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_back(bot_env):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_back")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1


@pytest.mark.asyncio
async def test_callback_unauthorized(bot_env):
    """Unauthorized button clicks are silent — no edit, no reveal of dispatch
    structure. Behaviour shifted from the legacy popup ('Unauthorized.') because
    Transport doesn't expose answer-with-text; transport.on_button auto-answers
    silently before the handler runs. See spec #0c Task 10 self-review."""
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_back", user_id=999, username="hacker")
    await bot._on_button_from_transport(click)
    assert fake.edited_messages == []
    assert fake.sent_messages == []


@pytest.mark.asyncio
async def test_projects_header_shows_count(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    await bot._on_projects_from_transport(_make_invocation("projects"))
    assert "0/1" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_edit_shows_fields(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_edit_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    edited = fake.edited_messages[-1]
    assert "myproj" in edited.text
    assert edited.buttons is not None
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "proj_efld_path_myproj" in button_values
    assert "proj_efld_model_myproj" in button_values
    assert "proj_info_myproj" in button_values  # Back button


@pytest.mark.asyncio
async def test_edit_field_prompt_and_save(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)

    # Clicking the "model" field button shows a model picker (not pending_edit)
    click, state = _make_button_click("proj_efld_model_myproj")
    await bot._on_button_from_transport(click)
    assert "pending_edit" not in state
    assert len(fake.edited_messages) == 1
    assert "Select model" in fake.edited_messages[-1].text

    # Clicking a model option saves it
    click2, _ = _make_button_click("proj_model_opus_myproj")
    await bot._on_button_from_transport(click2)
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"].get("model") == "opus"


@pytest.mark.asyncio
async def test_edit_field_rename_via_button(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    click, state = _make_button_click("proj_efld_name_myproj")
    await bot._on_button_from_transport(click)

    # pending_edit persists in the same user_data dict, so route the followup text
    # through _edit_field_save with the same state to complete the rename.
    save_update, save_ctx = _make_update(text="renamed")
    save_ctx.user_data = state
    await bot._edit_field_save(save_update, save_ctx)
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "renamed" in projects and "myproj" not in projects


@pytest.mark.asyncio
async def test_edit_cancel(bot_env):
    bot, pm, proj_cfg = bot_env
    _swap_fake_transport(bot)
    update, ctx = _make_update()
    ctx.user_data = {"pending_edit": {"name": "myproj", "field": "model"}}
    await bot._edit_cancel(update, ctx)
    assert "pending_edit" not in ctx.user_data


@pytest.mark.asyncio
async def test_button_click_cancels_pending_edit(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    # Start a non-model edit (e.g. "name") — this still uses pending_edit
    click, state = _make_button_click("proj_efld_name_myproj")
    await bot._on_button_from_transport(click)
    assert "pending_edit" in state

    # Click back — clears pending_edit (reusing the same user_data dict)
    click2, _ = _make_button_click("proj_back", user_data=state)
    await bot._on_button_from_transport(click2)
    assert "pending_edit" not in state


@pytest.mark.asyncio
async def test_edit_field_save_noop_without_pending(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update(text="some text")
    ctx.user_data = {}
    await bot._edit_field_save(update, ctx)  # should not raise or reply
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_setup_code_strips_non_digits_before_sign_in(bot_env):
    """Telegram invalidates login codes typed verbatim into any chat, so the
    setup wizard tells the user to obfuscate (e.g. 1 2 3 4 5). The handler
    must strip non-digits before calling sign_in or the code will be rejected.
    """
    bot, _, _ = bot_env
    _swap_fake_transport(bot)

    fake_client = MagicMock()
    fake_client.sign_in = AsyncMock()
    fake_bf = MagicMock()
    fake_bf._ensure_client = AsyncMock(return_value=fake_client)

    update, ctx = _make_update(text="7 0 3 9 8")
    ctx.user_data = {
        "setup_awaiting": "code",
        "setup_phone": "+995511166693",
        "setup_bf_client": fake_bf,
    }
    await bot._edit_field_save(update, ctx)

    fake_client.sign_in.assert_awaited_once_with("+995511166693", "70398")


@pytest.mark.asyncio
async def test_setup_promotes_authenticated_client_to_manager(bot_env):
    """After /setup successfully signs in, the freshly-authenticated
    TelegramClient must be promoted to ``self._telethon_client`` so that
    /create_project reuses it instead of opening a second SQLite connection
    against telethon.session (which would error with "database is locked").
    """
    bot, _, _ = bot_env
    _swap_fake_transport(bot)
    bot._telethon_client = None

    fake_client = MagicMock(name="setup_telethon_client")
    fake_client.sign_in = AsyncMock()
    fake_bf = MagicMock()
    fake_bf._ensure_client = AsyncMock(return_value=fake_client)
    fake_bf._owns_client = True

    update, ctx = _make_update(text="7 0 3 9 8")
    ctx.user_data = {
        "setup_awaiting": "code",
        "setup_phone": "+995511166693",
        "setup_bf_client": fake_bf,
    }
    await bot._edit_field_save(update, ctx)

    assert bot._telethon_client is fake_client
    # The wizard's BotFatherClient must surrender ownership so disconnect
    # paths don't double-close the now-shared client.
    assert fake_bf._owns_client is False


@pytest.mark.asyncio
async def test_execute_bot_creation_reuses_managers_telethon_client(
    bot_env, tmp_path: Path, monkeypatch
):
    """The manager keeps a persistent Telethon client connected to the
    telethon.session SQLite file. Constructing a new TelegramClient against
    the same file in /create_project would raise "database is locked", so
    BotFatherClient must adopt the manager's client when one exists.
    """
    bot, _, proj_cfg = bot_env
    _swap_fake_transport(bot)

    from link_project_to_chat.config import Config, save_config
    save_config(Config(telegram_api_id=1, telegram_api_hash="x"), proj_cfg)

    constructor_kwargs: dict = {}

    class FakeBotFatherClient:
        def __init__(self, api_id, api_hash, session_path, client=None):
            constructor_kwargs["client"] = client

        async def create_bot(self, display_name: str, username: str) -> str:
            raise RuntimeError("stop here — we only care about constructor args")

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(
        "link_project_to_chat.botfather.BotFatherClient", FakeBotFatherClient
    )

    sentinel_client = MagicMock(name="manager_telethon_client")
    bot._telethon_client = sentinel_client

    update, ctx = _make_update()
    ctx.user_data = {"create": {"config_path": str(proj_cfg), "name": "myproj"}}
    await bot._execute_bot_creation(
        ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM), ctx, "myproj"
    )

    assert constructor_kwargs["client"] is sentinel_client


def _write_team(proj_cfg: Path, team: str, bots: dict, group_chat_id: int = -1001) -> None:
    raw = json.loads(proj_cfg.read_text())
    raw.setdefault("teams", {})[team] = {
        "path": str(proj_cfg.parent),
        "group_chat_id": group_chat_id,
        "bots": bots,
    }
    proj_cfg.write_text(json.dumps(raw))


@pytest.mark.asyncio
async def test_on_teams_lists_one_button_per_team(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    _write_team(proj_cfg, "beta", {"manager": {"telegram_bot_token": "t3"}})
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    assert len(fake.sent_messages) == 1
    buttons = fake.sent_messages[-1].buttons
    assert buttons is not None
    button_values = [btn.value for row in buttons.rows for btn in row]
    assert button_values == ["team_info_acme", "team_info_beta"]


@pytest.mark.asyncio
async def test_on_teams_button_label_shows_running_count(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    buttons = fake.sent_messages[-1].buttons
    assert buttons is not None
    labels = [btn.label for row in buttons.rows for btn in row]
    assert any("0/2" in label and "acme" in label for label in labels)


@pytest.mark.asyncio
async def test_on_teams_empty_no_markup(bot_env):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    assert "No teams" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_team_info_shows_start_and_per_bot_status(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_info_acme")
    await bot._on_button_from_transport(click)
    edited = fake.edited_messages[-1]
    assert "acme" in edited.text
    assert "manager" in edited.text and "dev" in edited.text
    assert edited.buttons is not None
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_start_acme" in button_values
    assert "team_back" in button_values


@pytest.mark.asyncio
async def test_callback_team_start_invokes_start_team_for_each_bot(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.start_team = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="running")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_start_acme")
    await bot._on_button_from_transport(click)
    calls = {c.args for c in pm.start_team.call_args_list}
    assert calls == {("acme", "manager"), ("acme", "dev")}
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_stop_acme" in button_values


@pytest.mark.asyncio
async def test_callback_team_stop_invokes_stop_for_each_bot(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.stop = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="stopped")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_stop_acme")
    await bot._on_button_from_transport(click)
    stopped_keys = {c.args[0] for c in pm.stop.call_args_list}
    assert stopped_keys == {"team:acme:manager", "team:acme:dev"}
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_start_acme" in button_values


@pytest.mark.asyncio
async def test_callback_team_back_relists_teams(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_back")
    await bot._on_button_from_transport(click)
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_info_acme" in button_values


@pytest.mark.asyncio
async def test_guard_returns_false_when_effective_user_is_none(bot_env):
    """Regression: _guard must not crash when update.effective_user is None
    (anonymous channel admins, service messages, etc.)."""
    from types import SimpleNamespace

    bot, _pm, _cfg = bot_env
    fake = _swap_fake_transport(bot)

    update = SimpleNamespace(
        effective_user=None,
        effective_chat=SimpleNamespace(id=12345, type="private"),
        effective_message=SimpleNamespace(text=""),
    )

    allowed = await bot._guard(update)
    assert allowed is False
    assert any("Unauthorized" in m.text for m in fake.sent_messages)


@pytest.mark.asyncio
async def test_remove_user_revokes_trusted_binding_immediately(bot_env):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(
        json.dumps(
            {
                "allowed_usernames": ["testuser", "alice"],
                "trusted_users": {"alice": 42},
                "projects": {},
            }
        )
    )
    bot._allowed_usernames = ["testuser", "alice"]
    bot._trusted_users = {"alice": 42}
    fake = _swap_fake_transport(bot)

    invocation = _make_invocation("remove_user", args=["alice"])
    await bot._on_remove_user_from_transport(invocation)

    assert fake.sent_messages[-1].text == "Removed @alice."
    raw = json.loads(proj_cfg.read_text())
    assert raw["allowed_usernames"] == ["testuser"]
    assert raw["trusted_users"] == {"testuser": 1}
    revoked = Identity(
        transport_id="fake",
        native_id="42",
        display_name="alice",
        handle="alice",
        is_bot=False,
    )
    assert bot._auth_identity(revoked) is False
