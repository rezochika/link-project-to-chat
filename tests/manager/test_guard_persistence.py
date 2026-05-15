"""Manager PTB-shim auth alignment (Task 5 Step 2b).

Regression covers two pre-rewrite bugs:
  1. _guard called self._auth(user); after Task 5 Step 3 deletes _auth, this
     path would AttributeError.
  2. _guard didn't fire a persist tail; once Step 3 makes _auth_identity
     append to locked_identities on first contact, those appends would be
     lost on restart.

Test mocks _auth_identity / _persist_auth_if_dirty to verify wiring, so it
passes immediately after Step 2b lands (does not need to wait for Step 3).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from link_project_to_chat.config import AllowedUser
from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot() -> ManagerBot:
    bot = ManagerBot.__new__(ManagerBot)
    bot._transport = FakeTransport()
    bot._project_config_path = None
    bot._allowed_users = [AllowedUser(username="alice", role="executor")]
    bot._init_auth()
    bot._auth_dirty = False
    return bot


def _make_update(username: str = "alice", user_id: int = 98765, chat_id: int = 98765):
    user = SimpleNamespace(
        id=user_id, username=username, full_name=username.title(), is_bot=False,
    )
    chat = SimpleNamespace(id=chat_id, type="private")
    return SimpleNamespace(effective_user=user, effective_chat=chat)


async def test_guard_persists_on_allow(monkeypatch):
    """Allowed path fires _persist_auth_if_dirty after returning True."""
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)
    monkeypatch.setattr(bot, "_rate_limited", lambda key: False)

    assert await bot._guard(_make_update()) is True
    assert persisted == [True]


async def test_guard_persists_on_deny(monkeypatch):
    """Denied path STILL fires _persist_auth_if_dirty (covers first-contact
    append-then-deny rate-limit edge case)."""
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: False)

    assert await bot._guard(_make_update()) is False
    assert persisted == [True]


async def test_guard_uses_identity_key_for_rate_limit(monkeypatch):
    """Rate-limit bucket is keyed on _identity_key(identity), not raw user.id.
    Regression: legacy _guard passed raw int user.id; new manager _rate_limits
    is string-keyed on 'transport_id:native_id'."""
    bot = _make_bot()

    async def _noop_persist() -> None:
        return None

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _noop_persist)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)

    seen_keys: list = []

    def _capture(key):
        seen_keys.append(key)
        return False

    monkeypatch.setattr(bot, "_rate_limited", _capture)

    await bot._guard(_make_update(user_id=98765))
    assert seen_keys == ["telegram:98765"]


async def test_edit_field_save_persists_and_uses_identity_key(monkeypatch):
    """The other moved PTB-native call site. Same wiring as _guard, but on
    PTB's MessageHandler path (pending-edit branch).

    Rate-limit short-circuits before _apply_edit runs, so _persist_auth_if_dirty
    must still fire via try/finally — and the rate-limit bucket must be
    string-keyed on _identity_key(identity), matching _guard.
    """
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    seen_keys: list = []

    def _capture(key):
        seen_keys.append(key)
        return True  # rate-limited → handler returns before pop / _apply_edit

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)
    monkeypatch.setattr(bot, "_rate_limited", _capture)

    update = _make_update(user_id=98765)
    ctx = SimpleNamespace(user_data={"pending_edit": {"name": "x", "field": "path"}})

    await bot._edit_field_save(update, ctx)
    assert persisted == [True]
    assert seen_keys == ["telegram:98765"]
    # Rate-limited path exits before pop — pending_edit stays.
    assert "pending_edit" in ctx.user_data


async def test_edit_field_save_persists_on_auth_deny(monkeypatch):
    """Auth-denied pending-edit path also fires _persist_auth_if_dirty. The
    first-contact append happens INSIDE _auth_identity in Step 3 (lands later
    in Task 5), so missing the persist tail here would lose the lock the
    moment Step 3 starts appending.
    """
    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: False)

    update = _make_update()
    ctx = SimpleNamespace(user_data={"pending_edit": {"name": "x", "field": "path"}})

    await bot._edit_field_save(update, ctx)
    assert persisted == [True]
    assert "pending_edit" in ctx.user_data


# ─── Critical follow-up: persist on transport-native command + button paths ───
#
# Regression for two Critical bugs identified by code review on Task 5 commit
# ab6c4fc. The Task 5 rewrite makes _auth_identity → _get_user_role append a
# first-contact identity to AllowedUser.locked_identities and set
# _auth_dirty=True. The transport-native command handlers (registered via
# _transport.on_command) and the button dispatcher
# (_on_button_from_transport) both call _auth_identity but did not persist
# the dirty flag — so a first-contact lock created mid-session would be lost
# on restart, letting a spoofer who lands first after restart bind their own
# native_id to the username.

async def test_command_registration_wraps_with_persist(monkeypatch, tmp_path):
    """Manager's transport-native commands must be wrapped at registration
    so EVERY exit path (allow / deny / rate-limited / exception) fires
    _persist_auth_if_dirty. _guard_invocation alone does not — it's called
    from inside each handler, and historically had no try/finally tail."""
    import json
    from unittest.mock import AsyncMock, MagicMock
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport import (
        ChatKind,
        CommandInvocation,
        MessageRef,
    )
    from link_project_to_chat.transport.base import ChatRef, Identity

    proj_cfg = tmp_path / "projects.json"
    proj_cfg.write_text(json.dumps({"projects": {}}))
    pm = ProcessManager(project_config_path=proj_cfg)
    bot = ManagerBot(
        "TOKEN", pm,
        allowed_users=[AllowedUser(username="alice", role="executor")],
        project_config_path=proj_cfg,
    )

    # Use a FakeTransport (build() would require Telegram). Mirror what
    # build() does for command registration so the wrap path runs.
    fake = FakeTransport()
    bot._transport = fake

    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)
    monkeypatch.setattr(bot, "_rate_limited", lambda key: False)

    # Re-run the manager's command registration block. We exercise the
    # public wrapper helper rather than call build() (which needs telegram).
    bot._register_transport_commands()

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="98765",
        display_name="Alice", handle="alice", is_bot=False,
    )
    invocation = CommandInvocation(
        chat=chat,
        sender=sender,
        name="version",
        args=[],
        raw_text="/version",
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
    )
    # _track is async no-op; send_text on FakeTransport already does nothing
    # destructive. Run the wrapped handler that was registered with the fake.
    wrapped = fake._command_handlers["version"]
    await wrapped(invocation)

    # Even on the success path, the wrapper must fire persist.
    assert persisted == [True]


async def test_on_button_from_transport_persists_first_contact(monkeypatch):
    """_on_button_from_transport calls _auth_identity which may append a
    first-contact identity. Must call _persist_auth_if_dirty afterwards or
    the lock is lost on restart."""
    from unittest.mock import AsyncMock
    from link_project_to_chat.transport import ButtonClick, MessageRef
    from link_project_to_chat.transport.base import ChatRef, ChatKind, Identity

    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: True)

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="98765",
        display_name="Alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="100", chat=chat)
    # value="noop" — doesn't match any prefix in the dispatch ladder, so
    # the body completes the auth check and exits without raising. The
    # try/finally must fire persist regardless.
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="noop")

    await bot._on_button_from_transport(click)
    assert persisted == [True]


async def test_on_button_from_transport_persists_on_unauth(monkeypatch):
    """Even the unauthorized branch (silent return) must fire persist —
    _auth_identity may have appended a first-contact identity before
    returning False (e.g. role lookup raced with a config change)."""
    from link_project_to_chat.transport import ButtonClick, MessageRef
    from link_project_to_chat.transport.base import ChatRef, ChatKind, Identity

    bot = _make_bot()
    persisted: list[bool] = []

    async def _track() -> None:
        persisted.append(True)

    monkeypatch.setattr(bot, "_persist_auth_if_dirty", _track)
    monkeypatch.setattr(bot, "_auth_identity", lambda identity: False)

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="98765",
        display_name="Eve", handle="eve", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="proj_back")

    await bot._on_button_from_transport(click)
    assert persisted == [True]
