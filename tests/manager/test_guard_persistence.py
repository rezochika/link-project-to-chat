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
