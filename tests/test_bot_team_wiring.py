from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage, MessageRef
from link_project_to_chat.transport.fake import FakeTransport


def _team_bot_with_fake_transport(bot: ProjectBot) -> ProjectBot:
    """Replace a team ProjectBot's _transport with a FakeTransport for assertion."""
    bot._transport = FakeTransport()
    return bot


class _TelegramLikeFakeTransport(FakeTransport):
    TRANSPORT_ID = "telegram"

    async def send_text(self, chat, text, **kwargs):
        int(chat.native_id)
        return await super().send_text(chat, text, **kwargs)


class _TelegramChatNotFoundTransport(_TelegramLikeFakeTransport):
    async def send_text(self, chat, text, **kwargs):
        raise RuntimeError("Chat not found")


def _group_chat(chat_id: int) -> ChatRef:
    # transport_id="telegram" because the tests model Telegram-bound bots
    # (constructed with the legacy `group_chat_id: int` kwarg, which synthesizes
    # a Telegram-flavored RoomBinding). The bot's _transport is FakeTransport
    # for assertion convenience; ChatRef.transport_id is independent metadata.
    return ChatRef(transport_id="telegram", native_id=str(chat_id), kind=ChatKind.ROOM)


def _sender_identity(uid: int, handle: str, is_bot: bool) -> Identity:
    return Identity(
        transport_id="telegram", native_id=str(uid),
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
    reply_to = None
    reply_to_sender = None
    if reply_to_bot_username:
        reply_to = MessageRef(transport_id=chat.transport_id, native_id="0", chat=chat)
        reply_to_sender = Identity(
            transport_id=chat.transport_id, native_id="0",
            display_name=reply_to_bot_username,
            handle=reply_to_bot_username, is_bot=True,
        )
    return IncomingMessage(
        chat=chat,
        sender=_sender_identity(uid=sender_uid, handle=sender_handle, is_bot=sender_is_bot),
        text=text,
        files=[],
        reply_to=reply_to,
        native=None,
        is_relayed_bot_to_bot=is_relayed,
        message=MessageRef(transport_id=chat.transport_id, native_id="1", chat=chat),
        reply_to_sender=reply_to_sender,
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
    bot.task_manager.submit_agent = MagicMock()

    # Wrong group — should be silently ignored by the group chat_id guard.
    chat = _group_chat(-100_222)
    incoming = _group_incoming(chat, "@acme_manager hi", sender_handle="rezoc666")
    await bot._on_text_from_transport(incoming)

    # No replies sent, no Claude submission.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_allows_matching_chat_id_passes_routing(tmp_path):
    """When chat_id matches, the wrong-chat guard does not short-circuit. Other filters (auth, mention) still apply."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"  # required by group_filters
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()

    # Matching chat, but no mention → not addressed to the bot (early return
    # via is_directed_at_me=False, not the chat_id guard).
    chat = _group_chat(-100_111)
    incoming = _group_incoming(chat, "no mention here", sender_handle="someone")
    await bot._on_text_from_transport(incoming)

    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_no_chat_id_set_does_not_reject(tmp_path):
    """When group_chat_id is None (not yet captured), the guard should not fire."""
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=None,
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()

    # No chat_id bound — the guard does NOT fire. Capture would require a
    # trusted user, and "someone" isn't in the allowed list, so capture is
    # skipped. Then is_directed_at_me=False early-returns.
    chat = _group_chat(-100_999)
    incoming = _group_incoming(chat, "no mention", sender_handle="someone")
    await bot._on_text_from_transport(incoming)

    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()


@pytest.mark.asyncio
async def test_first_group_message_captures_chat_id(tmp_path, monkeypatch):
    """When group_chat_id=0 (sentinel), a trusted-user message captures the actual chat_id."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
        allowed_users=[
            AllowedUser(username="rezoc666", role="executor", locked_identities=["telegram:12345"]),
        ],
    )
    bot.bot_username = "acme_manager"
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

    # Capture happened — writes both the new RoomBinding shape and the legacy
    # group_chat_id mirror (the latter only for Telegram, for one release of
    # downgrade safety per spec #1's dual-write pattern).
    assert captured == [(
        "acme",
        {
            "room": {"transport_id": "telegram", "native_id": "-100999"},
            "group_chat_id": -100_999,
        },
    )]
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
    # No allowed_users → fail-closed (every sender denied).
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
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=0,
        allowed_users=[
            AllowedUser(username="rezoc666", role="executor", locked_identities=["telegram:12345"]),
        ],
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)

    captured = []
    def fake_patch_team(name, fields, *args, **kwargs):
        captured.append((name, fields))
    monkeypatch.setattr("link_project_to_chat.bot.patch_team", fake_patch_team)

    chat = _group_chat(-100_999)

    # First message captures (dual-write: new RoomBinding + legacy mirror).
    incoming1 = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming1)
    expected_capture = (
        "acme",
        {
            "room": {"transport_id": "telegram", "native_id": "-100999"},
            "group_chat_id": -100_999,
        },
    )
    assert captured == [expected_capture]
    assert bot.group_chat_id == -100_999

    # Second message: must NOT re-trigger capture.
    incoming2 = _group_incoming(
        chat, "@acme_manager hi",
        sender_uid=12345, sender_handle="rezoc666",
    )
    await bot._on_text_from_transport(incoming2)
    assert captured == [expected_capture]  # still only one entry


