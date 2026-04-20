from pathlib import Path

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage, MessageRef
from link_project_to_chat.transport.fake import FakeTransport


def _team_bot_with_fake_transport(bot: ProjectBot) -> ProjectBot:
    """Replace a team ProjectBot's _transport with a FakeTransport for assertion."""
    bot._transport = FakeTransport()
    return bot


def _group_chat(chat_id: int) -> ChatRef:
    return ChatRef(transport_id="fake", native_id=str(chat_id), kind=ChatKind.ROOM)


def _sender_identity(uid: int, handle: str, is_bot: bool) -> Identity:
    return Identity(
        transport_id="fake", native_id=str(uid),
        display_name=handle, handle=handle, is_bot=is_bot,
    )


def _group_incoming(
    chat: ChatRef,
    text: str,
    *,
    sender_uid: int = 1,
    sender_handle: str = "rezo",
    sender_is_bot: bool = False,
    is_relayed: bool = False,
    reply_to_bot_username: str | None = None,
) -> IncomingMessage:
    from types import SimpleNamespace
    native = None
    reply_to = None
    if reply_to_bot_username:
        reply_from_user = SimpleNamespace(username=reply_to_bot_username)
        reply_native = SimpleNamespace(from_user=reply_from_user)
        native = SimpleNamespace(reply_to_message=reply_native, message_id=1)
        reply_to = MessageRef(transport_id="fake", native_id="0", chat=chat)
    return IncomingMessage(
        chat=chat,
        sender=_sender_identity(uid=sender_uid, handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=native,
        is_relayed_bot_to_bot=is_relayed,
    )


def test_project_bot_derives_group_mode_from_team_args(tmp_path):
    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-1001234567890,
    )
    assert bot.group_mode is True
    assert bot.team_name == "acme"
    assert bot.role == "manager"
    assert bot.group_chat_id == -1001234567890


def test_project_bot_solo_mode_when_no_team(tmp_path):
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    assert bot.group_mode is False
    assert bot.team_name is None
    assert bot.role is None


import pytest
from unittest.mock import MagicMock, AsyncMock

from link_project_to_chat.bot import ProjectBot


@pytest.mark.asyncio
async def test_group_mode_rejects_wrong_chat_id(tmp_path):
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_claude = MagicMock()

    # Wrong group — should be silently ignored by the group chat_id guard.
    chat = _group_chat(-100_222)
    incoming = _group_incoming(chat, "@acme_manager hi", sender_handle="rezoc666")
    await bot._on_text_from_transport(incoming)

    # No replies sent, no Claude submission.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_allows_matching_chat_id_passes_routing(tmp_path):
    """When chat_id matches, the wrong-chat guard does not short-circuit. Other filters (auth, mention) still apply."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"  # required by group_filters
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_claude = MagicMock()

    # Matching chat, but no mention → not addressed to the bot (early return
    # via is_directed_at_me=False, not the chat_id guard).
    chat = _group_chat(-100_111)
    incoming = _group_incoming(chat, "no mention here", sender_handle="someone")
    await bot._on_text_from_transport(incoming)

    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_no_chat_id_set_does_not_reject(tmp_path):
    """When group_chat_id is None (not yet captured), the guard should not fire."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=None,
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_claude = MagicMock()

    # No chat_id bound — the guard does NOT fire. Capture would require a
    # trusted user, and "someone" isn't in the allowed list, so capture is
    # skipped. Then is_directed_at_me=False early-returns.
    chat = _group_chat(-100_999)
    incoming = _group_incoming(chat, "no mention", sender_handle="someone")
    await bot._on_text_from_transport(incoming)

    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


@pytest.mark.asyncio
async def test_first_group_message_captures_chat_id(tmp_path, monkeypatch):
    """When group_chat_id=0 (sentinel), a trusted-user message captures the actual chat_id."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)
    _team_bot_with_fake_transport(bot)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    chat = _group_chat(-100_999)
    incoming = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming)

    # Capture happened
    assert captured == [("acme", {"group_chat_id": -100_999})]
    assert bot.group_chat_id == -100_999


@pytest.mark.asyncio
async def test_unauth_user_does_not_trigger_capture(tmp_path, monkeypatch):
    """An unauthenticated message must NOT capture the chat_id."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=False)  # unauthorized
    _team_bot_with_fake_transport(bot)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    chat = _group_chat(-100_999)
    incoming = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=99999, sender_handle="randoc",
    )
    await bot._on_text_from_transport(incoming)

    assert captured == []
    assert bot.group_chat_id == 0  # unchanged


