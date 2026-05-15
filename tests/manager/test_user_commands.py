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


def _attach_running_pm(bot, running_keys: list[str]):
    """Attach a mock ProcessManager to `bot` reporting the given keys as
    running so user-mutation commands can record restart() calls.

    `running_keys` may include both project names and ``team:NAME:ROLE``
    keys — the helper uses _pm.list_running() (which covers both) and
    _pm.restart() (which dispatches by prefix).
    """
    pm = MagicMock()
    pm.list_running = MagicMock(return_value=list(running_keys))
    pm.restart = MagicMock(return_value=True)
    bot._pm = pm
    return pm


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
async def test_unauthorized_user_gets_reply(tmp_path):
    """Unauthorized callers must receive an 'Unauthorized.' reply, consistent
    with _guard_invocation. The original Task 6 silently returned, breaking
    UX symmetry with the rest of the manager bot."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor"),
    ])
    # Sender is NOT in the allow-list.
    inv = _invocation([], sender_handle="nobody", sender_id="666")
    await bot._on_users(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "Unauthorized" in text


@pytest.mark.asyncio
async def test_unauthorized_user_on_write_command_gets_reply(tmp_path):
    """Write commands also must reply 'Unauthorized.' rather than silently
    returning when the caller is not in the allow-list — symmetric with
    _guard_invocation."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor"),
    ])
    inv = _invocation(["charlie"], sender_handle="nobody", sender_id="666")
    await bot._on_add_user(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "Unauthorized" in text
    # And no write happened.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert not any(u.username == "charlie" for u in cfg.allowed_users)


@pytest.mark.asyncio
async def test_rate_limited_user_gets_reply(tmp_path):
    """Rate-limited callers must receive a 'Rate limited.' reply rather than
    silent acceptance. Verifies the rate-limit check is present in the new
    commands."""
    import collections
    import time

    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
    ])
    # Force rate-limit by maxing out the bucket for the admin identity.
    inv = _invocation([])
    key = bot._identity_key(inv.sender)
    bot._rate_limits[key] = collections.deque(
        [time.monotonic()] * bot._MAX_MESSAGES_PER_MINUTE
    )
    await bot._on_users(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "rate limit" in text.lower() or "try again" in text.lower()


@pytest.mark.asyncio
async def test_rate_limited_user_on_write_command_gets_reply(tmp_path):
    """Write commands (executor-gated) must also surface the rate-limit
    feedback rather than silently dropping the write."""
    import collections
    import time

    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
    ])
    inv = _invocation(["charlie"])
    key = bot._identity_key(inv.sender)
    bot._rate_limits[key] = collections.deque(
        [time.monotonic()] * bot._MAX_MESSAGES_PER_MINUTE
    )
    await bot._on_add_user(inv)
    text = bot._transport.send_text.await_args.args[1]
    assert "rate limit" in text.lower() or "try again" in text.lower()
    # And no write happened.
    from link_project_to_chat.config import load_config
    cfg = load_config(bot._project_config_path)
    assert not any(u.username == "charlie" for u in cfg.allowed_users)


def test_commands_list_includes_new_user_management_commands():
    """Task 6 added /promote_user, /demote_user, /reset_user_identity but
    those must also appear in COMMANDS so Telegram autocomplete shows them
    AND /help documents them. Without this entry, the commands work but
    are invisible to operators."""
    from link_project_to_chat.manager.bot import COMMANDS
    names = [c[0] for c in COMMANDS]
    assert "promote_user" in names
    assert "demote_user" in names
    assert "reset_user_identity" in names


# --- P1 #1: viewer cannot run state-changing manager commands ---

