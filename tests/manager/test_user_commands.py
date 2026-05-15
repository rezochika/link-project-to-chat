from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.config import AllowedUser, Config, save_config
from link_project_to_chat.transport.base import ChatKind, ChatRef, CommandInvocation, Identity, MessageRef


def _make_manager(tmp_path: Path, users: list[AllowedUser] | None = None) -> ManagerBot:
    cfg_file = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = list(users or [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])])
    save_config(cfg, cfg_file)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = cfg_file
    bot._allowed_users = list(cfg.allowed_users)
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    return bot


def _invocation(args: list[str], sender_handle: str = "admin", sender_id: str = "1") -> CommandInvocation:
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    sender = Identity(transport_id="telegram", native_id=sender_id, display_name=sender_handle, handle=sender_handle, is_bot=False)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    return CommandInvocation(chat=chat, sender=sender, name="cmd", args=args, raw_text=" ".join(args), message=msg)


@pytest.mark.asyncio
async def test_users_lists_current_state(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
        AllowedUser(username="bob", role="viewer"),
    ])
    await bot._on_users(_invocation([]))
    text = bot._transport.send_text.await_args.args[1]
    assert "alice" in text and "executor" in text and "12345" in text
    assert "bob" in text and "viewer" in text
    assert "not yet" in text.lower() or "—" in text  # bob has no locked id


@pytest.mark.asyncio
async def test_add_user_with_default_role_is_executor(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie"]))
    # Reload from disk to confirm persistence.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert any(u.username == "charlie" and u.role == "executor" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_add_user_with_explicit_role(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie", "viewer"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert any(u.username == "charlie" and u.role == "viewer" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_remove_user(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="bob", role="viewer"),
    ])
    # Sender is alice (executor)
    inv = _invocation(["bob"], sender_handle="alice", sender_id="1")
    await bot._on_remove_user(inv)
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert [u.username for u in cfg.allowed_users] == ["alice"]


@pytest.mark.asyncio
async def test_promote_user(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="viewer"),
    ])
    await bot._on_promote_user(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    alice = next(u for u in cfg.allowed_users if u.username == "alice")
    assert alice.role == "executor"


@pytest.mark.asyncio
async def test_demote_user(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor"),
    ])
    await bot._on_demote_user(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    alice = next(u for u in cfg.allowed_users if u.username == "alice")
    assert alice.role == "viewer"


@pytest.mark.asyncio
async def test_reset_user_identity_clears_locked_id(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
    ])
    await bot._on_reset_user_identity(_invocation(["alice"]))
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    alice = next(u for u in cfg.allowed_users if u.username == "alice")
    assert alice.locked_identities == []


@pytest.mark.asyncio
async def test_add_user_invalid_role_rejected(tmp_path):
    bot = _make_manager(tmp_path)
    await bot._on_add_user(_invocation(["charlie", "godmode"]))
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "invalid role" in text or "viewer" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_add_user(tmp_path):
    """Viewers must NOT be able to edit the allow-list — only executors."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-admin", role="viewer", locked_identities=["telegram:99"]),
    ])
    # Invocation from a viewer.
    inv = _invocation(["charlie"], sender_handle="viewer-admin", sender_id="99")
    await bot._on_add_user(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text
    # Confirm no write happened.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert not any(u.username == "charlie" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_viewer_can_list_users(tmp_path):
    """Viewers can use /users (read-only listing)."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_users(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "alice" in text
    assert "viewer-bob" in text


@pytest.mark.asyncio
async def test_promote_user_usage_message_says_promote_not_demote(tmp_path):
    """Regression test: _set_role used to compare new_role == 'promote' but
    callers pass the role string ('executor' / 'viewer'), so the usage
    message always said /demote_user even for /promote_user."""
    bot = _make_manager(tmp_path)
    # No args → usage message.
    await bot._on_promote_user(_invocation([]))
    text = bot._transport.send_text.await_args.args[1]
    assert "/promote_user" in text


@pytest.mark.asyncio
async def test_user_commands_work_without_explicit_config_path(monkeypatch, tmp_path):
    """When ManagerBot was constructed without a custom config path,
    `_load_config_for_users()` must fall back to DEFAULT_CONFIG instead of
    passing None to load_config (which would TypeError)."""
    # Redirect DEFAULT_CONFIG to a tmp file so the test doesn't touch the
    # user's home directory.
    cfg_path = tmp_path / "default-config.json"
    cfg_path.write_text(json.dumps({"allowed_users": []}))
    monkeypatch.setattr("link_project_to_chat.config.DEFAULT_CONFIG", cfg_path)
    monkeypatch.setattr("link_project_to_chat.manager.bot.DEFAULT_CONFIG", cfg_path)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = None       # ← this is the case the bug surfaced in
    bot._allowed_users = [AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"])]
    bot._init_auth()
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()

    inv = _invocation(["bob"], sender_handle="admin", sender_id="1")
    await bot._on_add_user(inv)
    # If _load_config_for_users passed None, this would have TypeError'd before
    # this line. Reaching here proves the fallback to DEFAULT_CONFIG worked.
    written = json.loads(cfg_path.read_text())
    assert any(u["username"] == "bob" for u in written.get("allowed_users", []))