@pytest.mark.asyncio
async def test_message_from_other_group_after_capture_rejected(tmp_path, monkeypatch):
    """After chat_id is captured, a message from a DIFFERENT group is silently rejected."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,  # already captured
        allowed_users=[
            AllowedUser(username="rezo", role="executor", locked_identities=["telegram:1"]),
        ],
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()

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
    bot.task_manager.submit_agent.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Single-message-or-attach for team-mode replies
#
# Long bot replies in team mode used to chunk-split into multiple Telegram
# messages. The relay coalesces fragile (3s window, requires same reply_to,
# only first chunk has the @peer mention), so the 2026-04-27 incident showed
# late/out-of-order parts being dropped — peer bot saw fragments. The fix is
# source-side: in team mode, never produce more than one Telegram message
# per agent reply. Anything past the limit goes into a file attachment that
# the peer bot can read.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_mode_short_reply_sends_one_message_no_attachment(tmp_path):
    """A short agent reply still uses the regular send_text path, no file."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)

    chat = _group_chat(-100_111)
    await bot._send_to_chat(chat, "@acme_dev_bot please implement P1-1")

    assert len(bot._transport.sent_messages) == 1
    assert len(bot._transport.sent_files) == 0
    assert "@acme_dev_bot" in bot._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_team_mode_long_reply_sends_one_message_plus_file(tmp_path):
    """A long agent reply must produce exactly ONE sent_text + ONE sent_file in
    team mode — never multiple chunked send_texts. The file holds the full
    original body so the peer bot can read it via incoming.files."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)

    chat = _group_chat(-100_111)
    long_body = "@acme_dev_bot here is a very long batch report.\n\n" + (
        "- detail line that runs on and on with lots of context " * 200
    )
    await bot._send_to_chat(chat, long_body)

    assert len(bot._transport.sent_messages) == 1, (
        f"expected 1 message in team mode, got {len(bot._transport.sent_messages)}"
    )
    assert len(bot._transport.sent_files) == 1, (
        f"expected 1 file attachment, got {len(bot._transport.sent_files)}"
    )
    # The visible message should reference the attachment.
    head = bot._transport.sent_messages[0].text
    assert "truncated" in head.lower() or "attached" in head.lower()
    # The attached file must contain the FULL original body.
    file_path = bot._transport.sent_files[0].path
    file_content = file_path.read_text(encoding="utf-8")
    assert long_body.strip() in file_content or "@acme_dev_bot" in file_content
    assert len(file_content) >= len(long_body)


@pytest.mark.asyncio
async def test_solo_mode_long_reply_still_chunks_into_multiple_messages(tmp_path):
    """Outside team mode, the existing chunked-split behavior is preserved
    (a human reading the chat is fine with multiple consecutive messages)."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    assert bot.group_mode is False
    _team_bot_with_fake_transport(bot)

    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    long_body = "x" * 8000  # well over Telegram's 4096
    await bot._send_to_chat(chat, long_body)

    # Solo mode: chunked into multiple messages, no attachment.
    assert len(bot._transport.sent_messages) >= 2
    assert len(bot._transport.sent_files) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Session-aware persona/history injection gate (Step 2)
#
# Codex (and Claude) CLIs persist conversation memory server-side via session
# resume. Re-prepending the persona + recent-history block to the user message
# every turn causes the model to re-execute the persona's procedural directives
# from scratch (e.g. the software_manager "Review protocol" preamble), which is
# the root of the 2026-04-27 looping incident on the codex manager.
#
# Fix: in team mode, when the backend has a resumable session, skip the
# persona + history prepend. The team_system_note (handled by the backend
# itself) and the actual user message still go through. Outside team mode,
# behavior is unchanged.
# ─────────────────────────────────────────────────────────────────────────────


def _make_fake_backend(session_id: str | None, supports_resume: bool = True):
    """Stand-in backend exposing only what _team_session_active reads."""
    class _Caps:
        def __init__(self, supports_resume: bool):
            self.supports_resume = supports_resume

    class _FakeBackend:
        name = "fake"

        def __init__(self) -> None:
            self.session_id = session_id
            self.capabilities = _Caps(supports_resume)

    return _FakeBackend()


@pytest.mark.asyncio
async def test_team_first_turn_relay_path_includes_persona_and_history(tmp_path):
    """Fresh session (session_id=None): persona + history MUST be prepended so
    the agent sees them at least once. They land in the backend's session
    memory and don't need to be re-sent on later turns."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        active_persona="software_manager",
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id=None)

    captured = []
    bot.task_manager.submit_agent = lambda **kw: captured.append(kw)

    # Seed history so the prepend block is non-empty.
    bot.conversation_log.append(
        ChatRef(transport_id="telegram", native_id="-100111", kind=ChatKind.ROOM),
        "user", "earlier turn", backend="fake",
    )

    incoming = _group_incoming(
        _group_chat(-100_111),
        "@acme_manager dispatch P1-1",
        sender_handle="acme_dev_bot", sender_is_bot=True, is_relayed=True,
    )
    await bot._submit_group_message_to_claude(incoming)

    assert len(captured) == 1
    prompt = captured[0]["prompt"]
    assert "[PERSONA: software_manager]" in prompt
    assert "[Recent conversation history" in prompt


@pytest.mark.asyncio
async def test_team_resumed_turn_relay_path_omits_persona_and_history(tmp_path):
    """Active session (session_id set, supports_resume=True): persona + history
    MUST NOT be re-prepended. The backend's session memory already has them.
    Re-injection is what triggered the codex manager loop on 2026-04-27."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        active_persona="software_manager",
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="abc-123-def")

    captured = []
    bot.task_manager.submit_agent = lambda **kw: captured.append(kw)

    bot.conversation_log.append(
        ChatRef(transport_id="telegram", native_id="-100111", kind=ChatKind.ROOM),
        "user", "earlier turn", backend="fake",
    )

    incoming = _group_incoming(
        _group_chat(-100_111),
        "@acme_manager dispatch P1-1",
        sender_handle="acme_dev_bot", sender_is_bot=True, is_relayed=True,
    )
    await bot._submit_group_message_to_claude(incoming)

    assert len(captured) == 1
    prompt = captured[0]["prompt"]
    assert "[PERSONA:" not in prompt, (
        f"persona was re-injected on a resumed session (loop trigger): {prompt[:200]!r}"
    )
    assert "[Recent conversation history" not in prompt, (
        f"history was re-injected on a resumed session: {prompt[:200]!r}"
    )
    # The actual peer message is still what gets submitted.
    assert "dispatch P1-1" in prompt