@pytest.mark.asyncio
async def test_second_message_after_capture_routes_normally(tmp_path, monkeypatch):
    """After chat_id is captured, subsequent messages from the same group should NOT re-trigger capture."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)
    _team_bot_with_fake_transport(bot)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    chat = _group_chat(-100_999)

    # First message captures.
    incoming1 = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming1)
    assert captured == [("acme", {"group_chat_id": -100_999})]
    assert bot.group_chat_id == -100_999

    # Second message: must NOT re-trigger capture.
    incoming2 = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming2)
    assert captured == [("acme", {"group_chat_id": -100_999})]  # still only one entry


@pytest.mark.asyncio
async def test_message_from_other_group_after_capture_rejected(tmp_path, monkeypatch):
    """After chat_id is captured, a message from a DIFFERENT group is silently rejected."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,  # already captured
    )
    bot.bot_username = "acme_manager"
    bot._auth = MagicMock(return_value=True)
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_claude = MagicMock()

    captured = []
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", lambda *a, **k: captured.append(a))

    chat = _group_chat(-100_222)  # wrong group
    incoming = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming)

    # No capture should happen, nothing sent, no Claude submission.
    assert captured == []
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


# --- settings callbacks must work in group chats for team bots ---


@pytest.mark.asyncio
async def test_permissions_callback_works_in_group_chat(tmp_path):
    """Team bots live in groups; /permissions + button click must work there.

    Previously _on_callback had a blanket "Only available in private chats"
    short-circuit that blocked every setting change on team bots. The port to
    _on_button still has to honor that: a click in a group is valid.
    """
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport import (
        ButtonClick, ChatKind, ChatRef, Identity, MessageRef,
    )
    from link_project_to_chat.transport.telegram import TelegramTransport

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        allowed_usernames=["rezo"],
        trusted_user_ids=[42],
    )
    # Stub the transport so we can observe the resulting edit.
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.bot.edit_message_text = AsyncMock()
    bot._transport = TelegramTransport(mock_app)

    chat = ChatRef(transport_id="telegram", native_id="-100111", kind=ChatKind.ROOM)
    msg = MessageRef(transport_id="telegram", native_id="500", chat=chat)
    sender = Identity(
        transport_id="telegram", native_id="42",
        display_name="Rezo", handle="rezo", is_bot=False,
    )
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="permissions_set_acceptEdits")

    await bot._on_button(click)

    # edit_message_text must have been called with the new permissions text.
    mock_app.bot.edit_message_text.assert_awaited_once()


# --- persona persistence for team bots ---


def test_persist_active_persona_team_bot_updates_team_config(tmp_path, monkeypatch):
    """Setting persona on a team bot writes to config.teams[team].bots[role], not projects."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import (
        Config,
        TeamBotConfig,
        TeamConfig,
        load_config,
        load_teams,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="old_manager"),
                        "dev":     TeamBotConfig(telegram_bot_token="t2", active_persona="old_dev"),
                    },
                )
            }
        ),
        cfg_path,
    )
    # Tests pass cfg_path explicitly to _persist_active_persona — no monkeypatch needed.
    _ = monkeypatch  # placeholder to keep the fixture arg; no longer needed

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t1",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot._persist_active_persona("software_manager", config_path=cfg_path)

    teams = load_teams(cfg_path)
    # Manager's persona was updated; dev's persona is preserved.
    assert teams["acme"].bots["manager"].active_persona == "software_manager"
    assert teams["acme"].bots["dev"].active_persona == "old_dev"
    # Tokens survive the full-bots-dict rewrite.
    assert teams["acme"].bots["manager"].telegram_bot_token == "t1"
    assert teams["acme"].bots["dev"].telegram_bot_token == "t2"
    # No stray projects entry was created.
    cfg = load_config(cfg_path)
    assert "acme_manager" not in cfg.projects


def test_persist_active_persona_team_bot_none_clears_role_only(tmp_path, monkeypatch):
    """Passing None clears this role's persona but preserves the other role's."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import (
        Config,
        TeamBotConfig,
        TeamConfig,
        load_teams,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="software_manager"),
                        "dev":     TeamBotConfig(telegram_bot_token="t2", active_persona="software_dev"),
                    },
                )
            }
        ),
        cfg_path,
    )
    # Tests pass cfg_path explicitly to _persist_active_persona — no monkeypatch needed.
    _ = monkeypatch  # placeholder to keep the fixture arg; no longer needed

    bot = ProjectBot(
        name="acme_dev", path=tmp_path, token="t2",
        team_name="acme", role="dev", group_chat_id=-100_111,
    )
    bot._persist_active_persona(None, config_path=cfg_path)

    teams = load_teams(cfg_path)
    # Dev's persona cleared; manager's survives.
    assert teams["acme"].bots["dev"].active_persona is None
    assert teams["acme"].bots["manager"].active_persona == "software_manager"


