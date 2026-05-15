from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot


def _make_manager(monkeypatch, projects=None):
    from link_project_to_chat.config import AllowedUser
    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = None
    bot._allowed_users = [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])]
    bot._init_auth()
    monkeypatch.setattr(bot, "_load_projects", lambda: projects or {})
    return bot


def test_available_plugins_returns_entry_point_names(monkeypatch):
    bot = _make_manager(monkeypatch)
    fake_ep = MagicMock()
    fake_ep.name = "demo"
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.importlib.metadata.entry_points",
        lambda group: [fake_ep] if group == "lptc.plugins" else [],
    )
    assert bot._available_plugins() == ["demo"]


def test_plugins_buttons_marks_active_and_available(monkeypatch):
    projects = {"myp": {"plugins": [{"name": "demo"}]}}
    bot = _make_manager(monkeypatch, projects)
    a = MagicMock(); a.name = "demo"
    b = MagicMock(); b.name = "other"
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.importlib.metadata.entry_points",
        lambda group: [a, b] if group == "lptc.plugins" else [],
    )
    buttons = bot._plugins_buttons("myp")
    labels = [btn.label for row in buttons.rows for btn in row]
    assert any(l.startswith("✓ demo") for l in labels)
    assert any(l.startswith("+ other") for l in labels)


@pytest.mark.asyncio
async def test_viewer_cannot_toggle_plugin(monkeypatch, tmp_path):
    """A viewer clicking the plugin toggle gets a Read-only reply; the
    project's plugins list is NOT modified."""
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [
        AllowedUser(username="viewer-admin", role="viewer", locked_identities=["telegram:9"]),
    ]
    save_config(cfg, cfg_path)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = cfg_path
    bot._allowed_users = list(cfg.allowed_users)
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()

    sender = Identity(transport_id="telegram", native_id="9", display_name="V", handle="viewer-admin", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="proj_ptog_demo|myp")

    monkeypatch.setattr(bot, "_load_projects", lambda: {"myp": {"plugins": []}})
    save_called = []
    monkeypatch.setattr(bot, "_save_projects", lambda p: save_called.append(p))

    # The manager bot's transport-native button entry point is
    # _on_button_from_transport (verified via grep on manager/bot.py - the
    # one registered via self._transport.on_button(...)). Use it directly
    # so the test exercises the real dispatch path.
    await bot._on_button_from_transport(click)

    assert save_called == []
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text


def _executor_click(value: str):
    from link_project_to_chat.transport.base import (
        ButtonClick, ChatKind, ChatRef, Identity, MessageRef,
    )
    sender = Identity(
        transport_id="telegram", native_id="1",
        display_name="Admin", handle="admin", is_bot=False,
    )
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    return ButtonClick(chat=chat, message=msg, sender=sender, value=value)


@pytest.mark.asyncio
async def test_executor_toggle_restarts_running_bot(monkeypatch):
    """Toggling a plugin on an actively-running project must stop+start the
    project bot so the in-memory plugin set matches the freshly-saved config.
    Without that, a 'disabled' plugin keeps serving requests until the operator
    notices and restarts manually."""
    bot = _make_manager(monkeypatch, projects={"myp": {"plugins": []}})
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()
    bot._pm = MagicMock()
    bot._pm.status = MagicMock(return_value="running")
    bot._pm.stop = MagicMock(return_value=True)
    bot._pm.start = MagicMock(return_value=True)
    monkeypatch.setattr(bot, "_save_projects", lambda p: None)
    monkeypatch.setattr(bot, "_plugins_buttons", lambda name: None)

    await bot._on_button_from_transport(_executor_click("proj_ptog_demo|myp"))

    bot._pm.stop.assert_called_once_with("myp")
    bot._pm.start.assert_called_once_with("myp")
    edit_text = bot._transport.edit_text.await_args.args[1].lower()
    assert "restart" in edit_text and "applied" in edit_text


@pytest.mark.asyncio
async def test_executor_toggle_when_stopped_does_not_call_pm_start(monkeypatch):
    """When the project bot is not running, the toggle just persists the
    config and surfaces 'will take effect on next start'. We must NOT
    auto-start a stopped bot — that would silently undo a deliberate stop."""
    bot = _make_manager(monkeypatch, projects={"myp": {"plugins": []}})
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()
    bot._pm = MagicMock()
    bot._pm.status = MagicMock(return_value="stopped")
    bot._pm.stop = MagicMock(return_value=False)
    bot._pm.start = MagicMock(return_value=False)
    monkeypatch.setattr(bot, "_save_projects", lambda p: None)
    monkeypatch.setattr(bot, "_plugins_buttons", lambda name: None)

    await bot._on_button_from_transport(_executor_click("proj_ptog_demo|myp"))

    bot._pm.stop.assert_not_called()
    bot._pm.start.assert_not_called()
    edit_text = bot._transport.edit_text.await_args.args[1].lower()
    assert "next start" in edit_text or "not running" in edit_text