@pytest.mark.asyncio
async def test_team_resumed_turn_human_path_omits_persona_and_history(tmp_path):
    """Same gate must apply to human-in-group messages (_on_text path), since
    they hit the same agent and the same loop risk."""
    from link_project_to_chat.bot import ProjectBot
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        active_persona="software_manager",
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="zzz-999")
    # Bypass auth + rate limit gates for this prompt-building test.
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()
    bot._rate_limited = MagicMock(return_value=False)

    captured = []
    bot.task_manager.submit_agent = lambda **kw: captured.append(kw)

    bot.conversation_log.append(
        ChatRef(transport_id="telegram", native_id="-100111", kind=ChatKind.ROOM),
        "user", "earlier turn", backend="fake",
    )

    incoming = _group_incoming(_group_chat(-100_111), "@acme_manager status?")
    await bot._on_text(incoming)

    assert len(captured) == 1
    prompt = captured[0]["prompt"]
    assert "[PERSONA:" not in prompt
    assert "[Recent conversation history" not in prompt
    assert "status?" in prompt


@pytest.mark.asyncio
async def test_team_mode_persona_change_clears_backend_session(tmp_path):
    """Step 2's gate hides persona+history when a session is active. So a
    `/persona` swap mid-session would otherwise be invisible to the agent
    until /reset. Clearing session_id on persona change forces the next
    turn to be fresh, re-injecting the new persona."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport import CommandInvocation
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        active_persona="software_manager",
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="session-before-swap")
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()

    chat = _group_chat(-100_111)
    ci = CommandInvocation(
        chat=chat, sender=_sender_identity(uid=1, handle="rezo", is_bot=False),
        name="persona", args=["software_dev"],
        raw_text="/persona software_dev",
        message=MessageRef(transport_id="telegram", native_id="100", chat=chat),
    )
    await bot._on_persona(ci)

    assert bot._active_persona == "software_dev"
    assert bot.task_manager.backend.session_id is None, (
        "session_id must be cleared on persona change in team mode so the "
        "Step-2 gate opens and the new persona injects on the next turn"
    )


@pytest.mark.asyncio
async def test_team_mode_stop_persona_clears_backend_session(tmp_path):
    """Same gate-driven reason for /stop_persona — without a fresh session,
    the resumed agent keeps acting under the old persona until /reset."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport import CommandInvocation
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        active_persona="software_manager",
    )
    bot.bot_username = "acme_manager"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="session-before-stop")
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()

    chat = _group_chat(-100_111)
    ci = CommandInvocation(
        chat=chat, sender=_sender_identity(uid=1, handle="rezo", is_bot=False),
        name="stop_persona", args=[],
        raw_text="/stop_persona",
        message=MessageRef(transport_id="telegram", native_id="100", chat=chat),
    )
    await bot._on_stop_persona(ci)

    assert bot._active_persona is None
    assert bot.task_manager.backend.session_id is None