@pytest.mark.asyncio
async def test_viewer_cannot_start_all(tmp_path):
    """Regression for P1 #1: viewers must not be able to start every project
    via /start_all — _guard_invocation only enforced auth + rate-limit, so a
    viewer could trigger process spawn without any executor gate."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    bot._pm = MagicMock()
    bot._pm.start_all = MagicMock(return_value=99)
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_start_all_from_transport(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text
    # Critical: no actual start was attempted.
    bot._pm.start_all.assert_not_called()


@pytest.mark.asyncio
async def test_viewer_cannot_stop_all(tmp_path):
    """Regression for P1 #1: viewers must not be able to stop every project."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    bot._pm = MagicMock()
    bot._pm.stop_all = MagicMock(return_value=99)
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_stop_all_from_transport(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text
    bot._pm.stop_all.assert_not_called()


@pytest.mark.asyncio
async def test_viewer_cannot_open_setup(tmp_path):
    """Regression for P1 #1: viewers must not be able to even render the
    /setup keyboard — every button arms a setup_awaiting state that ends in
    a credential write."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_setup_from_transport(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_open_global_model_picker(tmp_path):
    """Regression for P1 #1: viewers must not be able to render the
    global-default-model picker — clicking any option writes
    default_model_claude."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="viewer-bob", role="viewer", locked_identities=["telegram:200"]),
    ])
    inv = _invocation([], sender_handle="viewer-bob", sender_id="200")
    await bot._on_model_from_transport(inv)
    text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_executor_can_still_run_start_all(tmp_path):
    """Sanity: the executor path remains functional after gating."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:12345"]),
    ])
    bot._pm = MagicMock()
    bot._pm.start_all = MagicMock(return_value=3)
    inv = _invocation([], sender_handle="alice", sender_id="12345")
    await bot._on_start_all_from_transport(inv)
    bot._pm.start_all.assert_called_once()
    last_text = bot._transport.send_text.await_args.args[1].lower()
    assert "read-only" not in last_text


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


# --- Restart-on-mutation regressions ---------------------------------------
# Without these, a running project bot keeps its startup _allowed_users
# snapshot — so /demote_user / /remove_user / /reset_user_identity are
# ineffective until the operator manually restarts the bot.


@pytest.mark.asyncio
async def test_add_user_restarts_running_project_bots(tmp_path):
    bot = _make_manager(tmp_path)
    pm = _attach_running_pm(bot, ["proj_a", "proj_b"])
    await bot._on_add_user(_invocation(["charlie"]))
    pm.restart.assert_any_call("proj_a")
    pm.restart.assert_any_call("proj_b")


@pytest.mark.asyncio
async def test_remove_user_restarts_running_project_bots(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor"),
    ])
    pm = _attach_running_pm(bot, ["proj_a"])
    await bot._on_remove_user(_invocation(["alice"]))
    pm.restart.assert_called_once_with("proj_a")


@pytest.mark.asyncio
async def test_promote_user_restarts_running_project_bots(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="viewer"),
    ])
    pm = _attach_running_pm(bot, ["proj_a"])
    await bot._on_promote_user(_invocation(["alice"]))
    pm.restart.assert_called_once_with("proj_a")


@pytest.mark.asyncio
async def test_demote_user_restarts_running_project_bots(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor"),
    ])
    pm = _attach_running_pm(bot, ["proj_a"])
    await bot._on_demote_user(_invocation(["alice"]))
    pm.restart.assert_called_once_with("proj_a")


@pytest.mark.asyncio
async def test_reset_user_identity_restarts_running_project_bots(tmp_path):
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:9", "web:abc"]),
    ])
    pm = _attach_running_pm(bot, ["proj_a"])
    await bot._on_reset_user_identity(_invocation(["alice"]))
    pm.restart.assert_called_once_with("proj_a")


@pytest.mark.asyncio
async def test_user_mutation_restarts_running_team_bot(tmp_path):
    """Regression for the round-2 gap: list_all() only returned project names,
    so user mutations missed running team:NAME:ROLE keys. The helper now uses
    list_running() (which surfaces team keys) + restart() (which dispatches
    them through start_team), so demoting/removing/resetting a user reaches
    the running team bot too."""
    bot = _make_manager(tmp_path, [
        AllowedUser(username="admin", role="executor", locked_identities=["telegram:1"]),
        AllowedUser(username="alice", role="executor"),
    ])
    pm = _attach_running_pm(bot, ["proj_a", "team:demo:dev"])
    await bot._on_demote_user(_invocation(["alice"]))
    pm.restart.assert_any_call("proj_a")
    pm.restart.assert_any_call("team:demo:dev")


@pytest.mark.asyncio
async def test_user_mutation_skips_restart_when_no_bots_running(tmp_path):
    """If nothing is running there's nothing to restart — and we must NOT
    spawn a stopped bot just because a user mutation happened."""
    bot = _make_manager(tmp_path)
    pm = _attach_running_pm(bot, [])  # nothing running
    await bot._on_add_user(_invocation(["charlie"]))
    pm.restart.assert_not_called()


@pytest.mark.asyncio
async def test_user_mutation_no_op_does_not_restart(tmp_path):
    """A failed-validation path (invalid role) must NOT trigger a restart —
    nothing was persisted, so there's nothing for project bots to pick up."""
    bot = _make_manager(tmp_path)
    pm = _attach_running_pm(bot, ["proj_a"])
    await bot._on_add_user(_invocation(["charlie", "godmode"]))
    pm.restart.assert_not_called()
