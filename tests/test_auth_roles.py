"""Role-based auth tests (Task 5).

Covers the rewritten AuthMixin around `AllowedUser` as the sole source of
truth, identity locking via `locked_identities`, the same-transport spoof
guard, multi-transport user onboarding, `_persist_auth_if_dirty`'s
scope-aware persistence, and the gated state-changing buttons.
"""
from __future__ import annotations

import pytest

from link_project_to_chat._auth import AuthMixin
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import Identity


class _BotWithRoles(AuthMixin):
    """Minimal AuthMixin host for tests — only the new auth surface."""

    def __init__(self, allowed_users=None):
        self._allowed_users: list[AllowedUser] = list(allowed_users or [])
        self._init_auth()


def _identity(username: str, native_id: str = "1") -> Identity:
    return Identity(
        transport_id="telegram",
        native_id=native_id,
        display_name=username,
        handle=username,
        is_bot=False,
    )


def test_get_user_role_returns_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(_identity("alice")) == "executor"


def test_get_user_role_returns_viewer():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="bob", role="viewer")])
    assert bot._get_user_role(_identity("bob")) == "viewer"


def test_get_user_role_none_when_not_listed():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._get_user_role(_identity("bob")) is None


def test_empty_allowed_users_fails_closed():
    """Empty allowed_users denies everyone — no legacy 'allow-all' path."""
    bot = _BotWithRoles(allowed_users=[])
    assert bot._auth_identity(_identity("alice")) is False
    assert bot._require_executor(_identity("alice")) is False


def test_require_executor_blocks_viewer():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="bob", role="viewer")])
    assert bot._require_executor(_identity("bob")) is False


def test_require_executor_allows_executor():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(_identity("alice")) is True