@pytest.mark.asyncio
async def test_solo_mode_persona_change_preserves_backend_session(tmp_path):
    """Outside team mode, the per-turn injection gate is OFF — personas
    re-inject every turn anyway. Don't disrupt session continuity for
    solo users who change persona mid-conversation."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport import CommandInvocation
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(
        name="solo", path=tmp_path, token="t",
        active_persona="software_manager",
    )
    assert bot.group_mode is False
    bot.bot_username = "solo"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="solo-session-keep")
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()

    chat = ChatRef(transport_id="telegram", native_id="55", kind=ChatKind.DM)
    ci = CommandInvocation(
        chat=chat, sender=_sender_identity(uid=1, handle="rezo", is_bot=False),
        name="persona", args=["software_dev"],
        raw_text="/persona software_dev",
        message=MessageRef(transport_id="telegram", native_id="100", chat=chat),
    )
    await bot._on_persona(ci)

    assert bot._active_persona == "software_dev"
    assert bot.task_manager.backend.session_id == "solo-session-keep", (
        "solo mode must not alter backend session continuity on persona change"
    )


@pytest.mark.asyncio
async def test_solo_mode_resumed_session_still_injects_persona_and_history(tmp_path):
    """Outside team mode, leave existing behavior alone. Solo bots don't have
    the relay-loop failure mode, and changing solo persona injection would
    silently break /persona-mid-conversation flows users rely on today."""
    from link_project_to_chat.bot import ProjectBot
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(
        name="solo", path=tmp_path, token="t",
        active_persona="software_dev",
    )
    assert bot.group_mode is False
    bot.bot_username = "solo"
    _team_bot_with_fake_transport(bot)
    bot.task_manager._backend = _make_fake_backend(session_id="solo-session")
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()
    bot._rate_limited = MagicMock(return_value=False)

    captured = []
    bot.task_manager.submit_agent = lambda **kw: captured.append(kw)

    chat = ChatRef(transport_id="telegram", native_id="55", kind=ChatKind.DM)
    bot.conversation_log.append(chat, "user", "previous turn", backend="fake")

    incoming = IncomingMessage(
        chat=chat,
        sender=_sender_identity(uid=1, handle="rezo", is_bot=False),
        text="ship it",
        files=[],
        reply_to=None,
        native=None,
        message=MessageRef(transport_id="telegram", native_id="42", chat=chat),
    )
    await bot._on_text(incoming)

    assert len(captured) == 1
    prompt = captured[0]["prompt"]
    # Solo mode preserves the legacy injection — persona + history both prepend.
    assert "[PERSONA: software_dev]" in prompt
    assert "[Recent conversation history" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# A2 — RoomBinding-aware comparison for non-Telegram transports
#
# The four `int(incoming.chat.native_id) != self.group_chat_id` call sites in
# bot.py (auto-capture + wrong-room ignore + /halt + /resume) crashed for any
# transport whose native_id was not int-parseable (Web UUIDs, Google Chat
# "spaces/..."). Spec #1 added RoomBinding(transport_id, native_id) to config.py
# for this; A2 closes the call-site rewrite.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_capture_on_non_telegram_writes_only_room(tmp_path, monkeypatch):
    """Auto-capture on a non-Telegram transport must write the RoomBinding
    shape without trying to mirror a legacy `group_chat_id` int (which would
    fail int() parse for "spaces/..."-style native_ids)."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager",
    )
    bot.bot_username = "acme_manager"
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()
    _team_bot_with_fake_transport(bot)

    captured = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.patch_team",
        lambda name, fields, *a, **k: captured.append((name, fields)),
    )

    chat = ChatRef(
        transport_id="google_chat",
        native_id="spaces/AAAA1234",
        kind=ChatKind.ROOM,
    )
    incoming = _group_incoming(chat, "@acme_manager hi", sender_handle="rezoc666")
    await bot._on_text_from_transport(incoming)

    # Only the new shape; no legacy mirror because transport_id != "telegram".
    assert captured == [(
        "acme",
        {"room": {"transport_id": "google_chat", "native_id": "spaces/AAAA1234"}},
    )]
    # Internal canonical state matches.
    assert bot._room is not None
    assert bot._room.transport_id == "google_chat"
    assert bot._room.native_id == "spaces/AAAA1234"
    # Legacy attribute remains None (not derivable from a non-int native_id).
    assert bot.group_chat_id is None


@pytest.mark.asyncio
async def test_same_native_id_different_transport_treated_as_wrong_room(tmp_path):
    """A bot bound to a Telegram chat with native_id "12345" must NOT accept a
    message from a Web chat whose native_id is also "12345". Native IDs alone
    are not unique across transports."""
    from link_project_to_chat.config import RoomBinding
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager",
        room=RoomBinding(transport_id="telegram", native_id="12345"),
    )
    bot.bot_username = "acme_manager"
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()

    # Same native_id, different transport — must be silently rejected.
    foreign = ChatRef(transport_id="web", native_id="12345", kind=ChatKind.ROOM)
    incoming = _group_incoming(foreign, "@acme_manager hi", sender_handle="rezoc666")
    await bot._on_text_from_transport(incoming)

    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()


@pytest.mark.asyncio
async def test_group_mode_rejects_wrong_room_with_non_int_native_id(tmp_path):
    """A bot bound to a non-Telegram room must silently ignore wrong-room
    messages without crashing on int() parse of a string native_id.

    Auth is bypassed so the only gate that can reject this message is the
    wrong-room guard — otherwise the test would pass for the wrong reason.
    """
    from link_project_to_chat.config import RoomBinding
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager",
        room=RoomBinding(transport_id="google_chat", native_id="spaces/RIGHT"),
    )
    bot.bot_username = "acme_manager"
    bot._auth_identity = MagicMock(return_value=True)
    bot._require_executor = MagicMock(return_value=True)
    bot._persist_auth_if_dirty = AsyncMock()  # bypass auth
    _team_bot_with_fake_transport(bot)
    bot.task_manager.submit_agent = MagicMock()

    wrong = ChatRef(
        transport_id="google_chat",
        native_id="spaces/WRONG",
        kind=ChatKind.ROOM,
    )
    incoming = _group_incoming(wrong, "@acme_manager hi", sender_handle="rezoc666")

    # Must not raise ValueError from int("spaces/WRONG").
    await bot._on_text_from_transport(incoming)

    # Wrong-room guard correctly rejected — no submit, no transport sends.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()


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
    note = bot.task_manager.backend.team_system_note
    assert note is not None
    assert "acme_dev_claude_bot" in note
    assert "manager" in note  # self role
    assert "developer" in note  # peer role label


def test_codex_team_bot_with_peer_username_sets_team_system_note(tmp_path):
    """Codex team bots should get the same relay-routing note as Claude bots."""
    from link_project_to_chat.backends import codex as _codex  # noqa: F401
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="acme_dev_codex_bot",
        backend_name="codex",
        backend_state={"codex": {}},
    )

    note = bot.task_manager.backend.team_system_note
    assert note is not None
    assert "acme_dev_codex_bot" in note
    assert "BEGIN the reply with @acme_dev_codex_bot" in note


