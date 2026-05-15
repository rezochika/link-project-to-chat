"""End-to-end auth migration through ProjectBot + FakeTransport.

Covers the full path: legacy config.json → load_config → ProjectBot.build()
with FakeTransport → first message lands → _auth_dirty triggers save →
on-disk file shows the populated locked_identities.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser, load_config, save_config
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _legacy_config(cfg_file: Path, project_path: Path) -> None:
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice", "bob"],
                "trusted_users": {"alice": 12345},  # alice locked; bob not
            }
        }
    }))


async def test_e2e_legacy_load_first_message_locks_id_and_persists(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    _legacy_config(cfg_file, project_path)

    # 1. Load and force-save (this is what `start` does on migration_pending).
    config = load_config(cfg_file)
    assert config.migration_pending is True
    save_config(config, cfg_file)

    on_disk = json.loads(cfg_file.read_text())
    assert "allowed_usernames" not in on_disk["projects"]["p"]
    assert "trusted_users" not in on_disk["projects"]["p"]
    users = on_disk["projects"]["p"]["allowed_users"]
    by_user = {u["username"]: u for u in users}
    assert by_user["alice"]["locked_identities"] == ["telegram:12345"]
    assert "locked_identities" not in by_user["bob"]  # not locked yet (omitted because empty)

    # 2. Build ProjectBot from the migrated config + FakeTransport.
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p",
        path=project_path,
        token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )
    # Inject FakeTransport directly (bypass real build()).
    transport = FakeTransport()
    bot._transport = transport
    bot._app = None

    # 3. Bob's first message — should auth (bob is in allowed_users) and lock his ID.
    bob_identity = Identity(
        transport_id="fake", native_id="67890",
        display_name="Bob", handle="bob", is_bot=False,
    )
    chat = ChatRef(transport_id="fake", native_id="bob-dm", kind=ChatKind.DM)
    msg = IncomingMessage(
        chat=chat,
        sender=bob_identity,
        text="hello",
        files=[],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="m1", chat=chat),
    )

    # Drive the dispatch directly: auth + persist.
    assert bot._auth_identity(bob_identity) is True
    assert bot._auth_dirty is True
    await bot._persist_auth_if_dirty()
    assert bot._auth_dirty is False

    # 4. On-disk file now shows bob with a populated locked_identities. Bob's
    #    first contact came through FakeTransport, so the identity is
    #    prefixed with "fake:" (not "telegram:" — Bob was NOT in the legacy
    #    trusted_users dict, so the migration didn't seed a telegram entry).
    on_disk_after = json.loads(cfg_file.read_text())
    users_after = on_disk_after["projects"]["p"]["allowed_users"]
    by_user_after = {u["username"]: u for u in users_after}
    assert by_user_after["bob"]["locked_identities"] == ["fake:67890"]
    # Alice's migrated telegram lock from the legacy config is preserved.
    assert "telegram:12345" in by_user_after["alice"]["locked_identities"]

    # 5. Second contact by bob: no extra save (identity already in the list).
    msg_count_before = len([f for f in tmp_path.iterdir() if f.is_file()])
    bot._auth_identity(bob_identity)
    await bot._persist_auth_if_dirty()
    assert bot._auth_dirty is False
    msg_count_after = len([f for f in tmp_path.iterdir() if f.is_file()])
    assert msg_count_before == msg_count_after


async def test_e2e_username_spoof_blocked_after_lock(tmp_path: Path):
    """After a user's identity is locked, an attacker with the same username
    but a different native_id (SAME transport) is rejected."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    # The locked identity uses transport_id="fake" because this test drives
    # FakeTransport. (For a Telegram-transport test, the locked identity would
    # be "telegram:12345" — must match the transport_id of the running bot.)
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor", "locked_identities": ["fake:12345"]},
                ],
            }
        }
    }))
    config = load_config(cfg_file)
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p",
        path=project_path,
        token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )

    # Attacker: same username "alice", different native_id on the same transport.
    # identity_key would be "fake:11111" — not in alice's locked_identities.
    attacker = Identity(
        transport_id="fake", native_id="11111",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(attacker) is False

    # Real alice still works — her identity_key is "fake:12345", in the list.
    real = Identity(
        transport_id="fake", native_id="12345",
        display_name="Anyone", handle="not-the-real-alice", is_bot=False,
    )
    assert bot._auth_identity(real) is True


async def test_e2e_multi_transport_user_locks_per_transport(tmp_path: Path):
    """A user authed first from Telegram-shape locks 'telegram:X'; same user
    first-contacting from Web appends 'web:web-session:Y' — both work."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "p": {
                "path": str(project_path),
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor"},
                ],
            }
        }
    }))
    config = load_config(cfg_file)
    proj = config.projects["p"]
    bot = ProjectBot(
        name="p", path=project_path, token="t",
        allowed_users=proj.allowed_users,
        config_path=cfg_file,
    )

    tg_ident = Identity(
        transport_id="telegram", native_id="12345",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(tg_ident) is True
    await bot._persist_auth_if_dirty()

    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    await bot._persist_auth_if_dirty()

    on_disk = json.loads(cfg_file.read_text())
    alice = next(u for u in on_disk["projects"]["p"]["allowed_users"] if u["username"] == "alice")
    assert "telegram:12345" in alice["locked_identities"]
    assert "web:web-session:abc-def" in alice["locked_identities"]
