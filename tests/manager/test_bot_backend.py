"""Backend-aware persistence checks for the manager bot's project flows.

These tests assert that the on-disk JSON ends up in the new backend/backend_state
shape after the manager bot mutates per-project model or permissions, so the
project bot subprocess (which now reads backend_state for model defaults) sees
the user's intent.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager
from link_project_to_chat.transport import (
    ButtonClick,
    ChatKind,
    ChatRef,
    Identity,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _swap_fake_transport(bot: ManagerBot) -> FakeTransport:
    fake = FakeTransport()
    bot._transport = fake
    return fake


def _make_button_click(
    value: str,
    *,
    user_id: int = 1,
    username: str = "testuser",
    user_data: dict | None = None,
) -> tuple[ButtonClick, dict]:
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
    return (
        ButtonClick(
            chat=chat, message=msg, sender=sender, value=value, native=(update, ctx),
        ),
        state,
    )


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


@pytest.fixture
def bot_env(tmp_path: Path):
    proj_cfg = tmp_path / "projects.json"
    proj_cfg.write_text(json.dumps({"projects": {}}))
    pm = ProcessManager(project_config_path=proj_cfg)
    from link_project_to_chat.config import AllowedUser
    bot = ManagerBot(
        "TOKEN",
        pm,
        allowed_users=[
            AllowedUser(username="testuser", role="executor", locked_identities=["telegram:1"]),
        ],
        project_config_path=proj_cfg,
    )
    return bot, pm, proj_cfg


@pytest.mark.asyncio
async def test_proj_model_button_writes_backend_state(bot_env, tmp_path: Path):
    """Clicking a model button on the project edit screen must persist the
    selection under projects[<name>].backend_state.claude.model so the project
    subprocess (which prefers backend_state over the legacy flat key) picks it
    up on next launch."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "demo": {
                "path": str(tmp_path),
                "backend": "claude",
                "backend_state": {"claude": {"model": "sonnet"}},
            }
        }
    }))
    _swap_fake_transport(bot)

    click, _ = _make_button_click("proj_model_opus_demo")
    await bot._on_button_from_transport(click)

    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
    # v1.0.0 dropped the top-level mirror; canonical home is backend_state.
    assert "model" not in raw["projects"]["demo"]


@pytest.mark.asyncio
async def test_apply_edit_model_routes_through_backend_state(bot_env, tmp_path: Path):
    """`/edit_project demo model opus` must write backend_state. Pre-v1.0
    also wrote a legacy top-level mirror for downgrade safety; v1.0.0 dropped
    that mirror so the entry only carries the canonical nested shape."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"demo": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    update, ctx = _make_update(args=["demo", "model", "opus"])
    await bot._on_edit_project(update, ctx)

    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
    assert "model" not in raw["projects"]["demo"]


@pytest.mark.asyncio
async def test_apply_edit_permissions_routes_through_backend_state(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"demo": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    update, ctx = _make_update(args=["demo", "permissions", "acceptEdits"])
    await bot._on_edit_project(update, ctx)

    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["demo"]["backend_state"]["claude"]["permissions"] == "acceptEdits"
    assert "permissions" not in raw["projects"]["demo"]


@pytest.mark.asyncio
async def test_add_project_wizard_writes_backend_state(bot_env, tmp_path: Path):
    """The /add_project wizard's final step must persist the chosen model
    under backend_state.claude (with the legacy `model` mirror for downgrade
    safety) so a fresh project lands in the new shape."""
    bot, _pm, proj_cfg = bot_env
    _swap_fake_transport(bot)

    proj_path = tmp_path / "newproj"
    proj_path.mkdir()

    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)

    state = ctx.user_data
    for step_text, handler in [
        ("newproj", bot._add_name),
        (str(proj_path), bot._add_path),
        ("MYTOKEN", bot._add_token),
        ("myuser", bot._add_username),
    ]:
        u, _ = _make_update(text=step_text)
        step_ctx = MagicMock()
        step_ctx.user_data = state
        await handler(u, step_ctx)

    u, _ = _make_update(text="opus")
    final_ctx = MagicMock()
    final_ctx.user_data = state
    await bot._add_model(u, final_ctx)

    raw = json.loads(proj_cfg.read_text())
    proj = raw["projects"]["newproj"]
    assert proj["backend"] == "claude"
    assert proj["backend_state"]["claude"]["model"] == "opus"
    # v1.0.0 dropped the top-level mirror; canonical home is backend_state.
    assert "model" not in proj


@pytest.mark.asyncio
async def test_global_model_callback_writes_default_model_claude(bot_env, tmp_path: Path):
    """The manager's global /model picker writes the canonical
    ``default_model_claude`` field. The legacy ``default_model`` mirror was
    kept one release for downgrade safety; v1.0.0 stopped emitting it."""
    bot, _pm, proj_cfg = bot_env
    _swap_fake_transport(bot)

    click, _ = _make_button_click("global_model_haiku")
    await bot._on_button_from_transport(click)

    raw = json.loads(proj_cfg.read_text())
    assert raw["default_model_claude"] == "haiku"
    assert "default_model" not in raw