@pytest.mark.asyncio
async def test_switching_team_bot_to_codex_refreshes_team_system_note(tmp_path):
    """A runtime backend switch should populate the new backend with team context."""
    from link_project_to_chat.backends import codex as _codex  # noqa: F401
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="acme_dev_codex_bot",
        config_path=tmp_path / "config.json",
    )
    assert bot.task_manager.backend.name == "claude"

    await bot._switch_backend("codex")

    assert bot.task_manager.backend.name == "codex"
    note = bot.task_manager.backend.team_system_note
    assert note is not None
    assert "acme_dev_codex_bot" in note


def test_team_system_note_discourages_ack_echoing(tmp_path):
    """The note must tell the bot not to echo acknowledgments — the ping-pong cause.

    If this regresses, teams will loop on 'ok'/'agreed'/'standing by' forever.
    """
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="acme_dev_bot",
    )
    note = bot.task_manager.backend.team_system_note or ""
    lowered = note.lower()
    # Mentions that acks shouldn't be echoed, or that silence is a valid reply.
    assert (
        "acknowledg" in lowered
        or "ack-only" in lowered
        or "silence" in lowered
        or "don't reply" in lowered
        or "do not reply" in lowered
    ), f"system note does not discourage ack-echoing:\n{note}"


def test_team_system_note_no_longer_forces_every_reply_to_mention_peer(tmp_path):
    """The old 'EVERY reply must begin with @peer' rule is what created ping-pong loops.

    It has been relaxed so the bot can reply to the user without pinging the peer.
    """
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="acme_dev_bot",
    )
    note = bot.task_manager.backend.team_system_note or ""
    # The forbidden phrases from the old prompt must no longer appear.
    assert "Every single reply" not in note
    assert "Never send a reply without this @mention" not in note


def test_team_bot_without_peer_username_leaves_note_unset(tmp_path):
    """Missing peer @handle should leave team_system_note as None (no stale placeholder)."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_manager", path=tmp_path, token="t",
        team_name="acme", role="manager", group_chat_id=-100_111,
        peer_bot_username="",
    )
    assert bot.task_manager.backend.team_system_note is None


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
    note_init = bot.task_manager.backend.team_system_note
    assert note_init is not None
    assert "@acme_mgr_bot" in note_init
    assert "@acme_dev_2_bot" not in note_init  # self handle not known yet

    # Simulate _post_init after get_me() returned our real handle.
    bot.bot_username = "acme_dev_2_bot"
    bot._refresh_team_system_note()

    note_post = bot.task_manager.backend.team_system_note
    assert note_post is not None
    assert "@acme_dev_2_bot" in note_post  # self handle pinned
    assert "@acme_mgr_bot" in note_post    # peer handle still there


def test_refresh_team_system_note_preserves_team_authority(tmp_path):
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot(
        name="acme_dev", path=tmp_path, token="t",
        team_name="acme", role="dev", group_chat_id=-100_111,
        peer_bot_username="acme_mgr_bot",
    )
    authority = bot.task_manager.backend.team_authority
    assert authority is not None

    bot.bot_username = "acme_dev_2_bot"
    bot._refresh_team_system_note()

    assert bot.task_manager.backend.team_authority is authority
    assert bot._team_authority is authority


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


# -----------------------------------------------------------------------------
# Spec #0c Task 5: build() wires enable_team_relay from LP2C_TELETHON_SESSION.
# -----------------------------------------------------------------------------
def _make_team_bot_for_relay_test(tmp_path) -> ProjectBot:
    """Construct a team-mode ProjectBot with minimum kwargs for build() tests.

    Uses the AllowedUser model (post-v1.0 auth). The locked telegram identity
    is what the team-relay safety code reads as `authenticated_user_id`.
    """
    from link_project_to_chat.config import AllowedUser
    return ProjectBot(
        name="acme_dev",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="dev",
        group_chat_id=-100123,
        peer_bot_username="acme_manager_bot",
        allowed_users=[
            AllowedUser(
                username="rezoc",
                role="executor",
                locked_identities=["telegram:42"],
            )
        ],
    )


def _make_solo_bot_for_relay_test(tmp_path) -> ProjectBot:
    """Construct a solo-mode ProjectBot with no team_name."""
    return ProjectBot(name="solo", path=tmp_path, token="t")


def _stub_team_config_with_two_bots():
    """Return a dict matching ``load_teams`` output with two bot usernames."""
    from link_project_to_chat.config import TeamBotConfig, TeamConfig

    return {
        "acme": TeamConfig(
            path="/tmp/acme",
            group_chat_id=-100123,
            max_autonomous_turns=7,
            bots={
                "manager": TeamBotConfig(
                    telegram_bot_token="mt",
                    bot_username="acme_manager_bot",
                ),
                "dev": TeamBotConfig(
                    telegram_bot_token="dt",
                    bot_username="acme_dev_bot",
                ),
            },
        )
    }


def _stub_config_with_api_creds():
    """Return a minimal Config with telegram_api_id/telegram_api_hash set."""
    from link_project_to_chat.config import Config

    cfg = Config()
    cfg.telegram_api_id = 12345
    cfg.telegram_api_hash = "fakehash"
    return cfg


def test_team_mode_bot_calls_enable_team_relay_when_session_env_set(tmp_path, monkeypatch):
    """When LP2C_TELETHON_SESSION is set (and LP2C_TELETHON_SESSION_STRING is
    absent) and the bot is team-mode, build() delegates to
    TelegramTransport.enable_team_relay_from_session.

    Telethon client construction lives inside TelegramTransport (see
    test_enable_team_relay_from_session_builds_client in test_telegram_transport);
    here we just assert the bot hands the session credentials over.

    Hermeticity: production code prefers LP2C_TELETHON_SESSION_STRING when
    present (see bot.py:_after_ready), so this test must clear it before
    exercising the file-path fallback. Otherwise the test fails in any
    runtime where the string-session var is set globally (e.g., the team
    relay's own session).
    """
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.delenv("LP2C_TELETHON_SESSION_STRING", raising=False)
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    bot = _make_team_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session.assert_called_once()
    mock_transport.enable_team_relay_from_session_string.assert_not_called()
    call_kwargs = mock_transport.enable_team_relay_from_session.call_args.kwargs
    assert call_kwargs["session_path"] == str(session_path)
    assert call_kwargs["api_id"] == 12345
    assert call_kwargs["api_hash"] == "fakehash"
    assert call_kwargs["group_chat_id"] == -100123
    assert call_kwargs["team_name"] == "acme"
    assert call_kwargs["max_autonomous_turns"] == 7
    assert call_kwargs["team_authority"] is bot.task_manager.backend.team_authority
    assert call_kwargs["authenticated_user_id"] == "42"
    usernames = call_kwargs["team_bot_usernames"]
    assert "acme_manager_bot" in usernames
    assert "acme_dev_bot" in usernames


def test_team_mode_bot_prefers_session_string_when_both_env_vars_set(
    tmp_path, monkeypatch
):
    """When BOTH LP2C_TELETHON_SESSION_STRING and LP2C_TELETHON_SESSION are
    set, the string-session path wins and the file-path fallback is not
    called.

    Production behavior (bot.py:_after_ready): the string session is the
    preferred path because it avoids the on-disk SQLite session-file lock
    contention that hits when both the manager and the project bot try to
    open the same telethon.session file. This regression test makes the
    precedence explicit so a future refactor can't silently flip it.
    """
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION_STRING", "sentinel-string-session")
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    bot = _make_team_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session_string.assert_called_once()
    mock_transport.enable_team_relay_from_session.assert_not_called()
    call_kwargs = mock_transport.enable_team_relay_from_session_string.call_args.kwargs
    assert call_kwargs["session_string"] == "sentinel-string-session"
    assert call_kwargs["api_id"] == 12345
    assert call_kwargs["api_hash"] == "fakehash"
    assert call_kwargs["group_chat_id"] == -100123
    assert call_kwargs["team_name"] == "acme"
    usernames = call_kwargs["team_bot_usernames"]
    assert "acme_manager_bot" in usernames
    assert "acme_dev_bot" in usernames


def test_project_bot_build_registers_after_ready_callback(tmp_path):
    """build() must register the bot's _after_ready callback on the transport.

    Note: post_init/post_stop wiring used to live here, but moved to
    TelegramTransport.run() (Task 6 / I4). bot.py no longer touches the
    Application by name; see test_bot_run_delegates_to_transport_run below
    and tests/transport/test_telegram_transport.py for the wiring.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    app = SimpleNamespace()
    mock_transport = MagicMock()
    mock_transport.app = app

    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ):
        bot.build()

    mock_transport.on_ready.assert_called_once_with(bot._after_ready)