def test_persist_active_persona_solo_bot_uses_patch_project(tmp_path, monkeypatch):
    """Solo bots (no team_name) should still use patch_project — no team write."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import (
        Config,
        ProjectConfig,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            projects={
                "solo": ProjectConfig(path=str(tmp_path), telegram_bot_token="t"),
            }
        ),
        cfg_path,
    )
    # Tests pass cfg_path explicitly to _persist_active_persona — no monkeypatch needed.
    _ = monkeypatch  # placeholder to keep the fixture arg; no longer needed

    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    bot._persist_active_persona("teacher", config_path=cfg_path)

    cfg = load_config(cfg_path)
    assert cfg.projects["solo"].active_persona == "teacher"
    # No stray teams entry.
    assert cfg.teams == {}


def test_persist_active_persona_missing_team_logs_and_skips(tmp_path, monkeypatch, caplog):
    """Team bot with a team_name that isn't in config should warn, not raise."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import Config, save_config, load_teams

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)  # no teams
    # Tests pass cfg_path explicitly to _persist_active_persona — no monkeypatch needed.
    _ = monkeypatch  # placeholder to keep the fixture arg; no longer needed

    bot = ProjectBot(
        name="ghost_manager", path=tmp_path, token="t",
        team_name="ghost", role="manager", group_chat_id=-100_111,
    )
    with caplog.at_level("WARNING"):
        bot._persist_active_persona("software_manager", config_path=cfg_path)
    # Nothing was persisted (no teams created).
    assert load_teams(cfg_path) == {}
    assert any("ghost" in r.message for r in caplog.records)


# --- peer bot_username + team_system_note ---


def test_team_bot_with_peer_username_sets_team_system_note(tmp_path):
    """ProjectBot in team mode should inject peer @handle into the Claude client."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="acme_dev_claude_bot",
    )
    note = bot.task_manager.claude.team_system_note
    assert note is not None
    assert "acme_dev_claude_bot" in note
    assert "manager" in note  # self role
    assert "developer" in note  # peer role label


def test_team_bot_without_peer_username_leaves_note_unset(tmp_path):
    """Missing peer @handle should leave team_system_note as None (no stale placeholder)."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="",
    )
    assert bot.task_manager.claude.team_system_note is None


def test_team_system_note_pins_self_handle_after_refresh(tmp_path):
    """After get_me() populates self.bot_username the note must pin *both*
    the bot's own @handle and the peer's. Otherwise Claude invents an @handle
    from the persona name (the 2026-04-20 export showed a bot greet itself as
    ``@..._dev_claude_bot`` when the real handle was ``@..._dev_2_claude_bot``).
    """
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_dev", path=tmp_path, token="t",
        team_name="acme", role="dev", group_chat_id=-100_111,
        peer_bot_username="acme_mgr_bot",
    )
    # Before get_me(): note carries peer only.
    note_init = bot.task_manager.claude.team_system_note
    assert note_init is not None
    assert "@acme_mgr_bot" in note_init
    assert "@acme_dev_2_bot" not in note_init  # self handle not known yet

    # Simulate _post_init after get_me() returned our real handle.
    bot.bot_username = "acme_dev_2_bot"
    bot._refresh_team_system_note()

    note_post = bot.task_manager.claude.team_system_note
    assert note_post is not None
    assert "@acme_dev_2_bot" in note_post  # self handle pinned
    assert "@acme_mgr_bot" in note_post    # peer handle still there


def test_backfill_own_bot_username_writes_to_team_config(tmp_path, monkeypatch):
    """On startup, a team bot writes its getMe username into TeamConfig if missing."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import (
        Config,
        TeamBotConfig,
        TeamConfig,
        load_teams,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(
                            telegram_bot_token="t1",
                            active_persona="software_manager",
                            bot_username="",  # missing — should be backfilled
                        ),
                        "dev": TeamBotConfig(
                            telegram_bot_token="t2",
                            active_persona="software_dev",
                            bot_username="acme_dev_bot",  # already present
                        ),
                    },
                )
            }
        ),
        cfg_path,
    )

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t1",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager_bot"  # simulate getMe result
    bot._backfill_own_bot_username(config_path=cfg_path)

    teams = load_teams(cfg_path)
    # Own username backfilled, peer's preserved.
    assert teams["acme"].bots["manager"].bot_username == "acme_manager_bot"
    assert teams["acme"].bots["dev"].bot_username == "acme_dev_bot"


def test_teambotconfig_round_trips_permissions_and_bot_username(tmp_path):
    """TeamBotConfig fields survive save/load through config.json."""
    from link_project_to_chat.config import (
        Config,
        TeamBotConfig,
        TeamConfig,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-100_111,
                bots={
                    "manager": TeamBotConfig(
                        telegram_bot_token="t1",
                        active_persona="software_manager",
                        permissions="dangerously-skip-permissions",
                        bot_username="acme_mgr_bot",
                    ),
                    "dev": TeamBotConfig(telegram_bot_token="t2"),
                },
            )
        }
    )
    save_config(cfg, cfg_path)

    loaded = load_config(cfg_path)
    mgr = loaded.teams["acme"].bots["manager"]
    dev = loaded.teams["acme"].bots["dev"]
    assert mgr.permissions == "dangerously-skip-permissions"
    assert mgr.bot_username == "acme_mgr_bot"
    # Dev has defaults — no permissions / no bot_username.
    assert dev.permissions is None
    assert dev.bot_username == ""