def test_require_executor_blocks_unknown_user():
    bot = _BotWithRoles(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._require_executor(_identity("charlie")) is False


def test_require_executor_case_and_at_insensitive():
    bot = _BotWithRoles(allowed_users=[AllowedUser(username="alice", role="executor")])
    assert bot._require_executor(_identity("@ALICE")) is True


def test_first_contact_locks_identity():
    """First contact by username appends an identity to locked_identities."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    ident = _identity("alice", native_id="98765")
    bot._auth_identity(ident)  # First contact
    assert au.locked_identities == ["telegram:98765"]


def test_same_transport_spoof_blocked():
    """Same-transport spoof guard (security-critical).

    If a user already has any locked identity from a transport, an attacker
    who happens to know the username and lands on the same transport with
    a different native_id must NOT be able to bind their own identity via
    the username fallback. The fallback only runs when the user has zero
    locked identities from the incoming transport — once an identity is
    locked on telegram, every subsequent telegram contact has to match by
    identity_key (transport_id:native_id), never by handle.

    Without this guard, an attacker who renames themselves to "alice" on
    Telegram could authenticate themselves and overwrite the real alice's
    locked_identities. The earlier draft of _get_user_role had this hole;
    this test pins the fix.
    """
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Spoof attempt: same transport, different native_id, same username.
    attacker = _identity("alice", native_id="11111")  # transport_id="telegram"
    assert bot._auth_identity(attacker) is False
    # Alice's identity list was NOT mutated.
    assert au.locked_identities == ["telegram:12345"]
    assert bot._auth_dirty is False


def test_username_fallback_succeeds_for_genuinely_new_transport():
    """When the user has NO identity from the incoming transport, fallback
    succeeds and appends. That's the multi-transport onboarding case."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Different transport: web. transport_prefix "web:" not present in
    # locked_identities, so the fallback applies and appends.
    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    assert "telegram:12345" in au.locked_identities
    assert "web:web-session:abc-def" in au.locked_identities


def test_locked_identity_takes_precedence_over_username():
    """After identity is locked, validation goes through identity, not username."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:98765"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Attacker renames themselves to "alice" but their native_id is different
    # — same transport (telegram), different id, identity_key is "telegram:11111".
    attacker = _identity("alice", native_id="11111")
    assert bot._auth_identity(attacker) is False
    # The real alice still works — her identity matches even with a renamed
    # handle. (Her identity_key is "telegram:98765" regardless of handle.)
    ident_real = _identity("anything-else", native_id="98765")
    assert bot._auth_identity(ident_real) is True


# --- Step 4: _persist_auth_if_dirty (TDD) -----------------------------------


def test_auth_dirty_set_on_first_contact():
    """First contact by username appends to locked_identities AND sets _auth_dirty."""
    au = AllowedUser(username="alice", role="executor")
    bot = _BotWithRoles(allowed_users=[au])
    assert bot._auth_dirty is False
    bot._auth_identity(_identity("alice", native_id="98765"))
    # _identity helper uses transport_id="telegram"; the identity_key is "telegram:98765".
    assert au.locked_identities == ["telegram:98765"]
    assert bot._auth_dirty is True


def test_auth_dirty_unset_after_persist_call():
    """_persist_auth_if_dirty clears the flag and is idempotent on a clean state."""
    saves: list[int] = []

    class _Bot(AuthMixin):
        def __init__(self):
            self._allowed_users = [AllowedUser(username="alice", role="executor")]
            self._init_auth()

        def _save_config_for_auth(self):
            saves.append(1)

        async def _persist_auth_if_dirty(self):
            if self._auth_dirty:
                self._save_config_for_auth()
                self._auth_dirty = False

    import asyncio
    bot = _Bot()
    bot._auth_identity(_identity("alice", native_id="98765"))
    assert bot._auth_dirty is True
    asyncio.run(bot._persist_auth_if_dirty())
    assert bot._auth_dirty is False
    assert saves == [1]
    # Second call is a no-op.
    asyncio.run(bot._persist_auth_if_dirty())
    assert saves == [1]


def test_locked_id_already_present_does_not_dirty():
    """When the current identity is already in locked_identities, no first-contact write happens."""
    au = AllowedUser(username="alice", role="executor", locked_identities=["telegram:98765"])
    bot = _BotWithRoles(allowed_users=[au])
    bot._auth_identity(_identity("alice", native_id="98765"))
    assert bot._auth_dirty is False


# --- Step 6b: plugin-consumed message still persists -----------------------


async def test_plugin_consumed_message_still_persists_first_contact_lock():
    """A plugin that consumes the message (returns True from on_message)
    short-circuits the role gate. The first-contact identity lock applied
    by the transport authorizer must still get persisted via the try/finally
    wrapping the top-level handler."""
    persists: list[int] = []

    class _BotWithPersistCount(AuthMixin):
        def __init__(self, allowed_users):
            self._allowed_users = list(allowed_users)
            self._init_auth()

        async def _persist_auth_if_dirty(self):
            if self._auth_dirty:
                persists.append(1)
                self._auth_dirty = False

        async def _with_auth_persist(self, awaitable):
            try:
                await awaitable
            finally:
                await self._persist_auth_if_dirty()

    bot = _BotWithPersistCount(
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )

    async def consuming_handler():
        # Simulate auth + plugin consume.
        bot._auth_identity(_identity("alice", native_id="98765"))
        # Plugin returned True → handler returns early without role gate.
        return

    await bot._with_auth_persist(consuming_handler())
    assert persists == [1]


# --- Step 7c: parametrized gate test on every state-changing button --------


# All state-changing button-value examples — one per prefix from Step 7b.
# Tests are parametrized over this list so a missed gate fails loudly with
# a specific param name.
STATE_CHANGING_BUTTON_VALUES = [
    "model_set_haiku",
    "effort_set_medium",
    "thinking_set_on",
    "permissions_set_default",
    "backend_set_codex",
    "reset_confirm",
    "reset_cancel",
    "task_cancel_42",
    "lang_set_en",
    "skill_scope_project_test-skill",
    "pick_skill_test-skill",
    "skill_delete_confirm_project_test-skill",
    "persona_scope_project_test-persona",
    "pick_persona_test-persona",
    "persona_delete_confirm_project_test-persona",
    "ask_42_0_0",  # AskUserQuestion answer — task 42, q 0, option 0
]


@pytest.fixture
def _viewer_bot(tmp_path):
    """Minimal ProjectBot wired enough to call _on_button(click).

    Constructed via __new__ to skip the heavy real __init__ (which would
    spin up backends and a TaskManager). We hand-set every attribute that
    _on_button + _guard_executor reads.
    """
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import AllowedUser

    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.path = Path("/tmp/p")
    bot._allowed_users = [
        AllowedUser(username="viewer-user", role="viewer", locked_identities=["telegram:42"]),
    ]
    bot._auth_source = "project"
    bot._init_auth()
    bot._plugins = []
    bot._plugin_command_handlers = {}
    bot._shared_ctx = None
    bot._transport = MagicMock()
    bot._transport.send_text = AsyncMock()
    bot._transport.edit_text = AsyncMock()
    bot._effective_config_path = lambda: tmp_path / "config.json"

    # Stub task_manager so any task_cancel_* branch doesn't AttributeError
    # before the gate fires. (Belt and suspenders — the gate should run
    # first, but if it doesn't, the test fails loudly instead of erroring.)
    bot.task_manager = MagicMock()
    bot.task_manager.cancel = MagicMock()
    bot.task_manager.find_by_id = MagicMock(return_value=None)
    return bot


@pytest.mark.parametrize("value", STATE_CHANGING_BUTTON_VALUES)
async def test_state_changing_button_blocked_for_viewer(_viewer_bot, value):
    """Every state-changing button prefix from Step 7b must reply
    'Read-only access' to a viewer and NOT call its mutation path.

    A new state-changing button prefix added later but not gated will fail
    this parametrized test with the missing-value param name in the report.
    """
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    viewer = Identity(transport_id="telegram", native_id="42", display_name="V", handle="viewer-user", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=viewer, value=value)

    await _viewer_bot._on_button(click)

    # The gate fires → send_text was awaited with the Read-only reply.
    assert _viewer_bot._transport.send_text.await_count >= 1, (
        f"No Read-only reply for state-changing button {value!r}; gate missing?"
    )
    last_text = _viewer_bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in last_text, (
        f"Reply for {value!r} was {last_text!r}, expected 'Read-only access' text"
    )
    # No mutation path ran.
    _viewer_bot.task_manager.cancel.assert_not_called()


async def test_state_changing_button_passes_for_executor(_viewer_bot):
    """Executors get past the gate — sanity check the gate isn't accidentally
    rejecting authorized users."""
    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.transport.base import ButtonClick, ChatKind, ChatRef, Identity, MessageRef

    # Swap the bot's role to executor for this test.
    _viewer_bot._allowed_users = [
        AllowedUser(username="exec-user", role="executor", locked_identities=["telegram:42"]),
    ]
    sender = Identity(transport_id="telegram", native_id="42", display_name="E", handle="exec-user", is_bot=False)
    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=sender, value="model_set_haiku")

    await _viewer_bot._on_button(click)

    # No Read-only reply was sent; the gate let the executor through.
    sends = _viewer_bot._transport.send_text.await_args_list
    for call in sends:
        text = call.args[1].lower()
        assert "read-only" not in text, f"Executor saw Read-only reply: {text!r}"


# --- Step 10a: scope-aware persistence -------------------------------------


async def test_persist_writes_to_global_when_auth_source_is_global(tmp_path):
    """When the bot inherited users from Config.allowed_users via fallback,
    first-contact locks are written to the GLOBAL allow-list — NOT cloned
    into the project's empty list."""
    import json
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import save_config, Config, ProjectConfig

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [AllowedUser(username="admin", role="executor")]
    cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(cfg, cfg_path)

    # Simulate a bot that inherited from global (auth_source="global").
    bot = _BotWithRoles(allowed_users=cfg.allowed_users)
    bot._auth_source = "global"
    bot._effective_config_path = lambda: cfg_path
    bot.name = "p"

    bot._auth_identity(_identity("admin", native_id="99"))
    assert bot._auth_dirty is True
    # Test is async — await directly. Calling asyncio.run() inside an
    # already-running event loop raises RuntimeError.
    await ProjectBot._persist_auth_if_dirty(bot)

    # Re-read disk. The GLOBAL allow-list must show the locked identity;
    # the project's allowed_users must remain empty (not promoted to a copy).
    disk = json.loads(cfg_path.read_text())
    assert disk["allowed_users"][0]["locked_identities"] == ["telegram:99"]
    assert disk["projects"]["p"].get("allowed_users", []) == []


async def test_persist_merges_per_user_not_replace(tmp_path):
    """Persisting changes for user A must not overwrite changes to user B
    made by another bot writing concurrently."""
    import json
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import save_config, Config, ProjectConfig

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor"),
    ]
    cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(cfg, cfg_path)

    # Simulate: another bot wrote bob's identity to disk while ours was running.
    disk_cfg = Config()
    disk_cfg.allowed_users = [
        AllowedUser(username="alice", role="executor"),
        AllowedUser(username="bob", role="executor", locked_identities=["telegram:200"]),
    ]
    disk_cfg.projects["p"] = ProjectConfig(path="/tmp/p", telegram_bot_token="t")
    save_config(disk_cfg, cfg_path)

    # Our bot in-memory only has the original lists. Alice locks her ID.
    bot = _BotWithRoles(allowed_users=cfg.allowed_users)
    bot._auth_source = "global"
    bot._effective_config_path = lambda: cfg_path
    bot.name = "p"
    bot._auth_identity(_identity("alice", native_id="100"))
    # Async test — await directly.
    await ProjectBot._persist_auth_if_dirty(bot)

    disk = json.loads(cfg_path.read_text())
    by_user = {u["username"]: u for u in disk["allowed_users"]}
    assert by_user["alice"]["locked_identities"] == ["telegram:100"]
    assert by_user["bob"]["locked_identities"] == ["telegram:200"]  # NOT clobbered


# --- Step 10b: multi-transport identity ------------------------------------


def test_multi_transport_user_auths_from_both(tmp_path):
    """A user with locked_identities=["telegram:X", "web:web-session:Y"]
    auths successfully from EITHER transport."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345", "web:web-session:abc-def"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    # Telegram side.
    tg_ident = Identity(
        transport_id="telegram", native_id="12345",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(tg_ident) is True
    # Web side.
    web_ident = Identity(
        transport_id="web", native_id="web-session:abc-def",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True


def test_username_fallback_appends_to_identities_per_transport(tmp_path):
    """A user with one telegram lock who messages from web for the first
    time gets the web identity APPENDED — telegram lock is preserved."""
    au = AllowedUser(
        username="alice",
        role="executor",
        locked_identities=["telegram:12345"],
    )
    bot = _BotWithRoles(allowed_users=[au])
    web_ident = Identity(
        transport_id="web", native_id="web-session:new-session",
        display_name="Alice", handle="alice", is_bot=False,
    )
    assert bot._auth_identity(web_ident) is True
    assert au.locked_identities == ["telegram:12345", "web:web-session:new-session"]
    assert bot._auth_dirty is True