def test_bot_run_delegates_to_transport_run(tmp_path):
    """ProjectBot.run() must call self._transport.run() — the Transport owns
    the polling loop lifecycle (PTB run_polling, websocket loop, HTTP server)."""
    from unittest.mock import MagicMock, AsyncMock

    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    mock_transport = MagicMock()
    bot._transport = mock_transport

    bot.run()

    mock_transport.run.assert_called_once_with()


@pytest.mark.asyncio
async def test_telegram_text_dispatch_preserves_context_and_submits_agent(tmp_path):
    """Normal Telegram text should survive the transport dispatch path into TaskManager."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from link_project_to_chat.transport.telegram import TelegramTransport

    app = MagicMock()
    app.bot = MagicMock()
    transport = TelegramTransport(app)
    bot = ProjectBot(
        name="solo",
        path=tmp_path,
        token="t",
        allowed_usernames=["alice"],
        context_enabled=False,
    )
    bot._transport = transport
    bot.task_manager.submit_agent = MagicMock()
    transport.on_message(bot._on_text_from_transport)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="hello from telegram",
        photo=None,
        document=None,
        voice=None,
        audio=None,
        caption=None,
        reply_to_message=None,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)
    ctx = SimpleNamespace(user_data={})

    await transport._dispatch_message(update, ctx)

    from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
    expected_chat = ChatRef(transport_id="telegram", native_id="12345", kind=ChatKind.DM)
    bot.task_manager.submit_agent.assert_called_once_with(
        chat=expected_chat,
        message=MessageRef(transport_id="telegram", native_id="100", chat=expected_chat),
        prompt="hello from telegram",
    )


def test_team_mode_bot_uses_string_session_env_when_set(tmp_path, monkeypatch):
    """Spec D′: when LP2C_TELETHON_SESSION_STRING is set, build() calls
    enable_team_relay_from_session_string and ignores the path-mode fallback.

    Each subprocess builds an in-memory StringSession instead of opening the
    shared telethon.session file — eliminates the ``database is locked`` race.
    """
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("LP2C_TELETHON_SESSION_STRING", "1$encoded-session")
    # Path env may also be present (back-compat); string must win.
    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    bot = _make_team_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session_string.assert_called_once()
    mock_transport.enable_team_relay_from_session.assert_not_called()
    call_kwargs = mock_transport.enable_team_relay_from_session_string.call_args.kwargs
    assert call_kwargs["session_string"] == "1$encoded-session"
    assert call_kwargs["api_id"] == 12345
    assert call_kwargs["api_hash"] == "fakehash"
    assert call_kwargs["group_chat_id"] == -100123
    assert call_kwargs["team_name"] == "acme"
    assert call_kwargs["max_autonomous_turns"] == 7
    assert call_kwargs["team_authority"] is bot.task_manager.backend.team_authority
    assert call_kwargs["authenticated_user_id"] == "42"
    assert "acme_manager_bot" in call_kwargs["team_bot_usernames"]
    assert "acme_dev_bot" in call_kwargs["team_bot_usernames"]


def test_no_relay_when_string_and_path_env_both_unset(tmp_path, monkeypatch):
    """Neither env var → no relay, no surprise client construction."""
    from unittest.mock import MagicMock, patch

    monkeypatch.delenv("LP2C_TELETHON_SESSION_STRING", raising=False)
    monkeypatch.delenv("LP2C_TELETHON_SESSION", raising=False)

    bot = _make_team_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session_string.assert_not_called()
    mock_transport.enable_team_relay_from_session.assert_not_called()


def test_no_relay_when_session_env_unset(tmp_path, monkeypatch):
    """Without LP2C_TELETHON_SESSION, build() does NOT call enable_team_relay_from_session."""
    from unittest.mock import MagicMock, patch

    monkeypatch.delenv("LP2C_TELETHON_SESSION", raising=False)
    monkeypatch.delenv("LP2C_TELETHON_SESSION_STRING", raising=False)

    bot = _make_team_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session.assert_not_called()


def test_no_relay_when_solo_mode(tmp_path, monkeypatch):
    """A solo-mode bot (no team_name) does NOT call enable_team_relay_from_session even if env set."""
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    bot = _make_solo_bot_for_relay_test(tmp_path)

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session.assert_not_called()


def test_no_relay_when_team_missing_from_config(tmp_path, monkeypatch):
    """Defensive: if team_name is set but load_teams() doesn't have an entry,
    enable_team_relay_from_session is NOT called (logged as warning instead of silent no-op)."""
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    # team_name="missing_team" is intentionally not in the load_teams() stub.
    bot = ProjectBot(
        name="missing_team_dev",
        path=tmp_path,
        token="t",
        team_name="missing_team",
        role="dev",
        group_chat_id=-100123,
        peer_bot_username="missing_team_manager_bot",
    )

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value={},  # no entry for "missing_team"
    ), patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ):
        bot.build()

    mock_transport.enable_team_relay_from_session.assert_not_called()


def test_persist_active_persona_team_bot_uses_instance_config_path_by_default(tmp_path):
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, load_teams, save_config

    cfg_path = tmp_path / "custom-config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="old_mgr"),
                        "dev": TeamBotConfig(telegram_bot_token="t2", active_persona="old_dev"),
                    },
                ),
            }
        ),
        cfg_path,
    )

    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-100_111,
        config_path=cfg_path,
    )
    bot._persist_active_persona("software_manager")

    teams = load_teams(cfg_path)
    assert teams["acme"].bots["manager"].active_persona == "software_manager"
    assert teams["acme"].bots["dev"].active_persona == "old_dev"


@pytest.mark.asyncio
async def test_after_ready_backfills_team_username_into_instance_config(tmp_path):
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, load_teams, save_config

    cfg_path = tmp_path / "custom-config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1"),
                        "dev": TeamBotConfig(telegram_bot_token="t2", bot_username="acme_dev_bot"),
                    },
                ),
            }
        ),
        cfg_path,
    )

    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-100_111,
        config_path=cfg_path,
    )
    bot._transport = FakeTransport()

    await bot._after_ready(
        Identity(
            transport_id="fake",
            native_id="1",
            display_name="acme_manager_bot",
            handle="acme_manager_bot",
            is_bot=True,
        )
    )

    teams = load_teams(cfg_path)
    assert teams["acme"].bots["manager"].bot_username == "acme_manager_bot"
    assert teams["acme"].bots["dev"].bot_username == "acme_dev_bot"


@pytest.mark.asyncio
async def test_after_ready_team_bot_skips_startup_dm_ping(tmp_path):
    """Team bots have no DM with trusted users; the startup ping must skip
    them to avoid Forbidden / Chat-not-found stack traces on every restart."""
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "custom-config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
                ),
            }
        ),
        cfg_path,
    )

    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-100_111,
        config_path=cfg_path,
        allowed_users=[
            AllowedUser(username="admin", role="executor", locked_identities=["fake:8206818037"]),
        ],
    )
    bot._transport = FakeTransport()

    await bot._after_ready(
        Identity(
            transport_id="fake",
            native_id="1",
            display_name="acme_manager_bot",
            handle="acme_manager_bot",
            is_bot=True,
        )
    )

    assert not any(
        "Bot started" in m.text for m in bot._transport.sent_messages
    )


@pytest.mark.asyncio
async def test_after_ready_solo_bot_sends_startup_dm_ping(tmp_path):
    """Solo (non-team) bots must still send the startup ping to trusted users."""
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="solo", path=tmp_path, token="t",
        allowed_users=[
            AllowedUser(username="admin", role="executor", locked_identities=["fake:8206818037"]),
        ],
    )
    bot._transport = FakeTransport()

    await bot._after_ready(
        Identity(
            transport_id="fake",
            native_id="1",
            display_name="solo_bot",
            handle="solo_bot",
            is_bot=True,
        )
    )

    pings = [m for m in bot._transport.sent_messages if "Bot started" in m.text]
    assert len(pings) == 1
    assert pings[0].chat.native_id == "8206818037"


@pytest.mark.asyncio
async def test_after_ready_telegram_skips_non_numeric_startup_identity(tmp_path, caplog):
    """Already-saved bad legacy locks like telegram:browser_user must not make
    Telegram startup ping attempt an invalid chat_id conversion.
    """
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="solo", path=tmp_path, token="t",
        allowed_users=[
            AllowedUser(
                username="admin",
                role="executor",
                locked_identities=[
                    "telegram:browser_user",
                    "telegram:8206818037",
                    "web:browser_user",
                ],
            ),
        ],
    )
    bot._transport = _TelegramLikeFakeTransport()

    with caplog.at_level("ERROR", logger="link_project_to_chat.bot"):
        await bot._after_ready(
            Identity(
                transport_id="telegram",
                native_id="1",
                display_name="solo_bot",
                handle="solo_bot",
                is_bot=True,
            )
        )

    pings = [m for m in bot._transport.sent_messages if "Bot started" in m.text]
    assert [m.chat.native_id for m in pings] == ["8206818037"]
    assert "Failed to send startup message to browser_user" not in caplog.text


@pytest.mark.asyncio
async def test_after_ready_telegram_startup_ping_chat_not_found_is_warning(tmp_path, caplog):
    """A valid Telegram user id still may not be reachable by a newly created
    bot until the user opens that bot. Startup ping should be best-effort and
    avoid an error traceback for this expected platform response.
    """
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot(
        name="solo", path=tmp_path, token="t",
        allowed_users=[
            AllowedUser(
                username="admin",
                role="executor",
                locked_identities=["telegram:8206818037"],
            ),
        ],
    )
    bot._transport = _TelegramChatNotFoundTransport()

    with caplog.at_level("WARNING", logger="link_project_to_chat.bot"):
        await bot._after_ready(
            Identity(
                transport_id="telegram",
                native_id="1",
                display_name="solo_bot",
                handle="solo_bot",
                is_bot=True,
            )
        )

    assert "Startup message not delivered to 8206818037" in caplog.text
    assert "Failed to send startup message to 8206818037" not in caplog.text


@pytest.mark.asyncio
async def test_on_task_complete_team_bot_persists_session_in_team_config(tmp_path):
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, load_config, save_config
    from link_project_to_chat.task_manager import Task, TaskStatus, TaskType

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "acme": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1"),
                        "dev": TeamBotConfig(telegram_bot_token="t2"),
                    },
                )
            }
        ),
        cfg_path,
    )

    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t1",
        team_name="acme",
        role="manager",
        group_chat_id=-100_111,
        config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    bot.task_manager.backend.session_id = "sess-123"

    async def fake_finalize(_task):
        pass

    bot._finalize_claude_task = fake_finalize

    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    task = Task(
        id=1,
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
        type=TaskType.AGENT,
        input="hello",
        name="hello",
        status=TaskStatus.DONE,
    )

    await bot._on_task_complete(task)

    cfg = load_config(cfg_path)
    assert cfg.teams["acme"].bots["manager"].session_id == "sess-123"
    assert "acme_manager" not in cfg.projects


@pytest.mark.asyncio
async def test_reset_confirm_clears_team_session_from_team_config(tmp_path):
    import json

    from link_project_to_chat.transport import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -100_111,
                        "bots": {
                            "manager": {
                                "telegram_bot_token": "t1",
                                "session_id": "sess-123",
                            },
                            "dev": {
                                "telegram_bot_token": "t2",
                            },
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t1",
        team_name="acme",
        role="manager",
        group_chat_id=-100_111,
        config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    bot._auth_identity = lambda _sender: True
    bot._require_executor = lambda _sender: True
    bot.task_manager.backend.session_id = "sess-123"
    bot.task_manager.cancel_all = lambda: 0

    chat = ChatRef(transport_id="fake", native_id="-100111", kind=ChatKind.ROOM)
    msg = MessageRef(transport_id="fake", native_id="7", chat=chat)
    sender = Identity(
        transport_id="fake",
        native_id="42",
        display_name="Rezo",
        handle="rezo",
        is_bot=False,
    )
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="reset_confirm")

    await bot._on_button(click)

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "session_id" not in raw["teams"]["acme"]["bots"]["manager"]


def test_team_mode_bot_build_uses_instance_config_path_for_relay_bootstrap(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    session_path = tmp_path / "telethon.session"
    session_path.touch()
    monkeypatch.setenv("LP2C_TELETHON_SESSION", str(session_path))

    cfg_path = tmp_path / "custom-config.json"
    bot = ProjectBot(
        name="acme_dev",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="dev",
        group_chat_id=-100123,
        peer_bot_username="acme_manager_bot",
        config_path=cfg_path,
    )

    mock_transport = MagicMock()
    with patch(
        "link_project_to_chat.transport.telegram.TelegramTransport.build",
        return_value=mock_transport,
    ), patch(
        "link_project_to_chat.bot.load_teams",
        return_value=_stub_team_config_with_two_bots(),
    ) as mock_load_teams, patch(
        "link_project_to_chat.bot.load_config",
        return_value=_stub_config_with_api_creds(),
    ) as mock_load_config:
        bot.build()

    mock_load_config.assert_called_once_with(cfg_path)
    mock_load_teams.assert_called_once_with(cfg_path)
